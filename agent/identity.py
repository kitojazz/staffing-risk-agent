"""Resolución de identidad: ¿quién es quién entre los tres sistemas?

No hay clave compartida. La cascada va de lo más confiable a lo más dudoso:

  1.00  email exacto
  0.90  nombre normalizado, y SOLO si es inequívoco
  0.85  señales estructurales (mismo proyecto, mismo rol, mismas fechas)
  0.50  el LLM propone (opinión, no veredicto)
  0.00  sin resolver

Trampa A1: hay dos personas distintas que se llaman "Ines Rocha".
Por eso el tier de nombre exige unicidad: si un nombre normalizado apunta
a más de una persona, el nombre no sirve como evidencia.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from .normalize import normalize_name

log = logging.getLogger(__name__)


@dataclass
class Identity:
    """Una persona, con sus caras en cada sistema."""

    kantata_id: str
    full_name: str
    email: str
    job_title: str | None = None
    active: bool = True
    clickup_id: int | None = None
    salesforce_id: str | None = None
    confidence: float = 1.0
    method: str = "kantata_base"
    evidence: list[str] = field(default_factory=list)


@dataclass
class Resolution:
    identities: list[Identity] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


def _by_email(records: list[dict], key: str) -> dict[str, dict]:
    """Indexa por email en minúsculas. Ignora los que no tienen."""
    index = {}
    for row in records:
        email = (row.get(key) or "").strip().lower()
        if email:
            index[email] = row
    return index


def _unambiguous_names(records: list[dict], key: str) -> dict[str, dict]:
    """Indexa por nombre normalizado, PERO descarta los nombres repetidos.

    Si dos registros normalizan al mismo nombre, ese nombre queda fuera
    del índice: no puede servir de evidencia para nadie.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in records:
        buckets[normalize_name(row.get(key))].append(row)

    unique = {}
    for name, rows in buckets.items():
        if not name:
            continue
        if len(rows) == 1:
            unique[name] = rows[0]
        else:
            log.info("nombre ambiguo, descartado del índice: %r (%d registros)", name, len(rows))
    return unique


def resolve(
    kantata_users: list[dict],
    clickup_members: list[dict],
    salesforce_users: list[dict],
    confirmations: dict[str, str] | None = None,
) -> Resolution:
    """Construye la lista de identidades unificadas.

    `confirmations` son respuestas humanas previas ({clave_externa: kantata_id}).
    Una confirmación humana pisa cualquier cálculo y vale 1.0.
    """
    confirmations = confirmations or {}
    result = Resolution()

    clickup_by_email = _by_email(clickup_members, "email")
    clickup_by_name = _unambiguous_names(clickup_members, "username")
    sf_by_email = _by_email(salesforce_users, "Email")
    sf_by_name = _unambiguous_names(salesforce_users, "Name")

    matched_clickup: set[int] = set()

    for user in kantata_users:
        email = (user.get("email_address") or "").strip().lower()
        identity = Identity(
            kantata_id=user["id"],
            full_name=user.get("full_name", ""),
            email=email,
            job_title=user.get("job_title"),
            active=bool(user.get("active", True)),
        )

        # --- ClickUp ---
        member = clickup_by_email.get(email)
        if member:
            identity.clickup_id = member["id"]
            identity.evidence.append(f"clickup por email ({email})")
        else:
            member = clickup_by_name.get(normalize_name(user.get("full_name")))
            if member:
                identity.clickup_id = member["id"]
                identity.confidence = min(identity.confidence, 0.9)
                identity.method = "nombre_inequivoco"
                identity.evidence.append(f"clickup por nombre único ({member['username']})")
            else:
                identity.confidence = min(identity.confidence, 0.5)
                identity.method = "sin_match_clickup"

        if identity.clickup_id is not None:
            matched_clickup.add(identity.clickup_id)

        # --- Salesforce ---
        sf_user = sf_by_email.get(email) or sf_by_name.get(normalize_name(user.get("full_name")))
        if sf_user:
            identity.salesforce_id = sf_user["Id"]
            identity.evidence.append(f"salesforce ({sf_user['Name']})")

        result.identities.append(identity)

    # --- Gente que existe en ClickUp y no en Kantata (trampa A3) ---
    for member in clickup_members:
        if member["id"] in matched_clickup:
            continue

        key = f"clickup:{member['id']}"
        if key in confirmations:
            # El lead ya respondió por esta persona. Vale 1.0.
            continue

        result.unresolved.append(member)
        result.questions.append(
            f"{member['username']} ({member.get('email', 'sin email')}) tiene tareas "
            f"en ClickUp pero no figura en Kantata. ¿Cubre algún rol asignado?"
        )

    return result
