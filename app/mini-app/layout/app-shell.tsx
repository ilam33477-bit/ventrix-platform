import type { PropsWithChildren } from "react";

import type { MiniAppAuth, TabId } from "../types";
import { allSections, primaryTabs } from "./navigation";

export function MiniAppShell({ auth, active, onNavigate, canGoBack, onBack, children }: PropsWithChildren<{
  auth: MiniAppAuth;
  active: TabId;
  onNavigate: (tab: TabId) => void;
  canGoBack?: boolean;
  onBack?: () => void;
}>) {
  const userName = [auth.user.first_name, auth.user.last_name].filter(Boolean).join(" ") || "Пользователь";
  return <main className="mini-app-shell">
    <aside className="mini-sidebar">
      <div className="mini-brand"><span>V</span><strong>Ventrix</strong></div>
      <small>ПРОЕКТ</small><h2>{auth.tenant_name}</h2>
      <nav>{allSections.map((item) => <button key={item.id} className={active === item.id ? "active" : ""} onClick={() => onNavigate(item.id)}><span>{item.icon}</span>{item.label}</button>)}</nav>
      <div className="mini-user"><span>{userName.slice(0, 2).toUpperCase()}</span><div><strong>{userName}</strong><small>{auth.user.username ? `@${auth.user.username}` : auth.user.role}</small></div></div>
    </aside>
    <section className="mini-content">
      <header className="mini-topbar">{canGoBack && <button className="mini-back" aria-label="Назад" onClick={onBack}>←</button>}<div><p className="eyebrow">{auth.tenant_name}</p><h1>{allSections.find((item) => item.id === active)?.label ?? "Ventrix"}</h1></div><span className="mini-avatar">{userName.slice(0, 2).toUpperCase()}</span></header>
      <div className="mini-view">{children}</div>
    </section>
    <nav className="mini-bottom-nav" aria-label="Основная навигация">
      {primaryTabs.map((item) => <button key={item.id} className={active === item.id || (item.id === "more" && !primaryTabs.some((tab) => tab.id === active)) ? "active" : ""} onClick={() => onNavigate(item.id)}><span>{item.icon}</span>{item.label}</button>)}
    </nav>
  </main>;
}
