"use client";

import { useCallback } from "react";

import type { VentrixClientApi } from "../../api/client";
import type { TabId } from "../../types";
import { Card, EmptyState, SectionHeading, Skeleton, StatusBadge } from "../../components/ui";
import { useResource } from "../../hooks/use-resource";

export function ReportsView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.reports(), [api]);
  const { data, loading } = useResource(loader);
  return <><SectionHeading eyebrow="ОТЧЁТЫ" title="История аналитики" description="Готовые управленческие сводки по выбранным рабочим коммуникациям." />{loading ? <Skeleton lines={4} /> : data?.length ? <div className="section-list">{data.map((item) => <Card key={item.id}><StatusBadge tone={item.status === "ready" ? "success" : "neutral"}>{item.status}</StatusBadge><h3>{item.summary || "Отчёт Ventrix"}</h3></Card>)}</div> : <EmptyState title="Отчётов пока нет" description="Первый отчёт появится после завершения анализа." />}</>;
}

export function EmployeesView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.employees(), [api]);
  const { data, loading } = useResource(loader);
  return <><SectionHeading eyebrow="КОМАНДА" title="Сотрудники и рабочие аккаунты" description="Проверьте роли и привязку Telegram-аккаунтов сотрудников." />{loading ? <Skeleton lines={4} /> : data?.length ? <div className="section-list">{data.map((item) => <Card key={item.id} className="person-row"><span>{item.name.slice(0, 2).toUpperCase()}</span><div><h3>{item.name}</h3><p>{item.telegram_username ? `@${item.telegram_username}` : "Telegram не указан"}</p></div><StatusBadge tone={item.status === "active" ? "success" : "neutral"}>{item.role}</StatusBadge></Card>)}</div> : <EmptyState title="Сотрудники ещё не добавлены" description="Их можно добавить после подключения рабочего Telegram." />}</>;
}

export function GroupsView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.groups(), [api]);
  const { data, loading } = useResource(loader);
  return <><SectionHeading eyebrow="РАБОЧИЕ ГРУППЫ" title="Группы и напоминания" description="Telegram-группы проекта, в которых Ventrix может отслеживать обязательства." />{loading ? <Skeleton /> : data?.length ? <div className="section-list">{data.map((item) => <Card key={item.id}><h3>{item.title}</h3><p>{item.participants_count} участников</p><StatusBadge tone={item.status === "active" ? "success" : "neutral"}>{item.status}</StatusBadge></Card>)}</div> : <EmptyState title="Группы не подключены" description="Добавление групп доступно владельцу проекта из клиентского бота." />}</>;
}

export function SettingsView() {
  return <><SectionHeading eyebrow="НАСТРОЙКИ" title="Настройки клиента" description="Только понятные операционные параметры — технические AI-настройки остаются у владельца платформы." /><div className="section-list"><Card><h3>Уведомления</h3><p>Критичные события и готовность отчётов.</p></Card><Card><h3>Рабочее время</h3><p>Часовой пояс и время получения ежедневной сводки.</p></Card><Card><h3>Доступ команды</h3><p>Сотрудники и разрешения внутри этого проекта.</p></Card></div></>;
}

export function MoreView({ onNavigate }: { onNavigate: (tab: TabId) => void }) {
  const items: Array<[TabId, string, string]> = [["reports", "Отчёты", "Готовые аналитические сводки"], ["connections", "Telegram-аккаунты", "Сессии и папки анализа"], ["groups", "Рабочие группы", "Группы и напоминания"], ["settings", "Настройки", "Уведомления и доступ"]];
  return <><SectionHeading eyebrow="ЕЩЁ" title="Управление проектом" /><div className="more-grid">{items.map(([id, title, note]) => <button key={id} onClick={() => onNavigate(id)}><span>→</span><strong>{title}</strong><small>{note}</small></button>)}</div></>;
}
