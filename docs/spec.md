# DSA Coach — Specification v3

The authoritative design document. Operating invariants live in
[`CLAUDE.md`](../CLAUDE.md); a plain-language overview of the features lives in
[`features.md`](features.md).

This is v3. It supersedes two earlier drafts, since removed: v1 specified the product
but underweighted evidence quality, and v2 over-corrected by demoting automatic
monitoring to a manual form. v3 restores **automatic LeetCode monitoring as an MVP
requirement** while keeping v2's deterministic foundation. See §18 for the decisions
that produced it, and §19–20 for assumptions and open risks.

---

## 1. Product

A single-user adaptive DSA interview-preparation system.

The intended loop:

1. The user opens a LeetCode problem.
2. The extension detects the problem, language, active time, Run/Submit interactions
   and submission outcome — automatically, with no user action.
3. On a submission result, a tiny questionnaire asks only what telemetry cannot
   determine: how it was solved, the main blocker, and confidence in reproducing it.
4. The user answers in 1–2 clicks.
5. The combined event syncs idempotently to the API.
6. Deterministic code updates readiness and retention.
7. Every three relevant attempts, the system checks for a material change and either
   records an explained `no_change` or asks the coach to prescribe the next block.
8. Public-profile sync reconciles in the background and covers gaps.

**Never re-ask the user for information the extension already captured.** The
questionnaire exists solely to cover the telemetry blind spot.

Never guarantee interview success. This is a preparation coach, not a predictor.

---

## 2. Architecture: three layers

**Evidence (facts).** Attempts, telemetry, questionnaire answers, code snapshots,
problem metadata, consent records. Append-only where practical. Never inferred by a
model, never invented.

**Mechanism (deterministic).** Readiness ratings, retention scheduling, prerequisite
traversal, candidate filtering, block assembly, material-change detection. Pure,
versioned, unit-tested functions over the Evidence layer. **No LLM call is permitted
here.**

**Judgment (AI).** Diagnosis, teaching, block-shape prescription, conversation. Reads
the lower layers through narrow function tools. Writes only *specifications* and
*prose* — never facts.

> **AI writes the prescription. Deterministic code fills it.**

### Runtime provider

The deployed coach uses the **OpenAI Agents SDK for Python** — Agents SDK sessions,
function tools, structured Pydantic outputs, and built-in tracing (disabled in tests).

Orchestration sits behind a `CoachRuntime` interface so a provider swap is contained,
but **only the OpenAI implementation is built in the MVP.** Do not write a second
adapter speculatively.

`OPENAI_API_KEY` exists only on the server. Never in the web bundle, never in the
extension.

---

## 3. Evidence: the attempt record

An attempt is assembled from two sources that must never be confused.

### 3.1 Captured automatically (extension)

- `problem_slug`, resolved to a catalogue `problem_id`
- `language`
- `started_at`, `submitted_at`
- `active_seconds` (§3.4)
- `run_count`, `submit_count`
- `submission_outcome`: `accepted` | `wrong_answer` | `runtime_error` |
  `compile_error` | `tle` | `unknown`
- `capture_confidence`: `high` | `medium` | `low`

### 3.2 Captured by questionnaire (1–2 clicks)

Shown once, on a submission result. Every field is optional and dismissible.

- **`resolution`** — *(one click, always asked)*
  `independent` | `after_hint` | `after_editorial` | `failed`
- **`blocker`** — *(one click, asked only on `after_hint` / `after_editorial` /
  `failed`)*
  `pattern_not_recognized` | `pattern_known_impl_failed` | `edge_cases` |
  `complexity` | `data_structure_choice` | `language_api` | `misread_problem`
- **`confidence_cold_redo`** — *(1–5, asked on success only, and only on first
  exposure or a lapsed re-solve — not on every attempt)*

Dismissal is a valid outcome. A dismissed questionnaire yields
`resolution: unknown`, which is treated as weak evidence (§6.2), not as a failure.

### 3.3 Amendment (required — the editorial timing problem)

The questionnaire fires at submission, but "solved after editorial" frequently
happens *afterwards*: the user fails, dismisses the prompt, reads the editorial, then
solves. The prompt fires at the wrong moment to capture the thing it most needs.

Therefore:

- **Every attempt is amendable** from the dashboard for 7 days. Amending recomputes
  the affected ratings and schedules from the corrected evidence.
- The extension **re-prompts once** — cheaply, non-modally — if it observes the
  solutions or editorial tab opened on a problem with a failed attempt in the last
  hour.
- Amendments are recorded, not overwritten: keep `amended_at` and the prior value.

### 3.4 Active time — and the idle trap

Excluding "idle" time sounds obviously correct and is a trap. **Staring at the screen
thinking is the most valuable activity in a DSA attempt and is indistinguishable from
idleness by any input-based heuristic.** Subtracting it systematically under-measures
exactly the sessions where the real work happened.

Therefore exclude only:

- Time when the tab is hidden (`document.visibilityState !== 'visible'`), and
- Contiguous gaps with no input **longer than 5 minutes** (configurable).

Record `excluded_seconds` and `exclusion_reason_counts` alongside `active_seconds` so
the measurement is auditable and the threshold can be revisited against real data.

### 3.5 What v3 deliberately does not ask

v2 collected `approach_complexity` by hand. A 1–2 click questionnaire cannot, and
should not, ask for it. **Recover it from static analysis of the submitted code in the
diagnosis feature (§7.3) instead**, so the "it passed but the complexity was wrong"
signal survives without user burden.

---

## 4. Chrome extension (MVP — Phase 2)

### 4.1 Stack

