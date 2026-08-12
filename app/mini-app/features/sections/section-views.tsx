"use client";

import { useCallback, useState } from "react";

import type { VentrixClientApi } from "../../api/client";
import { Card, EmptyState, SectionHeading, Skeleton, StatusBadge } from "../../components/ui";
import { ConnectionManager } from "../connections/connection-manager";
import { useResource } from "../../hooks/use-resource";
import type { ClientSettings, Employee, ReportDetail, TabId } from "../../types";

export function ReportsView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.reports(), [api]);
  const { data, loading, error } = useResource(loader);
  const [detail, setDetail] = useState<ReportDetail | null>(null);
  if (detail) return <><SectionHeading eyebrow="ОТЧЁТ" title={detail.summary || "Отчёт Ventrix"} /><button className="text-action" onClick={() => setDetail(null)}>← К списку</button><div className="statistics-grid">{Object.entries(detail.metrics).map(([key, value]) => <Card key={key}><small>{key}</small><h3>{value}</h3></Card>)}</div><div className="section-list">{detail.sections.map((section) => <Card key={section.key}><h3>{section.key}</h3><pre className="report-data">{JSON.stringify(section.data, null, 2)}</pre></Card>)}</div></>;
  return <><SectionHeading eyebrow="ОТЧЁТЫ" title="История аналитики" description="Компания, сотрудники, клиенты, решённые и открытые проблемы." />{loading ? <Skeleton lines={4} /> : data?.length ? <div className="section-list">{data.map((item) => <button className="section-button" key={item.id} onClick={() => void api.report(item.id).then(setDetail)}><Card><StatusBadge tone={item.status === "ready" ? "success" : "neutral"}>{item.status}</StatusBadge><h3>{item.summary || "Отчёт Ventrix"}</h3><p>{item.period_start ? `${new Date(item.period_start).toLocaleDateString("ru-RU")} — ${new Date(item.period_end ?? item.period_start).toLocaleDateString("ru-RU")}` : "Период формируется"}</p></Card></button>)}</div> : <EmptyState title="Отчётов пока нет" description="Первый consolidated-отчёт появится после анализа всех аккаунтов." />}{error && <p className="form-error">{error}</p>}</>;
}

