/** TanStack Query bindings. Components consume these, never `fetch` directly. */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";

import { api, ApiError } from "./api";
import type { AttemptFormValues, OnboardingFormValues } from "./schemas";
import type {
  Attempt,
  CoachRun,
  ConsentResult,
  ConsentState,
  DeviceInfo,
  Health,
  Placement,
  Plan,
  ReadinessReport,
  Retention,
  Teaching,
  Today,
  TriggerBatch,
  Unlock,
  User,
} from "./types";

export const keys = {
  health: ["health"] as const,
  me: ["me"] as const,
  today: ["today"] as const,
  plan: ["plan", "current"] as const,
  attempts: ["attempts"] as const,
  readiness: ["progress", "readiness"] as const,
  placement: ["progress", "placement"] as const,
  retention: ["progress", "retention"] as const,
  unlocks: ["progress", "unlocks"] as const,
  triggers: ["plan", "triggers"] as const,
  devices: ["devices"] as const,
  consent: ["consents", "code-capture"] as const,
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

export function useReadiness(): UseQueryResult<ReadinessReport, ApiError> {
  return useQuery({ queryKey: keys.readiness, queryFn: api.readiness, retry: false });
}

export function useRetention(): UseQueryResult<Retention, ApiError> {
  return useQuery({ queryKey: keys.retention, queryFn: api.retention, retry: false });
}

export function useUnlocks(): UseQueryResult<Unlock[], ApiError> {
  return useQuery({ queryKey: keys.unlocks, queryFn: api.unlocks, retry: false });
}

export function useTriggers(): UseQueryResult<TriggerBatch[], ApiError> {
  return useQuery({ queryKey: keys.triggers, queryFn: api.triggers, retry: false });
}

export function useBuildNextBlock() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.buildNextBlock,
    onSuccess: () => {
      void client.invalidateQueries();
    },
  });
}

export function useDevices(): UseQueryResult<DeviceInfo[], ApiError> {
  return useQuery({ queryKey: keys.devices, queryFn: api.devices, retry: false });
}

export function useCreatePairingCode() {
  return useMutation({ mutationFn: api.createPairingCode });
}

export function useRevokeDevice() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.revokeDevice(id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.devices });
    },
  });
}

export function usePlacement(): UseQueryResult<Placement, ApiError> {
  return useQuery({ queryKey: keys.placement, queryFn: api.placement, retry: false });
}

export function useAskCoach() {
  const client = useQueryClient();
  return useMutation<CoachRun, ApiError>({
    mutationFn: api.askCoach,
    onSuccess: () => {
      void client.invalidateQueries();
    },
  });
}

export function useConsent(): UseQueryResult<ConsentState, ApiError> {
  return useQuery({ queryKey: keys.consent, queryFn: api.consentState, retry: false });
}

export function useSetConsent() {
  const client = useQueryClient();
  return useMutation<ConsentResult, ApiError, "once" | "always" | "never">({
    mutationFn: (decision) => api.setConsent(decision),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.consent });
    },
  });
}

export function useRevokeConsent() {
  const client = useQueryClient();
  return useMutation<ConsentResult, ApiError>({
    mutationFn: api.revokeConsent,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.consent });
    },
  });
}

export function useHint() {
  return useMutation<Teaching, ApiError, string>({
    mutationFn: (problemSlug) => api.hint(problemSlug),
  });
}
