# DSA Coach

An adaptive DSA interview-preparation system for a single user.

A Chrome extension monitors LeetCode activity automatically; a 1–2 click
post-submission questionnaire captures only what telemetry cannot determine;
deterministic code computes readiness and schedules practice; an AI coach diagnoses
failures, prescribes the shape of the next practice block, and teaches.

It is a preparation coach, not a predictor. It will never tell you that you will
pass an interview.

## Documentation

| Document | For |
|---|---|
| [`docs/features.md`](docs/features.md) | What it does, in plain language |
| [`docs/spec.md`](docs/spec.md) | The design — data model, algorithms, phases |
| [`docs/development.md`](docs/development.md) | Setup and commands |
| [`CLAUDE.md`](CLAUDE.md) | Operating rules and invariants |

## Status: Phase 4 complete

| Phase | | |
|---|---|---|
| **0** | Usable skeleton — API, schema, catalogue, provisional plan, manual logging | **Done** |
| **1** | Deterministic mechanism — readiness, prerequisites, retention, block assembly | **Done** |
| **2** | Chrome extension — automatic capture, the 1-click questionnaire | **Built, not verified** |
| **3** | Adaptive placement — informative first block, provisional to evidence-based | **Done** |
| **4** | AI judgment layer — prescriptions, validation, clamping, fallback | **Done** |
| 5 | Teaching — hints, diagnosis, review, mock interviews | Next |
| 6 | Profile reconciliation and hardening | |

> **Phase 2 caveat.** The extension is built, builds cleanly, and its logic is
> tested — but its LeetCode DOM selectors have **never been run against a live
> problem page**. Until someone loads it in Chrome and solves a problem, assume
> automatic capture does not work. Manual logging is unaffected and remains the
> reliable path. See [docs/development.md](docs/development.md#verifying-the-extension).

Usable today: onboard, log attempts by hand, and get a real scheduled block built
from your recorded evidence — readiness per pattern, prerequisite gating, spaced
re-solves, and a plan review every three relevant attempts. All deterministic;
there is no AI in the system yet, and by design there never needs to be for the
scheduler to work.

## Quick start

```bash
cd apps/api && uv sync && uv run alembic upgrade head && uv run dsa-coach seed
uv run uvicorn dsa_coach.main:app --reload

# second terminal
cd apps/web && npm install && npm run dev
```

Then open <http://localhost:5173>. Full instructions in
[`docs/development.md`](docs/development.md).

## Layout

```
apps/api/       FastAPI + SQLAlchemy + Alembic  (Python 3.12, managed by uv)
apps/web/       React + TypeScript + Vite + Tailwind
apps/extension/ WXT + Manifest V3 (Chrome)
docs/         Specification, features, development guide
```

## A note on the numbers

The 50 seeded problems carry **curator-estimated ratings, not measured ones**. They
are stored with a high rating deviation so the readiness model discounts them, and
the interface shows "approx." rather than a precise figure. Every catalogue import
records its source, licence, checksum and known limitations.

The same principle runs through the project: nothing is presented as more certain
than it is. Uncertain captures are marked uncertain, unknown values stay null rather
than being guessed, and readiness will ship as bands rather than percentages until a
calibration report earns the extra precision.
