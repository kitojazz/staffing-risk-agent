"""Normalization. This is where all the mess found in the audit gets cleaned.

Each function maps to a concrete, documented data trap.
Nothing is dropped silently: whatever is discarded is recorded in `anomalies`.
"""

import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)


@dataclass
class Anomaly:
    """Something odd in the data. Goes to the data-quality channel, not the lead's."""

    kind: str
    detail: str
    source: str


@dataclass
class Normalized:
    records: list[dict] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)


def strip_accents(text: str) -> str:
    """'Ines' with accent -> 'Ines'. Needed because accents vary across systems."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def normalize_name(text: str | None) -> str:
    """Name comparison key: no accents, lowercase, collapsed whitespace."""
    if not text:
        return ""
    return " ".join(strip_accents(text).lower().split())


def parse_date(value) -> date | None:
    """The three sources use different formats. Unify them here.

    Kantata:    '2026-08-19'
    Salesforce: '2026-09-10T00:00:00.000+0000'
    ClickUp:    '1786233600000'  (epoch in milliseconds, as a string)
    """
    if value is None or value == "":
        return None

    text = str(value)

    # ClickUp: all digits = epoch in milliseconds
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).date()

    # Salesforce: ISO with a timezone suffix
    if "T" in text:
        text = text.split("T")[0]

    try:
        return date.fromisoformat(text)
    except ValueError:
        log.warning("unparseable date: %r", value)
        return None


def normalize_allocation_percentage(value) -> float | None:
    """Trap C1: the field mixes two scales.

    Values present: 0.25, 1.0, 30, 40 ... 100.
    We assume <= 1 is a fraction and scale it to a percentage.

    Verified: Simon Zhao has 1.0 on Corvane plus 3 active tasks there,
    i.e. full allocation. If 1.0 meant 1%, it wouldn't add up.

    THIS IS AN ASSUMPTION. It's declared in the decision log.
    """
    if value is None:
        return None
    number = float(value)
    return number * 100 if number <= 1 else number


def ms_to_hours(value) -> float:
    """ClickUp stores time_estimate in milliseconds. May be null."""
    if value is None:
        return 0.0
    return float(value) / 3_600_000


def normalize_allocations(raw: list[dict], known_project_ids: set[str]) -> Normalized:
    """Normalize allocations and detect orphans.

    Trap C2: a_9018 points to p_5099, which does not exist.
    Decision: count it anyway (the load on the person is real) but report it
    as an anomaly. Discarding it would be inventing capacity.
    """
    out = Normalized()

    for row in raw:
        record = dict(row)
        record["allocation_percentage"] = normalize_allocation_percentage(
            row.get("allocation_percentage")
        )
        record["start_date"] = parse_date(row.get("start_date"))
        record["end_date"] = parse_date(row.get("end_date"))
        record["orphan"] = row.get("project_id") not in known_project_ids

        if record["orphan"]:
            out.anomalies.append(
                Anomaly(
                    kind="orphan_allocation",
                    detail=(
                        f"{row['id']} allocates {record['allocation_percentage']}% "
                        f"to {row['project_id']}, which does not exist in projects"
                    ),
                    source="kantata/allocations",
                )
            )

        out.records.append(record)

    return out


def overlaps(start_a, end_a, start_b, end_b) -> bool:
    """Do two date ranges overlap? A None is treated as open-ended."""
    if start_a and end_b and start_a > end_b:
        return False
    if start_b and end_a and start_b > end_a:
        return False
    return True