WXT, React, TypeScript, Manifest V3. Minimal host permissions —
`https://leetcode.com/problems/*` only. No `<all_urls>`, no `tabs` permission beyond
what is required.

### 4.2 Architecture

- **Content script** with a **replaceable LeetCode DOM adapter** (`adapters/leetcode/`).
  All selectors, all markup assumptions, and all result-panel parsing live in that one
  directory behind a stable interface. Nothing else in the extension knows LeetCode's
  DOM exists.
- **SPA navigation detection** — LeetCode is a client-routed SPA; a full page load is
  the exception. Detect route changes via the History API (`pushState`/`replaceState`
  patch + `popstate`), and close the previous problem's session on transition.
- **`MutationObserver`** on the submission result panel to detect outcomes, scoped as
  narrowly as possible and disconnected when not needed.
- **Background service worker** owning the durable queue and sync.
- **Local durable queue** (IndexedDB) with client-generated `event_uuid` per event.
  Events survive browser restart and offline periods.
- **Authenticated batch upload** with bounded exponential backoff and jitter. Retry
  only retryable failures; a 4xx other than 429 is terminal and surfaces in the UI.
- **Popup UI** showing: current state (`monitoring` / `paused` / `disconnected`),
  last successful sync, queued event count, and the pause and disconnect controls.

### 4.3 Degradation

The DOM adapter **will** break when LeetCode reships their frontend. Design for it:

- Every adapter read returns `{ value, confidence }`, never a bare value.
- If a required field cannot be determined, emit the event with
  `capture_confidence: low` and the field omitted. **Never fabricate a result.**
- If the adapter fails to find its anchor elements at all, enter a visible **degraded
  state**: the popup says monitoring is impaired, and the extension falls back to
  prompting the user manually rather than silently capturing nothing.
- Adapter health is reported to the API so degradation is visible in the dashboard,
  not just locally.
- Manual attempt logging in the web app (built in Phase 0) remains permanently
  available as the fallback path.

### 4.4 Privacy

- Monitoring is opt-in and explicitly enabled by the user.
- Pause and disconnect are always one click away in the popup.
- Code capture is **off by default** (§8).
- Never collect passwords, cookies, session tokens, or any page content outside the
  problem workspace.
- The extension never contains the `OPENAI_API_KEY`.

---

## 5. Authentication and device pairing

The MVP is local and single-user, but must not depend on a permanently embedded
shared secret. A token committed to frontend or extension code is not acceptable even
locally.

### 5.1 Design

- A bootstrap `APP_SECRET` (env var, server-only, never shipped to a client) is used
  **only** to authorize the pairing flow.
- **Dashboard:** on first run the API issues a session token to the web app, stored in
  an `httpOnly` cookie where possible.
- **Extension pairing:** Settings displays a short-lived, single-use pairing code. The
  extension exchanges it for a **device token**. The token is shown to the extension
  once and stored in `chrome.storage.local`.
- **Only a hash of each device token is stored server-side** (Argon2id or bcrypt).
  The plaintext is never persisted by the API.
- **Scopes are separate.** An extension device token may write attempt events and read
  extension config. It may not read the plan, the coach, or user settings. A dashboard
  session token may not impersonate a device.
- **Revocation** is immediate, per device, from Settings. A revoked token's subsequent
  batches are rejected with a terminal error, and the extension surfaces the
  disconnected state.
- Pairing codes expire in 10 minutes and are single-use.

All of this sits behind `get_current_user()` and a `require_scope()` dependency, so
replacing it with real auth later touches two functions.

---

## 6. Mechanism: deterministic core

### 6.1 Patterns, not topics

Topics ("arrays") are not skills. **Patterns** are: *variable-size sliding window with
a frequency map*, *monotonic stack*, *binary search on the answer*, *backtracking with
pruning*, *interval merge*, *prefix sum with hashmap*, *top-k with a heap*,
*tree DFS returning a tuple*, *0/1 knapsack*, and so on.

Patterns are what transfer to an unseen problem. The skill model runs on patterns;
topics remain only as a curriculum and UI grouping.

Problems carry **weighted** pattern edges (a problem may be 0.7 sliding-window,
0.3 hashmap).

### 6.2 Readiness estimate (Glicko-2 — experimental)

**Glicko-2 is the starting implementation, not a settled decision.** Treat it as an
experiment that must earn its place against a simpler baseline (§6.3).

Why Glicko-2 over plain Elo: v2 claimed Elo produced a calibrated probability and a
standard error. It produces neither. Glicko-2 maintains an explicit **rating deviation
(RD)** and volatility, which are needed here for sparse per-pattern evidence and for
placement early-stopping (§9).

Why it may still be the wrong choice: **per-pattern evidence will be extremely
sparse.** A realistic first month yields perhaps 5–10 first-exposure attempts on any
given pattern. Glicko-2 was designed for competitive populations with far more
observations per rated entity, and at n≈6 its output may be indistinguishable from a
much simpler estimator — while costing considerably more complexity and carrying
assumptions this domain violates (§6.2 adjustments below, and §19).

**Therefore both models sit behind one interface from Phase 1:**

```python
class ReadinessModel(Protocol):
    version: str
    def predict(self, state: PatternState, problem: Problem) -> Prediction: ...
    def update(self, state: PatternState, ev: AttemptEvidence) -> PatternState: ...
```

Implementations: `Glicko2ReadinessModel` and `BetaBaselineReadinessModel` (§6.3). Both
run on every attempt; both log predictions. Only the configured primary drives
scheduling. Switching primaries is a config change, not a rewrite.

Maintain per-pattern `(rating, rd, volatility)` plus a global estimate.

**Outcome score** from `resolution`:

