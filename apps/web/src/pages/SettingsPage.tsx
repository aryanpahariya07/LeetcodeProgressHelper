import { useState } from "react";

import { Banner, Button, Card, Empty, Loading } from "../components/ui";
import { formatDateTime } from "../lib/format";
import {
  useConsent,
  useCreatePairingCode,
  useDevices,
  useRevokeConsent,
  useSetConsent,
} from "../lib/queries";
import { useRevokeDevice } from "../lib/queries";
import type { DeviceInfo, PairingCode } from "../lib/types";

export function SettingsPage() {
  const devices = useDevices();
  const createCode = useCreatePairingCode();
  const revoke = useRevokeDevice();
  const [code, setCode] = useState<PairingCode | null>(null);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-ink">Settings</h1>
        <p className="mt-1 text-sm text-ink-subtle">
          Connect the browser extension and manage what has access to your data.
        </p>
      </header>

      <Card
        title="Connect the browser extension"
        description="The extension records LeetCode attempts automatically so you don't have to log them by hand."
      >
        <ol className="mb-4 list-decimal space-y-1 pl-5 text-sm text-ink-subtle">
          <li>Install the extension and open it from the toolbar.</li>
          <li>Generate a code below.</li>
          <li>Type it into the extension and press Connect.</li>
        </ol>

        {code ? (
          <div className="rounded-md border border-line bg-inset p-4">
            <p className="font-mono text-2xl tracking-widest text-ink">{code.code}</p>
            <p className="mt-2 text-xs text-ink-faint">
              Single use, and expires in {Math.round(code.expires_in_seconds / 60)} minutes.
              Generating another code invalidates this one.
            </p>
          </div>
        ) : (
          <Button
            onClick={() => createCode.mutate(undefined, { onSuccess: setCode })}
            disabled={createCode.isPending}
          >
            {createCode.isPending ? "Generating…" : "Generate pairing code"}
          </Button>
        )}

        {createCode.isError && <Banner tone="error">{createCode.error.message}</Banner>}
      </Card>

      <Card
        title="Connected devices"
        description="Revoking takes effect immediately — the device's next request is refused."
      >
        {devices.isLoading && <Loading label="Loading devices…" />}
        {devices.isError && <Banner tone="error">{devices.error.message}</Banner>}
        {devices.data?.length === 0 && <Empty>No devices connected yet.</Empty>}

        {devices.data && devices.data.length > 0 && (
          <ul className="divide-y divide-line">
            {devices.data.map((device) => (
              <DeviceRow
                key={device.id}
                device={device}
                onRevoke={() => revoke.mutate(device.id)}
                revoking={revoke.isPending}
              />
            ))}
          </ul>
        )}
      </Card>

      <CodeCaptureCard />

      <Card title="What the extension can and cannot do">
        <ul className="list-disc space-y-1 pl-5 text-sm text-ink-subtle">
          <li>It runs only on LeetCode problem pages — nowhere else on the web.</li>
          <li>
            Its credential can <strong>send attempts and read its own settings</strong>. It
            cannot read your plan, your progress, or your other devices.
          </li>
          <li>Code capture is off by default and controlled above.</li>
          <li>Pausing it in the extension stops recording without disconnecting.</li>
        </ul>
      </Card>
    </div>
  );
}

function DeviceRow({
  device,
  onRevoke,
  revoking,
}: {
  device: DeviceInfo;
  onRevoke: () => void;
  revoking: boolean;
}) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-3 py-3">
      <div className="min-w-0">
        <p className="font-medium text-ink">
          {device.name}
          {!device.active && (
            <span className="ml-2 rounded bg-inset px-2 py-0.5 text-xs font-medium text-ink-subtle">
              revoked
            </span>
          )}
        </p>
        <p className="text-xs text-ink-faint">
          Added {formatDateTime(device.created_at)}
          {device.last_seen_at && ` · last seen ${formatDateTime(device.last_seen_at)}`}
        </p>
        <p className="mt-0.5 text-xs text-ink-faint">{device.scopes.join(", ") || "no scopes"}</p>
      </div>
      {device.active && (
        <Button variant="secondary" onClick={onRevoke} disabled={revoking}>
          Revoke
        </Button>
      )}
    </li>
  );
}

/**
 * Code-capture consent (spec §8).
 *
 * Off by default, and the disclosure is shown verbatim from the server rather
 * than paraphrased here — the exact wording is what the decision is recorded
 * against, and a friendlier summary in the UI would mean the user agreed to
 * something other than what was stored.
 */
function CodeCaptureCard() {
  const consent = useConsent();
  const setConsent = useSetConsent();
  const revoke = useRevokeConsent();
  const state = consent.data;

  return (
    <Card
      title="Code capture"
      description="Needed for failure diagnosis and solution review. Off unless you turn it on."
    >
      {consent.isLoading && <Loading label="Loading…" />}

      {state && (
        <>
          <p className="rounded-md border border-line bg-inset p-3 text-sm text-ink-subtle">
            {state.disclosure}
          </p>

          <p className="mt-3 text-sm text-ink-subtle">
            Current setting:{" "}
            <strong className="text-ink-muted">
              {state.decision === "always"
                ? "Always allowed"
                : state.decision === "never"
                  ? "Not allowed"
                  : state.decision === "once"
                    ? "Allowed once"
                    : "Not set"}
            </strong>
            {state.stored_snippets > 0 && ` · ${state.stored_snippets} snippet(s) stored`}
          </p>

          <div className="mt-4 flex flex-wrap gap-2">
            <Button onClick={() => setConsent.mutate("once")} disabled={setConsent.isPending}>
              Allow once
            </Button>
            <Button
              variant="secondary"
              onClick={() => setConsent.mutate("always")}
              disabled={setConsent.isPending}
            >
              Always allow
            </Button>
            <Button
              variant="secondary"
              onClick={() => revoke.mutate()}
              disabled={revoke.isPending}
            >
              Turn off and delete
            </Button>
          </div>

          {(setConsent.data || revoke.data) && (
            <div className="mt-3">
              <Banner tone="success">
                {(revoke.data ?? setConsent.data)!.message}
              </Banner>
            </div>
          )}
        </>
      )}
    </Card>
  );
}
