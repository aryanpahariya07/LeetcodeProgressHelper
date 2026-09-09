import { NavLink, Route, Routes } from "react-router-dom";

import { useHealth } from "./lib/queries";
import { LogAttemptPage } from "./pages/LogAttemptPage";
import { OnboardingPage } from "./pages/OnboardingPage";
import { ProgressPage } from "./pages/ProgressPage";
import { SettingsPage } from "./pages/SettingsPage";
import { TodayPage } from "./pages/TodayPage";

export default function App() {
  return (
    <div className="min-h-screen bg-canvas text-ink">
      <Header />
      <main className="mx-auto max-w-4xl px-4 py-8">
        <Routes>
          <Route path="/" element={<TodayPage />} />
          <Route path="/onboarding" element={<OnboardingPage />} />
          <Route path="/progress" element={<ProgressPage />} />
          <Route path="/log" element={<LogAttemptPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<p className="text-sm text-ink-subtle">Not found.</p>} />
        </Routes>
      </main>
    </div>
  );
}

function Header() {
  const health = useHealth();

  return (
    <header className="border-b border-line bg-surface">
      <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-4 px-4 py-4">
        <div className="flex items-center gap-6">
          <span className="font-semibold text-ink">DSA Coach</span>
          <nav className="flex gap-4 text-sm">
            <NavItem to="/">Today</NavItem>
            <NavItem to="/progress">Progress</NavItem>
            <NavItem to="/log">Log attempt</NavItem>
            <NavItem to="/settings">Settings</NavItem>
          </nav>
        </div>
        <ApiStatus
          state={health.isLoading ? "loading" : health.isError ? "down" : "up"}
          problems={health.data?.catalogue_problems}
        />
      </div>
    </header>
  );
}

function NavItem({ to, children }: { to: string; children: string }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        isActive ? "font-medium text-ink" : "text-ink-faint hover:text-ink"
      }
    >
      {children}
    </NavLink>
  );
}

/** Phase 0's connectivity check: the shell must show whether the API is reachable. */
function ApiStatus({
  state,
  problems,
}: {
  state: "loading" | "up" | "down";
  problems?: number;
}) {
  const config = {
    loading: { dot: "bg-ink-faint", text: "Checking API…" },
    up: { dot: "bg-positive", text: `API connected · ${problems ?? 0} problems` },
    down: { dot: "bg-negative", text: "API unreachable" },
  } as const;
  const { dot, text } = config[state];

  return (
    <span className="flex items-center gap-2 text-xs text-ink-subtle" role="status">
      <span className={`h-2 w-2 rounded-full ${dot}`} aria-hidden />
      {text}
    </span>
  );
}
