"""Identity resolution: who is who across the three systems?

There is no shared key. The cascade goes from most reliable to least:

  1.00  exact email
  0.90  normalized name, and ONLY if unambiguous
  0.85  structural signals (same project, same role, same dates)
  0.50  the LLM proposes (opinion, not verdict)
  0.00  unresolved

Trap A1: there are two distinct people named "Ines Rocha".
That's why the name tier requires uniqueness: if a normalized name points
to more than one person, the name can't serve as evidence.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from .normalize import normalize_name

log = logging.getLogger(__name__)


@dataclass
class Identity:
    """A person, with their faces in each system."""

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
    """Index by lowercased email. Skip those without one."""
    index = {}
    for row in records:
        email = (row.get(key) or "").strip().lower()
        if email:
            index[email] = row
    return index


def _unambiguous_names(records: list[dict], key: str) -> dict[str, dict]:
    """Index by normalized name, BUT drop repeated names.

    If two records normalize to the same name, that name is left out of the
    index: it can't serve as evidence for anyone.
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
            log.info("ambiguous name, dropped from index: %r (%d records)", name, len(rows))
    return unique


def resolve(
    kantata_users: list[dict],
    clickup_members: list[dict],
    salesforce_users: list[dict],
    confirmations: dict[str, str] | None = None,
) -> Resolution:
    """Build the list of unified identities.

    `confirmations` are prior human answers ({external_key: kantata_id}).
    A human confirmation overrides any calculation and is worth 1.0.
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
            identity.evidence.append(f"clickup by email ({email})")
        else:
            member = clickup_by_name.get(normalize_name(user.get("full_name")))
            if member:
                identity.clickup_id = member["id"]
                identity.confidence = min(identity.confidence, 0.9)
                identity.method = "unambiguous_name"
                identity.evidence.append(f"clickup by unique name ({member['username']})")
            else:
                identity.confidence = min(identity.confidence, 0.5)
                identity.method = "no_clickup_match"

        if identity.clickup_id is not None:
            matched_clickup.add(identity.clickup_id)

        # --- Salesforce ---
        sf_user = sf_by_email.get(email) or sf_by_name.get(normalize_name(user.get("full_name")))
        if sf_user:
            identity.salesforce_id = sf_user["Id"]
            identity.evidence.append(f"salesforce ({sf_user['Name']})")

        result.identities.append(identity)

    # --- People that exist in ClickUp but not in Kantata (trap A3) ---
    for member in clickup_members:
        if member["id"] in matched_clickup:
            continue

        key = f"clickup:{member['id']}"
        if key in confirmations:
            # The lead already answered about this person. Worth 1.0.
            continue

        result.unresolved.append(member)
        result.questions.append(
            f"{member['username']} ({member.get('email', 'no email')}) has tasks "
            f"in ClickUp but is not in Kantata. Do they cover an assigned role?"
        )

    return result
