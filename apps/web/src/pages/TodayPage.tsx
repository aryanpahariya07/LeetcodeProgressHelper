import { Link } from "react-router-dom";

import { Banner, Button, Card, Empty, Loading } from "../components/ui";
import { DIFFICULTY_CLASSES, formatMinutes, ratingBand } from "../lib/format";
import { useAskCoach, useMe, usePlacement, useToday } from "../lib/queries";
import type { Placement, PlanItem } from "../lib/types";

export function TodayPage() {
  const me = useMe();
  const today = useToday();
  const placement = usePlacement();

  if (me.isLoading || today.isLoading) return <Loading label="Loading your plan…" />;

  if (me.isError && me.error.status === 409) {
    return (
      <Card title="Welcome">
        <p className="text-sm text-slate-600">
          You haven&apos;t set up a plan yet.
        </p>
        <Link
          to="/onboarding"
          className="mt-4 inline-block rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
        >
          Get started
        </Link>
      </Card>
    );
  }

  if (today.isError) return <Banner tone="error">{today.error.message}</Banner>;

  const data = today.data;
  if (!data?.plan) return <Empty>No plan yet.</Empty>;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-slate-900">Today</h1>
        <p className="mt-1 text-sm text-slate-600">
          {data.items.length} problems · {formatMinutes(data.total_target_minutes)}
        </p>
      </header>

      {placement.data && !placement.data.complete && (
        <PlacementBanner placement={placement.data} />
      )}

      {data.is_provisional && placement.data?.complete && (
        <Banner tone="info">
          <strong>This plan is provisional.</strong> Build a new block to switch to one
          assembled from your recorded attempts.
        </Banner>
      )}

      {data.items.length === 0 ? (
        <Empty>Nothing outstanding in this block. Nice work.</Empty>
      ) : (
        <ol className="space-y-3">
          {data.items.map((item) => (
            <ProblemRow key={item.id} item={item} />
          ))}
        </ol>
      )}

      <CoachCard />

      <Card title="Logging attempts">
        <p className="text-sm text-slate-600">
          The browser extension records attempts automatically. Logging by hand stays
          available permanently as the fallback.
        </p>
        <Link
          to="/log"
          className="mt-4 inline-block rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50"
        >
          Log an attempt
        </Link>
      </Card>
    </div>
  );
}

function ProblemRow({ item }: { item: PlanItem }) {
  const problem = item.problem;
  if (!problem) return null;

  return (
    <li className="flex items-center justify-between gap-4 rounded-lg border border-slate-200 bg-white p-4">
      <div className="min-w-0">
        <a
          href={problem.url}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-slate-900 underline-offset-2 hover:underline"
        >
          {problem.title}
        </a>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
          <span className={`rounded px-2 py-0.5 font-medium ${DIFFICULTY_CLASSES[problem.difficulty]}`}>
            {problem.difficulty}
          </span>
          <span>rating {ratingBand(problem.rating, problem.rating_rd)}</span>
          <span>· {formatMinutes(item.target_minutes)}</span>
        </div>
      </div>
    </li>
  );
}

/**
 * Placement is the first practice block, not a gate (spec §9). The wording has
 * to make that obvious — nothing here is blocked on finishing.
 */
function PlacementBanner({ placement }: { placement: Placement }) {
  const done = placement.attempts;
  const total = placement.max_attempts;
  const pct = Math.min(100, Math.round((done / total) * 100));

  return (
    <div className="rounded-md border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-900">
      <p>
        <strong>Working out where you stand.</strong> These problems are picked to tell the
        system the most about you, not to target a weakness it hasn&apos;t found yet.
      </p>
      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-sky-200">
        <div className="h-full rounded-full bg-sky-600" style={{ width: `${pct}%` }} />
      </div>
      <p className="mt-1 text-xs text-sky-800">
        {done} of up to {total} problems · {placement.reason}
      </p>
    </div>
  );
}

/**
 * Asking the coach for the next block.
 *
 * The result deliberately says which runtime produced it and whether the coach
 * was used at all. A plan built without the coach is a normal outcome, not a
 * hidden failure (invariant 4), and clamped advice says what was adjusted rather
 * than quietly presenting itself as what was asked for.
 */
function CoachCard() {
  const ask = useAskCoach();
  const result = ask.data;

  return (
    <Card
      title="Ask the coach"
      description="The coach prescribes the shape of the next block. Deterministic code picks the problems."
    >
      <Button onClick={() => ask.mutate()} disabled={ask.isPending}>
        {ask.isPending ? "Thinking…" : "Prescribe my next block"}
      </Button>

      {ask.isError && <div className="mt-4"><Banner tone="error">{ask.error.message}</Banner></div>}

      {result && (
        <div className="mt-4 space-y-3">
          {result.used_fallback && (
            <Banner tone="warning">
              Built without the coach — it was unavailable. Everything else works as
              normal.
            </Banner>
          )}

          {result.diagnosis && (
            <p className="text-sm text-slate-800">
              <strong>Diagnosis.</strong> {result.diagnosis}
            </p>
          )}

          <p className="text-sm text-slate-700">{result.message}</p>

          {result.violations.length > 0 && (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2">
              <p className="text-xs font-medium text-amber-900">
                Adjusted before applying:
              </p>
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-amber-900">
                {result.violations.map((v) => (
                  <li key={`${v.kind}-${v.detail}`}>{v.detail}</li>
                ))}
              </ul>
            </div>
          )}

          <p className="text-xs text-slate-500">
            {result.runtime}
            {result.model ? ` · ${result.model}` : ""}
            {result.validation ? ` · ${result.validation}` : ""}
          </p>
        </div>
      )}
    </Card>
  );
}
