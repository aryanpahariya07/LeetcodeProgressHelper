/** TanStack Query bindings. Components consume these, never `fetch` directly. */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";

import { api, ApiError } from "./api";
import type { AttemptFormValues, OnboardingFormValues } from "./schemas";
import type { Attempt, Health, Plan, Today, User } from "./types";

export const keys = {
  health: ["health"] as const,
  me: ["me"] as const,
  today: ["today"] as const,
  plan: ["plan", "current"] as const,
  attempts: ["attempts"] as const,
};

export function useHealth(): UseQueryResult<Health, ApiError> {
  return useQuery({
    queryKey: keys.health,
    queryFn: api.health,
    refetchInterval: 30_000,
    retry: false,
  });
}

export function useMe(): UseQueryResult<User, ApiError> {
  return useQuery({
    queryKey: keys.me,
    queryFn: api.me,
    // 409 means "not onboarded yet", which is a normal state, not a failure.
    retry: false,
  });
}

export function useToday(): UseQueryResult<Today, ApiError> {
  return useQuery({ queryKey: keys.today, queryFn: api.today, retry: false });
}

export function usePlan(): UseQueryResult<Plan, ApiError> {
  return useQuery({ queryKey: keys.plan, queryFn: api.currentPlan, retry: false });
}

export function useAttempts(): UseQueryResult<Attempt[], ApiError> {
  return useQuery({ queryKey: keys.attempts, queryFn: () => api.attempts(), retry: false });
}

export function useOnboard() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (values: OnboardingFormValues) => api.onboard(values),
    onSuccess: () => {
      void client.invalidateQueries();
    },
  });
}

export function useLogAttempt() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (values: AttemptFormValues) => api.logAttempt(values),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.attempts });
      void client.invalidateQueries({ queryKey: keys.today });
    },
  });
}
