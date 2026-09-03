"""Normalización. Acá se limpia toda la basura que encontramos en la auditoría.

Cada función corresponde a una trampa concreta y documentada.
Nada se descarta en silencio: lo que se tira, se registra en `anomalies`.
"""

import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)


@dataclass
class Anomaly:
    """Algo raro en los datos. Va al canal de calidad de datos, no al del lead."""

    kind: str
    detail: str
    source: str


@dataclass
class Normalized:
    records: list[dict] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)


def strip_accents(text: str) -> str:
    """'Inés' -> 'Ines'. Necesario porque las tildes van y vienen por sistema."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def normalize_name(text: str | None) -> str:
    """Clave de comparación de nombres: sin tildes, minúsculas, espacios colapsados."""
    if not text:
        return ""
    return " ".join(strip_accents(text).lower().split())


def parse_date(value) -> date | None:
    """Las tres fuentes usan formatos distintos. Acá se unifican.

    Kantata:    '2026-08-19'
    Salesforce: '2026-09-10T00:00:00.000+0000'
    ClickUp:    '1786233600000'  (epoch en milisegundos, como string)
    """
    if value is None or value == "":
        return None

    text = str(value)

    # ClickUp: puro dígito = epoch en milisegundos
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).date()

    # Salesforce: ISO con zona horaria pegada sin dos puntos
    if "T" in text:
        text = text.split("T")[0]

    try:
        return date.fromisoformat(text)
    except ValueError:
        log.warning("fecha no parseable: %r", value)
        return None


def normalize_allocation_percentage(value) -> float | None:
    """Trampa C1: el campo mezcla dos escalas.

    Valores presentes: 0.25, 1.0, 30, 40 ... 100.
    Asumimos que <= 1 está en fracción y lo llevamos a porcentaje.

    Verificado: Simon Zhao tiene 1.0 en Corvane y 3 tareas activas ahí,
    o sea dedicación completa. Si 1.0 fuera 1%, no cerraría.

    ESTO ES UNA ASUNCIÓN. Va declarada en el decision log.
    """
    if value is None:
        return None
    number = float(value)
    return number * 100 if number <= 1 else number


def ms_to_hours(value) -> float:
    """ClickUp guarda time_estimate en milisegundos. Puede venir null."""
    if value is None:
        return 0.0
    return float(value) / 3_600_000


def normalize_allocations(raw: list[dict], known_project_ids: set[str]) -> Normalized:
    """Normaliza allocations y detecta las huérfanas.

    Trampa C2: a_9018 apunta a p_5099, que no existe.
    Decisión: la contamos igual (la carga sobre la persona es real) pero
    la reportamos como anomalía. Descartarla sería inventar capacidad.
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
                    kind="allocation_huerfana",
                    detail=(
                        f"{row['id']} asigna {record['allocation_percentage']}% "
                        f"a {row['project_id']}, que no existe en projects"
                    ),
                    source="kantata/allocations",
                )
            )

        out.records.append(record)

    return out


def overlaps(start_a, end_a, start_b, end_b) -> bool:
    """¿Se solapan dos rangos de fechas? Un None se trata como abierto."""
    if start_a and end_b and start_a > end_b:
        return False
    if start_b and end_a and start_b > end_a:
        return False
    return True