function EmployeeEditor({ api, employee, done }: { api: VentrixClientApi; employee?: Employee; done: (created?: Pick<Employee, "id" | "name">) => void }) {
  const [username, setUsername] = useState(employee?.telegram_username ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    setBusy(true); setError("");
    const normalizedUsername = username.trim().replace(/^@+/, "");
    try {
      if (employee) {
        await api.updateEmployee(employee.id, { telegram_username: normalizedUsername });
        done();
      } else {
        const created = await api.createEmployee({
          display_name: normalizedUsername,
          telegram_username: normalizedUsername,
          role: "employee",
          notifications_enabled: true,
          criticality_threshold: 85,
        });
        done({ id: created.id, name: normalizedUsername });
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Не удалось сохранить"); } finally { setBusy(false); }
  }
  const validUsername = /^[A-Za-z0-9_]{3,64}$/.test(username.trim().replace(/^@+/, ""));
  return <Card className="control-form employee-editor"><h3>{employee ? "Изменить сотрудника" : "Новый сотрудник"}</h3><p>Укажите только Telegram username. Числовой Telegram ID Ventrix определит после первого входа сотрудника.</p><label>Telegram username<div className="username-input"><span>@</span><input autoCapitalize="none" autoCorrect="off" value={username.replace(/^@+/, "")} onChange={(event) => setUsername(event.target.value.replace(/[^A-Za-z0-9_@]/g, ""))} placeholder="username" /></div></label><div className="fixed-role"><small>Роль</small><strong>Сотрудник</strong><span>Доступ к данным других сотрудников не выдаётся.</span></div>{username && !validUsername && <p className="field-hint">Используйте латинские буквы, цифры и знак подчёркивания.</p>}{error && <p className="form-error">{error}</p>}<div className="form-actions"><button onClick={() => done()}>Отмена</button><button className="primary-action" disabled={busy || !validUsername} onClick={() => void save()}>{busy ? "Сохраняем…" : employee ? "Сохранить" : "Создать и подключить"}</button></div></Card>;
}

export function EmployeesView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.employees(), [api]);
  const { data, loading, error, reload } = useResource(loader);
  const [editing, setEditing] = useState<Employee | "new" | null>(null);
  const [connecting, setConnecting] = useState<Pick<Employee, "id" | "name"> | null>(null);
  const done = (created?: Pick<Employee, "id" | "name">) => { setEditing(null); if (created) setConnecting(created); void reload(); };
  if (connecting) return <><SectionHeading eyebrow="КОМАНДА" title={`Telegram @${connecting.name}`} description="Код придёт в официальный служебный чат Telegram подключаемого аккаунта." /><button className="text-action section-back" onClick={() => { setConnecting(null); void reload(); }}>← Вернуться к сотрудникам</button><ConnectionManager api={api} connections={[]} assignedEmployee={connecting} /></>;
  if (editing) return <><SectionHeading eyebrow="КОМАНДА" title="Доступ сотрудника" /><EmployeeEditor api={api} employee={editing === "new" ? undefined : editing} done={done} /></>;
  return <><SectionHeading eyebrow="КОМАНДА" title="Сотрудники и доступ" description="Добавьте @username и при необходимости подключите рабочую Telegram-сессию сотрудника." /><button className="primary-action section-action" onClick={() => setEditing("new")}>Добавить сотрудника</button>{loading ? <Skeleton lines={4} /> : data?.length ? <div className="section-list">{data.map((item) => <Card className="employee-card" key={item.id}><div className="person-row"><span>{item.name.slice(0, 2).toUpperCase()}</span><div><h3>{item.telegram_username ? `@${item.telegram_username}` : item.name}</h3><p>Роль: сотрудник · доступ {item.access_status ?? "не связан"}</p></div><StatusBadge tone={item.status === "active" ? "success" : "neutral"}>{item.status === "active" ? "активен" : "неактивен"}</StatusBadge></div><div className="employee-actions"><button onClick={() => setEditing(item)}>Изменить username</button><button className="primary-action" onClick={() => setConnecting({ id: item.id, name: item.telegram_username ?? item.name })}>Подключить Telegram</button></div></Card>)}</div> : <EmptyState title="Сотрудники ещё не добавлены" description="Добавьте Telegram username — числовой ID вводить не нужно." />}{error && <p className="form-error">{error}</p>}</>;
}

export function CommitmentsView({ api, onOpenProblem }: { api: VentrixClientApi; onOpenProblem: () => void }) {
  const loader = useCallback(() => api.commitments(), [api]);
  const { data, loading, error, reload } = useResource(loader);
  async function complete(id: string) { await api.updateCommitment(id, "completed", "Подтверждено пользователем в Mini App"); await reload(); }
  return <><SectionHeading eyebrow="ОБЯЗАТЕЛЬСТВА" title="Обещания и сроки" description="Открытые и просроченные действия с ответственным и связанной проблемой." />{loading ? <Skeleton /> : data?.length ? <div className="section-list">{data.map((item) => <Card key={item.id}><div className="problem-card-top"><StatusBadge tone={item.status === "open" ? "warning" : "success"}>{item.status}</StatusBadge><small>{item.deadline_at ? new Date(item.deadline_at).toLocaleString("ru-RU") : "без срока"}</small></div><h3>{item.expected_action}</h3><p>Уверенность {Math.round(item.confidence * 100)}%</p>{item.status === "open" && <button className="primary-action" onClick={() => void complete(item.id)}>Отметить выполненным</button>}{item.linked_problem_id && <button className="text-action" onClick={onOpenProblem}>Открыть связанную проблему</button>}</Card>)}</div> : <EmptyState title="Открытых обязательств нет" description="Обещания появятся после анализа коммуникаций." />}{error && <p className="form-error">{error}</p>}</>;
}

export function GroupsView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.groups(), [api]);
  const { data, loading, error, reload } = useResource(loader);
  async function toggle(id: string, enabled: boolean) { await api.updateGroup(id, { notifications_enabled: enabled }); await reload(); }
  return <><SectionHeading eyebrow="РАБОЧИЕ ГРУППЫ" title="Группы и напоминания" description="Разрешённые назначения, пороги и cooldown для групповых уведомлений." />{loading ? <Skeleton /> : data?.length ? <div className="section-list">{data.map((item) => <Card key={item.id}><h3>{item.title}</h3><p>{item.participants_count ?? 0} участников · порог {item.minimum_criticality}</p><label className="inline-check"><input type="checkbox" checked={item.notifications_enabled} onChange={(event) => void toggle(item.id, event.target.checked)} /> Уведомления включены</label></Card>)}</div> : <EmptyState title="Группы не подключены" description="Добавьте клиентского бота в рабочую группу и зарегистрируйте chat ID." />}{error && <p className="form-error">{error}</p>}</>;
}

