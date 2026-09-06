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
    triggers.py      relevance and material-change detection

  services/        database glue around the mechanism layer
    readiness.py     applies evidence to both models; logs predictions
    retention.py     review schedule persistence
    scheduling.py    builds and persists the next block
    triggers.py      three-attempt batching with durable snapshots
    pipeline.py      what runs after an attempt is recorded

apps/web/src/
  lib/             API client, schemas, formatting, queries — all business logic
  components/      Presentational primitives
  pages/           Today, Onboarding, Progress, Log attempt
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

## What Phase 1 deliberately does not do

- **No extension.** Attempts are logged by hand. Phase 2 automates capture; manual
  logging then stays as the permanent fallback.
- **No placement.** The first plan is still the static curriculum lookup. Phase 3
  turns placement into the first practice block.
- **No AI.** The coach arrives in Phase 4. Until then a material change is recorded
  as `pending_agent` and the deterministic scheduler carries on regardless — which
  is the behaviour invariant 4 requires permanently, not a stopgap.
- **No real authentication.** Single local user, resolved server-side through
  `get_current_user()`. Pairing and revocable device tokens are Phase 2 (spec §5).

## Known limitation: prerequisite gating at cold start

With no evidence, nothing is calibrated, so almost every non-root pattern is locked
and the block assembler has very little to draw on. On a fresh database a first block
can come back with one or two problems.

This follows spec §6.4 as written — a prerequisite must be *demonstrated*, and
"unknown" is not demonstrated — but it makes the scheduler weakest exactly when a new
user needs it most. It is flagged for a decision rather than silently patched
(`CLAUDE.md`, "When the spec is wrong"). The likely fix is to distinguish *refuted*
prerequisites (calibrated and below threshold → lock) from *unknown* ones
(uncalibrated → allow, but deprioritise), which preserves the rule that a guess never
counts as a demonstration.
