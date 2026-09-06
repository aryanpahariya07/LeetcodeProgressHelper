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
    <section className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      {title && <h2 className="text-lg font-semibold text-slate-900">{title}</h2>}
      {description && <p className="mt-1 text-sm text-slate-600">{description}</p>}
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
      <label htmlFor={htmlFor} className="text-sm font-medium text-slate-800">
        {label}
      </label>
      {hint && (
        <p id={hintId} className="text-xs text-slate-500">
          {hint}
        </p>
      )}
      {children}
      {error && (
        <p id={errorId} role="alert" className="text-xs font-medium text-rose-700">
          {error}
        </p>
      )}
    </div>
  );
}

export const inputClass =
  "rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 " +
  "focus:border-slate-900 focus:outline-none focus:ring-2 focus:ring-slate-900/20 " +
  "disabled:bg-slate-100";

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
      ? "bg-slate-900 text-white hover:bg-slate-700"
      : "border border-slate-300 bg-white text-slate-800 hover:bg-slate-50";
  return (
    <button
      type={type}
      disabled={disabled}
      onClick={onClick}
      className={`rounded-md px-4 py-2 text-sm font-medium transition focus:outline-none focus:ring-2 focus:ring-slate-900/30 disabled:cursor-not-allowed disabled:opacity-50 ${styles}`}
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
    info: "border-sky-200 bg-sky-50 text-sky-900",
    warning: "border-amber-200 bg-amber-50 text-amber-900",
    error: "border-rose-200 bg-rose-50 text-rose-900",
    success: "border-emerald-200 bg-emerald-50 text-emerald-900",
  } as const;
  return (
    <div role="status" className={`rounded-md border px-4 py-3 text-sm ${tones[tone]}`}>
      {children}
    </div>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <p role="status" className="py-8 text-center text-sm text-slate-500">
      {label}
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-dashed border-slate-300 px-4 py-8 text-center text-sm text-slate-500">
      {children}
    </p>
  );
}
