/** Small presentational primitives shared across pages. */

import type { ReactNode } from "react";

export function Card({
  title,
  description,
  children,
}: {
  title?: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-line bg-surface p-6 shadow-sm">
      {title && <h2 className="text-lg font-semibold text-ink">{title}</h2>}
      {description && <p className="mt-1 text-sm text-ink-subtle">{description}</p>}
      <div className={title ? "mt-4" : undefined}>{children}</div>
    </section>
  );
}

export function Field({
  label,
  htmlFor,
  error,
  hint,
  children,
}: {
  label: string;
  htmlFor: string;
  error?: string;
  hint?: string;
  children: ReactNode;
}) {
  const hintId = hint ? `${htmlFor}-hint` : undefined;
  const errorId = error ? `${htmlFor}-error` : undefined;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={htmlFor} className="text-sm font-medium text-ink-muted">
        {label}
      </label>
      {hint && (
        <p id={hintId} className="text-xs text-ink-faint">
          {hint}
        </p>
      )}
      {children}
      {error && (
        <p id={errorId} role="alert" className="text-xs font-medium text-danger-ink">
          {error}
        </p>
      )}
    </div>
  );
}

export const inputClass =
  "rounded-md border border-line-strong bg-inset px-3 py-2 text-sm text-ink " +
  "focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30 " +
  "disabled:bg-canvas disabled:text-ink-faint";

export function Button({
  children,
  type = "button",
  variant = "primary",
  disabled,
  onClick,
}: {
  children: ReactNode;
  type?: "button" | "submit";
  variant?: "primary" | "secondary";
  disabled?: boolean;
  onClick?: () => void;
}) {
  const styles =
    variant === "primary"
      ? "bg-accent text-on-accent hover:bg-accent-hover"
      : "border border-line-strong bg-surface text-ink-muted hover:bg-inset";
  return (
    <button
      type={type}
      disabled={disabled}
      onClick={onClick}
      className={`rounded-md px-4 py-2 text-sm font-medium transition focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50 ${styles}`}
    >
      {children}
    </button>
  );
}

export function Banner({
  tone,
  children,
}: {
  tone: "info" | "warning" | "error" | "success";
  children: ReactNode;
}) {
  const tones = {
    info: "border-info-line bg-info-bg text-info-ink",
    warning: "border-warn-line bg-warn-bg text-warn-ink",
    error: "border-danger-line bg-danger-bg text-danger-ink",
    success: "border-success-line bg-success-bg text-success-ink",
  } as const;
  return (
    <div role="status" className={`rounded-md border px-4 py-3 text-sm ${tones[tone]}`}>
      {children}
    </div>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <p role="status" className="py-8 text-center text-sm text-ink-subtle">
      {label}
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-dashed border-line-strong px-4 py-8 text-center text-sm text-ink-subtle">
      {children}
    </p>
  );
}
