"""Grouping, prioritization and message rendering.

The hard facts (names, dates, hours, links) are written by this module from
the verified payload. The LLM only contributes the headline and the "why it
matters". If its output fails validation, the template is used instead.
"""

import logging
from dataclasses import dataclass, field

from . import config, llm
from .rules import Risk

log = logging.getLogger(__name__)

TOP_N = 5


@dataclass
class RiskGroup:
    """Risks sharing a cause. A leave affecting two projects is one problem
    with two impacts, not two problems."""

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
    """Group by (kind, person, role). Those without a person go alone by project."""
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
    if risk.kind != "leave_without_backup":
        return None
    return risk.headline.split(" is the only")[0].strip() or None


def _role_of(risk: Risk) -> str | None:
    if " in the " not in risk.headline or " role on " not in risk.headline:
        return None
    return risk.headline.split(" in the ")[1].split(" role on ")[0].strip()


def build_payload(groups: list[RiskGroup]) -> dict:
    """The only thing the LLM sees. Small, verified, no raw API data."""
    return {
        "groups": [
            {
                "id": group.key,
                "kind": group.kind,
                "person": group.person,
                "role": group.role,
                "projects": group.projects,
                "days": group.days,
                "hours": round(group.hours),
                "certain": group.certain,
            }
            for group in groups[:TOP_N]
        ]
    }


def _fallback_text(group: RiskGroup) -> str:
    """Deterministic template. Used if the LLM fails or invents something."""
    projects = ", ".join(group.projects)
    if group.kind == "vacant_role":
        return f"{projects} has no lead assigned, with {round(group.hours)}h of budget left."
    verb = "is" if group.certain else "may be"
    return f"{group.person} is on leave and {projects} {verb} left without {group.role} coverage."


def render(groups: list[RiskGroup], data_notes: list[str]) -> dict:
    """Build the final message. Returns a dict with text and run metadata."""
    if not groups:
        return {"send": False, "reason": "no risks", "text": ""}

    top = groups[:TOP_N]
    payload = build_payload(top)

    headline = f"{len(groups)} coverage risk(s) in the next {config.WINDOW_DAYS} days"
    texts: dict[str, str] = {}
    llm_used = False
    llm_note = ""

    try:
        generated, violations = llm.compose(payload)
        if violations:
            llm_note = f"model output discarded: {'; '.join(violations[:3])}"
            log.warning(llm_note)
        else:
            headline = generated.get("headline", headline)
            texts = {item["id"]: item["text"] for item in generated.get("items", [])}
            llm_used = True
    except llm.LLMUnavailable as exc:
        llm_note = f"model unavailable: {exc}"
        log.warning(llm_note)

    lines = [f"*{headline}*", ""]

    for group in top:
        body = texts.get(group.key) or _fallback_text(group)
        marker = "•" if group.certain else "❓"
        lines.append(f"{marker} {body}")
        # Facts and sources are written by the code, never by the model.
        for risk in group.risks:
            sources = ", ".join(f"{e.system}:{e.record_id}" for e in risk.evidence)
            lines.append(f"    _{risk.project_title} — impact in {risk.days_until_impact}d — {sources}_")
        lines.append("")

    if len(groups) > TOP_N:
        lines.append(f"_and {len(groups) - TOP_N} more, ordered by severity._")

    if data_notes:
        lines.append("")
        lines.append(f"_Partial data: {'; '.join(data_notes)}_")

    return {
        "send": True,
        "text": "\n".join(lines).strip(),
        "llm_used": llm_used,
        "llm_note": llm_note,
        "groups": len(groups),
    }