| resolution | score |
|---|---|
| `independent` | 1.0 |
| `after_hint` | 0.6 |
| `after_editorial` | 0.25 |
| `failed` | 0.0 |
| `unknown` (dismissed) | derived from `submission_outcome`, at reduced weight |

**Required adjustments.** These are deviations from textbook Glicko-2 and must be
documented as such in code — they mean the model's statistical guarantees are
approximate, which is precisely why §6.3 exists.

- **First exposure vs. re-solve.** Only first exposures update the readiness rating.
  Re-solves update the retention track (§6.5) and never inflate readiness.
- **Hint/editorial usage** — folded into the outcome score above.
- **Problem rating reliability.** Each problem carries a `rating_rd`. Manually
  estimated ratings get a high RD; contest-derived ratings a low one. Low-reliability
  problems move the user's rating less.
- **Sparse per-pattern evidence.** Handled natively by RD. A pattern's readiness is
  **not displayed** until RD falls below the display threshold; until then the UI shows
  "Calibrating."
- **Multi-pattern problems.** The update is split across the problem's pattern edges
  by weight. This is non-standard; document it.
- **Repeated attempts on the same problem.** Only the first attempt within a session
  updates readiness. Subsequent attempts on the same problem contribute at a decayed
  weight and are capped.
- **Source and capture confidence.** `capture_confidence: low` and `public_sync`
  evidence carry reduced weight. Public sync reveals only accepted problems and cannot
  distinguish independent solving from editorial use — weight it accordingly.

### 6.3 Calibration and baseline comparison — do not skip this

The output of §6.2 is an **estimated readiness signal, not a probability.**

- User-facing label: **"Estimated readiness"** or **"Predicted solve likelihood
  (estimate)."** Never "probability," never a bare percentage presented as fact.
- Log every prediction made before an attempt alongside the actual outcome, in
  `readiness_predictions` — **one row per model**, tagged with `model_version`.
- Until calibration is demonstrated, display **bands** (Not ready / Developing /
  Approaching / Ready), which are honest about the resolution the model actually has.

#### The baseline

`BetaBaselineReadinessModel` — roughly 20 lines, and the thing Glicko-2 must beat.

Per pattern, split first-exposure attempts into two or three rating buckets. Within a
bucket, maintain a Beta-Binomial posterior over the outcome score (§6.2):
`Beta(α₀ + Σscore, β₀ + Σ(1 − score))`, with a weak prior seeded from the user's
self-assessed level.

- **Readiness** = posterior mean.
- **Uncertainty** = posterior credible-interval width, which drives the "Calibrating"
  state and placement early-stopping exactly as RD does.

This baseline is not a strawman. Its uncertainty is a genuine posterior rather than a
heuristic deviation, it degrades gracefully at n=3, and it makes no assumptions this
domain violates. Its one real disadvantage is that it uses problem difficulty only
through coarse bucketing, where Glicko-2 uses it continuously. Whether that advantage
survives sparse data is precisely the open question.

#### The decision rule

Both models run from Phase 1. The comparison happens in the Phase 6 calibration report
and requires real data — it cannot be settled by argument in advance.

Compare on held-out first-exposure predictions, minimum 60 observations:

- **Brier score** and **log loss** (discrimination and sharpness)
- **Reliability curve** (calibration)
- **Agreement on scheduling decisions** — how often do the two models actually produce
  a different block? If they rarely disagree, the complexity buys nothing regardless of
  which scores marginally better.

**Keep Glicko-2 only if it beats the baseline on Brier score by a margin that survives
the confidence interval, or produces materially better blocks. Otherwise make the
baseline primary and delete the Glicko-2 implementation** — do not keep both running
out of sunk cost. Record the decision, the numbers, and the date in this section.

Until that comparison runs, **the baseline is the primary model.** It is simpler, its
uncertainty is better founded, and starting there means the sparse-data regime is
handled correctly by default rather than by assumption.

### 6.4 Prerequisite DAG

An explicit `pattern_prerequisites(pattern_id, requires_pattern_id, strength)` edge
table.

**A prerequisite has three states, not two.** This section previously said a pattern
is unlocked only when every prerequisite clears its threshold with sufficient
confidence. Phase 1 showed that deadlocks a new user: before any evidence exists
nothing is calibrated, so on a fresh account 17 of 22 patterns locked and the
scheduler returned a single problem against a 51-minute budget — weakest exactly when
it is needed most. The rule is therefore:

| State | Meaning | Effect |
|---|---|---|
| **demonstrated** | calibrated, at or above `PREREQUISITE_READINESS_MIN` | unlocks |
| **refuted** | calibrated, below the threshold | **locks** |
| **unknown** | not calibrated — no verdict either way | unlocks **provisionally** |

Only a *refuted* prerequisite locks. **Locking requires evidence, not the absence of
it.**

A **provisional** unlock means the pattern is schedulable but is never chosen as a
focus and is never presented to the user as earned. This preserves the rule that
actually matters — a guess never counts as a demonstration, in either direction —
while letting a new user receive a usable plan.

The scheduler focuses only on established (non-provisional) unlocked patterns, and
will schedule a locked pattern only as a single, explicitly labelled stretch item.

### 6.5 Retention (FSRS)

**Implementation:** pin `py-fsrs` at an exact version. Record the version in
`review_schedule.fsrs_version` on every row. Write a test that exercises the upgrade
path — an FSRS version bump must migrate or recompute existing schedules
deterministically, never silently reinterpret stored parameters.

**Grading comes from demonstrated recall, not from confidence.** v2 used
`confidence_cold_redo` as the grade; confidence and recall are different things, and
fluency during a session systematically inflates confidence.

When a **scheduled re-solve** occurs, grade on the actual result:

