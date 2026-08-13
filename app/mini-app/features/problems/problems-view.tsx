"use client";

import { useCallback, useMemo, useState } from "react";

import type { VentrixClientApi } from "../../api/client";
import { Card, EmptyState, SectionHeading, Skeleton, StatusBadge } from "../../components/ui";
import { useResource } from "../../hooks/use-resource";
import type { Problem, ProblemDetail, ProblemStatus } from "../../types";

const NEXT_ACTIONS: Partial<Record<ProblemStatus, Array<[ProblemStatus, string]>>> = {
  new: [["needs_confirmation", "Проверить"], ["acknowledged", "Принять"]],
  needs_confirmation: [["acknowledged", "Подтвердить"], ["false_positive", "Ложное срабатывание"]],
  acknowledged: [["assigned", "Назначить"]],
  assigned: [["in_progress", "В работу"], ["false_positive", "Не проблема"]],
  in_progress: [["waiting", "Ждём клиента"], ["resolved", "Решено"], ["false_positive", "Не проблема"]],
  waiting: [["in_progress", "Вернуть в работу"], ["false_positive", "Не проблема"]],
  resolved: [["reopened", "Открыть снова"]],
  auto_resolved: [["reopened", "Открыть снова"]],
  reopened: [["assigned", "Назначить"], ["in_progress", "В работу"]],
};

const STATUS_LABELS: Record<string, string> = {
  new: "Новая", needs_confirmation: "Нужно проверить", acknowledged: "Подтверждена",
  assigned: "Назначена", in_progress: "В работе", waiting: "Ждём ответа",
  resolved: "Решена", auto_resolved: "Исправление подтверждено",
  false_positive: "Не проблема", reopened: "Открыта снова",
};

function problemTitle(problem: Problem) {
  if (problem.type === "client_without_answer") return "Клиент ждёт ответа";
  return problem.explanation.replaceAll("SLA", "установленного времени ответа");
}

