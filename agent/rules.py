"""Motor de reglas. Determinístico, auditable, sin modelo.

Definición de riesgo (fijada en el diseño):

    Un proyecto activo tiene trabajo por delante y
      (a) un rol sin nadie asignado, o
      (b) la única persona que lo cubre está de licencia sin backup.

Ventana: 14 días por defecto (>= staffing lead time de 1-3 semanas).

Regla de datos parciales (CALM / mundo abierto):
  - afirmaciones POSITIVAS ("X está al 130%") son seguras con datos parciales
  - afirmaciones NEGATIVAS ("nadie cubre este rol") exigen completitud
Si falta una fuente que alimenta una negación, el riesgo se degrada a
incertidumbre en vez de afirmarse.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from .normalize import overlaps

log = logging.getLogger(__name__)

ACTIVE_STATUSES = {"Active", "In Flight", "In Progress"}


@dataclass
class Evidence:
    """Un hecho con su origen. Todo lo que el agente afirme debe tener uno."""

    claim: str
    system: str
    record_id: str


@dataclass
class Risk:
    project_id: str
    project_title: str
    client: str
    kind: str                      # 'rol_vacante' | 'licencia_sin_backup'
    headline: str
    days_until_impact: int
    hours_at_stake: float
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)
    certain: bool = True           # False => se formula como pregunta

    @property
    def fingerprint(self) -> str:
        """Huella para deduplicar entre corridas. Si cambia, es un riesgo distinto."""
        return f"{self.project_id}:{self.kind}:{self.days_until_impact // 7}"

    @property
    def severity(self) -> float:
        """Urgencia x magnitud x confianza. Determinístico y explicable."""
        urgency = 1.0 / max(1, self.days_until_impact)
        return urgency * max(1.0, self.hours_at_stake) * self.confidence


def _active_projects(projects: list[dict], today: date, window_days: int) -> list[dict]:
    """Proyectos activos con trabajo dentro de la ventana."""
    horizon = today + timedelta(days=window_days)
    out = []
    for project in projects:
        if project.get("status") not in ACTIVE_STATUSES:
            continue
        start = project.get("start_date")
        if start and start > horizon:
            continue          # todavía no arranca
        out.append(project)
    return out


def detect_vacant_lead(
    projects: list[dict],
    today: date,
    window_days: int,
    complete_sources: bool,
) -> list[Risk]:
    """Regla (a): proyecto activo sin lead asignado.

    En estos datos, `lead_user_id: null` es la única forma en que un
    'rol vacante' es representable: los proyectos no declaran los roles
    que necesitan. Eso es una limitación del modelo de datos, no del agente.
    """
    risks = []

    for project in _active_projects(projects, today, window_days):
        if project.get("lead_user_id"):
            continue

        due = project.get("due_date")
        days = (due - today).days if due else window_days

        risks.append(
            Risk(
                project_id=project["id"],
                project_title=project.get("title", project["id"]),
                client=project.get("client_name", ""),
                kind="rol_vacante",
                headline=f"{project.get('title')} no tiene lead asignado",
                days_until_impact=max(0, days),
                hours_at_stake=float(project.get("budgeted_hours") or 0),
                # Negación: sin datos completos no la afirmamos.
                confidence=1.0 if complete_sources else 0.6,
                certain=complete_sources,
                evidence=[
                    Evidence(
                        claim="lead_user_id está vacío",
                        system="kantata",
                        record_id=f"projects/{project['id']}",
                    )
                ],
            )
        )

    return risks


def detect_leave_without_backup(
    projects: list[dict],
    allocations: list[dict],
    time_off: list[dict],
    identities_by_kantata_id: dict,
    today: date,
    window_days: int,
    complete_sources: bool,
) -> list[Risk]:
    """Regla (b): la única persona en un proyecto se va de licencia."""
    horizon = today + timedelta(days=window_days)
    risks = []

    # Quién está asignado a cada proyecto dentro de la ventana
    by_project: dict[str, list[dict]] = {}
    for alloc in allocations:
        if not overlaps(alloc.get("start_date"), alloc.get("end_date"), today, horizon):
            continue
        by_project.setdefault(alloc["project_id"], []).append(alloc)

    # Licencias que caen en la ventana. 'Pending' cuenta: es riesgo, no certeza.
    leaves: dict[str, list[dict]] = {}
    for leave in time_off:
        if not overlaps(leave.get("start_date"), leave.get("end_date"), today, horizon):
            continue
        leaves.setdefault(leave["user_id"], []).append(leave)

    for project in _active_projects(projects, today, window_days):
        assigned = by_project.get(project["id"], [])
        if not assigned:
            continue                      # sin nadie: lo agarra la otra regla

        # Agrupar por ROL, no por proyecto. Tres personas en un proyecto no son
        # backup entre sí si hacen cosas distintas: si se va la única Technical
        # Architect, la QA y el Engagement Manager no la reemplazan.
        by_role: dict[str, list[dict]] = {}
        for alloc in assigned:
            person = identities_by_kantata_id.get(alloc["user_id"])
            role = (person.job_title if person else None) or "rol desconocido"
            by_role.setdefault(role, []).append(alloc)

        for role, holders in by_role.items():
            if len(holders) != 1:
                continue                  # hay más de uno en ese rol: hay backup

            only_one = holders[0]
            user_leaves = leaves.get(only_one["user_id"], [])
            if not user_leaves:
                continue

            leave = user_leaves[0]
            person = identities_by_kantata_id.get(only_one["user_id"])
            name = person.full_name if person else only_one["user_id"]
            pending = str(leave.get("status", "")).lower() == "pending"
            days = max(0, (leave["start_date"] - today).days) if leave.get("start_date") else 0

            risks.append(
                Risk(
                    project_id=project["id"],
                    project_title=project.get("title", project["id"]),
                    client=project.get("client_name", ""),
                    kind="licencia_sin_backup",
                    headline=(
                        f"{name} es la única persona en el rol {role} de "
                        f"{project.get('title')} y estará de licencia"
                    ),
                    days_until_impact=days,
                    hours_at_stake=float(project.get("budgeted_hours") or 0),
                    confidence=(0.7 if pending else 1.0) * (1.0 if complete_sources else 0.6),
                    certain=complete_sources and not pending,
                    evidence=[
                        Evidence(
                            claim=f"única allocation activa con rol {role} en {project['id']}",
                            system="kantata",
                            record_id=f"allocations/{only_one['id']}",
                        ),
                        Evidence(
                            claim=f"licencia {leave.get('status')} del "
                            f"{leave.get('start_date')} al {leave.get('end_date')}",
                            system="kantata",
                            record_id=f"time_off/{leave['id']}",
                        ),
                    ],
                )
            )

    return risks