| observed re-solve result | FSRS grade |
|---|---|
| failed to reproduce | `Again` |
| reproduced with major difficulty | `Hard` |
| reproduced with minor difficulty | `Good` |
| reproduced easily | `Easy` |

"Difficulty" is derived deterministically from active time and submit count relative
to the user's first-exposure baseline on that problem — not asked. **Hint or editorial
use reduces the grade by one level, floored at `Again`.**

`confidence_cold_redo` is still stored, as a **secondary signal only**. It feeds a
genuinely interesting derived metric: the gap between predicted and actual recall —
how well the user knows what they know.

### 6.6 Material-change detection and the three-attempt trigger

The user expects frequent reconsideration; unconditional rewriting produces churn that
destroys trust in the plan. Reconcile as follows.

**After every three newly processed relevant attempts:**

1. Recalculate readiness and retention deterministically.
2. Evaluate material change.
3. If evidence is weak → record an explained **`no_change`** result. Show it in the
   UI: *"Reviewed after 3 attempts — no change; evidence is consistent with the
   current plan."* Silence looks like a broken product; an explained no-change looks
   like a working one.
4. If material → run the agent to prescribe the next block.

**"Relevant attempt"** means: a first exposure, or a lapsed re-solve (graded `Again`
or `Hard`). Routine successful re-solves and repeat attempts on the same problem
within a session do **not** advance the trigger counter — otherwise a review-heavy day
triggers evaluations on evidence that says nothing new about skill.

**Material change** means any of:

- A pattern's rating moved beyond `MATERIAL_RATING_DELTA`, or
- A pattern's RD crossed the display threshold in either direction, or
- A prerequisite flipped locked ↔ unlocked, or
- The current block is complete, or
- Three consecutive same-blocker failures on one pattern, or
- The weekly boundary.

The trigger batch is recorded (§10) so the counter is durable, idempotent, and never
double-fires on a retry.

### 6.7 Block assembly

Given a validated prescription (§7.2):

- **Filter:** active catalogue problems, prerequisites unlocked, outside the cooldown
  window, rating inside the prescribed band, pattern matches.
- **Compose** to the prescribed mix. Default hypothesis: **60% target weakness /
  25% interleaved / 15% due re-solves.** Interleaving is deliberate — blocked practice
  inflates in-session performance and degrades transfer, and the interleaved items must
  **not** reveal their pattern to the user.
- **Respect the time budget.** Estimate minutes per problem from **both** the
  problem's rating and the predicted success on it:
  `BASE * (rating / 1500) ** 1.5 * (1 + SPREAD * (0.5 - score))`.

  Two inputs because they are known at different times. An earlier draft used the
  rating gap alone, which needs a user rating the Beta baseline does not produce; a
  score-only version then rated every problem identically before any evidence
  existed, and a fresh account received a one-item block. Rating is a fact from day
  one; readiness is not. Under-schedule rather than over-schedule.
- **Guarantee a review slot.** On a small block the 15% retention share rounds to
  zero, so overdue re-solves get crowded out entirely. Whenever a review is actually
  due, one slot is reserved out of the larger share.
- **Deterministic** given the same inputs and seed. Fully reproducible in tests.

---

## 7. Judgment: the coach agent

One agent. OpenAI Agents SDK. Structured Pydantic outputs. Narrow function tools. No
unrestricted SQL. Tracing enabled outside tests.

### 7.1 Tools

```text
get_user_goal()
get_readiness_summary()              # ratings, RDs, locked/unlocked patterns
get_recent_attempts(limit, since)    # includes resolution + blocker
get_retention_status()               # due, lapsed, at-risk
get_current_plan()
get_catalogue_stats(pattern_ids)     # counts by rating band — availability, not IDs
propose_block(prescription)          # structured; validated before application
record_no_change(reason, evidence_attempt_ids)
```

Note there is **no** `search_candidate_problems` and no tool that returns problem IDs
for scheduling. `get_catalogue_stats` returns availability counts so the agent can
prescribe a feasible band without ever selecting items.

**Tool security:** user identity from server-side run context only; enforced query
limits; every returned ID validated; optimistic concurrency on plan version; all
state-changing calls logged and idempotent.

### 7.2 Prescription

```python
class BlockPrescription(BaseModel):
    focus_patterns: list[PatternWeight]
    rating_band: tuple[int, int]
    size: int
    mix: BlockMix                          # weakness / interleaved / retention
    timed: bool
    diagnosis: str                         # what the evidence shows
    evidence_attempt_ids: list[UUID]
    rationale: str                         # shown to the user
    confidence: Literal["low", "medium", "high"]
```

The Mechanism layer **validates** every prescription against hard constraints — time
budget, locked prerequisites, catalogue availability, rating band sanity. The result is
persisted in `prescription_validations` as `accepted` | `clamped` | `rejected`, with
the specific violations recorded.

**A prescription that violates constraints is clamped and explained, never silently
dropped.** A rejected prescription falls back to the deterministic scheduler, and the
user is told the coach's suggestion could not be applied and why.

Do not parse consequential actions from prose. Structured output only.

### 7.3 Teaching features (Phase 5)

- **Hint ladder.** Five levels, strictly increasing: (1) pattern family,
  (2) key insight as a question, (3) the invariant or recurrence, (4) approach outline,
  (5) full approach. Never skip levels. **Never emit code at levels 1–4.** The level
  used is recorded on the attempt and feeds the outcome score.
- **Failure diagnosis.** Input: problem, submitted code, reported blocker, recent
  attempts on related patterns. Output: what specifically went wrong, and whether it
  repeats a prior failure mode. Includes the static complexity analysis that replaces
  v2's manual `approach_complexity` field (§3.5). Requires code consent (§8).
