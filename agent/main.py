"""Punto de entrada. El cron y el endpoint HTTP llaman a la MISMA función,
así no hay dos comportamientos que puedan divergir.

    POST /run      fuerza una corrida (esto es lo que dispara Go Nimbly)
    GET  /health   heartbeat: última corrida exitosa
"""

import logging
from datetime import date, datetime, timezone

import httpx
from fastapi import FastAPI

from . import compose, config, identity
from .ingest import ApiClient, Deadline, Fetched
from .normalize import normalize_allocations, parse_date
from .rules import detect_leave_without_backup, detect_vacant_lead
from .state import MemoryStore, Store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agent")

app = FastAPI(title="Staffing Risk Agent")


def _store() -> Store:
    return Store() if config.DATABASE_URL else MemoryStore()


def _dates(rows: list[dict], *fields: str) -> list[dict]:
    """Parsea las fechas de una colección. Los tres sistemas usan formatos distintos."""
    return [{**row, **{f: parse_date(row.get(f)) for f in fields}} for row in rows]


def run_once(today: date | None = None) -> dict:
    """Una corrida completa del pipeline. Devuelve el resumen."""
    today = today or datetime.now(timezone.utc).date()
    store = _store()
    store.migrate()
    run_id = store.start_run()

    deadline = Deadline(config.RUN_BUDGET_SECONDS)
    client = ApiClient(config.API_BASE_URL, config.CANDIDATE_TOKEN, deadline)

    try:
        # --- 1. Ingesta ---
        fetched: dict[str, Fetched] = {
            "users": client.fetch_collection("users", "/kantata/users", "users"),
            "projects": client.fetch_collection("projects", "/kantata/projects", "projects"),
            "allocations": client.fetch_collection("allocations", "/kantata/allocations", "allocations"),
            "time_off": client.fetch_collection("time_off", "/kantata/time_off", "time_off"),
            "members": client.fetch_collection("members", "/clickup/members", "members"),
            "sf_users": client.fetch_collection("sf_users", "/salesforce/users", "records"),
            "tasks": client.fetch_clickup_tasks(),
        }
    finally:
        client.close()

    # --- 2. ¿Podemos decidir algo? ---
    # Si falta una fuente crítica, el agente se calla. Alertar con datos
    # incompletos produce falsos positivos, y un falso positivo quema el canal.
    broken = [name for name in config.CRITICAL_SOURCES if not fetched[name].ok]
    if broken:
        detail = {"skipped": True, "broken_sources": broken}
        store.finish_run(run_id, "degraded", detail)
        _heartbeat_ok()   # el agente corrió: está vivo. La API caída va por otro canal.
        log.error("fuentes críticas caídas, no alerto: %s", broken)
        return {"sent": False, "reason": f"fuentes críticas caídas: {broken}"}

    complete = all(f.complete for f in fetched.values())
    notes = [f"{f.name} incompleto" for f in fetched.values() if not f.complete]

    # --- 3. Normalización ---
    projects = _dates(fetched["projects"].records, "start_date", "due_date")
    allocations = normalize_allocations(
        fetched["allocations"].records, {p["id"] for p in projects}
    )
    allocations.records = _dates(allocations.records, "start_date", "end_date")
    time_off = _dates(fetched["time_off"].records, "start_date", "end_date")

    # --- 4. Identidad ---
    resolution = identity.resolve(
        fetched["users"].records,
        fetched["members"].records,
        fetched["sf_users"].records,
        confirmations=store.confirmations(),
    )
    identities = {i.kantata_id: i for i in resolution.identities}

    # --- 5. Reglas (determinístico) ---
    risks = detect_vacant_lead(projects, today, config.WINDOW_DAYS, complete)
    risks += detect_leave_without_backup(
        projects, allocations.records, time_off, identities, today, config.WINDOW_DAYS, complete
    )

    # --- 6. Memoria: ¿qué de esto es nuevo? ---
    fresh = []
    decisions = {"nuevo": 0, "cambio": 0, "recordatorio": 0, "silencio": 0}
    for risk in risks:
        payload = {"headline": risk.headline, "days": risk.days_until_impact}
        decision = store.decide(risk.fingerprint, payload)
        decisions[decision.reason] += 1
        if decision.send:
            fresh.append(risk)
            store.record_alert(risk.fingerprint, risk.project_id, risk.kind, payload)
        else:
            log.info("silencio para %s (%s)", risk.fingerprint, decision.reason)

    # --- 7. Redacción y salida ---
    groups = compose.group_by_cause(fresh)
    message = compose.render(groups, notes)

    if message["send"]:
        _deliver(message["text"])

    summary = {
        "sent": message["send"],
        "risks_total": len(risks),
        "risks_new": len(fresh),
        "decisions": decisions,
        "groups": len(groups),
        "questions": resolution.questions,
        "anomalies": [a.detail for a in allocations.anomalies],
        "partial_sources": notes,
        "llm_used": message.get("llm_used", False),
        "llm_note": message.get("llm_note", ""),
        "text": message.get("text", ""),
    }
    store.finish_run(run_id, "ok", summary)
    _heartbeat_ok()
    return summary


def _heartbeat_ok() -> None:
    """Avisa al dead man's switch que la corrida terminó bien.

    Solo se llama tras un run exitoso. Si el agente muere antes, el ping
    no sale y el servicio externo dispara la alerta por ausencia.
    """
    if not config.HEARTBEAT_URL:
        return
    try:
        httpx.get(config.HEARTBEAT_URL, timeout=10.0)
    except httpx.HTTPError as exc:
        log.warning("no pude pinguear el heartbeat: %s", exc)


def _deliver(text: str) -> None:
    """Webhook si hay URL configurada; si no, al log. El brief acepta las dos."""
    if not config.SLACK_WEBHOOK_URL:
        log.info("SLACK (sin webhook configurado):\n%s", text)
        return
    try:
        httpx.post(config.SLACK_WEBHOOK_URL, json={"text": text}, timeout=10.0)
    except httpx.HTTPError as exc:
        log.error("no pude postear a Slack: %s", exc)
        log.info("SLACK (fallback a log):\n%s", text)


@app.get("/")
def root():
    return {
        "service": "Staffing Risk Agent",
        "trigger": "POST /run",
        "health": "GET /health",
    }


@app.post("/run")
def trigger_run():
    return run_once()


@app.get("/health")
def health():
    store = _store()
    store.migrate()   # idempotente: crea las tablas si es la primera vez
    last = store.last_successful_run()
    return {"status": "ok", "last_successful_run": last}


if __name__ == "__main__":
    import json

    print(json.dumps(run_once(), indent=2, ensure_ascii=False, default=str))