function ProblemDetailPanel({ api, problem, onChanged, onClose }: {
  api: VentrixClientApi;
  problem: ProblemDetail;
  onChanged: () => Promise<void>;
  onClose: () => void;
}) {
  const employeesLoader = useCallback(() => api.employees(), [api]);
  const { data: employees } = useResource(employeesLoader);
  const [reason, setReason] = useState("");
  const [evidence, setEvidence] = useState("");
  const [employeeId, setEmployeeId] = useState(problem.responsible_employee_id ?? "");
  const [deadline, setDeadline] = useState(problem.deadline_at?.slice(0, 16) ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function transition(status: ProblemStatus) {
    const actualReason = reason.trim() || `Статус изменён на ${status}`;
    setBusy(true);
    setError("");
    try {
      await api.transitionProblem(problem.id, {
        status,
        reason: actualReason,
        evidence: evidence.trim() || undefined,
        responsible_employee_id: employeeId || undefined,
        deadline_at: deadline ? new Date(deadline).toISOString() : undefined,
      });
      await onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Не удалось изменить проблему");
    } finally {
      setBusy(false);
    }
  }

  return <Card className="problem-detail">
    <div className="problem-card-top"><StatusBadge tone="warning">{STATUS_LABELS[problem.status] ?? problem.status}</StatusBadge><button className="text-action" onClick={onClose}>← К списку</button></div>
    <h3>{problemTitle(problem)}</h3>
    <p className="problem-explanation">{problem.explanation.replaceAll("SLA", "установленного времени ответа")}</p>
    <div className="dialog-context"><h4>Контекст переписки</h4>{problem.context_messages.map((message) => <div className={`${message.outgoing ? "outgoing" : "incoming"} ${message.is_source ? "source" : ""}`} key={message.id}><small>{message.outgoing ? "Сотрудник" : "Клиент"} · {new Date(message.sent_at).toLocaleString("ru-RU")}</small><p>{message.text || "Сообщение без текста"}</p></div>)}</div>
    <p><strong>Ожидаемое действие:</strong> {problem.recommended_action}</p>
    <p><strong>Ответственный:</strong> {problem.responsible_employee_name ?? "не назначен"}</p>
    <div className="control-form">
      <label>Ответственный<select value={employeeId} onChange={(event) => setEmployeeId(event.target.value)}><option value="">Не назначен</option>{employees?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <label>Дедлайн<input type="datetime-local" value={deadline} onChange={(event) => setDeadline(event.target.value)} /></label>
      <label>Причина / комментарий<textarea value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Что сделано или почему меняется статус" /></label>
      <label>Доказательство исправления<textarea value={evidence} onChange={(event) => setEvidence(event.target.value)} placeholder="Ссылка, сообщение или результат проверки" /></label>
    </div>
    {error && <p className="form-error">{error}</p>}
    <div className="problem-actions">{(NEXT_ACTIONS[problem.status] ?? []).map(([status, label]) => <button key={status} disabled={busy} onClick={() => void transition(status)}>{label}</button>)}</div>
    <h4>История</h4>
    <div className="timeline">{problem.transitions.map((item, index) => <div key={`${item.occurred_at}-${index}`}><i /><p><strong>{item.from_status} → {item.to_status}</strong><span>{item.reason}</span><small>{new Date(item.occurred_at).toLocaleString("ru-RU")}</small></p></div>)}</div>
    {!!problem.verifications.length && <><h4>Проверки исправления</h4><div className="timeline">{problem.verifications.map((item) => <div key={item.checked_at}><i /><p><strong>{item.outcome} · {Math.round(item.confidence * 100)}%</strong><span>{item.reason}</span></p></div>)}</div></>}
  </Card>;
}

export function ProblemsView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.problems(), [api]);
  const { data, loading, error, reload } = useResource(loader);
  const [filter, setFilter] = useState<"all" | Problem["priority"]>("all");
  const [detail, setDetail] = useState<ProblemDetail | null>(null);
  const visible = useMemo(() => (data ?? []).filter((item) => filter === "all" || item.priority === filter), [filter, data]);

  async function open(problemId: string) { setDetail(await api.problem(problemId)); }
  async function refreshDetail() {
    if (!detail) return;
    const refreshed = await api.problem(detail.id).catch(() => null);
    setDetail(refreshed);
    await reload();
  }

  if (detail) return <><SectionHeading eyebrow="ПРОБЛЕМА" title="Карточка и история" /><ProblemDetailPanel api={api} problem={detail} onChanged={refreshDetail} onClose={() => setDetail(null)} /></>;
  return <>
    <SectionHeading eyebrow="РАБОЧИЕ СИТУАЦИИ" title="Что требует решения" description="Только подтверждённые риски: кто отвечает, что произошло и какой следующий шаг." />
    <div className="chip-row">{(["all", "critical", "high", "medium"] as const).map((item) => <button key={item} className={filter === item ? "active" : ""} onClick={() => setFilter(item)}>{item === "all" ? "Все" : item === "critical" ? "Критичные" : item === "high" ? "Высокие" : "Средние"}</button>)}</div>
    {loading ? <Skeleton lines={4} /> : <div className="problem-cards">{visible.map((problem) => <button className="problem-card-button" key={problem.id} onClick={() => void open(problem.id)}><Card className="problem-card"><div className="problem-card-top"><StatusBadge tone={problem.priority === "critical" ? "danger" : "warning"}>{problem.priority === "critical" ? "Критично" : problem.priority === "high" ? "Важно" : "Проверить"}</StatusBadge><small>{STATUS_LABELS[problem.status] ?? problem.status}</small></div><h3>{problemTitle(problem)}</h3><p>{problem.explanation.replaceAll("SLA", "установленного времени ответа")}</p><blockquote>{problem.evidence}</blockquote><p><strong>Что сделать:</strong> {problem.recommended_action}</p><footer>{problem.deadline_at ? `До ${new Date(problem.deadline_at).toLocaleString("ru-RU")}` : "Без жёсткого срока"}</footer></Card></button>)}</div>}
    {error && <p className="form-error">{error}</p>}
    {!loading && !visible.length && <EmptyState title="Активных проблем нет" description="Список обновится после следующего анализа." />}
  </>;
}
