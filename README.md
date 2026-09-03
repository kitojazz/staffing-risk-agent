# Staffing Risk Agent

An agent that detects coverage risks in delivery projects by reconciling Kantata,
Salesforce and ClickUp, and reports them to Slack.

---

## What counts as "staffing risk"

> An active project has work ahead and **(a)** a role with nobody assigned,
> or **(b)** the only person covering that role is on leave without backup.

Window: **14 days** (configurable). The criterion is the *staffing lead time*:
covering a role takes 1 to 3 weeks, so alerting with less margin is alerting late.

What the agent does **not** do: it doesn't suggest who to assign. That decision
needs context that isn't in the data (real skills, client relationship, career
plans). Pretending it knows would be worse than staying silent.

---

## How to run it

### Local

```bash
# 1. Start the mock API (in another terminal)
cd eng-case-study && uvicorn app.main:app --port 8000

# 2. Run the agent once
pip install -r requirements.txt
API_BASE_URL=http://localhost:8000 python -m agent.main
```

Without `DATABASE_URL` it uses an in-memory store. Without `GEMINI_API_KEY` it
falls back to the deterministic template. Without `SLACK_WEBHOOK_URL` it prints
the message to the log. All three degradations are intentional.

### As a service

```bash
uvicorn agent.main:app --host 0.0.0.0 --port 8000
```

| Endpoint | What it does |
|---|---|
| `POST /run` | Forces a run and returns the summary |
| `GET /health` | Heartbeat: timestamp of the last successful run |

The cron and the endpoint call the **same** function (`run_once`). There are no
two paths that can drift apart.

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `API_BASE_URL` | `http://localhost:8000` | Base of the mock API |
| `CANDIDATE_TOKEN` | empty | `X-Candidate-Token` header if the stub requires it |
| `DATABASE_URL` | empty | Postgres. Without it, in-memory store |
| `GEMINI_API_KEY` | empty | Without it, deterministic template |
| `LLM_MODEL` | `gemini-flash-lite-latest` | Fast; no heavy thinking |
| `LLM_BASE_URL` | Gemini OpenAI-compat endpoint | Switch provider = change this |
| `SLACK_WEBHOOK_URL` | empty | Without it, the message goes to the log |
| `HEARTBEAT_URL` | empty | Dead man's switch ping target |
| `WINDOW_DAYS` | `14` | Analysis window |
| `MAX_ATTEMPTS` | `4` | Retries per request |
| `RUN_BUDGET_SECONDS` | `120` | Time budget per run |
| `REMINDER_DAYS` | `7` | How often to repeat a still-live risk |
| `RESOLUTION_TTL_DAYS` | `90` | Expiry of human confirmations |

---

## Architecture

```
Trigger           weekly cron + POST /run
    v
Ingestion         3 APIs · Retry-After honored · exponential backoff
                  4 attempts · 120s budget · marks completeness
    v
Normalization     scales, dates, nulls, orphans
    v
Identity          email -> unambiguous name -> signals -> LLM
    v
Rules             deterministic, with traceable evidence
    v
Memory            Postgres: new, changed, or already alerted?
    v
Drafting          LLM with structured output + fact validation
    v
Output            Slack webhook, or log
```

### Modules

| File | Responsibility |
|---|---|
| `config.py` | Everything tunable, in one place |
| `ingest.py` | HTTP with retries. Returns `Fetched`, never a bare list |
| `normalize.py` | Cleanup of the mess documented in the audit |
| `identity.py` | Cascading identity resolution, with a score |
| `rules.py` | Risk detection. No model |
| `state.py` | Memory between runs + heartbeat |
| `llm.py` | Drafting and tie-breaking. With output validation |
| `compose.py` | Grouping by cause, prioritization, rendering |
| `main.py` | Orchestration and endpoints |

---

## Where the model works, and where it doesn't

**Yes:**
- Drafts the headline and the "why it matters" of each risk group
- Breaks ties on identities the deterministic cascade didn't resolve

