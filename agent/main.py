"""Entry point. The cron and the HTTP endpoint call the SAME function, so
there can't be two behaviors that drift apart.

    POST /run      forces a run (this is what Go Nimbly triggers)
    GET  /health   heartbeat: last successful run
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
    """Parse the dates of a collection. The three systems use different formats."""
    return [{**row, **{f: parse_date(row.get(f)) for f in fields}} for row in rows]


def run_once(today: date | None = None) -> dict:
    """One full pipeline run. Returns the summary."""
    today = today or datetime.now(timezone.utc).date()
    store = _store()
    store.migrate()
    run_id = store.start_run()

    deadline = Deadline(config.RUN_BUDGET_SECONDS)
    client = ApiClient(config.API_BASE_URL, config.CANDIDATE_TOKEN, deadline)

    try:
        # --- 1. Ingestion ---
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

    # --- 2. Can we decide anything? ---
    # If a critical source is missing, the agent stays silent. Alerting on
    # incomplete data produces false positives, and a false positive burns
    # the channel.
    broken = [name for name in config.CRITICAL_SOURCES if not fetched[name].ok]
    if broken:
        detail = {"skipped": True, "broken_sources": broken}
        store.finish_run(run_id, "degraded", detail)
        _heartbeat_ok()   # the agent ran: it's alive. The broken API goes on another channel.
        log.error("critical sources down, not alerting: %s", broken)
        return {"sent": False, "reason": f"critical sources down: {broken}"}

    complete = all(f.complete for f in fetched.values())
    notes = [f"{f.name} incomplete" for f in fetched.values() if not f.complete]

    # --- 3. Normalization ---
    projects = _dates(fetched["projects"].records, "start_date", "due_date")
    allocations = normalize_allocations(
        fetched["allocations"].records, {p["id"] for p in projects}
    )
    allocations.records = _dates(allocations.records, "start_date", "end_date")
    time_off = _dates(fetched["time_off"].records, "start_date", "end_date")

    # --- 4. Identity ---
    resolution = identity.resolve(
        fetched["users"].records,
        fetched["members"].records,
        fetched["sf_users"].records,
        confirmations=store.confirmations(),
    )
    identities = {i.kantata_id: i for i in resolution.identities}

    # --- 5. Rules (deterministic) ---
    risks = detect_vacant_lead(projects, today, config.WINDOW_DAYS, complete)
    risks += detect_leave_without_backup(
        projects, allocations.records, time_off, identities, today, config.WINDOW_DAYS, complete
    )

    # --- 6. Memory: what of this is new? ---
    fresh = []
    decisions = {"new": 0, "changed": 0, "reminder": 0, "silence": 0}
    for risk in risks:
        payload = {"headline": risk.headline, "days": risk.days_until_impact}
        decision = store.decide(risk.fingerprint, payload)
        decisions[decision.reason] += 1
        if decision.send:
            fresh.append(risk)
            store.record_alert(risk.fingerprint, risk.project_id, risk.kind, payload)
        else:
            log.info("silence for %s (%s)", risk.fingerprint, decision.reason)

    # --- 7. Drafting and output ---
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
    """Tell the dead man's switch the run finished fine.

    Called only after the agent ran. If the agent dies earlier, the ping
    never goes out and the external service alerts on absence.
    """
    if not config.HEARTBEAT_URL:
        return
    try:
        httpx.get(config.HEARTBEAT_URL, timeout=10.0)
    except httpx.HTTPError as exc:
        log.warning("could not ping heartbeat: %s", exc)


def _deliver(text: str) -> None:
    """Webhook if a URL is configured; otherwise to the log. The brief accepts both."""
    if not config.SLACK_WEBHOOK_URL:
        log.info("SLACK (no webhook configured):\n%s", text)
        return
    try:
        httpx.post(config.SLACK_WEBHOOK_URL, json={"text": text}, timeout=10.0)
    except httpx.HTTPError as exc:
        log.error("could not post to Slack: %s", exc)
        log.info("SLACK (fallback to log):\n%s", text)


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
    store.migrate()   # idempotent: creates tables on first run
    last = store.last_successful_run()
    return {"status": "ok", "last_successful_run": last}


if __name__ == "__main__":
    import json

    print(json.dumps(run_once(), indent=2, ensure_ascii=False, default=str))
