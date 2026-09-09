import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";

import { Banner, Button, Card, Empty, Field, inputClass } from "../components/ui";
import { BLOCKER_LABELS, RESOLUTION_LABELS, confidenceNote, formatDateTime } from "../lib/format";
import { useAttempts, useLogAttempt } from "../lib/queries";
import { attemptSchema, type AttemptFormValues } from "../lib/schemas";

const DEFAULTS: AttemptFormValues = {
  problem_slug: "",
  resolution: "independent",
  blocker: "",
  submission_outcome: "accepted",
  language: "python",
  active_minutes: undefined,
  confidence_cold_redo: undefined,
  is_resolve: false,
  notes: "",
};

export function LogAttemptPage() {
  const log = useLogAttempt();
  const attempts = useAttempts();

  const {
    register,
    handleSubmit,
    reset,
    watch,
    formState: { errors },
  } = useForm<AttemptFormValues>({
    resolver: zodResolver(attemptSchema),
    defaultValues: DEFAULTS,
  });

  const resolution = watch("resolution");
  const solvedIndependently = resolution === "independent";

  const onSubmit = handleSubmit((values) => {
    log.mutate(values, { onSuccess: () => reset(DEFAULTS) });
  });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-ink">Log an attempt</h1>
        <p className="mt-1 text-sm text-ink-subtle">
          The extension will capture most of this automatically from Phase 2. Only the
          questions telemetry cannot answer will remain.
        </p>
      </header>

      <Card>
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <Field
            label="Problem"
            htmlFor="problem_slug"
            hint="Slug or a pasted LeetCode URL."
            error={errors.problem_slug?.message}
          >
            <input
              id="problem_slug"
              placeholder="two-sum"
              className={inputClass}
              {...register("problem_slug")}
            />
          </Field>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="How did it go?" htmlFor="resolution" error={errors.resolution?.message}>
              <select id="resolution" className={inputClass} {...register("resolution")}>
                {Object.entries(RESOLUTION_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </Field>

            <Field
              label="Main blocker"
              htmlFor="blocker"
              hint={solvedIndependently ? "Not applicable — you solved it yourself." : undefined}
              error={errors.blocker?.message}
            >
              <select
                id="blocker"
                className={inputClass}
                disabled={solvedIndependently}
                {...register("blocker")}
              >
                <option value="">None</option>
                {Object.entries(BLOCKER_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </Field>

            <Field label="Judge result" htmlFor="submission_outcome">
              <select id="submission_outcome" className={inputClass} {...register("submission_outcome")}>
                <option value="accepted">Accepted</option>
                <option value="wrong_answer">Wrong answer</option>
                <option value="tle">Time limit exceeded</option>
                <option value="runtime_error">Runtime error</option>
                <option value="compile_error">Compile error</option>
                <option value="unknown">Not recorded</option>
              </select>
            </Field>

            <Field label="Language" htmlFor="language">
              <input id="language" className={inputClass} {...register("language")} />
            </Field>

            <Field
              label="Time spent (minutes)"
              htmlFor="active_minutes"
              hint="Leave blank if unsure — blank is recorded as unknown, not guessed."
              error={errors.active_minutes?.message}
            >
              <input
                id="active_minutes"
                type="number"
                min={0}
                className={inputClass}
                {...register("active_minutes")}
              />
            </Field>

            <Field
              label="Could you redo this cold in 3 days? (1–5)"
              htmlFor="confidence_cold_redo"
              hint="Optional. Stored as a secondary signal only."
              error={errors.confidence_cold_redo?.message}
            >
              <input
                id="confidence_cold_redo"
                type="number"
                min={1}
                max={5}
                className={inputClass}
                {...register("confidence_cold_redo")}
              />
            </Field>
          </div>

          <label className="flex items-center gap-2 text-sm text-ink-subtle">
            <input type="checkbox" {...register("is_resolve")} />
            This was a re-solve of a problem I&apos;d done before
          </label>

          <Field label="Notes" htmlFor="notes">
            <textarea id="notes" rows={2} className={inputClass} {...register("notes")} />
          </Field>

          {log.isError && <Banner tone="error">{log.error.message}</Banner>}
          {log.isSuccess && <ResultBanner result={log.data} />}

          <Button type="submit" disabled={log.isPending}>
            {log.isPending ? "Saving…" : "Log attempt"}
          </Button>
        </form>
      </Card>

      <Card title="Recent attempts">
        {attempts.isLoading && <p className="text-sm text-ink-subtle">Loading…</p>}
        {attempts.data?.length === 0 && <Empty>Nothing logged yet.</Empty>}
        {attempts.data && attempts.data.length > 0 && (
          <ul className="divide-y divide-line">
            {attempts.data.map((attempt) => {
              const note = confidenceNote(attempt.capture_confidence);
              return (
                <li key={attempt.id} className="py-3 text-sm">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="font-medium text-ink">{attempt.problem.title}</span>
                    <span className="text-xs text-ink-faint">
                      {formatDateTime(attempt.submitted_at)}
                    </span>
                  </div>
                  <p className="mt-1 text-ink-subtle">
                    {RESOLUTION_LABELS[attempt.resolution]}
                    {attempt.blocker && ` · ${BLOCKER_LABELS[attempt.blocker]}`}
                    {` · via ${attempt.source}`}
                  </p>
                  {note && <p className="mt-0.5 text-xs text-warn-ink">{note}</p>}
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </div>
  );
}

function ResultBanner({ result }: { result: { status: string; error: string | null } }) {
  if (result.status === "accepted") return <Banner tone="success">Attempt recorded.</Banner>;
  if (result.status === "duplicate")
    return <Banner tone="info">Already recorded — nothing changed.</Banner>;
  return <Banner tone="warning">{result.error ?? "Could not record this attempt."}</Banner>;
}
