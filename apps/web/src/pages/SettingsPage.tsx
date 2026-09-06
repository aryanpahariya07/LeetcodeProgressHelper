import { useState } from "react";

import { Banner, Button, Card, Empty, Loading } from "../components/ui";
import { formatDateTime } from "../lib/format";
import { useCreatePairingCode, useDevices, useRevokeDevice } from "../lib/queries";
import type { DeviceInfo, PairingCode } from "../lib/types";

export function SettingsPage() {
  const devices = useDevices();
  const createCode = useCreatePairingCode();
  const revoke = useRevokeDevice();
  const [code, setCode] = useState<PairingCode | null>(null);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-slate-900">Settings</h1>
        <p className="mt-1 text-sm text-slate-600">
          Connect the browser extension and manage what has access to your data.
        </p>
      </header>

      <Card
        title="Connect the browser extension"
        description="The extension records LeetCode attempts automatically so you don't have to log them by hand."
      >
        <ol className="mb-4 list-decimal space-y-1 pl-5 text-sm text-slate-600">
          <li>Install the extension and open it from the toolbar.</li>
          <li>Generate a code below.</li>
          <li>Type it into the extension and press Connect.</li>
        </ol>

        {code ? (
          <div className="rounded-md border border-slate-200 bg-slate-50 p-4">
            <p className="font-mono text-2xl tracking-widest text-slate-900">{code.code}</p>
            <p className="mt-2 text-xs text-slate-500">
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
          <ul className="divide-y divide-slate-200">
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

      <Card title="What the extension can and cannot do">
        <ul className="list-disc space-y-1 pl-5 text-sm text-slate-600">
          <li>It runs only on LeetCode problem pages — nowhere else on the web.</li>
          <li>
            Its credential can <strong>send attempts and read its own settings</strong>. It
            cannot read your plan, your progress, or your other devices.
          </li>
          <li>Code capture is off and is not part of this phase.</li>
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
        <p className="font-medium text-slate-900">
          {device.name}
          {!device.active && (
            <span className="ml-2 rounded bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
              revoked
            </span>
          )}
        </p>
        <p className="text-xs text-slate-500">
          Added {formatDateTime(device.created_at)}
          {device.last_seen_at && ` · last seen ${formatDateTime(device.last_seen_at)}`}
        </p>
        <p className="mt-0.5 text-xs text-slate-400">{device.scopes.join(", ") || "no scopes"}</p>
      </div>
      {device.active && (
        <Button variant="secondary" onClick={onRevoke} disabled={revoking}>
          Revoke
        </Button>
      )}
    </li>
  );
}
