import { Banner, Card, Empty, Loading } from "../components/ui";
import { BAND_LABELS, BAND_STYLES, formatDateTime } from "../lib/format";
import { useReadiness, useRetention, useTriggers, useUnlocks } from "../lib/queries";
import type { PatternReadiness } from "../lib/types";

export function ProgressPage() {
  const readiness = useReadiness();
  const retention = useRetention();
  const unlocks = useUnlocks();
  const triggers = useTriggers();

  if (readiness.isLoading) return <Loading label="Working out where you stand…" />;
  if (readiness.isError) return <Banner tone="error">{readiness.error.message}</Banner>;

  const report = readiness.data;
  if (!report) return <Empty>No readiness data yet.</Empty>;

  const practised = report.patterns.filter((p) => p.evidence_count > 0);
  const untouched = report.patterns.filter((p) => p.evidence_count === 0);
  const locked = unlocks.data?.filter((u) => !u.unlocked) ?? [];

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-ink">Progress</h1>
        <p className="mt-1 text-sm text-ink-subtle">
          {report.calibrated_count} of {report.total_count} patterns have enough evidence
          to show a band.
        </p>
      </header>

      <Banner tone="info">{report.disclaimer}</Banner>

      <Card
        title="Readiness by pattern"
        description="Patterns are what transfer to an unseen problem. Topics are not."
      >
        {practised.length === 0 ? (
          <Empty>
            Nothing practised yet. Log a few attempts and estimates will appear here.
          </Empty>
        ) : (
          <ul className="divide-y divide-line">
            {practised
              .slice()
              .sort((a, b) => a.estimate - b.estimate)
              .map((pattern) => (
                <PatternRow key={pattern.pattern_id} pattern={pattern} />
              ))}
          </ul>
        )}

        {untouched.length > 0 && (
          <p className="mt-4 text-xs text-ink-faint">
            {untouched.length} patterns have no evidence yet and are not shown.
          </p>
        )}
      </Card>

      <div className="grid gap-6 sm:grid-cols-2">
        <Card
          title="Retention"
          description="Tracked separately from skill — a re-solve never inflates readiness."
        >
          {retention.data ? (
            <dl className="grid grid-cols-3 gap-4 text-center">
              <Stat label="Tracked" value={retention.data.tracked} />
              <Stat label="Due now" value={retention.data.due} />
              <Stat label="Lapses" value={retention.data.lapses} />
            </dl>
          ) : (
            <p className="text-sm text-ink-subtle">Loading…</p>
          )}
        </Card>

        <Card
          title="Locked patterns"
          description="Prerequisites must be demonstrated, not assumed."
        >
          {locked.length === 0 ? (
            <Empty>Nothing is locked.</Empty>
          ) : (
            <ul className="space-y-2 text-sm">
              {locked.slice(0, 6).map((entry) => (
                <li key={entry.slug}>
                  <span className="font-medium text-ink-muted">{entry.slug}</span>
                  <span className="text-ink-faint"> — needs {entry.blocked_by.join(", ")}</span>
                </li>
              ))}
              {locked.length > 6 && (
                <li className="text-xs text-ink-faint">and {locked.length - 6} more</li>
              )}
            </ul>
          )}
        </Card>
      </div>

      <Card
        title="Plan reviews"
        description="Every third relevant attempt triggers a review — including the ones that change nothing."
      >
        {!triggers.data || triggers.data.length === 0 ? (
          <Empty>No reviews yet. Three relevant attempts triggers the first.</Empty>
        ) : (
          <ul className="divide-y divide-line">
            {triggers.data.map((batch) => (
              <li key={batch.id} className="py-3">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span
                    className={`rounded px-2 py-0.5 text-xs font-medium ${
                      batch.material
                        ? "bg-warn-bg text-warn-ink"
                        : "bg-inset text-ink-subtle"
                    }`}
                  >
                    {batch.material ? "Change detected" : "No change"}
                  </span>
                  <span className="text-xs text-ink-faint">
                    {formatDateTime(batch.evaluated_at)}
                  </span>
                </div>
                <p className="mt-1 text-sm text-ink-subtle">{batch.explanation}</p>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function PatternRow({ pattern }: { pattern: PatternReadiness }) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-3 py-3">
      <div className="min-w-0">
        <p className="font-medium text-ink">{pattern.name}</p>
        <p className="text-xs text-ink-faint">
          {pattern.evidence_count} {pattern.evidence_count === 1 ? "observation" : "observations"}
        </p>
      </div>
      <span
        className={`rounded-full px-3 py-1 text-xs font-medium ${BAND_STYLES[pattern.band]}`}
      >
        {BAND_LABELS[pattern.band]}
      </span>
    </li>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt className="text-xs text-ink-faint">{label}</dt>
      <dd className="text-2xl font-semibold text-ink">{value}</dd>
    </div>
  );
}
