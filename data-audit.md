# Data Audit — Go Nimbly case study

Reference date: 2026-09-02. None of these inconsistencies are documented in the README.

---

## A. Identity resolution (people)

### A1. Two distinct people with the same visible name
| System | Person 1 | Person 2 |
|---|---|---|
| Kantata | `u_10031` Inés Rocha — Solutions Consultant | `u_10077` Ines Rocha Duarte — Technical Architect |
| ClickUp | `4410034` "Ines Rocha" | `4410088` "Ines Rocha" |
| Salesforce | `...SLV02` "Ines Rocha" | `...SLV09` "Ines Rocha" |

Emails: `ines.rocha@` vs `i.rocha.duarte@`.

**Risk:** matching by name merges them → concluding a role is covered when it
isn't. A serious false positive.

### A2. One person with different names per system
| Kantata | ClickUp | Salesforce |
|---|---|---|
| M. Ferreira | Matías Ferreira | Matías Ferreira |
| Nathaniel Whitlock | Nate Whitlock | Nate Whitlock |
| Marta Zielinska-Ortiz | Marta Z-O | Marta Zielinska |
| Devika Balasubramanian | Devika B. | Devika Balasubramanian |
| Inés / Tomás / Lucía | Ines / Tomas / Lucia | Ines / Tomas / Lucia |

Inconsistent accents, initials, truncated surnames.

**Combined A1+A2 conclusion:** the name is useless as a key in either direction.
Email is the only reliable signal.

### A3. People that exist in only one system
- **R. Vance** (`rowan@vanceadvisory.io`, ClickUp id `4410500`, role 4 = guest):
  external contractor. Has tasks assigned, **does not exist in Kantata**. Real
  work invisible to the resourcing system.
- **Desmond Kerrigan** (`u_10099`): in Kantata with `active: False`. Absent from
  ClickUp and Salesforce.

### A4. Incompatible ID types
- Kantata: prefixed string (`u_10024`)
- ClickUp: integer (`4410021`)
- Salesforce: 15-char SF string (`005Ho00000FRN01`)

---

## B. Identity resolution (clients)

### B1. Different account names between SF and Kantata
| Salesforce | Kantata |
|---|---|
| Ironvale Data Group | Ironvale |
| Quillspace Software | Quillspace |
| Kestrel Logistics | *(does not exist)* |

### B2. ClickUp lists with no key to Kantata projects
`"Veridia Hierarchy"` ↔ `"Veridia — Account Hierarchy Redesign"`.
The join can only be made by client-name prefix. Fragile.

---

## C. Internal data inconsistencies

### C1. `allocation_percentage` with two mixed scales
Values present: `0.25`, `1.0`, `30`…`100`.

| Allocation | Person | Project | Raw |
|---|---|---|---|
| `a_9004` | Devika Balasubramanian | Quillspace | `0.25` |
| `a_9012` | Simon Zhao | Corvane CPQ | `1.0` |

**Verification:** Simon with `1.0` has 3 active tasks on Corvane (one blocked) →
full allocation, not 1%.
**Decision:** normalize `value ≤ 1 → ×100`. It's an assumption, declared in the
decision log.

### C2. Orphan references
- `a_9018` (Simon Zhao, 30%) points to `p_5099`, a **non-existent project**.
- `p_5005` (Corvane CPQ, Active, 760h) has `lead_user_id: null`.

### C3. Undocumented null fields
- 10 of 52 ClickUp tasks with `time_estimate: null`
- `Halden — Phase 3 Scope` (85% prob) with `Estimated_Delivery_Hours__c: null`

### C4. States that contradict the dates
Projects `Active` with a `due_date` already past:
- `p_5003` Fernbrook Health — overdue 2026-08-29
- `p_5006` Quillspace — overdue 2026-08-24

### C5. Salesforce duplicate
- `Corvane — CPQ Phase 2`
- `Corvane CPQ — Phase II`

Same AccountId, same CloseDate (2026-10-23), same 820h, same probability (25%).
**It's the same opportunity loaded twice.** Summing demand without dedup counts
1640 nonexistent hours.

### C6. Cross-system contradiction
- Salesforce: `Tessellate — Multi-Track Integration` = **Closed Lost** (2026-08-11)
- Kantata: `p_5007` Tessellate = **Active**, with allocations through 2026-11-13

One of the two is stale. Either people are assigned to a lost deal, or SF lies.

### C7. Incoming demand with no project
`Kestrel — RevOps Foundation`: 90% probability, 700h estimated, CloseDate
2026-08-28 (already past). No project or allocation in Kantata.

---

## D. Overload detected (after normalizing C1)

| Person | Load | Detail |
|---|---|---|
| Simon Zhao | **130%** | 100% Corvane + 30% on `p_5099` (phantom project) |
| Devika Balasubramanian | **125%** | 100% Halden + 25% Quillspace |

Declared capacity: 40h weekly for all 15 users (uniform).

---

## E. Convergence: Corvane CPQ is the textbook case

`p_5005` accumulates, all at once:
- No `lead_user_id` (literal vacant role)
- 3 unassigned tasks, all its own (`Untriaged — …`)
- Simon Zhao, its main resource, at 130%
- A duplicated expansion opportunity in SF worth 820h
- One task in `blocked` status

It's the scenario the exercise wants the agent to detect.

---

## F. Stub behaviors (read from the code, not the README)

### F1. `CANDIDATE_TOKENS`
If the variable is set, all endpoints require an `X-Candidate-Token` header. Empty
by default. If they give a token, it must be supported.

### F2. `CHAOS_ENABLED=false`
Disables failure injection. Useful for development; **not** for the demo.

### F3. Latency is blocking
`chaos.py` uses `time.sleep()` inside an `async` middleware. It blocks the event
loop, so parallelizing requests yields far less than expected against this stub.
Worth measuring before investing in concurrency.

### F4. Pagination without a total
`/clickup/tasks` returns `last_page` but **no** `totalSize`. If a page fails with
a 429 mid-traversal, missing data goes undetected. Pages must be counted
client-side.

### F5. Inconsistent envelopes
| Endpoint | Shape |
|---|---|
| `/kantata/*` | `{"<collection>": [...], "count": n}` |
| `/kantata/projects/{id}` | bare object, no envelope |
| `/salesforce/*` | `{"records": [...], "totalSize": n}` |
| `/clickup/members` | `{"members": [...]}` — no count |
| `/clickup/tasks` | `{"tasks": [...], "last_page": bool}` |

### F6. Date formats per system
| System | Format |
|---|---|
| Kantata | `"2026-08-19"` (ISO date) |
| Salesforce | `"2026-09-10T00:00:00.000+0000"` |
| ClickUp | `"1786233600000"` (epoch ms **as a string**) |

ClickUp's `time_estimate` comes in milliseconds (`14400000` = 4h).

---

## G. Summary for the decision log

**Handled:**
- Person identity by email, with structural signals as backup (A1, A2, A4)
- Normalization of `allocation_percentage` (C1)
- Orphan references counted and recorded (C2)
- Nulls with an explicit default (C3)
- Salesforce opportunity dedupe (C5)
- Date formats unified to UTC (F6)

**Detected and deliberately NOT handled:**
- Tessellate SF/Kantata contradiction (C6) — needs a decision on which system is
  the source of truth; that's a business decision, not a technical one
- Overdue projects still `Active` (C4) — reported as an observation, not corrected
- External contractors with no Kantata record (A3) — reported as uncertainty in
  the message
- Client matching by prefix (B1, B2) — works with these data, breaks with
  ambiguous names
