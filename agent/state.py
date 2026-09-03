"""Memoria entre corridas.

Guarda tres cosas distintas:

  1. alerted_risks   — qué se avisó y cuándo (para no repetir)
  2. resolutions     — respuestas humanas (para no volver a preguntar)
  3. runs            — heartbeat: el silencio del agente debe significar
                       "todo bien", no "me morí"

Sin (3) no podés distinguir un agente sano de uno caído: los dos callan.
"""

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from . import config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerted_risks (
    fingerprint   TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    payload       JSONB NOT NULL,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_alerted  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS resolutions (
    key           TEXT PRIMARY KEY,
    answer        TEXT NOT NULL,
    answered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_version  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id            SERIAL PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL,
    detail        JSONB
);
"""


@dataclass
class AlertDecision:
    """Qué hacer con un riesgo, comparado contra lo ya avisado."""

    send: bool
    reason: str        # 'nuevo' | 'cambio' | 'recordatorio' | 'silencio'


class Store:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or config.DATABASE_URL

    @contextmanager
    def _conn(self):
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            yield conn

    def migrate(self) -> None:
        with self._conn() as conn:
            conn.execute(SCHEMA)

    # --- Riesgos avisados -------------------------------------------------

    def decide(self, fingerprint: str, payload: dict) -> AlertDecision:
        """¿Mando este riesgo o me callo?

        - no existe            -> nuevo, se manda
        - existe y cambió      -> cambio, se manda
        - existe, igual, viejo -> recordatorio a los REMINDER_DAYS
        - existe, igual, nuevo -> silencio
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload, last_alerted FROM alerted_risks WHERE fingerprint = %s",
                (fingerprint,),
            ).fetchone()

        if row is None:
            return AlertDecision(send=True, reason="nuevo")

        if row["payload"] != payload:
            return AlertDecision(send=True, reason="cambio")

        age = datetime.now(timezone.utc) - row["last_alerted"]
        if age >= timedelta(days=config.REMINDER_DAYS):
            return AlertDecision(send=True, reason="recordatorio")

        return AlertDecision(send=False, reason="silencio")

    def record_alert(self, fingerprint: str, project_id: str, kind: str, payload: dict) -> None:
        """Idempotente: correr dos veces el mismo día no duplica nada."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO alerted_risks (fingerprint, project_id, kind, payload)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (fingerprint) DO UPDATE
                   SET payload = EXCLUDED.payload,
                       last_alerted = now()
                """,
                (fingerprint, project_id, kind, json.dumps(payload, default=str)),
            )

    # --- Confirmaciones humanas -------------------------------------------

    def confirmations(self) -> dict[str, str]:
        """Respuestas vigentes. Vencen a los RESOLUTION_TTL_DAYS."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.RESOLUTION_TTL_DAYS)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key, answer FROM resolutions WHERE answered_at >= %s", (cutoff,)
            ).fetchall()
        return {row["key"]: row["answer"] for row in rows}

    def save_confirmation(self, key: str, answer: str, data_version: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO resolutions (key, answer, data_version)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE
                   SET answer = EXCLUDED.answer,
                       answered_at = now(),
                       data_version = EXCLUDED.data_version
                """,
                (key, answer, data_version),
            )

    def invalidate_confirmations(self, keys: list[str]) -> None:
        """Los datos de esa persona/proyecto cambiaron: hay que volver a preguntar."""
        if not keys:
            return
        with self._conn() as conn:
            conn.execute("DELETE FROM resolutions WHERE key = ANY(%s)", (keys,))

    # --- Heartbeat --------------------------------------------------------

    def start_run(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "INSERT INTO runs (status) VALUES ('running') RETURNING id"
            ).fetchone()
        return row["id"]

    def finish_run(self, run_id: int, status: str, detail: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = now(), status = %s, detail = %s WHERE id = %s",
                (status, json.dumps(detail, default=str), run_id),
            )

    def last_successful_run(self) -> datetime | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT finished_at FROM runs WHERE status = 'ok' "
                "ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        return row["finished_at"] if row else None


class MemoryStore(Store):
    """Store en memoria para desarrollo y para los casos dorados. Sin Postgres."""

    def __init__(self):
        self._alerts: dict[str, dict] = {}
        self._resolutions: dict[str, dict] = {}
        self._runs: list[dict] = []

    def migrate(self) -> None:
        pass

    def decide(self, fingerprint: str, payload: dict) -> AlertDecision:
        row = self._alerts.get(fingerprint)
        if row is None:
            return AlertDecision(send=True, reason="nuevo")
        if row["payload"] != payload:
            return AlertDecision(send=True, reason="cambio")
        if datetime.now(timezone.utc) - row["last_alerted"] >= timedelta(days=config.REMINDER_DAYS):
            return AlertDecision(send=True, reason="recordatorio")
        return AlertDecision(send=False, reason="silencio")

    def record_alert(self, fingerprint: str, project_id: str, kind: str, payload: dict) -> None:
        self._alerts[fingerprint] = {
            "project_id": project_id,
            "kind": kind,
            "payload": payload,
            "last_alerted": datetime.now(timezone.utc),
        }

    def confirmations(self) -> dict[str, str]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.RESOLUTION_TTL_DAYS)
        return {k: v["answer"] for k, v in self._resolutions.items() if v["at"] >= cutoff}

    def save_confirmation(self, key: str, answer: str, data_version: str | None = None) -> None:
        self._resolutions[key] = {"answer": answer, "at": datetime.now(timezone.utc)}

    def invalidate_confirmations(self, keys: list[str]) -> None:
        for key in keys:
            self._resolutions.pop(key, None)

    def start_run(self) -> int:
        self._runs.append({"status": "running", "started_at": datetime.now(timezone.utc)})
        return len(self._runs) - 1

    def finish_run(self, run_id: int, status: str, detail: dict) -> None:
        self._runs[run_id].update(status=status, detail=detail, finished_at=datetime.now(timezone.utc))

    def last_successful_run(self) -> datetime | None:
        ok = [r for r in self._runs if r.get("status") == "ok"]
        return ok[-1]["finished_at"] if ok else None
