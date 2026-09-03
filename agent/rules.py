"""Rules engine. Deterministic, auditable, no model.

Risk definition (fixed in design):

    An active project has work ahead and
      (a) a role with nobody assigned, or
      (b) the only person covering the role is on leave without backup.

Window: 14 days by default (>= staffing lead time of 1-3 weeks).

Partial-data rule (CALM / open world):
  - POSITIVE claims ("X is at 130%") are safe with partial data
  - NEGATIVE claims ("nobody covers this role") require completeness
If a source feeding a negation is missing, the risk degrades to
uncertainty instead of being asserted.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from .normalize import overlaps

log = logging.getLogger(__name__)

ACTIVE_STATUSES = {"Active", "In Flight", "In Progress"}


@dataclass
class Evidence:
    """A fact with its source. Everything the agent asserts must have one."""

    claim: str
    system: str
    record_id: str


@dataclass
class Risk:
    project_id: str
    project_title: str
    client: str
    kind: str                      # 'vacant_role' | 'leave_without_backup'
    headline: str
    days_until_impact: int
    hours_at_stake: float
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)
    certain: bool = True           # False => phrased as a question

    @property
    def fingerprint(self) -> str:
        """Fingerprint to dedupe across runs. If it changes, it's a different risk."""
        return f"{self.project_id}:{self.kind}:{self.days_until_impact // 7}"

    @property
    def severity(self) -> float:
        """Urgency x magnitude x confidence. Deterministic and explainable."""
        urgency = 1.0 / max(1, self.days_until_impact)
        return urgency * max(1.0, self.hours_at_stake) * self.confidence


def _active_projects(projects: list[dict], today: date, window_days: int) -> list[dict]:
    """Active projects with work inside the window."""
    horizon = today + timedelta(days=window_days)
    out = []
    for project in projects:
        if project.get("status") not in ACTIVE_STATUSES:
            continue
        start = project.get("start_date")
        if start and start > horizon:
            continue          # hasn't started yet
        out.append(project)
    return out


def detect_vacant_lead(
    projects: list[dict],
    today: date,
    window_days: int,
    complete_sources: bool,
) -> list[Risk]:
    """Rule (a): active project with no lead assigned.

    In this data, `lead_user_id: null` is the only way a 'vacant role' is
    representable: projects don't declare the roles they need. That's a
    limitation of the data model, not of the agent.
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
                kind="vacant_role",
                headline=f"{project.get('title')} has no lead assigned",
                days_until_impact=max(0, days),
                hours_at_stake=float(project.get("budgeted_hours") or 0),
                # Negation: without complete data we don't assert it.
                confidence=1.0 if complete_sources else 0.6,
                certain=complete_sources,
                evidence=[
                    Evidence(
                        claim="lead_user_id is empty",
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
    """Rule (b): the only person in a role goes on leave."""
    horizon = today + timedelta(days=window_days)
    risks = []

    # Who is assigned to each project inside the window
    by_project: dict[str, list[dict]] = {}
    for alloc in allocations:
        if not overlaps(alloc.get("start_date"), alloc.get("end_date"), today, horizon):
            continue
        by_project.setdefault(alloc["project_id"], []).append(alloc)

    # Leaves within the window. 'Pending' counts: it's a risk, not a certainty.
    leaves: dict[str, list[dict]] = {}
    for leave in time_off:
        if not overlaps(leave.get("start_date"), leave.get("end_date"), today, horizon):
            continue
        leaves.setdefault(leave["user_id"], []).append(leave)

    for project in _active_projects(projects, today, window_days):
        assigned = by_project.get(project["id"], [])
        if not assigned:
            continue                      # nobody: handled by the other rule

        # Group by ROLE, not by project. Three people on a project are not
        # backup for each other if they do different things: if the only
        # Technical Architect leaves, the QA and the Engagement Manager
        # don't replace them.
        by_role: dict[str, list[dict]] = {}
        for alloc in assigned:
            person = identities_by_kantata_id.get(alloc["user_id"])
            role = (person.job_title if person else None) or "unknown role"
            by_role.setdefault(role, []).append(alloc)

        for role, holders in by_role.items():
            if len(holders) != 1:
                continue                  # more than one in that role: there's backup

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
                    kind="leave_without_backup",
                    headline=(
                        f"{name} is the only person in the {role} role on "
                        f"{project.get('title')} and will be on leave"
                    ),
                    days_until_impact=days,
                    hours_at_stake=float(project.get("budgeted_hours") or 0),
                    confidence=(0.7 if pending else 1.0) * (1.0 if complete_sources else 0.6),
                    certain=complete_sources and not pending,
                    evidence=[
                        Evidence(
                            claim=f"only active allocation with role {role} on {project['id']}",
                            system="kantata",
                            record_id=f"allocations/{only_one['id']}",
                        ),
                        Evidence(
                            claim=f"{leave.get('status')} leave from "
                            f"{leave.get('start_date')} to {leave.get('end_date')}",
                            system="kantata",
                            record_id=f"time_off/{leave['id']}",
                        ),
                    ],
                )
            )

    return risks
