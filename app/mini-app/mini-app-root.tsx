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
  const [activeTab, setActiveTab] = useState<TabId>("dashboard");

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
      case "problems": return <ProblemsView api={api} />;
      case "commitments": return <CommitmentsView api={api} onOpenProblem={() => setActiveTab("problems")} />;
      case "statistics": return <StatisticsView summary={session.auth.dashboard_summary} />;
      case "reports": return <ReportsView api={api} />;
      case "employees": return <EmployeesView api={api} />;
      case "connections": return <ConnectionManager api={api} connections={session.bootstrap.connections} />;
      case "groups": return <GroupsView api={api} />;
      case "settings": return <SettingsView api={api} />;
      case "more": return <MoreView onNavigate={setActiveTab} />;
      default: return <DashboardView summary={session.auth.dashboard_summary} bootstrap={session.bootstrap} onOpenProblems={() => setActiveTab("problems")} />;
    }
  })();

  return <MiniAppShell auth={session.auth} active={activeTab} onNavigate={setActiveTab}>{content}</MiniAppShell>;
}
