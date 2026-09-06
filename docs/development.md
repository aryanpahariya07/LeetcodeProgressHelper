# Development

Local setup and the commands you'll actually use. Design lives in
[`spec.md`](spec.md); the feature list is in [`features.md`](features.md).

## Prerequisites

| Tool | Version used | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | 0.11.8 | Manages Python itself — no system Python needed |
| Node.js | 24.x | npm 11 |

There is no system Python requirement. `uv` downloads and pins CPython 3.12 on
first sync.

## First run

```bash
# 1. API dependencies (installs Python 3.12 if absent)
cd apps/api
uv sync

# 2. Create the database and apply migrations
uv run alembic upgrade head

# 3. Seed the problem catalogue (idempotent — safe to re-run)
uv run dsa-coach seed

# 4. Start the API on :8000
uv run uvicorn dsa_coach.main:app --reload

# 5. In a second terminal — the web app on :5173
cd apps/web
npm install
npm run dev
```

Open <http://localhost:5173>. The header shows a live API connection indicator; if
it reads "API unreachable", step 4 isn't running.

Vite proxies `/api` to `127.0.0.1:8000`, so the browser never makes a cross-origin
request in development and no base-URL variable is needed.

Configuration defaults are fine for local use. To override, copy `.env.example` to
`apps/api/.env`.

## Commands

Run from `apps/api`:

| Command | Does |
|---|---|
| `uv run uvicorn dsa_coach.main:app --reload` | Start the API |
| `uv run pytest` | Tests |
| `uv run ruff check .` | Lint |
| `uv run ruff format .` | Format |
| `uv run mypy` | Type check (strict) |
| `uv run alembic upgrade head` | Apply migrations |
| `uv run alembic check` | Detect model/migration drift (invariant 11) |
| `uv run alembic revision --autogenerate -m "..."` | New migration |
| `uv run dsa-coach seed` | Seed / refresh the catalogue |
| `uv run dsa-coach import-catalogue --file X --rating-source Y` | Import a catalogue file |

Run from `apps/web`:

| Command | Does |
|---|---|
| `npm run dev` | Start the web app |
| `npm test` | Tests |
| `npm run lint` | Lint |
| `npm run typecheck` | Type check |
| `npm run build` | Production build |

## Layout

```
apps/api/src/dsa_coach/
  main.py          FastAPI app, CORS, request-size limit
  config.py        Deployment settings
  tuning.py        Product hypotheses — thresholds with their rationale
  auth.py          get_current_user() — the identity boundary (invariant 10)
  db.py            Async engine, session factory, declarative base
  models.py        ORM models (spec §10)
  schemas.py       Request/response models
  ingest.py        Idempotent attempt ingestion (invariant 7)
  catalogue.py     Catalogue import with provenance
  curriculum.py    Provisional plan — a static lookup, NOT a scheduler
  cli.py           dsa-coach commands
  data/            catalogue.json
  routers/         health, onboarding, plan, progress, attempts, extension

  mechanism/       PURE FUNCTIONS ONLY — no database, no LLM, ever
    evidence.py      attempt -> weighted evidence; what counts and how much
    readiness/       base.py (protocol), baseline.py (PRIMARY), glicko.py (experimental)
    prerequisites.py DAG gating and topological order
    retention.py     FSRS wrapper; grade derived from demonstrated recall
    blocks.py        block assembly: mix, budget, determinism
    placement.py     stopping rule and information-maximising selection
    prescription.py  validate + clamp a coach prescription (invariant 3)
    triggers.py      relevance and material-change detection

  coach/           the judgment layer — the only place the provider is imported
    runtime.py       CoachRuntime protocol, context and outcome types
    stub.py          deterministic coach: test double AND production fallback
    openai_runtime.py  OpenAI Agents SDK implementation

  services/        database glue around the mechanism layer
    coach.py         run, validate, record, hand to the scheduler
    placement.py     placement progress — derived from evidence, never stored
    readiness.py     applies evidence to both models; logs predictions
    retention.py     review schedule persistence
    scheduling.py    builds and persists the next block
    triggers.py      three-attempt batching with durable snapshots
    pipeline.py      what runs after an attempt is recorded

apps/web/src/
  lib/             API client, schemas, formatting, queries — all business logic
  components/      Presentational primitives
  pages/           Today, Onboarding, Progress, Log attempt, Settings
```

