# DSA Coach — Operating Rules

**Full specification: [docs/spec.md](docs/spec.md). Read the relevant sections before starting any milestone.**
This file holds the invariants only. It is loaded every session; keep it short.

## What this is

A single-user adaptive DSA interview-preparation system. A Chrome extension monitors
LeetCode activity automatically; a 1–2 click post-submission questionnaire captures
only what telemetry cannot determine; deterministic code computes readiness and
schedules practice; an AI coach diagnoses failures, prescribes the shape of the next
practice block, and teaches.

## Two different AIs — do not confuse them

- **Claude Code (you)** is the *development* agent. You write this project.
- **The Codex SDK** is the *runtime* coach inside the application.

Both are coding agents driven from a terminal, so the confusion is easy and costly.
The runtime coach is deliberately stripped of every agentic capability — read-only,
approves nothing, no tools, empty working directory — because here it is only an
inference endpoint that prescribes block *shape* (spec §2).

When the spec says "the agent," it means the runtime coach, not you.

## Invariants

Never violate these. If a task appears to require it, stop and say so.

1. **Three layers.** Evidence (facts) → Mechanism (deterministic) → Judgment (AI).
   Data flows upward. The Judgment layer never writes facts.
2. **No LLM call inside the Mechanism layer.** Ever. It is pure functions only.
3. **The AI prescribes block *shape*; deterministic code selects problem IDs.**
   The agent never emits a list of problems to schedule.
4. **The product must remain fully usable with the AI provider unavailable.**
   The deterministic scheduler can always produce a valid next block.
5. **Never fabricate.** Test results, ratings, problem metadata, submissions, timings,
   or user activity. If something was not verified, say it was not verified.
6. **Uncertain telemetry is recorded as uncertain.** If the extension cannot reliably
   determine a value, it emits `capture_confidence: low` or omits the field. It never
   guesses.
7. **All ingestion is idempotent**, deduplicated by client-generated `event_uuid`.
   All state-changing agent tool calls are idempotent.
8. **Every plan change is auditable, evidence-cited, and reversible.**
9. **Code capture is off by default.** It requires explicit consent, and the consent
   prompt must disclose that code is transmitted to the AI provider.
10. **Identity comes from server-side context** via `get_current_user()` — never from
    a client-supplied ID, never from a model-generated tool argument.
11. **Migrations for every schema change.** No silent schema drift.
12. **Readiness numbers are uncalibrated estimates.** Never present one as a
    probability in the UI until calibration evaluation exists (spec §6.2).

## Per-milestone workflow

Inspect repo → summarize state → state plan → name risks → implement → run
lint/type/test → report changed files, *actual* command output, and remaining
limitations.

## When the spec is wrong

The spec will turn out to be incorrect or impractical in places. That is expected.

**Do not silently diverge.** If implementation reveals that a requirement in
`docs/spec.md` is wrong, unworkable, or more costly than it is worth:

1. Stop before implementing the divergence.
2. Explain the specific problem, the options, and your recommendation.
3. Obtain approval.
4. **Update `docs/spec.md` in the same change** that implements the new approach, so
   spec and code never drift apart.

This applies to material divergences — a changed data model, a dropped requirement, a
different algorithm, a reordered phase. It does not apply to typos, wording, or
filling in details the spec left open.

If you notice code and spec have already diverged, say so rather than quietly
matching one to the other.

## Engineering rules

- Preserve existing work. No destructive Git commands.
- Business logic lives outside React components.
- All LeetCode specifics sit behind `LeetCodeProvider` (backend) and the DOM adapter
  (extension). Nothing else knows LeetCode exists.
- Tuning thresholds live in one config module with their rationale in comments. They
  are product hypotheses, not constants.
- Validate every AI output and tool argument before it reaches the database.
- Stop and ask before anything requiring bypass of access controls, CAPTCHAs,
  anti-bot measures, or platform terms.
- Never weaken an acceptance criterion silently. Document anything that still needs
  manual verification.

## Commands

**Populate this section during Phase 0 and keep it current.** Any change to tooling,
scripts, or task runners updates this section in the same commit. A stale command here
is a bug — it is the first thing read every session.

All verified working as of Phase 5. Toolchain: `uv` 0.11.8 (manages Python 3.12 —
no system Python required), Node 24 / npm 11.

```bash
# --- api (from apps/api) ---
uv sync                                          # install deps
uv run uvicorn dsa_coach.main:app --reload       # api + web    :8000
uv run pytest                                    # test         (423 tests)
uv run ruff check .                              # lint
uv run ruff format .                             # format
uv run mypy                                      # typecheck    (strict)
uv run alembic upgrade head                      # migrate
uv run alembic check                             # schema drift (invariant 11)
uv run alembic revision --autogenerate -m "..."  # new migration
uv run dsa-coach seed                            # seed catalogue (idempotent)
# serving the web app from :8000 needs `npm run build` in apps/web first
uv run dsa-coach import-catalogue --file F --rating-source S

# --- runtime coach ---
# The default runtime is Codex, which authenticates with a ChatGPT account.
# Once per machine; without it the coach falls back to the deterministic
# scheduler and the product still works (invariant 4).
codex login

# --- web (from apps/web) ---
npm install
npm run dev                                      # frontend dev :5173 (proxies to :8000)
npm run build                                    # -> dist/, served by the API
npm test                                         # test         (24 tests)
npm run lint
npm run typecheck

# --- extension (from apps/extension) ---
npm install
npm run dev                                      # load .output/chrome-mv3 unpacked
npm test                                         # test         (77 tests)
npm run lint
npm run typecheck
npm run build
```
