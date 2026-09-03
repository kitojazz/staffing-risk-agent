"""Capa de ingesta: trae datos de los tres sistemas.

Nunca devuelve una lista pelada. Siempre devuelve un Fetched, que dice
si los datos están completos o no. Una lista pelada miente por omisión.
"""

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import config

log = logging.getLogger(__name__)


@dataclass
class Fetched:
    """Resultado de traer una colección. `complete` es lo importante."""

    name: str
    records: list[dict] = field(default_factory=list)
    complete: bool = True
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Deadline:
    """Presupuesto de tiempo compartido por toda la corrida."""

    def __init__(self, seconds: int):
        self.expires_at = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0


class ApiClient:
    def __init__(self, base_url: str, token: str = "", deadline: Deadline | None = None):
        headers = {"X-Candidate-Token": token} if token else {}
        self._client = httpx.Client(base_url=base_url, headers=headers, timeout=10.0)
        self._deadline = deadline or Deadline(config.RUN_BUDGET_SECONDS)

    def close(self) -> None:
        self._client.close()

    def _sleep(self, seconds: float) -> bool:
        """Espera, salvo que se acabe el presupuesto. Devuelve False si expiró."""
        if seconds >= self._deadline.remaining():
            return False
        time.sleep(seconds)
        return True

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET con reintentos. Levanta excepción si se agotan los intentos."""
        last_error: Exception | None = None

        for attempt in range(config.MAX_ATTEMPTS):
            if self._deadline.expired():
                raise TimeoutError(f"presupuesto agotado antes de {path}")

            try:
                response = self._client.get(path, params=params)
            except httpx.RequestError as exc:
                last_error = exc
                wait = config.BACKOFF_BASE_SECONDS * (2**attempt)
                log.warning("%s falló (%s), reintento en %.1fs", path, exc, wait)
                if not self._sleep(wait):
                    break
                continue

            if response.status_code == 429:
                # El servidor nos dice cuánto esperar. Obedecemos.
                wait = float(response.headers.get("Retry-After", 1))
                log.info("429 en %s, Retry-After=%.0fs", path, wait)
                if not self._sleep(wait):
                    break
                continue

            if response.status_code >= 500:
                # Sin Retry-After: backoff exponencial propio.
                wait = config.BACKOFF_BASE_SECONDS * (2**attempt)
                log.warning("%s devolvió %s, reintento en %.1fs", path, response.status_code, wait)
                if not self._sleep(wait):
                    break
                continue

            response.raise_for_status()
            return response.json()

        raise RuntimeError(f"{path}: {config.MAX_ATTEMPTS} intentos agotados ({last_error})")

    def fetch_collection(self, name: str, path: str, key: str) -> Fetched:
        """Trae una colección de un endpoint no paginado."""
        try:
            payload = self.get(path)
        except Exception as exc:
            log.error("no pude traer %s: %s", name, exc)
            return Fetched(name=name, complete=False, error=str(exc))
        return Fetched(name=name, records=payload.get(key, []))

    def fetch_clickup_tasks(self) -> Fetched:
        """Trae tareas de ClickUp, paginadas.

        ClickUp no devuelve un total, solo `last_page`. Si una página falla,
        seguimos con lo que tenemos pero marcamos complete=False.
        """
        records: list[dict] = []
        page = 0
        complete = True
        error = None

        while True:
            try:
                payload = self.get("/clickup/tasks", params={"page": page})
            except Exception as exc:
                log.error("página %s de clickup falló: %s", page, exc)
                complete = False
                error = str(exc)
                break

            records.extend(payload.get("tasks", []))
            if payload.get("last_page", True):
                break
            page += 1

        return Fetched(name="tasks", records=records, complete=complete, error=error)