## The mechanism layer

Everything under `mechanism/` is a pure function over plain dataclasses. No database
handle, no clock it did not receive as an argument, and — invariant 2 — no LLM call,
ever. That is why its ~130 tests run in well under a second, and why the scheduler is
reproducible: same evidence and seed, same block.

`services/` is the only place that touches the database. To change how something is
*calculated*, edit `mechanism/`; to change how it is *stored or fetched*, edit
`services/`.

### Two readiness models

Both run on every attempt and log a prediction, so the Phase 6 bake-off has real data
(spec §6.3). The Beta-Binomial baseline is **primary**; Glicko-2 is **experimental**
and must beat it or be deleted. Switching primary is one line in
`mechanism/readiness/__init__.py`.

Compare them on the same evidence:

```bash
curl localhost:8000/api/v1/progress/readiness
curl "localhost:8000/api/v1/progress/readiness?model_version=glicko2-v1"
```

They already disagree on small samples — Glicko swings harder on sparse data, which
is exactly the behaviour the comparison exists to judge.

On the frontend the same separation applies: business logic lives in `lib/`, never
inside a `.tsx` file. That is an engineering rule in `CLAUDE.md`, and it is also why
the web tests cover the interesting behaviour without rendering anything.

## Adding a migration

Schema changes always ship with a migration — invariant 11, and
`tests/test_migrations.py` enforces it by diffing the migrated database against the
models. Add a column without a migration and the suite fails.

```bash
cd apps/api
uv run alembic revision --autogenerate -m "add whatever"
# read the generated file before applying it — autogenerate is a draft
uv run alembic upgrade head
```

## About the catalogue ratings

The catalogue ships 50 problems whose **ratings are curator estimates, not measurements**.
They carry `rating_source = manual` and `rating_rd = 300` so the readiness model
discounts them heavily, and the UI shows "approx." instead of a number.

`catalogue_sources` records the licence, snapshot date, checksum and known
limitations of every import. If a licensed contest-derived dataset is adopted later,
re-source the ratings through `import-catalogue --rating-source contest_derived`
rather than editing values in place.

## What Phase 5 deliberately does not do

- **No public-profile sync.** Reconciling LeetCode's accepted-problem history
  against recorded attempts is Phase 6.
- **No calibration report.** The readiness bake-off (spec §6.3) runs in Phase 6,
  and until it does, readiness stays in bands.
- **No scheduled retention purge.** `purge_expired` exists and is tested, but
  nothing calls it on a timer yet.
- **No amendment re-prompt.** Spec §3.3 wants the extension to notice you opening
  the editorial after a failure and ask again. Not built yet — attempts are
  editable in the web app instead.
- **No automatic coach runs.** A material change is recorded as `pending_agent`
  and the coach is asked on demand. Scheduling that automatically is Phase 6 work;
  the deterministic scheduler carries on regardless either way.
- **Dashboard auth is still by locality.** The extension now holds a real scoped,
  revocable credential; the dashboard is trusted because it is on localhost. Both
  paths go through `get_current_user()`, so a real session token is one function.

## The extension

```bash
cd apps/extension
npm install
npm run build          # -> .output/chrome-mv3
```

Load it in Chrome: `chrome://extensions` → enable Developer mode → **Load
unpacked** → select `apps/extension/.output/chrome-mv3`.

Then in the web app go to **Settings**, generate a pairing code, and type it into
the extension popup. The extension holds a scoped, revocable credential from that
point on; revoking it in Settings kills it on the device's next request.

### Layout

```
apps/extension/
  entrypoints/
    background.ts          service worker: owns the queue and ALL network access
    leetcode.content.ts    watches the page; holds no credentials
    popup/                 pairing, pause, disconnect, sync status
  src/
    adapters/leetcode/     THE ONLY PLACE THAT KNOWS LEETCODE'S MARKUP
    lib/                   pure logic — active time, session, retry, storage, api
    ui/questionnaire.ts    the 1-2 click panel, in a closed shadow root
```

