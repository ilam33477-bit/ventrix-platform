"use client";

import { useCallback, useMemo } from "react";

import type { VentrixClientApi } from "../../api/client";
import { AnimatedNumber, Card, EmptyState, Skeleton, StatusBadge } from "../../components/ui";
import { useResource } from "../../hooks/use-resource";
import { cleanExplanation, formatRelativeAge, priorityLabel, priorityTone, problemPerson, problemTitle, sortProblemsByPriority } from "../../lib/problem-presentation";
import type { Bootstrap, DashboardSummary, MiniAppAuth, Problem } from "../../types";

type DashboardData = Problem[];

export function DashboardView({ api, auth, summary, bootstrap, onOpenProblems, onOpenProblem }: {
  api: VentrixClientApi;
  auth: MiniAppAuth;
  summary: DashboardSummary;
  bootstrap: Bootstrap;
  onOpenProblems: () => void;
  onOpenProblem: (problemId: string) => void;
}) {
  const loader = useCallback(() => api.problems(), [api]);
  const { data, loading, error, reload } = useResource<DashboardData>(loader);
  const problems = data ?? bootstrap.problems;
  const attention = useMemo(() => sortProblemsByPriority(problems).slice(0, 3), [problems]);
  const leadProblem = attention[0];
  const waitingClients = problems.filter((problem) => problem.type === "client_without_answer").length;
  const activeConnections = bootstrap.connections.filter((connection) => ["connected", "ready", "syncing"].includes(connection.status));
  const messagesToday = bootstrap.connections.reduce((total, item) => total + (item.messages_today ?? 0), 0);
  const firstName = auth.user.first_name || auth.user.username || "руководитель";
  const monitoringActive = activeConnections.length > 0;

  return <div className="dashboard-command">
    <header className="dashboard-greeting">
      <div><h2>Добрый день, {firstName}</h2><p>{monitoringActive ? "Здесь только ситуации, где нужна ваша реакция." : "Подключите рабочий Telegram, чтобы начать мониторинг."}</p></div>
      <StatusBadge tone={monitoringActive ? "success" : "warning"}>{monitoringActive ? "Мониторинг работает" : "Мониторинг не запущен"}</StatusBadge>
    </header>

    <Card className={`daily-status ${leadProblem ? "attention" : "calm"}`}>
      <div><StatusBadge tone={leadProblem ? "warning" : "success"}>{leadProblem ? "Нужна реакция" : "Всё спокойно"}</StatusBadge><h3>{leadProblem ? <><AnimatedNumber value={problems.length} /> {situationWord(problems.length)} в работе</> : "Новых рисков нет"}</h3><p>{leadProblem ? "Сначала показаны самые важные ситуации." : "Продолжаем мониторинг рабочих диалогов."}</p></div>
      {leadProblem && <button className="command-primary" onClick={onOpenProblems}>Посмотреть ситуации <span aria-hidden="true">→</span></button>}
    </Card>

    <section className="command-metrics" aria-label="Ключевые показатели">
      <div><span>Ждут ответа</span><strong><AnimatedNumber value={waitingClients} /></strong></div>
      <div><span>Обязательства</span><strong><AnimatedNumber value={summary.commitments} /></strong></div>
      <div><span>Сообщения сегодня</span><strong><AnimatedNumber value={messagesToday} /></strong></div>
    </section>

    <section className="attention-section">
      <header><h3>Сначала проверьте</h3>{problems.length > 0 && <button className="text-action" onClick={onOpenProblems}>Все ситуации →</button>}</header>
      {loading && !problems.length ? <Skeleton lines={3} /> : attention.length ? <div className="attention-list">{attention.map((problem) => <button key={problem.id} onClick={() => onOpenProblem(problem.id)} className="attention-item"><span className={`priority-rail ${problem.priority}`} /><div className="attention-person"><strong>{problemPerson(problem)}</strong><small>{formatRelativeAge(problem.occurred_at)}</small></div><div className="attention-copy"><strong>{problemTitle(problem)}</strong><p>{cleanExplanation(problem.explanation)}</p></div><StatusBadge tone={priorityTone(problem.priority)}>{priorityLabel(problem.priority)}</StatusBadge><span className="attention-arrow" aria-hidden="true">→</span></button>)}</div> : <EmptyState title="Ничего критичного не обнаружено" description="Ventrix продолжает мониторинг рабочих диалогов." />}
      {error && <div className="inline-error" role="status"><span>Не удалось обновить сводку.</span><button onClick={() => void reload()}>Повторить</button></div>}
    </section>

  </div>;
}

function situationWord(value: number) {
  const mod100 = value % 100;
  const mod10 = value % 10;
  if (mod100 >= 11 && mod100 <= 14) return "ситуаций";
  if (mod10 === 1) return "ситуация";
  if (mod10 >= 2 && mod10 <= 4) return "ситуации";
  return "ситуаций";
}