- **Post-solve review.** Even on an accepted solution: actual vs. optimal complexity,
  idiomatic issues, edge cases that got lucky, and the interview-communication version.
  "It passed" and "it would pass an interview" are different bars.
- **Mock interview.** Timed. The user states approach and complexity *before* coding;
  the agent pushes back, then scores against a rubric (correctness, complexity,
  communication, edge cases). Recorded as an attempt with `timed = true`.

### 7.4 Failure behavior

On agent failure: keep the current plan unchanged, record a sanitized error in
`agent_runs`, retry only retryable errors with bounded exponential backoff, never
double-apply an update, and **fall back to the deterministic scheduler**, which can
always produce a valid block. The UI states plainly that the coach is unavailable and
the plan is running on the deterministic scheduler.

---

## 8. Privacy and consent

**Code capture is off by default.**

The first time the user requests diagnosis or review, prompt:

- **Allow once** — analyze this submission, do not persist the code.
- **Always allow for LeetCode** — persist code for problems on LeetCode.
- **Do not allow** — the feature is disabled; diagnosis runs in degraded mode using
  only telemetry and the reported blocker.

The consent prompt must state, in the prompt itself:

| Question | Answer |
|---|---|
| Is code stored? | Only under "always allow." Under "allow once," it is held in memory for the request and never written to disk. |
| Is code sent to the AI provider? | **Yes** — to OpenAI, for that request. This must be stated explicitly. |
| Retention | Default 90 days, configurable, then hard-deleted by a scheduled job. |
| Storage | In `attempt_code`, a separate table from `attempts` so it can be dropped independently. Encrypted at rest via the deployment's disk/DB encryption; document the actual mechanism rather than claiming application-level encryption that is not implemented. |
| Deletion | One control in Settings deletes all stored code immediately. Per-attempt deletion also available. |
| Can analysis run without persistence? | Yes — "allow once" mode. |

Consent is versioned: `consent_version`, `granted_at`, `scope`, `revoked_at`. If the
disclosure text changes materially, consent must be re-obtained.

Also required: full data export (JSON), full account deletion, restricted CORS, request
size limits, rate limiting, sanitized logging (never log code or tokens), HTTPS in any
deployed environment.

---

## 9. Onboarding and placement

**Do not block the user from receiving a plan.** v2 required 8–12 problems across 2–3
sessions before plan v1 — that is onboarding friction with nothing shown for it.

1. **Onboarding collects:** display name, preferred language, target companies, target
   date or duration, days per week, minutes per day, self-assessed level, approximate
   problems solved, LeetCode username (optional).
2. **A provisional plan is created immediately** — a static curriculum lookup keyed by
   self-assessed level. It is explicitly not scheduled or optimized, and it is labelled
   **provisional** in the UI.
3. **The placement questions are the first practice block.** The user starts solving
   immediately; the placement is invisible as a separate step.
4. **The plan updates as placement evidence arrives.** Each attempt narrows RD.
5. **The UI shows "Calibrating"** per pattern until RD is below the display threshold.
6. **Early stopping:** placement ends when every foundational pattern's RD is below
   `PLACEMENT_RD_TARGET`, or at 12 problems, whichever comes first. The uncertainty
   measure is Glicko-2's rating deviation (§6.2) — a defined, implemented quantity,
   not an asserted standard error.

Next-problem selection during placement targets the rating where expected score is
nearest 0.5, subject to pattern coverage.

If a public LeetCode profile is connected, seed the prior from accepted-problem
history. This can shorten placement; it can never replace it, since accepted-only
history says nothing about failures or editorial use.

---

## 10. Data model

UUID primary keys, timezone-aware timestamps, foreign keys, indexes, and a uniqueness
constraint on `attempt_events.event_uuid`.

**Core**

- `users` — id, display_name, timezone, preferred_language, created_at, updated_at
- `user_goals` — id, user_id, target_companies, target_date, days_per_week,
  minutes_per_day, self_assessed_level, active
- `problems` — id, provider, external_id, slug, title, url, difficulty, rating,
  **rating_rd**, rating_source, catalogue_source_id, is_active
- `patterns` — id, slug, name, description
- `problem_patterns` — problem_id, pattern_id, weight
- `topics` / `problem_topics` — curriculum grouping only
- `pattern_prerequisites` — pattern_id, requires_pattern_id, strength

**Attempts**

- `attempts` — id, user_id, problem_id, resolution, blocker, confidence_cold_redo,
  hint_level_used, is_resolve, timed, language, started_at, submitted_at,
  active_seconds, excluded_seconds, run_count, submit_count, submission_outcome,
  source (`extension` | `manual` | `public_sync`), capture_confidence,
  **amended_at**, **prior_values** (jsonb), notes, raw_metadata
- `attempt_code` — attempt_id, language, code, retention_until, **deleted_at**,
  created_at

**Ingestion and jobs**

- `attempt_events` — id, device_id, **event_uuid** (unique), payload, received_at,
  **processing_status** (`pending` | `processed` | `duplicate` | `invalid`),
  processed_at, attempt_id, error
- `ingest_batches` — id, device_id, event_count, accepted, duplicates, rejected,
  received_at
- `trigger_batches` — id, user_id, attempt_ids, relevant_count, evaluated_at,
  material (bool), outcome (`no_change` | `prescribed`), agent_run_id
- `background_jobs` — id, job_key (unique), kind, status, attempts, last_error,
  scheduled_at, completed_at  *(idempotent by `job_key`)*

**Devices and consent**

- `devices` — id, user_id, name, kind (`extension` | `web`), **token_hash**, scopes,
  created_at, last_seen_at, **revoked_at**