The content script never calls the API. It observes and hands finished attempts
to the background worker, so navigating away mid-sync cannot lose an event.

### Verifying the extension

**The DOM selectors have not been checked against a live LeetCode page.** The
tests prove the parsing logic is right *given* markup of a certain shape; they
cannot prove the shape is right. Until this is done, assume capture is broken.

To verify:

1. Load the extension, pair it, open any LeetCode problem.
2. Solve and submit it. The questionnaire should appear bottom-right.
3. Answer it, then check the attempt appears in the web app's Activity list with
   `source: extension`.
4. If nothing appears, open the extension popup — a broken adapter reports
   **"Monitoring impaired"** rather than failing silently (spec §4.3).
5. Fix selectors in `src/adapters/leetcode/adapter.ts` only. Nothing outside that
   directory should need to change.

Selectors deliberately avoid CSS classes: LeetCode ships hashed class names that
change on essentially every deploy. The adapter prefers the URL, then
`data-e2e-locator` attributes, then user-facing verdict text.

### Why the API needs a host permission

The extension's manifest requests `http://127.0.0.1:8000/*` alongside LeetCode.
That is the DSA Coach API: a host permission is what lets the service worker
reach it without CORS, and the API's CORS allowlist deliberately covers only the
web app. If the API moves, update both `wxt.config.ts` and `apiBaseUrl` in
`src/lib/storage.ts`.

## Placement (Phase 3)

Placement is **not a test**. It is the first practice block, with problems chosen
to be informative rather than comfortable, and the user is never blocked waiting
for it to finish (spec §9).

Placement state is **derived, never stored** — a function of the attempts recorded
and the readiness they produced. There is no flag to fall out of sync with the
evidence, and deleting an attempt gives the honest answer rather than a stale one.

Two ideas drive the selection:

- **Aim at a coin flip.** An attempt you are equally likely to pass or fail says
  the most about where you stand. One you would certainly pass says almost nothing.
- **Breadth before depth.** One problem per uncovered foundational pattern before
  any pattern gets a second.

It ends on whichever comes first: every foundational pattern calibrated, or
`PLACEMENT_MAX_PROBLEMS` distinct problems — with a floor so a short lucky run
cannot end it early. Grinding one problem does not advance it; placement counts
distinct problems, because it is asking about breadth.

While placing, the plan is `provisional` and generated by `placement_block`. On
completion it becomes `active` and comes from `deterministic_block_assembler`.

```bash
curl localhost:8000/api/v1/progress/placement
```

### Placement almost always ends at the ceiling right now

With curator-estimated ratings (`rating_rd = 300`), evidence is discounted enough
that a pattern needs roughly six observations to calibrate. Twelve placement
problems spread across five foundational patterns gives about two each — so
placement reliably ends via the problem ceiling, not via calibration, and patterns
still read "Calibrating" afterwards.

That is honest rather than broken: the ceiling's stated reason says the evidence
is "still thin in places", and the plan afterwards is based on real attempts
rather than a questionnaire. It would improve on its own if a contest-derived
rating dataset replaced the manual estimates (§11), because the evidence would
stop being discounted so heavily.

## The judgment layer (Phase 4)

The coach decides the **shape** of the next block. Deterministic code decides the
problems. That split is invariant 3, and it is structural rather than a rule the
model is asked to follow: `Prescription` has no field for a problem id, so there
is no way to supply one.

```
coach/
  runtime.py          the CoachRuntime protocol, context and outcome types
  stub.py             deterministic coach — test double AND production fallback
  openai_runtime.py   the only file that imports the provider
mechanism/
  prescription.py     validate + clamp — pure, and the reason invariant 3 holds
services/coach.py     run, validate, record, hand to the scheduler
```

### Running without a key is a supported configuration

Leave `OPENAI_API_KEY` unset and `build_runtime` returns the deterministic coach.
There is no "AI disabled" branch anywhere else in the codebase, because there does
not need to be one — the absence of a key simply selects a coach that reasons
arithmetically instead of statistically, and everything downstream is unchanged.

