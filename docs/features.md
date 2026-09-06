# DSA Coach — Feature List

Review document. Every feature has an ID — reference them when you want something
changed. Design detail lives in [`spec.md`](spec.md).

**The loop:** open problem → solve → extension records everything objective →
1 click says whether you solved it yourself → done. Every 3rd meaningful attempt, the
plan is re-checked.

| Area | Features | Mostly lands in |
|---|---|---|
| [Onboarding](#onboarding-onb) | 5 | Phase 0, 3 |
| [Capture](#capture-cap) | 12 | Phase 2 |
| [Scoring](#scoring-sco) | 8 | Phase 1 |
| [Scheduling](#scheduling-sch) | 6 | Phase 1 |
| [Coach](#coach-ai) | 12 | Phase 4, 5 |
| [Privacy & control](#privacy--control-priv) | 8 | Phase 2, 5 |
| [Outcome](#outcome-out) | 1 | Phase 6 |

---

## Onboarding (ONB)

| ID | Feature | Phase |
|---|---|---|
| ONB-1 | 2-minute form: goal, companies, target date, days/week, minutes/day, self-assessed level | 0 |
| ONB-2 | Plan appears immediately — static curriculum for your level, labelled *provisional* | 0 |
| ONB-3 | Placement test is disguised as your first practice block — no separate exam | 3 |
| ONB-4 | Patterns show **Calibrating** instead of a score until there's enough evidence | 3 |
| ONB-5 | Placement stops early once confident; hard cap 12 problems | 3 |

## Capture (CAP)

| ID | Feature | Phase |
|---|---|---|
| CAP-1 | Auto-detects problem, language, start/end time | 2 |
| CAP-2 | Active time = wall time − hidden tab − gaps over 5 min. **Thinking time counts.** | 2 |
| CAP-3 | Counts runs and submissions; records outcome (AC / WA / TLE / RE / CE) | 2 |
| CAP-4 | 1 click after submit: solved independently / after hint / after editorial / failed | 2 |
| CAP-5 | 2nd click asks the blocker — only if you didn't solve it independently | 2 |
| CAP-6 | Prompt is dismissable. Dismissal = weak evidence, not a failure | 2 |
| CAP-7 | Attempts editable for 7 days from the Activity page | 2 |
| CAP-8 | Re-prompts once if you open the editorial after a failed attempt | 2 |
| CAP-9 | Flags low confidence when unsure; leaves the field empty rather than guessing | 2 |
| CAP-10 | Visible degraded state when LeetCode's markup changes; falls back to asking you | 2 |
| CAP-11 | Manual attempt logging — permanent fallback, built first | 0 |
| CAP-12 | Public profile sync flags problems you solved but the extension missed | 6 |

## Scoring (SCO)

| ID | Feature | Phase |
|---|---|---|
| SCO-1 | Scored per **pattern** (sliding window, monotonic stack…), not per topic | 1 |
| SCO-2 | Shown as bands — Not ready / Developing / Approaching / Ready. No percentages | 1 |
| SCO-3 | Two scoring models run side by side; the simple one is primary | 1 |
| SCO-4 | Bake-off on real data in Phase 6; **losing model gets deleted** | 6 |
| SCO-5 | Retention scored separately from skill — a re-solve never inflates your rating | 1 |
| SCO-6 | Review intervals driven by how well you actually re-solved, not how confident you felt | 1 |
| SCO-7 | Hint or editorial use lowers the score for that attempt | 1 |
| SCO-8 | Shaky captures and profile-sync data move your score less than clean captures | 1 |

## Scheduling (SCH)

| ID | Feature | Phase |
|---|---|---|
| SCH-1 | Blocks fit your stated time budget; under-schedules rather than over-schedules | 1 |
| SCH-2 | Mix: ~60% weakness / 25% interleaved / 15% due re-solves | 1 |
| SCH-3 | Interleaved problems don't tell you what pattern they are | 1 |
| SCH-4 | Prerequisites gate what can be scheduled; max one labelled stretch problem | 1 |
| SCH-5 | Re-solves are from scratch — not "read your old solution" | 1 |
| SCH-6 | Fully deterministic and reproducible given the same inputs | 1 |

## Coach (AI)

| ID | Feature | Phase |
|---|---|---|
| AI-1 | Prescribes the **shape** of the next block; code picks the actual problems | 4 |
| AI-2 | Re-checks the plan every 3 meaningful attempts (re-solves mostly don't count) | 4 |
| AI-3 | Only runs when something material changed — prevents plan churn | 4 |
| AI-4 | **"No change" is shown explicitly**, with a reason. Not silence | 4 |
| AI-5 | Every plan change cites the specific attempts behind it | 4 |
| AI-6 | Every plan change is revertible | 4 |
| AI-7 | Prescriptions are validated against your time budget and prerequisites, then clamped | 4 |
| AI-8 | Hint ladder, 5 levels, no skipping. Levels 1–4 contain no code | 5 |
| AI-9 | Reads your failed code, says what broke, flags if it repeats a past mistake | 5 |
| AI-10 | Reviews *accepted* solutions: real vs. claimed complexity, edge cases, how to explain it | 5 |
| AI-11 | Timed mock interview — state approach first, scored on a rubric | 5 |
| AI-12 | **Scheduler works fully with the AI turned off.** Tested, not aspirational | 4 |

## Privacy & control (PRIV)

| ID | Feature | Phase |
|---|---|---|
| PRIV-1 | Code capture **off by default** | 5 |
| PRIV-2 | First diagnosis request asks: once / always / never — and states code goes to OpenAI | 5 |
| PRIV-3 | Stored code auto-deletes after 90 days; delete-all button in Settings | 5 |
| PRIV-4 | Pause or disconnect monitoring in one click | 2 |
| PRIV-5 | Extension is paired per device; revoke it and it dies immediately | 2 |
| PRIV-6 | Extension can only send attempts — can't read your plan, coach or settings | 2 |
| PRIV-7 | Full JSON export; full account deletion | 6 |
| PRIV-8 | Extension has permission for LeetCode problem pages only | 2 |

## Outcome (OUT)

| ID | Feature | Phase |
|---|---|---|
| OUT-1 | Chart: first-attempt success rate over time at fixed difficulty. **Flat line = product isn't working** | 6 |

---

## Calls worth arguing about

The opinionated decisions. Push back on any of these.

| # | Decision | The tension |
|---|---|---|
| 1 | **Thinking time counts** (CAP-2) | Standard idle-detection would subtract it. But staring at the screen deriving the recurrence is the valuable part, and it looks identical to idleness. |
| 2 | **Questionnaire exists at all** (CAP-4) | It's the one thing you must do manually. Nothing observable distinguishes "solved it" from "solved it after the editorial" — and those imply opposite plans. |
| 3 | **Bands, not percentages** (SCO-2) | Less satisfying to look at. A percentage would imply precision the model hasn't earned yet. |
| 4 | **Simple model is primary** (SCO-3/4) | The sophisticated model may not beat a 20-line baseline at realistic sample sizes. Both run; data decides; loser is deleted. |
| 5 | **25% interleaved** (SCH-2/3) | Feels like wasted reps. Practising one pattern in a row teaches you to solve it *when you already know which pattern it is* — not the interview condition. |
| 6 | **Material-change gate** (AI-3) | You asked for re-checks every 3 attempts. 3 attempts rarely justifies a change, and constant reshuffling destroys trust. Gate + explicit "no change" resolves it. |
| 7 | **AI can't pick problems** (AI-1) | Looks like a limitation. It's what makes hallucinated / already-solved / too-hard recommendations structurally impossible. |
| 8 | **Extension is Phase 2, not Phase 0** | It's the core product but the riskiest build. Manual logging in Phase 0 means a slip never blocks you from practising. |

## Explicitly not doing

- Predicting or guaranteeing interview outcomes
- Company-specific difficulty numbers (no documented source exists — varies by role, level, location, stage)
- Showing a precise probability before calibration is proven
- Guessing at unobservable data — missing stays missing
- Copying problem statements (metadata and links only)
- Anything requiring CAPTCHA bypass, anti-bot evasion, or scraping behind access controls

## Usable from when?

| Phase | You get | Usable? |
|---|---|---|
| 0 | Onboarding, provisional plan, Today, manual logging | **Yes** — practise and log by hand |
| 1 | Real scoring, prerequisites, review scheduling, real blocks | **Yes** — properly scheduled, zero AI |
| 2 | The extension — automatic capture, 1-click prompt | **Yes** — the product as intended |
| 3 | Placement, calibrated plans | Sharper |
| 4 | The coach — evidence-cited prescriptions | Adaptive |
| 5 | Hints, diagnosis, review, mock interviews | Teaching |
| 6 | Profile sync, calibration, hardening | Polished |