- `pairing_codes` — id, user_id, code_hash, expires_at, consumed_at
- `consents` — id, user_id, scope (`monitoring` | `code_capture`), **consent_version**,
  granted_at, revoked_at, disclosure_text_hash

**Scoring and scheduling**

- `pattern_ratings` — id, user_id, pattern_id, rating, **rd**, volatility,
  evidence_count, last_practiced_at, **scoring_config_version**
- `readiness_predictions` — id, user_id, problem_id, predicted_score, actual_outcome,
  predicted_at, resolved_at  *(feeds calibration, §6.3)*
- `review_schedule` — id, user_id, problem_id, stability, difficulty, due_at,
  last_reviewed_at, lapses, **fsrs_version**

**Planning**

- `plans` — id, user_id, goal_id, version, status (`provisional` | `active` |
  `superseded` | `rolled_back`), summary, generation_context, valid_from, valid_until
- `plan_items` — id, plan_id, block_id, sequence, problem_id, item_type, role
  (`weakness` | `interleaved` | `retention`), target_minutes, status
- `prescriptions` — id, user_id, plan_id, focus_patterns, rating_band, mix, diagnosis,
  rationale, evidence_attempt_ids, confidence, agent_run_id, created_at
- `prescription_validations` — id, prescription_id, result (`accepted` | `clamped` |
  `rejected`), violations (jsonb), applied_plan_version
- `plan_changes` — id, user_id, plan_id, before_state, after_state, reason, evidence,
  trigger, agent_run_id, **reverted_by**, created_at
- `agent_runs` — id, user_id, agent_name, trigger, status, input_summary,
  output_summary, trace_id, model, usage_metadata, error_code, started_at, finished_at

**Provenance**

- `catalogue_sources` — id, name, url, **license**, snapshot_date, **version**,
  **checksum**, transformation_notes, **known_limitations**, imported_at

**Plan rollback.** Any `plan_change` is reversible: restoring `before_state` creates a
new plan version marked `rolled_back` with `reverted_by` set. History is never
destroyed.

---

## 11. Catalogue

**Phase 0 ships 30–50 carefully verified problems** covering the foundational patterns
— not 150. Sourcing and validating a large rated catalogue delays the first usable
version and front-loads licensing risk.

Build a repeatable `import-catalogue` command for later expansion.

For every imported dataset, record in `catalogue_sources`: source, **license**,
snapshot date, version/checksum, transformation process, and known quality
limitations.

**If an external rating dataset cannot be used legally or reliably, fall back visibly
to manually curated approximate ratings** — set a high `rating_rd`, set
`rating_source: manual`, and surface the limitation in the docs and the UI. Never
fabricate a rating and never present a manual estimate as a measured one.

Store metadata and legitimate links only. Do not copy protected problem statements.

---

## 12. Target companies — no numeric bands

**Do not assume DSA rating bands for Amazon, Google, Microsoft, or any other company.**
Interview difficulty varies by role, level, location, team, and interview stage.
Unsupported numeric targets create false precision, and v2's `target_rating_band` was
exactly that.

Target companies may legitimately influence:

- Topic and pattern emphasis
- Assessment format (OA vs. onsite)
- Time limits
- Mock-interview style
- Curated problem collections

Any company-specific difficulty estimate must be **labelled approximate, sourced, and
user-configurable**. If no documented evidence source exists, do not ship the number.

---

## 13. Stack

**Backend:** Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2.x async, SQLite locally
with a Postgres-compatible schema, Alembic, Pytest, Ruff, full type annotations, `uv`.

**Frontend:** React, TypeScript, Vite, TanStack Query, Tailwind, shadcn/ui, React Hook
Form, Zod, Recharts, Vitest.

**Extension:** WXT, React, TypeScript, Manifest V3, Vitest for business logic (queue,
active-time accounting, dedup) with the DOM adapter tested against fixture HTML.

**Runtime AI:** OpenAI Agents SDK for Python (§2).

**Deferred until justified:** Redis, durable worker queues, Docker Compose,
multi-tenant auth, a `packages/` layer, multi-agent orchestration.

---

## 14. API

`/api/v1`

```text
GET    /me                              PATCH  /me
POST   /onboarding

POST   /extension/pair                  # exchange pairing code -> device token
GET    /extension/config
POST   /extension/events/batch          # idempotent, partial success
GET    /devices                         DELETE /devices/{id}

POST   /attempts                        # manual fallback logging
GET    /attempts
PATCH  /attempts/{id}                   # amendment (§3.3)
POST   /attempts/{id}/code

GET    /today
GET    /progress/readiness              # bands + Calibrating states
GET    /progress/retention
GET    /plan/current                    GET    /plan/history
POST   /plan/prescribe
POST   /plan/changes/{id}/revert
GET    /plan/changes

POST   /coach/hint                      POST   /coach/diagnose
POST   /coach/review
POST   /coach/mock/start                POST   /coach/mock/turn

GET    /consents                        POST   /consents
POST   /privacy/export                  POST   /privacy/delete
POST   /integrations/leetcode/sync      GET    /integrations/leetcode/status
```

The batch endpoint enforces: idempotency by `event_uuid`, a maximum batch size,
per-event validation, **partial success responses** (per-event accepted / duplicate /
invalid), device scope checking, and server-assigned receipt timestamps.

---

## 15. UI

1. **Today** — the current block, per-problem start links, the current prescription's
   rationale in one sentence, and monitoring status.
2. **Activity** — attempts with source and capture confidence; **amend** control;
   flags for anything solved-but-not-captured from profile sync.
3. **Progress** — readiness bands per pattern (with "Calibrating" where RD is high),
   retention health, what is due.
