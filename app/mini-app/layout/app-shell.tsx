"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { PropsWithChildren } from "react";

import { Icon } from "../components/icons";
import { IconButton, ProfileButton, SegmentedControl, StatusBadge } from "../components/ui";
import { applyTheme, getThemeMode, setThemeMode, subscribeToTheme } from "../theme/theme";
import type { ThemeMode } from "../theme/theme";
import type { MiniAppAuth, ProjectAccess, TabId } from "../types";
import { allSections, primaryTabs } from "./navigation";

export function MiniAppShell({ auth, access, active, onNavigate, canGoBack, onBack, children }: PropsWithChildren<{
  auth: MiniAppAuth;
  access: ProjectAccess;
  active: TabId;
  onNavigate: (tab: TabId) => void;
  canGoBack?: boolean;
  onBack?: () => void;
}>) {
  const userName = [auth.user.first_name, auth.user.last_name].filter(Boolean).join(" ") || "Пользователь";
  const initials = userName.slice(0, 2).toUpperCase();
  const [profileOpen, setProfileOpen] = useState(false);
  const [theme, setTheme] = useState<ThemeMode>(() => typeof window === "undefined" ? "telegram" : getThemeMode());
  const profileRef = useRef<HTMLElement>(null);
  const profileOpener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    applyTheme();
    const unsubscribe = subscribeToTheme((mode) => setTheme(mode));
    const telegramRefresh = () => {
      if (getThemeMode() === "telegram") applyTheme("telegram");
    };
    window.addEventListener("ventrix:telegram-theme-change", telegramRefresh);
    return () => {
      unsubscribe();
      window.removeEventListener("ventrix:telegram-theme-change", telegramRefresh);
    };
  }, []);
  useEffect(() => {
    if (!profileOpen) return;
    const handleKeydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setProfileOpen(false);
      if (event.key !== "Tab" || !profileRef.current) return;
      const focusable = Array.from(profileRef.current.querySelectorAll<HTMLElement>("button:not(:disabled),[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex]:not([tabindex='-1'])"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable.at(-1)!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeydown);
    profileRef.current?.querySelector<HTMLElement>("button")?.focus();
    return () => {
      document.removeEventListener("keydown", handleKeydown);
      profileOpener.current?.focus();
    };
  }, [profileOpen]);
  const accessState = useMemo(() => projectAccessState(access), [access]);
  const chooseTheme = (mode: ThemeMode) => {
    setThemeMode(mode);
    setTheme(mode);
  };
  const openSettings = () => {
    setProfileOpen(false);
    onNavigate("settings");
  };
  const openProfile = () => {
    profileOpener.current = document.activeElement as HTMLElement | null;
    setProfileOpen(true);
  };
  return <main className="mini-app-shell">
    <aside className="mini-sidebar">
      <div className="mini-brand"><span>V</span><strong>Ventrix</strong></div>
      <small>ПРОЕКТ</small><h2>{auth.tenant_name}</h2>
      <nav>{allSections.map((item) => <button key={item.id} className={active === item.id ? "active" : ""} onClick={() => onNavigate(item.id)}><Icon name={item.icon} />{item.label}</button>)}</nav>
      <button className="mini-user" onClick={openProfile}><span>{initials}</span><div><strong>{userName}</strong><small>{auth.user.username ? `@${auth.user.username}` : auth.user.role}</small></div></button>
    </aside>
    <section className="mini-content">
      <header className="mini-topbar">{canGoBack && <IconButton className="mini-back" label="Назад" onClick={onBack}><Icon name="back" /></IconButton>}<div><p className="eyebrow">{auth.tenant_name}</p><h1>{active === "more" ? "Ещё" : allSections.find((item) => item.id === active)?.label ?? "Ventrix"}</h1></div><ProfileButton initials={initials} label="Открыть профиль" aria-expanded={profileOpen} onClick={openProfile} /></header>
      <div className="mini-view screen-enter" key={active}>{children}</div>
    </section>
    <nav className="mini-bottom-nav" aria-label="Основная навигация">
      {primaryTabs.map((item) => <button key={item.id} className={active === item.id || (item.id === "more" && !primaryTabs.some((tab) => tab.id === active)) ? "active" : ""} onClick={() => onNavigate(item.id)}><Icon name={item.icon} /><span>{item.label}</span></button>)}
    </nav>
    {profileOpen && <div className="profile-overlay" role="presentation" onMouseDown={(event) => event.currentTarget === event.target && setProfileOpen(false)}><section className="profile-sheet" role="dialog" aria-modal="true" aria-labelledby="profile-title" ref={profileRef}><header><div className="profile-identity"><span>{initials}</span><div><h2 id="profile-title">{userName}</h2><p>{auth.user.username ? `@${auth.user.username}` : "Username не указан"}</p></div></div><IconButton label="Закрыть профиль" onClick={() => setProfileOpen(false)}><Icon name="close" /></IconButton></header><div className={`access-card ${accessState.tone}`}><StatusBadge tone={accessState.tone}>{accessState.label}</StatusBadge><strong>{accessState.title}</strong><p>{accessState.description}</p></div><div className="profile-facts"><span><small>Роль в проекте</small><strong>{roleLabel(auth.user.role)}</strong></span><span><small>Мониторинг</small><strong>{access.analysis_enabled ? "Работает" : "Приостановлен"}</strong></span></div><div className="profile-theme"><h3>Тема</h3><SegmentedControl label="Тема интерфейса" value={theme} onChange={chooseTheme} options={[{ value: "light", label: "Светлая" }, { value: "dark", label: "Тёмная" }, { value: "telegram", label: "Telegram" }]} /></div><button className="profile-settings" onClick={openSettings}><Icon name="settings" /><span><strong>Настройки</strong><small>Расписание, уведомления и чувствительность</small></span></button></section></div>}
  </main>;
}

function projectAccessState(access: ProjectAccess): { tone: "success" | "warning" | "danger" | "neutral"; label: string; title: string; description: string } {
  if (!access.expires_at) return { tone: "neutral", label: "Статус Ventrix", title: "Срок доступа не указан", description: "Данные о сроке активности пока недоступны." };
  const end = new Date(access.expires_at.length === 10 ? `${access.expires_at}T00:00:00` : access.expires_at);
  const today = new Date();
  const remaining = Math.ceil((end.getTime() - new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()) / 86_400_000);
  const formatted = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" }).format(end);
  if (access.status === "suspended") return { tone: "danger", label: "Требует продления", title: "Мониторинг приостановлен", description: `Текущий срок доступа — до ${formatted}.` };
  if (remaining < 0 || access.status === "expired") return { tone: "danger", label: "Требует продления", title: "Мониторинг остановлен", description: `Доступ завершился ${formatted}.` };
  if (remaining < 7) return { tone: "warning", label: "Скоро завершится", title: `Ventrix активен до ${formatted}`, description: `Осталось ${remaining} ${dayWord(remaining)}.` };
  return { tone: "success", label: "Ventrix активен", title: `До ${formatted}`, description: `Осталось ${remaining} ${dayWord(remaining)}.` };
}

function dayWord(value: number) {
  const mod100 = value % 100;
  const mod10 = value % 10;
  if (mod100 >= 11 && mod100 <= 14) return "дней";
  if (mod10 === 1) return "день";
  if (mod10 >= 2 && mod10 <= 4) return "дня";
  return "дней";
}

function roleLabel(role: string) {
  if (role === "owner") return "Владелец";
  if (role === "manager") return "Руководитель";
  if (role === "employee") return "Сотрудник";
  if (role === "observer") return "Наблюдатель";
  return role;
}