**No:**
- Arithmetic, dates, windows
- Vacancy detection
- Severity score
- Confidence score (the model contributes a signal; the code computes it)

Uses Gemini (`gemini-flash-lite-latest`) via its OpenAI-compatible endpoint.
The provider is swappable: only `LLM_BASE_URL`, `LLM_MODEL` and the key env var
change.

The reason for the deterministic cut is reproducibility. If the lead asks "why
is Corvane first?", there has to be a computation to show.

### Grounding

The model never sees raw API data. It receives a small, already-verified payload,
returns JSON with fixed fields, and the final message is built by the code:
names, dates, hours and links are written by `compose.py`, not by the model.

### How I know if it broke

`llm.validate_output` compares the output against the payload and rejects numbers
or entities that weren't there. If there are violations, the output is discarded
and the deterministic template is used.

Tested with deliberately hallucinated outputs:

| Case | Result |
|---|---|
| Clean output | passes |
| Invents a person ("Pedro Gomez") | blocked |
| Invents a number (1200 hours) | blocked |
| Adds false background ("works at Accenture since 2019") | blocked |

---

## Failure handling

The stub fails on purpose: `429` (~12.5%) on any endpoint and `500` (~30%) on
`/kantata/time_entries`.

- **429** -> honor the server's `Retry-After`. Guessing costs more.
- **5xx** -> our own exponential backoff (1s, 2s, 4s). No jitter: with a single
  client it adds nothing and only adds log noise.
- **4 attempts** -> ~99% success at a 30% failure rate.
- **120s budget** per run, shared. Without it, three endpoints in simultaneous
  retry hang the run.

### Critical vs enriching sources

| Type | Collections | If it fails |
|---|---|---|
| Critical | `projects`, `allocations`, `time_off`, `users` | The agent **stays silent** |
| Enriching | `tasks`, `members`, `opportunities`, `time_entries` | Continues, marks partial data |

The problem's scope avoids `time_entries`, the most fragile endpoint. That wasn't
luck: the risk definition was chosen knowing where the fragility was.

### Partial data: what can be asserted

Operating rule, grounded in the closed-world assumption (Reiter, 1978) and the
CALM theorem (Hellerstein, 2010):

- **Positive claims** ("Simon is at 130%") come from data we do have. More data
  can only confirm them -> **safe with partial data**.
- **Negative claims** ("nobody covers this role") depend on data we don't have.
  A single missing record flips them -> **require completeness**.

With incomplete data, the agent asserts the positive and degrades the negative
to uncertainty. It doesn't lose the case silently: it reports "couldn't evaluate X".

---

## When the agent stays silent

- No risks -> sends nothing
- The risk is identical to one already alerted and less than 7 days old -> silence
- A critical source is down -> silence, recorded in `runs`

Silence is a decision, not a default. That's why `/health` exists: without a
heartbeat, a healthy agent and a dead one look the same.

## When it asks instead of asserting

When the ambiguity **changes the decision**. Real example from this data:
R. Vance has tasks in ClickUp and doesn't exist in Kantata. If they cover a role,
there's no risk; if not, there is. The agent asks instead of assuming.

The answer is saved in `resolutions` with confidence 1.0 and not asked again,
unless 90 days pass or the data for that person or project changes.

---

## Idempotency

Running the agent twice the same day doesn't duplicate alerts. The `alerted_risks`
table uses the risk fingerprint as its primary key, with `ON CONFLICT DO UPDATE`.

---

## Deploy

`render.yaml` defines the web service and a Postgres database on the free tier.
(The weekly cron was removed because Render no longer offers a free tier for cron
jobs; the agent is triggered via `POST /run`, and in production the schedule would
live in EventBridge on AWS.)

Render was chosen for three concrete reasons: Postgres free tier and an HTTP
endpoint on the same platform, with minimal build config. In production this would
go to AWS (EventBridge + Lambda/ECS + RDS + Secrets Manager), to live where the
rest of the infrastructure already is instead of adding one more platform.

Postgres and not a JSON file because Render's filesystem is ephemeral: the agent's
memory would be wiped on every redeploy.