4. **Plan** — blocks, prescription history with cited evidence, version diffs,
   `no_change` decisions shown as first-class events, revert control.
5. **Coach** — hint ladder, diagnosis, review, mock interview.
6. **Settings** — goal, extension pairing and device revocation, monitoring pause,
   code-capture consent, retention, export, delete.

Loading, empty and error states everywhere. Keyboard accessible. Responsive. A visible
banner whenever the coach is unavailable or the extension is degraded.

---

## 16. Phases and exit criteria

### Phase 0 — Usable skeleton
FastAPI + SQLite/Alembic + React shell. 30–50 verified catalogue problems with pattern
tags. Provisional onboarding plan (**a static curriculum lookup — not a scheduler;
scheduling is Phase 1**). Attempt-ingestion API with `event_uuid` idempotency. Minimal
manual attempt logging. Today screen.

**Exit:** the user can complete onboarding, see a provisional plan, and log a real
attempt manually today. Ingestion is provably idempotent under test. `CLAUDE.md`'s
Commands section is populated with verified commands.

### Phase 1 — Deterministic mechanism
Pattern model; `ReadinessModel` interface with **both** the Beta-Binomial baseline
(primary) and Glicko-2 (experimental), both logging to `readiness_predictions`;
prerequisite DAG; FSRS re-solve scheduling (pinned version); block assembly;
material-change detection; three-attempt trigger batching. Comprehensive unit tests on
every pure function.

**Exit:** the system produces a defensible, time-budget-respecting next block with **no
AI involved**; readiness and retention update correctly from seeded evidence; both
readiness models run and log predictions; switching the primary model is a config
change with no other code edit. All mechanism tests green.

### Phase 2 — Chrome extension MVP
WXT/MV3 scaffold, LeetCode DOM adapter, SPA navigation, MutationObserver result
detection, active-time accounting, local durable queue, pairing and device tokens,
idempotent batch sync with backoff, post-submission questionnaire, amendment
re-prompt, pause/disconnect, degraded-state handling.

**Exit:** attempting a real LeetCode problem produces a complete attempt in the
dashboard with no manual data entry beyond the 1–2 click questionnaire. Replaying the
same batch creates no duplicates. Revoking the device rejects subsequent events.

### Phase 3 — Adaptive placement
Placement as the first practice block, provisional → calibrated plan transition,
RD-based early stopping, "Calibrating" UI states, optional profile-seeded priors.

**Exit:** a new user reaches a calibrated plan through normal practice, never through a
blocking test, and RD demonstrably falls with evidence.

### Phase 4 — AI judgment layer
`CoachRuntime` over the OpenAI Agents SDK, narrow function tools, structured
prescriptions, prescription validation and clamping, three-attempt evaluation with
material-change gating, `no_change` recording, plan versioning with optimistic
concurrency, `agent_runs` audit, deterministic fallback, agent evaluations.

**Exit:** prescriptions are applied only after validation; an out-of-budget or
locked-prerequisite prescription is clamped and explained; killing the API key leaves
the scheduler fully functional; evaluation suite green on deterministic doubles.

### Phase 5 — Teaching features
Hint ladder, code diagnosis with static complexity analysis, post-solve review, mock
interview, code-capture consent flow.

**Exit:** hints never leak the next level; diagnosis cites only real attempts;
diagnosis works in degraded mode without code consent; consent is recorded and
revocable, and revocation deletes stored code.

### Phase 6 — Reconciliation and hardening
Optional public-profile sync, extension/profile deduplication, calibration report,
security/privacy/accessibility review, deployment docs.

**Exit:** profile sync flags solved-but-uncaptured problems without creating
duplicates; the calibration report runs on real data and the **§6.3 model decision is
made, recorded with its numbers, and the losing implementation deleted**; the a11y and
security reviews are documented with findings addressed.

---

## 17. Acceptance criteria (MVP)

The MVP is complete when the user can:

1. Complete onboarding and receive a **provisional plan** immediately.
2. Install and **pair** the Chrome extension.
3. Enable LeetCode monitoring **explicitly**.
4. Open and attempt a LeetCode problem.
5. Have objective activity captured **automatically** — no manual data entry.
6. Complete the subjective questionnaire in **1–2 clicks** (and dismiss it freely).
7. See the combined attempt in the dashboard, with source and capture confidence.
8. Process three attempts **without duplicates**, including on batch replay.
9. See readiness and retention **recalculated deterministically**.
10. Receive either an explained **`no_change`** decision or an **evidence-cited**
    adjusted block.
11. **Pause monitoring and revoke** the extension device.
12. **Keep using the scheduler when the AI provider is unavailable.**

**Plus one outcome criterion**, because every criterion above measures the machine
rather than the user: after 30 days of use, the system can display first-attempt
success rate over time within a fixed rating band, and the user can judge from it
whether they are actually improving. If that number is flat, the product is not
working, regardless of how green the test suite is.

---

## 18. Key decisions made

1. **Automatic monitoring restored to the MVP.** The extension is Phase 2, not
   deferred. Manual logging remains as a permanent fallback, not the primary path.
2. **Hybrid capture.** Telemetry for objective facts, a 1–2 click questionnaire for the
   telemetry blind spot (independent / hint / editorial / failed).
3. **Attempts are amendable, and the extension re-prompts after editorial access** —
   because the editorial is usually read *after* the submission event fires.
4. **Runtime provider: OpenAI Agents SDK**, behind a `CoachRuntime` interface with one
   implementation. Claude Code is the development agent; the two are never conflated.
5. **Readiness sits behind a `ReadinessModel` interface with two implementations.**
   A Beta-Binomial baseline is primary; Glicko-2 is experimental and must beat it on
   real data (§6.3) or be deleted. v2's claim that Elo yields a calibrated probability
   and a standard error was unsupported; neither replacement inherits that claim.