function SettingsForm({ api, settings, saved }: { api: VentrixClientApi; settings: ClientSettings; saved: (value: ClientSettings) => void }) {
  const [value, setValue] = useState(settings);
  const [error, setError] = useState("");
  const patch = <K extends keyof ClientSettings>(key: K, next: ClientSettings[K]) => setValue((current) => ({ ...current, [key]: next }));
  async function save() { try { saved(await api.updateSettings({ timezone: value.timezone, daily_report_time: value.daily_report_time, analysis_enabled: value.analysis_enabled, history_window_days: value.history_window_days, manager_notification_threshold: value.manager_notification_threshold, employee_notification_threshold: value.employee_notification_threshold, group_notification_threshold: value.group_notification_threshold, notification_immediate_threshold: value.notification_immediate_threshold, employee_notifications_enabled: value.employee_notifications_enabled, group_reminders_enabled: value.group_reminders_enabled })); } catch (cause) { setError(cause instanceof Error ? cause.message : "Не удалось сохранить"); } }
  return <div className="section-list"><Card className="control-form"><h3>Расписание</h3><label>Часовой пояс<input value={value.timezone} onChange={(event) => patch("timezone", event.target.value)} /></label><label>Ежедневный отчёт<input type="time" value={value.daily_report_time.slice(0, 5)} onChange={(event) => patch("daily_report_time", event.target.value)} /></label><label>История<select value={value.history_window_days} onChange={(event) => patch("history_window_days", Number(event.target.value))}><option value="3">3 дня</option><option value="7">7 дней</option><option value="14">14 дней</option><option value="30">30 дней</option></select></label><label className="inline-check"><input type="checkbox" checked={value.analysis_enabled} onChange={(event) => patch("analysis_enabled", event.target.checked)} /> Регулярный анализ</label></Card><Card className="control-form"><h3>Уведомления</h3>{(["manager_notification_threshold", "employee_notification_threshold", "group_notification_threshold", "notification_immediate_threshold"] as const).map((key) => <label key={key}>{key.replaceAll("_", " ")}: {value[key]}<input type="range" min="0" max="100" value={value[key]} onChange={(event) => patch(key, Number(event.target.value))} /></label>)}<label className="inline-check"><input type="checkbox" checked={value.employee_notifications_enabled} onChange={(event) => patch("employee_notifications_enabled", event.target.checked)} /> Уведомления сотрудникам</label><label className="inline-check"><input type="checkbox" checked={value.group_reminders_enabled} onChange={(event) => patch("group_reminders_enabled", event.target.checked)} /> Напоминания в группах</label></Card>{error && <p className="form-error">{error}</p>}<button className="primary-action" onClick={() => void save()}>Сохранить настройки</button></div>;
}

export function SettingsView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.settings(), [api]);
  const { data, loading, error, reload } = useResource(loader);
  return <><SectionHeading eyebrow="НАСТРОЙКИ" title="Операционные настройки" description="Расписание, timezone и политика уведомлений. Секреты и model routing остаются у владельца платформы." />{loading ? <Skeleton /> : data ? <SettingsForm api={api} settings={data} saved={() => void reload()} /> : <EmptyState title="Настройки недоступны" description="Для этого раздела нужна роль владельца или менеджера." />}{error && <p className="form-error">{error}</p>}</>;
}

export function MoreView({ onNavigate }: { onNavigate: (tab: TabId) => void }) {
  const items: Array<[TabId, string, string]> = [["commitments", "Обязательства", "Обещания и дедлайны"], ["reports", "Отчёты", "Consolidated-сводки"], ["connections", "Telegram-аккаунты", "Сессии и папки анализа"], ["groups", "Рабочие группы", "Группы и напоминания"], ["settings", "Настройки", "Политика и расписание"]];
  return <><SectionHeading eyebrow="ЕЩЁ" title="Управление проектом" /><div className="more-grid">{items.map(([id, title, note]) => <button key={id} onClick={() => onNavigate(id)}><span>→</span><strong>{title}</strong><small>{note}</small></button>)}</div></>;
}
