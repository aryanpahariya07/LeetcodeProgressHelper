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
  routers/         health, onboarding, plan, attempts, extension

apps/web/src/
  lib/             API client, schemas, formatting, queries — all business logic
  components/      Presentational primitives
  pages/           Today, Onboarding, Log attempt
```

Business logic lives in `lib/`, never inside a `.tsx` file. That's an engineering
rule in `CLAUDE.md`, and it's also why the web tests can cover the interesting
behaviour without rendering anything.

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

Phase 0 ships 50 problems whose **ratings are curator estimates, not measurements**.
They carry `rating_source = manual` and `rating_rd = 300` so the readiness model
(Phase 1) discounts them heavily, and the UI shows "approx." instead of a number.

`catalogue_sources` records the licence, snapshot date, checksum and known
limitations of every import. If a licensed contest-derived dataset is adopted later,
re-source the ratings through `import-catalogue --rating-source contest_derived`
rather than editing values in place.

## What Phase 0 deliberately does not do

- **No scheduler.** The onboarding plan is a static curriculum lookup. Readiness,
  prerequisites, retention and block assembly are Phase 1.
- **No extension.** Attempts are logged by hand. Phase 2 automates capture; manual
  logging then stays as the permanent fallback.
- **No AI.** The coach arrives in Phase 4, and the product must keep working without
  it.
- **No real authentication.** Single local user, resolved server-side through
  `get_current_user()`. Pairing and revocable device tokens are Phase 2 (spec §5).
