"use client";

import { useState } from "react";

import { ErrorState, Skeleton } from "./components/ui";
import { ConnectionManager } from "./features/connections/connection-manager";
import { DashboardView } from "./features/dashboard/dashboard-view";
import { OnboardingFlow } from "./features/onboarding/onboarding-flow";
import { ProblemsView } from "./features/problems/problems-view";
import { CommitmentsView, EmployeesView, GroupsView, MoreView, ReportsView, SettingsView } from "./features/sections/section-views";
import { StatisticsView } from "./features/statistics/statistics-view";
import { useMiniAppSession } from "./hooks/use-mini-app-session";
import { MiniAppShell } from "./layout/app-shell";
import type { TabId } from "./types";

export function MiniAppRoot() {
  const { launchState, session, api, error, refresh, advanceOnboarding } = useMiniAppSession();
  const launchTarget = typeof window !== "undefined" ? miniAppLaunchTarget() : {};
  const requestedSection = launchTarget.section;
  const requestedProblemId = launchTarget.problemId;
  const [activeTab, setActiveTab] = useState<TabId>(
    requestedSection === "problems"
      ? "problems"
      : requestedSection === "reports"
        ? "reports"
        : "dashboard",
  );
  const [dashboardProblemId, setDashboardProblemId] = useState<string | undefined>();
  const [history, setHistory] = useState<TabId[]>([]);
  const primary = new Set<TabId>(["dashboard", "problems", "statistics", "employees", "more"]);
  function navigate(tab: TabId) {
    if (tab !== activeTab) setHistory((current) => [...current, activeTab].slice(-8));
    setActiveTab(tab);
  }
  function goBack() {
    setHistory((current) => {
      const previous = current.at(-1) ?? "more";
      setActiveTab(previous);
      return current.slice(0, -1);
    });
  }

  if (launchState === "outside_telegram") {
    return <main className="state-shell"><section className="state-card"><p className="eyebrow">VENTRIX MINI APP</p><h1>Откройте Ventrix через вашего Telegram-бота</h1><p>Эта панель безопасно определяет ваш проект по подписанным данным Telegram. Откройте клиентского бота и нажмите «Ventrix AI».</p><div className="launch-hint"><span>1</span><p>Откройте бот вашего проекта</p><span>2</span><p>Нажмите «Ventrix AI»</p></div></section></main>;
  }
  if (launchState === "denied") {
    return <main className="state-shell"><section className="state-card"><p className="eyebrow">ДОСТУП НЕ ПОДТВЕРЖДЁН</p><h1>У вас нет доступа к этому проекту</h1><p>Откройте Ventrix из клиентского бота, в tenant которого вы добавлены.</p></section></main>;
  }
  if (launchState === "error") return <ErrorState message={error} retry={() => void refresh()} />;
  if (!session || !api) {
    return <main className="state-shell"><section className="state-card"><p className="eyebrow">VENTRIX</p><h1>{launchState === "authenticating" ? "Проверяем доступ к проекту…" : "Открываем Mini App…"}</h1><Skeleton lines={3} /></section></main>;
  }
  if (!session.bootstrap.onboarding.completed) {
    return <OnboardingFlow api={api} bootstrap={session.bootstrap} onAdvance={advanceOnboarding} />;
  }

  const content = (() => {
    switch (activeTab) {
      case "problems": return <ProblemsView api={api} initialProblemId={dashboardProblemId ?? requestedProblemId} />;
      case "commitments": return <CommitmentsView api={api} onOpenProblem={() => navigate("problems")} />;
      case "statistics": return <StatisticsView api={api} summary={session.auth.dashboard_summary} />;
      case "reports": return <ReportsView api={api} />;
      case "employees": return <EmployeesView api={api} canManage={session.auth.permissions.includes("*") || session.auth.permissions.includes("employees.manage")} onOpenGroups={() => navigate("groups")} />;
      case "connections": return <ConnectionManager api={api} connections={session.bootstrap.connections} />;
      case "groups": return <GroupsView api={api} />;
      case "settings": return <SettingsView api={api} />;
      case "more": return <MoreView onNavigate={navigate} canManageProject={session.auth.permissions.includes("*") || session.auth.permissions.includes("settings.manage")} canReadAllReports={session.auth.permissions.includes("*") || session.auth.permissions.includes("reports.read")} />;
      default: return <DashboardView api={api} auth={session.auth} summary={session.auth.dashboard_summary} bootstrap={session.bootstrap} onOpenProblems={() => { setDashboardProblemId(undefined); navigate("problems"); }} onOpenProblem={(problemId) => { setDashboardProblemId(problemId); navigate("problems"); }} />;
    }
  })();

  return <MiniAppShell auth={session.auth} access={session.access} active={activeTab} onNavigate={navigate} canGoBack={!primary.has(activeTab) && history.length > 0} onBack={goBack}>{content}</MiniAppShell>;
}

function miniAppLaunchTarget(): { section?: string; problemId?: string } {
  const query = new URLSearchParams(window.location.search);
  const telegramStartParameter = new URLSearchParams(
    window.Telegram?.WebApp?.initData ?? "",
  ).get("start_param") ?? query.get("tgWebAppStartParam");
  const explicitSection = query.get("section") ?? undefined;
  const explicitProblemId = query.get("problem_id") ?? undefined;
  if (explicitSection || explicitProblemId) {
    return { section: explicitSection, problemId: explicitProblemId };
  }
  if (telegramStartParameter?.startsWith("problem_")) {
    return {
      section: "problems",
      problemId: telegramStartParameter.slice("problem_".length),
    };
  }
  if (telegramStartParameter?.startsWith("report_")) return { section: "reports" };
  return {};
}
