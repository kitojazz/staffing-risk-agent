# Decision Log — Staffing Risk Agent

## What I decided "staffing risk" means

An active project with work ahead and **(a)** a role with nobody assigned, or
**(b)** the only person in a role on leave without backup. Window of **14 days**.

**Why this cut, among other defensible ones:** it crosses all three systems
(which the exercise weighs) and relies on `projects`, `allocations` and
`time_off` — deliberately avoiding `time_entries`, the endpoint that fails 30%
of the time. The 14-day window comes from the *staffing lead time*: covering a
role takes 1–3 weeks, so alerting with less margin is alerting late.

**Rejected:** "over-allocated person" (lives almost entirely in one system,
crosses less) and "incoming Salesforce demand" (depends on deal probabilities,
noisier). They go in as future lines, not as the core.

**With more time:** roles aren't modeled in the data — a "vacant role" is only
representable today as `lead_user_id: null`. With a real role model, rule (a)
would be much richer.

## Backup is counted per role, not per project

Three people on a project aren't backup for each other if they do different
things. If the only Technical Architect leaves, the QA doesn't replace them. The
rule groups allocations by `job_title` before evaluating coverage. Without this,
the agent detected no leave risk at all (every project has ≥2 people).

## Where the model works, and what I put in its context

It drafts the headline and the "why it matters" of each group, and breaks ties on
identities the deterministic cascade doesn't resolve. **It only sees a small,
already-verified payload** (person, role, projects, days, hours) — never raw API
responses. It returns JSON with fixed fields; the final message is built by the
code, which writes names, dates, hours and links.

**With the provided data, the LLM identity tier does not fire:** the 14 active
users resolve by exact email. I kept it as a safety net for production (different
domains, aliases, missing emails) and say so explicitly rather than pretending it
carries weight. The model's real work is the drafting.

## Where I kept it deterministic, and why

Risk detection, arithmetic, dates, window, severity score and confidence score.
All of it has to be reproducible and auditable: if the lead asks "why is Corvane
first?", there must be a computation to show. The model contributes a signal to
the confidence, but the number is computed by the code; an LLM's self-reported
confidence is not calibrated. Temperature 0 to reduce variance — not to guarantee
truth; validation handles that.

## How I know if the model part broke

`validate_output` compares the output against the payload and rejects numbers or
entities that weren't there. If there are violations, it's discarded and the
deterministic template is used. Tested with deliberately hallucinated outputs:
inventing a person, a number, or adding false background → all three are blocked
with the exact reason. The cost of being wrong is high ("being wrong once costs
you the channel"), so when in doubt the system degrades to a template, not to
silence.

## Data: the mess I handled and the mess I let through

I audited the fixtures and documented 10 inconsistencies, none in the README
(details in `data-audit.md`). **Handled:** person identity by email, with name as
backup (there are two distinct people named "Ines Rocha" — matching by name would
merge them); normalization of `allocation_percentage` (mixes fractions and
percentages — I assumed ≤1 is a fraction, verified against Simon Zhao's real
load); orphan allocations counted and reported; Salesforce opportunity dedupe by
fingerprint; three different date formats unified to UTC.

**Deliberately not handled:** Tessellate shows Closed Lost in Salesforce and
Active in Kantata — this is a business decision (which system is the truth), not
a technical one, so it's reported as a conflict and not resolved. Overdue projects
still "Active" are observed, not corrected. Client matching by prefix works with
9 clients and breaks at scale.

## Failures and re-runs

`429` → I honor the server's `Retry-After`. `5xx` → our own exponential backoff
(no jitter: with a single client it adds nothing). 4 attempts (~99% at 30%
failure), 120s budget per run. **Critical** source down → the agent stays silent
and records it; **enriching** source down → continues and marks partial data.
With incomplete data I assert the positive ("Simon at 130%") and degrade the
negative ("nobody covers X") to uncertainty — grounded in the closed-world
assumption and CALM. The `alerted_risks` table uses the fingerprint as PK with an
upsert: re-running the same day doesn't duplicate. The decision breakdown
(`new/changed/reminder/silence`) is stored on every run, so a false silence is
auditable rather than invisible.

## Alive vs healthy: two channels

The dead man's switch is pinged whenever the agent runs (alive is alive, even
degraded). Source health goes on another channel. A single channel can't tell
"the agent died" from "the API failed". `/health` is passive today; the next step
is an external dead man's switch (Healthchecks.io) that detects silence without
anyone having to look.

## Hosting

**Render**: Postgres free tier and an HTTP endpoint on one platform, minimal build
config. Postgres and not a JSON on disk because Render's filesystem is ephemeral —
memory would be wiped on every redeploy. **In production**: AWS (EventBridge +
Lambda/ECS + RDS + Secrets Manager), to live where the rest of the infra already
is instead of adding one more platform.

## Considered and rejected

**Pydantic** for modeling data: it wouldn't have caught any of the 10 traps
(they're semantic, not type errors). A default in normalization handles the nulls
without adding a dependency.

## Deploy note: model choice

The LLM provider is swappable by env var (`LLM_BASE_URL` + `LLM_MODEL`), and that
paid off during the deploy. Gemini was used via its OpenAI-compatible endpoint.
Two live lessons: `gemini-2.5-flash` is discontinued for new accounts (returns
404), and 3.x thinking models took longer than the timeout against Render. The fix
was `gemini-flash-lite-latest`: fast, enough to draft a short alert, no heavy
thinking. That this was resolved by changing a variable — without touching the
grounding or validation logic — is proof the model/code boundary landed in the
right place.
