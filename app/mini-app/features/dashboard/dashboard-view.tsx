"use client";

import { useCallback, useMemo } from "react";

import type { VentrixClientApi } from "../../api/client";
import { AnimatedNumber, Card, ChartContainer, EmptyState, Skeleton, StatusBadge } from "../../components/ui";
import { Icon } from "../../components/icons";
import { useResource } from "../../hooks/use-resource";
import { cleanExplanation, formatRelativeAge, priorityLabel, priorityTone, problemPerson, problemTitle, sortProblemsByPriority } from "../../lib/problem-presentation";
import type { Bootstrap, ClientSettings, DashboardSummary, MiniAppAuth, Problem } from "../../types";

type DashboardData = { problems: Problem[]; settings: ClientSettings };

export function DashboardView({ api, auth, summary, bootstrap, onOpenProblems, onOpenProblem }: {
  api: VentrixClientApi;
  auth: MiniAppAuth;
  summary: DashboardSummary;
  bootstrap: Bootstrap;
  onOpenProblems: () => void;
  onOpenProblem: (problemId: string) => void;
}) {
  const loader = useCallback(() => Promise.all([api.problems(), api.settings()]).then(([problems, settings]) => ({ problems, settings })), [api]);
  const { data, loading, error, reload } = useResource<DashboardData>(loader);
  const problems = data?.problems ?? bootstrap.problems;
  const attention = useMemo(() => sortProblemsByPriority(problems).slice(0, 3), [problems]);
  const leadProblem = attention[0];
  const waitingClients = problems.filter((problem) => problem.type === "client_without_answer").length;
  const activeConnections = bootstrap.connections.filter((connection) => ["connected", "ready", "syncing"].includes(connection.status));
  const personalDialogs = bootstrap.connections.reduce((total, item) => total + (item.personal_dialogs ?? 0), 0);
  const messagesToday = bootstrap.connections.reduce((total, item) => total + (item.messages_today ?? 0), 0);
  const contactsToday = bootstrap.connections.reduce((total, item) => total + (item.new_contacts_today ?? 0), 0);
  const activityMax = Math.max(messagesToday, contactsToday, 1);
  const firstName = auth.user.first_name || auth.user.username || "руководитель";
  const monitoringActive = activeConnections.length > 0;

  return <div className="dashboard-command">
    <header className="dashboard-greeting">
      <div><p className="eyebrow">РАБОЧАЯ СВОДКА</p><h2>Добрый день, {firstName}</h2><p>{monitoringActive ? "Ventrix следит за рабочими диалогами и выделяет только ситуации, где нужна реакция." : "Подключите рабочий Telegram, чтобы Ventrix начал мониторинг диалогов."}</p></div>
      <StatusBadge tone={monitoringActive ? "success" : "warning"}>{monitoringActive ? "Мониторинг работает" : "Мониторинг не запущен"}</StatusBadge>
    </header>

    <Card className={`command-state ${leadProblem ? "attention" : "calm"}`}>
      <div className="command-state-count"><span>{leadProblem ? "Требуют внимания" : "Текущий статус"}</span><strong>{leadProblem ? <AnimatedNumber value={problems.length} /> : "Спокойно"}</strong></div>
      <div className="command-state-copy">
        {leadProblem ? <><StatusBadge tone={priorityTone(leadProblem.priority)}>{priorityLabel(leadProblem.priority)}</StatusBadge><h3>{problemTitle(leadProblem)}</h3><p><strong>{problemPerson(leadProblem)}</strong> · {formatRelativeAge(leadProblem.occurred_at)}</p><blockquote>{leadProblem.evidence}</blockquote></> : <><span className="calm-mark" aria-hidden="true">✓</span><h3>Критичных ситуаций сейчас нет</h3><p>Продолжаем мониторинг. Новые рабочие риски появятся здесь после проверки контекста переписки.</p></>}
      </div>
      {leadProblem && <button className="command-primary" onClick={() => onOpenProblem(leadProblem.id)}>Открыть ситуацию <span aria-hidden="true">→</span></button>}
    </Card>

    <section className="command-metrics" aria-label="Ключевые показатели">
      <div><span>В работе</span><strong><AnimatedNumber value={problems.length} /></strong></div>
      <div><span>Ждут ответа</span><strong><AnimatedNumber value={waitingClients} /></strong></div>
      <div><span>Обязательства</span><strong><AnimatedNumber value={summary.commitments} /></strong></div>
    </section>

    <section className="attention-section">
      <header><div><p className="eyebrow">ПРИОРИТЕТ</p><h3>Требует внимания</h3></div>{problems.length > 0 && <button className="text-action" onClick={onOpenProblems}>Все ситуации →</button>}</header>
      {loading && !problems.length ? <Skeleton lines={3} /> : attention.length ? <div className="attention-list">{attention.map((problem) => <button key={problem.id} onClick={() => onOpenProblem(problem.id)} className="attention-item"><span className={`priority-rail ${problem.priority}`} /><div className="attention-person"><strong>{problemPerson(problem)}</strong><small>{formatRelativeAge(problem.occurred_at)}</small></div><div className="attention-copy"><strong>{problemTitle(problem)}</strong><p>{cleanExplanation(problem.explanation)}</p></div><StatusBadge tone={priorityTone(problem.priority)}>{priorityLabel(problem.priority)}</StatusBadge><span className="attention-arrow" aria-hidden="true">→</span></button>)}</div> : <EmptyState title="Ничего критичного не обнаружено" description="Ventrix продолжает мониторинг рабочих диалогов." />}
      {error && <div className="inline-error" role="status"><span>Не удалось обновить сводку.</span><button onClick={() => void reload()}>Повторить</button></div>}
    </section>

    <section className="dashboard-operations">
      <ChartContainer title="Активность сегодня" description="Только новые события подключённых рабочих аккаунтов." className="activity-card">
        {messagesToday || contactsToday ? <div className="activity-bars">
          <div><header><span>Сообщения</span><strong>{messagesToday.toLocaleString("ru-RU")}</strong></header><div><i style={{ "--activity-width": `${messagesToday / activityMax * 100}%` } as React.CSSProperties} /></div></div>
          <div><header><span>Новые контакты</span><strong>{contactsToday.toLocaleString("ru-RU")}</strong></header><div><i style={{ "--activity-width": `${contactsToday / activityMax * 100}%` } as React.CSSProperties} /></div></div>
        </div> : <div className="activity-quiet"><span aria-hidden="true">—</span><strong>Новых событий сегодня нет</strong><p>Мониторинг продолжает работать в фоне.</p></div>}
      </ChartContainer>

      <Card className="monitor-card">
        <header><div><p className="eyebrow">МОНИТОРИНГ</p><h3>Telegram подключён</h3></div><span className={`monitor-pulse ${monitoringActive ? "active" : ""}`} aria-label={monitoringActive ? "Работает" : "Не работает"} /></header>
        <div className="monitor-facts"><div><Icon name="telegram" /><span>Аккаунты<strong>{activeConnections.length} из {bootstrap.connections.length}</strong></span></div><div><Icon name="alert" /><span>Личные диалоги<strong>{personalDialogs.toLocaleString("ru-RU")}</strong></span></div><div><Icon name="groups" /><span>Рабочие группы<strong>{summary.groups}</strong></span></div></div>
        {bootstrap.progress && <div className="monitor-progress"><div><span>Последняя проверка</span><strong>{bootstrap.progress.status === "completed" ? "Завершена" : `${bootstrap.progress.percent}%`}</strong></div><div><i style={{ width: `${bootstrap.progress.percent}%` }} /></div><small>{bootstrap.progress.dialogs_completed.toLocaleString("ru-RU")} диалогов проверено</small></div>}
      </Card>

      <Card className="next-report-card"><span className="report-mark"><Icon name="report" /></span><div><p className="eyebrow">СЛЕДУЮЩИЙ ОТЧЁТ</p><strong>{formatNextReport(data?.settings.next_analysis_at)}</strong><small>{data?.settings.next_analysis_at ? "По расписанию проекта" : "Расписание пока недоступно"}</small></div></Card>
    </section>
  </div>;
}

function formatNextReport(value: string | null | undefined) {
  if (!value) return "Не запланирован";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "Не запланирован";
  return new Intl.DateTimeFormat("ru-RU", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(date);
}
