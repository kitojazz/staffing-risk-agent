"""Agrupación, priorización y renderizado del mensaje.

Los hechos duros (nombres, fechas, horas, links) los escribe este módulo
desde el payload verificado. El LLM solo aporta el titular y el "por qué
importa". Si su salida no pasa la validación, se usa el template.
"""

import logging
from dataclasses import dataclass, field

from . import config, llm
from .rules import Risk

log = logging.getLogger(__name__)

TOP_N = 5


@dataclass
class RiskGroup:
    """Riesgos que comparten causa. Una licencia que afecta dos proyectos
    es un problema con dos impactos, no dos problemas."""

    key: str
    kind: str
    person: str | None
    role: str | None
    projects: list[str] = field(default_factory=list)
    risks: list[Risk] = field(default_factory=list)

    @property
    def severity(self) -> float:
        return max(risk.severity for risk in self.risks)

    @property
    def days(self) -> int:
        return min(risk.days_until_impact for risk in self.risks)

    @property
    def hours(self) -> float:
        return sum(risk.hours_at_stake for risk in self.risks)

    @property
    def certain(self) -> bool:
        return all(risk.certain for risk in self.risks)


def group_by_cause(risks: list[Risk]) -> list[RiskGroup]:
    """Agrupa por (tipo, persona, rol). Los sin persona van solos por proyecto."""
    groups: dict[str, RiskGroup] = {}

    for risk in risks:
        person = _person_of(risk)
        role = _role_of(risk)
        key = f"{risk.kind}|{person or risk.project_id}|{role or ''}"

        group = groups.get(key)
        if group is None:
            group = RiskGroup(key=key, kind=risk.kind, person=person, role=role)
            groups[key] = group

        group.risks.append(risk)
        group.projects.append(risk.project_title)

    return sorted(groups.values(), key=lambda g: -g.severity)


def _person_of(risk: Risk) -> str | None:
    if risk.kind != "licencia_sin_backup":
        return None
    return risk.headline.split(" es la única")[0].strip() or None


def _role_of(risk: Risk) -> str | None:
    if " en el rol " not in risk.headline:
        return None
    return risk.headline.split(" en el rol ")[1].split(" de ")[0].strip()


def build_payload(groups: list[RiskGroup]) -> dict:
    """Lo único que ve el LLM. Chico, verificado, sin datos crudos de la API."""
    return {
        "grupos": [
            {
                "id": group.key,
                "tipo": group.kind,
                "persona": group.person,
                "rol": group.role,
                "proyectos": group.projects,
                "dias": group.days,
                "horas": round(group.hours),
                "certero": group.certain,
            }
            for group in groups[:TOP_N]
        ]
    }


def _fallback_text(group: RiskGroup) -> str:
    """Template determinístico. Se usa si el LLM falla o inventa algo."""
    projects = ", ".join(group.projects)
    if group.kind == "rol_vacante":
        return f"{projects} no tiene lead asignado y quedan {round(group.hours)}h de budget."
    verb = "queda" if group.certain else "podría quedar"
    return f"{group.person} está de licencia y {projects} {verb} sin cobertura de {group.role}."


def render(groups: list[RiskGroup], data_notes: list[str]) -> dict:
    """Arma el mensaje final. Devuelve dict con texto y metadatos de la corrida."""
    if not groups:
        return {"send": False, "reason": "sin riesgos", "text": ""}

    top = groups[:TOP_N]
    payload = build_payload(top)

    headline = f"{len(groups)} riesgo(s) de cobertura en los próximos {config.WINDOW_DAYS} días"
    texts: dict[str, str] = {}
    llm_used = False
    llm_note = ""

    try:
        generated, violations = llm.compose(payload)
        if violations:
            llm_note = f"salida del modelo descartada: {'; '.join(violations[:3])}"
            log.warning(llm_note)
        else:
            headline = generated.get("titular", headline)
            texts = {item["id"]: item["texto"] for item in generated.get("items", [])}
            llm_used = True
    except llm.LLMUnavailable as exc:
        llm_note = f"modelo no disponible: {exc}"
        log.warning(llm_note)

    lines = [f"*{headline}*", ""]

    for group in top:
        body = texts.get(group.key) or _fallback_text(group)
        marker = "•" if group.certain else "❓"
        lines.append(f"{marker} {body}")
        # Los hechos y las fuentes los escribe el código, nunca el modelo.
        for risk in group.risks:
            sources = ", ".join(f"{e.system}:{e.record_id}" for e in risk.evidence)
            lines.append(f"    _{risk.project_title} — impacto en {risk.days_until_impact}d — {sources}_")
        lines.append("")

    if len(groups) > TOP_N:
        lines.append(f"_y {len(groups) - TOP_N} más, ordenados por severidad._")

    if data_notes:
        lines.append("")
        lines.append(f"_Datos parciales: {'; '.join(data_notes)}_")

    return {
        "send": True,
        "text": "\n".join(lines).strip(),
        "llm_used": llm_used,
        "llm_note": llm_note,
        "groups": len(groups),
    }
