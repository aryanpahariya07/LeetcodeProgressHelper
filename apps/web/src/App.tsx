import { NavLink, Route, Routes } from "react-router-dom";

import { useHealth } from "./lib/queries";
import { LogAttemptPage } from "./pages/LogAttemptPage";
import { OnboardingPage } from "./pages/OnboardingPage";
import { ProgressPage } from "./pages/ProgressPage";
import { TodayPage } from "./pages/TodayPage";

export default function App() {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <Header />
      <main className="mx-auto max-w-4xl px-4 py-8">
        <Routes>
          <Route path="/" element={<TodayPage />} />
          <Route path="/onboarding" element={<OnboardingPage />} />
          <Route path="/progress" element={<ProgressPage />} />
          <Route path="/log" element={<LogAttemptPage />} />
          <Route path="*" element={<p className="text-sm text-slate-600">Not found.</p>} />
        </Routes>
      </main>
    </div>
  );
}

function Header() {
  const health = useHealth();

  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-4 px-4 py-4">
        <div className="flex items-center gap-6">
          <span className="font-semibold">DSA Coach</span>
          <nav className="flex gap-4 text-sm">
            <NavItem to="/">Today</NavItem>
            <NavItem to="/progress">Progress</NavItem>
            <NavItem to="/log">Log attempt</NavItem>
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
        isActive ? "font-medium text-slate-900" : "text-slate-500 hover:text-slate-900"
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
    loading: { dot: "bg-slate-300", text: "Checking API…" },
    up: { dot: "bg-emerald-500", text: `API connected · ${problems ?? 0} problems` },
    down: { dot: "bg-rose-500", text: "API unreachable" },
  } as const;
  const { dot, text } = config[state];

  return (
    <span className="flex items-center gap-2 text-xs text-slate-600" role="status">
      <span className={`h-2 w-2 rounded-full ${dot}`} aria-hidden />
      {text}
    </span>
  );
}