That is invariant 4, and `tests/test_coach.py::TestInvariantFour` is what keeps it
true.

### What happens to a bad prescription

Every prescription is validated against facts it cannot argue with:

| Situation | Outcome |
|---|---|
| Names a pattern that does not exist | dropped, recorded |
| Targets a locked pattern | dropped, recorded |
| Targets a *provisionally* unlocked pattern | dropped — aiming at unproven foundations is aiming at a guess |
| Asks for more than the day allows | clamped to what fits |
| Band outside the catalogue | clamped; an empty band widens rather than returning nothing |
| Removes interleaving | refused, raised back to the floor |
| Nothing valid left | rejected; the deterministic scheduler builds the plan |

Clamping is never silent. The adjustments appear in the API response, in the UI,
and in `prescription_validations`. The **original** prescription is stored, not
the clamped one — otherwise the audit trail would agree with whatever was applied
and tell you nothing.

### Errors are sanitized

A provider error message can echo the prompt back, and the prompt contains the
user's practice history. Only the exception class name is stored. Verified: a
wrong API key yields `error_code=unavailable`, `error_detail='AuthenticationError'`,
and nothing else.

### Agent evaluations

`tests/test_coach.py` runs against deterministic doubles — no network, no key, no
flakiness (spec §15). Each double is a specific way a model can be wrong:
hallucinating a pattern, exceeding the budget, removing interleaving, timing out,
rate-limiting, returning nothing at all. Live-model evaluation stays opt-in and is
not part of the suite.

## Teaching and code consent (Phase 5)

```
mechanism/
  hints.py         the ladder: level gating and the no-code check
  complexity.py    static complexity estimate from source
coach/teaching.py  TeachingRuntime: hint, diagnose, review, mock (+ stub)
services/
  consent.py       consent state, code storage, deletion, retention purge
  teaching.py      orchestration, and what gets recorded
```

### The hint ladder holds because it is checked, not requested

The model is told not to include code below level 5. It is also **checked**, on
every hint, before the user sees it — a hint that contains code is refused
outright rather than stripped, because a snippet with its lines removed is still
a spoiler.

Levels cannot be skipped either. Asking for level 5 on your first hint gets you
level 1. Without that, "give me a hint" quietly becomes "give me the answer", and
the hint level recorded against the attempt stops meaning anything — which
matters, because that level feeds the readiness score.

The code detector is deliberately trigger-happy. A false positive costs a
slightly worse hint; a false negative costs the whole exercise.

### Code capture

Off by default. Three decisions, and they are genuinely different:

| Decision | Sent to OpenAI | Stored locally |
|---|---|---|
| not set | no | no |
| `once` | yes, this request | **no** |
| `always` | yes | yes, 90 days |
| `never` | no | no — and existing snippets are deleted |

**Withdrawing deletes.** Not "stops collecting" — a real `DELETE`. `attempt_code`
is a separate table precisely so every snippet can go without touching a single
piece of practice evidence.

The disclosure text is hashed, and the hash is stored with the decision. Edit the
wording materially and every existing consent is invalidated, because agreement
to weaker wording is not agreement to this wording. `disclosure_hash()` reads the
constant at call time rather than binding it as a default argument — binding it
would have frozen the hash at import and made the whole mechanism inert.

### Diagnosis works without consent

Deliberately. Requiring code to get any help at all would make the consent
meaningless — you would be choosing between privacy and the product. Without
code the diagnosis is thinner, `degraded: true` says so, and what it can still
do is name a *repeating* blocker, which is often the more useful observation
anyway.

### The complexity estimate is a heuristic

`mechanism/complexity.py` counts loop nesting, spots sorts and halving searches,
and notices recursion. It does not understand the code. It returns a confidence,
never claims better than "medium", and displays as "looks like O(n^2)" rather
than a verdict. Its job is to recover the `approach_complexity` signal the 1-2
click questionnaire has no room to ask for (spec §3.5) — and to flag the one case
worth flagging: an accepted solution that would not have survived a bigger input.

It is never used to score the user.
