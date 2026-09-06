import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { useNavigate } from "react-router-dom";

import { Banner, Button, Card, Field, inputClass } from "../components/ui";
import { useOnboard } from "../lib/queries";
import { onboardingSchema, type OnboardingFormValues } from "../lib/schemas";

export function OnboardingPage() {
  const navigate = useNavigate();
  const onboard = useOnboard();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<OnboardingFormValues>({
    resolver: zodResolver(onboardingSchema),
    defaultValues: {
      display_name: "",
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      preferred_language: "python",
      target_companies: "",
      target_date: "",
      days_per_week: 5,
      minutes_per_day: 60,
      self_assessed_level: "beginner",
      approx_problems_solved: 0,
    },
  });

  const onSubmit = handleSubmit((values) => {
    onboard.mutate(values, { onSuccess: () => navigate("/") });
  });

  return (
    <div className="mx-auto max-w-2xl">
      <h1 className="text-2xl font-semibold text-slate-900">Set up your plan</h1>
      <p className="mt-2 text-sm text-slate-600">
        Two minutes. You get a plan straight away — it starts out provisional and sharpens
        as evidence arrives.
      </p>

      <form onSubmit={onSubmit} className="mt-6 space-y-6" noValidate>
        <Card title="About you">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Display name" htmlFor="display_name" error={errors.display_name?.message}>
              <input id="display_name" className={inputClass} {...register("display_name")} />
            </Field>
            <Field label="Preferred language" htmlFor="preferred_language">
              <select id="preferred_language" className={inputClass} {...register("preferred_language")}>
                <option value="python">Python</option>
                <option value="java">Java</option>
                <option value="cpp">C++</option>
                <option value="javascript">JavaScript</option>
                <option value="typescript">TypeScript</option>
                <option value="go">Go</option>
              </select>
            </Field>
            <Field
              label="Current level"
              htmlFor="self_assessed_level"
              hint="A starting point only — placement will correct it."
              error={errors.self_assessed_level?.message}
            >
              <select id="self_assessed_level" className={inputClass} {...register("self_assessed_level")}>
                <option value="beginner">Beginner</option>
                <option value="intermediate">Intermediate</option>
                <option value="advanced">Advanced</option>
              </select>
            </Field>
            <Field
              label="Problems solved so far"
              htmlFor="approx_problems_solved"
              error={errors.approx_problems_solved?.message}
            >
              <input
                id="approx_problems_solved"
                type="number"
                min={0}
                className={inputClass}
                {...register("approx_problems_solved")}
              />
            </Field>
          </div>
        </Card>

        <Card title="Your goal">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Target companies"
              htmlFor="target_companies"
              hint="Comma separated. Affects emphasis and format, not difficulty targets."
            >
              <input id="target_companies" className={inputClass} {...register("target_companies")} />
            </Field>
            <Field label="Target date" htmlFor="target_date" hint="Optional.">
              <input id="target_date" type="date" className={inputClass} {...register("target_date")} />
            </Field>
            <Field
              label="Days per week"
              htmlFor="days_per_week"
              error={errors.days_per_week?.message}
            >
              <input
                id="days_per_week"
                type="number"
                min={1}
                max={7}
                className={inputClass}
                {...register("days_per_week")}
              />
            </Field>
            <Field
              label="Minutes per day"
              htmlFor="minutes_per_day"
              hint="Plans are filled to about 85% of this."
              error={errors.minutes_per_day?.message}
            >
              <input
                id="minutes_per_day"
                type="number"
                min={10}
                max={600}
                className={inputClass}
                {...register("minutes_per_day")}
              />
            </Field>
          </div>
        </Card>

        {onboard.isError && <Banner tone="error">{onboard.error.message}</Banner>}

        <Button type="submit" disabled={onboard.isPending}>
          {onboard.isPending ? "Creating your plan…" : "Create my plan"}
        </Button>
      </form>
    </div>
  );
}
