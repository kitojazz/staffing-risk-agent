"""Ingestion layer: pulls data from the three systems.

Never returns a bare list. Always returns a Fetched, which says whether
the data is complete or not. A bare list lies by omission.
"""

import logging
import time
from dataclasses import dataclass, field

import httpx

from . import config

log = logging.getLogger(__name__)


@dataclass
class Fetched:
    """Result of pulling a collection. `complete` is the important part."""

    name: str
    records: list[dict] = field(default_factory=list)
    complete: bool = True
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Deadline:
    """Time budget shared across the whole run."""

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
        """Sleep, unless the budget runs out. Returns False if it expired."""
        if seconds >= self._deadline.remaining():
            return False
        time.sleep(seconds)
        return True

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET with retries. Raises if attempts are exhausted."""
        last_error: Exception | None = None

        for attempt in range(config.MAX_ATTEMPTS):
            if self._deadline.expired():
                raise TimeoutError(f"budget exhausted before {path}")

            try:
                response = self._client.get(path, params=params)
            except httpx.RequestError as exc:
                last_error = exc
                wait = config.BACKOFF_BASE_SECONDS * (2**attempt)
                log.warning("%s failed (%s), retrying in %.1fs", path, exc, wait)
                if not self._sleep(wait):
                    break
                continue

            if response.status_code == 429:
                # The server tells us how long to wait. We obey.
                wait = float(response.headers.get("Retry-After", 1))
                log.info("429 on %s, Retry-After=%.0fs", path, wait)
                if not self._sleep(wait):
                    break
                continue

            if response.status_code >= 500:
                # No Retry-After: our own exponential backoff.
                wait = config.BACKOFF_BASE_SECONDS * (2**attempt)
                log.warning("%s returned %s, retrying in %.1fs", path, response.status_code, wait)
                if not self._sleep(wait):
                    break
                continue

            response.raise_for_status()
            return response.json()

        raise RuntimeError(f"{path}: {config.MAX_ATTEMPTS} attempts exhausted ({last_error})")

    def fetch_collection(self, name: str, path: str, key: str) -> Fetched:
        """Fetch a collection from a non-paginated endpoint."""
        try:
            payload = self.get(path)
        except Exception as exc:
            log.error("could not fetch %s: %s", name, exc)
            return Fetched(name=name, complete=False, error=str(exc))
        return Fetched(name=name, records=payload.get(key, []))

    def fetch_clickup_tasks(self) -> Fetched:
        """Fetch ClickUp tasks, paginated.

        ClickUp returns no total, only `last_page`. If a page fails, we keep
        what we have but mark complete=False.
        """
        records: list[dict] = []
        page = 0
        complete = True
        error = None

        while True:
            try:
                payload = self.get("/clickup/tasks", params={"page": page})
            except Exception as exc:
                log.error("clickup page %s failed: %s", page, exc)
                complete = False
                error = str(exc)
                break

            records.extend(payload.get("tasks", []))
            if payload.get("last_page", True):
                break
            page += 1

        return Fetched(name="tasks", records=records, complete=complete, error=error)