6. **Readiness is presented as bands, not percentages**, until a calibration report
   justifies otherwise.
7. **FSRS grades on demonstrated recall**, derived from re-solve performance; confidence
   is a secondary signal only. `py-fsrs` pinned, version recorded, upgrade path tested.
8. **Three-attempt trigger retained, gated by material change**, with an explained
   `no_change` surfaced in the UI rather than silence.
9. **Placement is the first practice block**, not a gate. A provisional plan exists from
   minute one.
10. **Code capture off by default**, just-in-time three-way consent, explicit disclosure
    that code is sent to OpenAI.
11. **No company rating bands.** Company targets influence emphasis and format only.
12. **30–50 verified problems in Phase 0**, with a provenance table and a repeatable
    import command.
13. **Pairing-based device tokens**, hashed server-side, scoped, revocable. No embedded
    permanent secret.
14. **Idle time is only hidden-tab time plus >5 min input gaps**, recorded and
    auditable — thinking time is not idleness.
15. **`approach_complexity` is derived from code analysis**, not asked, preserving the
    signal without questionnaire burden.

## 19. Remaining assumptions

- LeetCode's DOM exposes submission outcome, language and problem slug reliably enough
  for `capture_confidence: high` on the common path. **Unverified — Phase 2 must
  validate this first, before building on it.**
- A contest-derived rating dataset is legally usable. If not, the manual fallback in
  §11 applies and RDs rise accordingly.
- LeetCode's public profile interface remains available for reconciliation. Nothing in
  the product depends on it.
- The user will answer the questionnaire consistently. If dismissal rates are high in
  practice, resolution quality degrades and the model must lean harder on telemetry.
- Glicko-2's assumptions are violated by the weighted multi-pattern update, and
  per-pattern evidence will be far sparser than the regime it was designed for. It is
  **experimental**, held to the §6.3 comparison, and deleted if it does not earn its
  complexity. The Beta-Binomial baseline is primary until then.
- A single agent suffices. Do not add more without evaluation evidence.

## 20. Risks requiring validation

| Risk | Why it matters | Validation |
|---|---|---|
| **DOM adapter fragility** | The whole automatic path rests on it; LeetCode ships frontend changes without notice | Fixture-based adapter tests; visible degraded state; permanent manual fallback; adapter health reported to the API |
| **Questionnaire fatigue** | If dismissal is common, the highest-value signal disappears | Track dismissal rate from day one; if it exceeds ~30%, reduce to a single question |
| **Readiness miscalibration** | Bad estimates produce bad blocks and erode trust | `readiness_predictions` logging from Phase 1; calibration report in Phase 6; bands until proven |
| **Model complexity buys nothing** | Glicko-2 may be indistinguishable from a 20-line baseline at realistic per-pattern sample sizes, while costing real complexity | Both models run behind `ReadinessModel` from Phase 1; baseline is primary; §6.3 decision rule with a delete-the-loser outcome |
| **Rating dataset licensing** | Blocks or degrades the scheduler | Resolve before Phase 0 seed; manual fallback ready |
| **Plan churn** | Frequent changes destroy trust more than infrequent ones | Material-change gating; `no_change` shown explicitly; monitor change frequency |
| **Thinking time misread as idle** | Systematically under-measures the best sessions | Conservative 5-min threshold; `excluded_seconds` recorded for later review |
| **Phase 2 schedule risk** | Extension work is the largest unknown in the plan | Phase 0's manual logging means Phase 2 slipping never blocks practice |

---

## 21. Phase 0 assignment (exact)

**Before coding:** inspect the repository, report Git status, summarize existing files,
propose the precise file tree, and state assumptions — especially the catalogue rating
source's license and availability (§11), which is the one external dependency that can
force a fallback. Resolve it before seeding.

**Build:**

1. Monorepo: `apps/api`, `apps/web`, `docs/`, `.env.example`, README.
2. FastAPI app with `/api/v1/health`; Ruff, mypy/pyright, Pytest configured.
3. SQLAlchemy 2.x async + Alembic; initial migration covering the §10 tables needed
   now: `users`, `user_goals`, `problems`, `patterns`, `problem_patterns`,
   `pattern_prerequisites`, `attempts`, `attempt_events`, `catalogue_sources`.
   Postgres-compatible schema on SQLite.
4. Catalogue seed: **30–50 verified problems** with weighted pattern tags, ratings (with
   `rating_rd` and `rating_source`), and a populated `catalogue_sources` row.
   `import-catalogue` command for later expansion.
5. `POST /api/v1/onboarding` → provisional plan via **static curriculum lookup**.
   Explicitly not a scheduler.
6. `POST /api/v1/attempts` (manual) and `POST /api/v1/extension/events/batch`, both
   idempotent by `event_uuid`, with partial-success responses on the batch endpoint.
7. React shell: onboarding form, Today screen showing the provisional plan, manual
   attempt-logging form.
8. Tests: ingestion, duplicate `event_uuid` rejection, batch partial success, onboarding
   validation, catalogue seed integrity.
9. `docs/development.md` with exact local startup instructions.
10. **Populate the Commands section of `CLAUDE.md`** with the real, verified commands
    for running, testing, linting, type-checking, migrating and seeding. Every command
    listed must have been executed successfully at least once.

**Do not** begin readiness modelling, FSRS, block assembly, the extension, or the agent
in Phase 0.

**Report:** files created/changed, commands executed, **actual** test/lint/type output,
architecture decisions, assumptions, known limitations, exact startup instructions, and
the recommended next milestone.
