"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { VentrixClientApi } from "../../api/client";
import { Button, Card, EmptyState, Skeleton, StatusBadge } from "../../components/ui";
import { useResource } from "../../hooks/use-resource";
import { cleanExplanation, formatRelativeAge, priorityLabel, priorityTone, problemPerson, problemTitle, PROBLEM_STATUS_LABELS, sortProblemsByPriority } from "../../lib/problem-presentation";
import type { Problem, ProblemDetail, ProblemStatus } from "../../types";

type ProblemFilter = "all" | Problem["priority"] | "resolved";
type ProblemFeed = { active: Problem[]; resolved: Problem[] };

const NEXT_ACTIONS: Partial<Record<ProblemStatus, Array<[ProblemStatus, string, "primary" | "secondary" | "ghost"]>>> = {
  new: [["needs_confirmation", "Проверить ситуацию", "primary"], ["acknowledged", "Подтвердить", "secondary"]],
  needs_confirmation: [["acknowledged", "Подтвердить", "primary"], ["false_positive", "Не проблема", "ghost"]],
  acknowledged: [["assigned", "Назначить сотруднику", "primary"]],
  assigned: [["in_progress", "Взять в работу", "primary"], ["false_positive", "Не проблема", "ghost"]],
  in_progress: [["resolved", "Отметить решённой", "primary"], ["waiting", "Отложить", "secondary"], ["false_positive", "Не проблема", "ghost"]],
  waiting: [["in_progress", "Вернуть в работу", "primary"], ["false_positive", "Не проблема", "ghost"]],
  resolved: [["reopened", "Открыть снова", "secondary"]],
  auto_resolved: [["reopened", "Открыть снова", "secondary"]],
  false_positive: [["reopened", "Вернуть как проблему", "secondary"]],
  reopened: [["assigned", "Назначить сотруднику", "primary"], ["in_progress", "Взять в работу", "secondary"]],
};

function ProblemDetailPanel({ api, problem, onChanged, onClose }: {
  api: VentrixClientApi;
  problem: ProblemDetail;
  onChanged: () => Promise<void>;
  onClose: () => void;
}) {
  const employeesLoader = useCallback(() => api.employees(), [api]);
  const { data: employees, loading: employeesLoading } = useResource(employeesLoader);
  const [employeeId, setEmployeeId] = useState(problem.responsible_employee_id ?? "");
  const [deadline, setDeadline] = useState(problem.deadline_at?.slice(0, 16) ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  async function transition(status: ProblemStatus, label: string) {
    if (status === "assigned" && !employeeId) {
      setError("Выберите сотрудника перед назначением.");
      return;
    }
    const reason = status === "false_positive"
      ? "Пользователь отметил карточку как не проблему."
      : status === "reopened"
        ? "Пользователь вернул карточку в работу."
        : `Действие в Mini App: ${label}.`;
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await api.transitionProblem(problem.id, {
        status,
        reason,
        responsible_employee_id: employeeId || undefined,
        deadline_at: deadline ? new Date(deadline).toISOString() : undefined,
      });
      setSuccess(label);
      await onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Не удалось изменить ситуацию");
    } finally {
      setBusy(false);
    }
  }

  return <article className="problem-detail-view">
    <header className="problem-detail-nav"><button className="text-action" onClick={onClose}>← Все ситуации</button><StatusBadge tone={priorityTone(problem.priority)}>{priorityLabel(problem.priority)}</StatusBadge></header>

    <Card className="problem-detail-hero">
      <div className="problem-detail-kicker"><span>СИТУАЦИЯ</span><small>{formatRelativeAge(problem.occurred_at)}</small></div>
      <h2>{problemTitle(problem)}</h2>
      <div className="problem-identity"><div><strong>{problem.dialog_title ?? "Клиент"}</strong><span>{problemPerson(problem)}</span></div><StatusBadge tone={problem.status === "resolved" || problem.status === "auto_resolved" ? "success" : "neutral"}>{PROBLEM_STATUS_LABELS[problem.status] ?? problem.status}</StatusBadge></div>
    </Card>

    <section className="problem-detail-flow">
      <div className="detail-section"><p className="detail-label">ПРИЧИНА</p><h3>Почему Ventrix обратил внимание</h3><p>{cleanExplanation(problem.explanation)}</p></div>

      <Card className="evidence-panel"><div className="evidence-mark" aria-hidden="true">“</div><div><p className="detail-label">ДОКАЗАТЕЛЬСТВО</p><blockquote>{problem.evidence}</blockquote></div></Card>

      <div className="detail-section context-section"><p className="detail-label">КОНТЕКСТ</p><h3>Переписка вокруг ситуации</h3>{problem.context_messages.length ? <div className="dialog-context">{problem.context_messages.map((message) => <div className={`${message.outgoing ? "outgoing" : "incoming"} ${message.is_source ? "source" : ""}`} key={message.id}><small>{message.outgoing ? "Сотрудник" : problemPerson(problem)} · {new Date(message.sent_at).toLocaleString("ru-RU")}</small><p>{message.text || "Сообщение без текста"}</p>{message.is_source && <em>Исходное сообщение</em>}</div>)}</div> : <div className="context-empty">Контекст сообщений для этой ситуации не найден.</div>}</div>

      <div className="detail-section assignment-section"><p className="detail-label">ОТВЕТСТВЕННЫЙ</p><h3>{problem.responsible_employee_name ?? "Сотрудник пока не назначен"}</h3><div className="problem-routing"><div><span>Рабочий аккаунт</span><strong>{problem.connection_username ? `@${problem.connection_username}` : problem.connection_name ?? "Не определён"}</strong></div><div><span>Диалог</span><strong>{problem.dialog_username ? `@${problem.dialog_username}` : problem.dialog_title ?? "Не определён"}</strong></div></div><div className="assignment-controls"><label>Назначить сотрудника<select disabled={employeesLoading || busy} value={employeeId} onChange={(event) => setEmployeeId(event.target.value)}><option value="">Не назначен</option>{employees?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>Срок решения<input disabled={busy} type="datetime-local" value={deadline} onChange={(event) => setDeadline(event.target.value)} /></label></div></div>

      <Card className="next-step-panel"><span aria-hidden="true">→</span><div><p className="detail-label">СЛЕДУЮЩИЙ ШАГ</p><strong>{problem.recommended_action}</strong>{problem.deadline_at && <small>Срок: {new Date(problem.deadline_at).toLocaleString("ru-RU")}</small>}</div></Card>

      <div className="detail-section action-section"><p className="detail-label">ДЕЙСТВИЯ</p>{error && <p className="form-error" role="alert">{error}</p>}{success && <p className="action-success" role="status"><span aria-hidden="true">✓</span>{success}</p>}<div className="problem-actions">{(NEXT_ACTIONS[problem.status] ?? []).map(([status, label, variant]) => <Button variant={variant} key={status} disabled={busy} onClick={() => void transition(status, label)}>{busy ? "Сохраняем…" : label}</Button>)}</div></div>

      {(problem.transitions.length > 0 || problem.verifications.length > 0) && <details className="problem-history"><summary>История ситуации <span>{problem.transitions.length + problem.verifications.length}</span></summary><div className="timeline">{problem.transitions.map((item, index) => <div key={`${item.occurred_at}-${index}`}><i /><p><strong>{PROBLEM_STATUS_LABELS[item.from_status] ?? item.from_status} → {PROBLEM_STATUS_LABELS[item.to_status] ?? item.to_status}</strong><span>{item.reason}</span><small>{new Date(item.occurred_at).toLocaleString("ru-RU")}</small></p></div>)}{problem.verifications.map((item) => <div key={item.checked_at}><i /><p><strong>Проверка исправления: {item.outcome}</strong><span>{item.reason}</span><small>{new Date(item.checked_at).toLocaleString("ru-RU")}</small></p></div>)}</div></details>}
    </section>
  </article>;
}

export function ProblemsView({ api, initialProblemId }: { api: VentrixClientApi; initialProblemId?: string }) {
  const [filter, setFilter] = useState<ProblemFilter>("all");
  const loader = useCallback(async (): Promise<ProblemFeed> => {
    const [active, resolved, autoResolved] = await Promise.all([api.problems(), api.problems("resolved"), api.problems("auto_resolved")]);
    return { active, resolved: [...resolved, ...autoResolved] };
  }, [api]);
  const { data, loading, error, reload } = useResource(loader);
  const [detail, setDetail] = useState<ProblemDetail | null>(null);
  const [openingId, setOpeningId] = useState("");
  const [openError, setOpenError] = useState("");
  const openedInitialProblem = useRef<string | null>(null);

  const active = useMemo(() => data?.active ?? [], [data]);
  const resolved = useMemo(() => data?.resolved ?? [], [data]);
  const visible = useMemo(() => sortProblemsByPriority(filter === "resolved" ? resolved : active.filter((item) => filter === "all" || item.priority === filter)), [active, filter, resolved]);
  const criticalCount = active.filter((item) => item.priority === "critical").length;
  const highCount = active.filter((item) => item.priority === "high").length;
  const initialLoading = loading && !data;

  const open = useCallback(async (problemId: string) => {
    setOpeningId(problemId);
    setOpenError("");
    try {
      setDetail(await api.problem(problemId));
    } catch (cause) {
      setOpenError(cause instanceof Error ? cause.message : "Не удалось открыть ситуацию");
    } finally {
      setOpeningId("");
    }
  }, [api]);

  useEffect(() => {
    if (!initialProblemId || openedInitialProblem.current === initialProblemId) return;
    openedInitialProblem.current = initialProblemId;
    void open(initialProblemId);
  }, [initialProblemId, open]);

  async function refreshDetail() {
    if (!detail) return;
    const refreshed = await api.problem(detail.id).catch(() => null);
    setDetail(refreshed);
    await reload();
  }

  if (detail) return <ProblemDetailPanel api={api} problem={detail} onChanged={refreshDetail} onClose={() => setDetail(null)} />;

  const filters: Array<{ value: ProblemFilter; label: string; count?: number }> = [
    { value: "all", label: "Все", count: active.length },
    { value: "critical", label: "Критичные", count: criticalCount },
    { value: "high", label: "Высокие", count: highCount },
    { value: "medium", label: "Средние", count: active.filter((item) => item.priority === "medium").length },
    { value: "resolved", label: "Решённые", count: resolved.length },
  ];

  return <div className="problems-workspace">
    <header className="problems-summary"><div><p className="eyebrow">РАБОЧИЕ СИТУАЦИИ</p><h2>{initialLoading ? "Проверяем рабочие ситуации" : active.length ? `${active.length} ${situationWord(active.length)} требуют решения` : "Сейчас всё под контролем"}</h2><p>{initialLoading ? "Собираем актуальные статусы и ответственных." : active.length ? "Сначала показаны самые важные и давно ожидающие реакции ситуации." : "Новых подтверждённых рисков в рабочих диалогах нет."}</p></div>{!initialLoading && <div className="problems-summary-facts"><span><i className="critical" />Критичные<strong>{criticalCount}</strong></span><span><i className="high" />Высокие<strong>{highCount}</strong></span></div>}</header>

    {!initialLoading && <div className="problem-filters" role="tablist" aria-label="Фильтр ситуаций">{filters.map((item) => <button role="tab" aria-selected={filter === item.value} className={filter === item.value ? "active" : ""} key={item.value} onClick={() => setFilter(item.value)}><span>{item.label}</span>{item.count !== undefined && <small>{item.count}</small>}</button>)}</div>}

    {loading ? <div className="problem-list-loading"><Skeleton lines={4} /><Skeleton lines={3} /></div> : <div className="problem-cards" key={filter}>{visible.map((problem) => <button className="problem-card-button" key={problem.id} onClick={() => void open(problem.id)} disabled={openingId === problem.id}><Card className={`problem-card priority-${problem.priority}`}><div className="problem-card-top"><StatusBadge tone={priorityTone(problem.priority)}>{priorityLabel(problem.priority)}</StatusBadge><span className="problem-age">{formatRelativeAge(problem.occurred_at)}</span></div><div className="problem-card-person"><span>{problemPerson(problem).replace("@", "").slice(0, 2).toUpperCase()}</span><div><strong>{problemPerson(problem)}</strong><small>{problem.connection_username ? `Рабочий аккаунт @${problem.connection_username}` : problem.connection_name ?? "Рабочий аккаунт не указан"}</small></div></div><div className="problem-card-copy"><p className="problem-type">{problemTitle(problem)}</p><h3>{cleanExplanation(problem.explanation)}</h3><blockquote>{problem.evidence}</blockquote></div><div className="problem-card-meta"><span><small>Ответственный</small><strong>{problem.responsible_employee_name ?? "Не назначен"}</strong></span><span><small>Статус</small><strong>{PROBLEM_STATUS_LABELS[problem.status] ?? problem.status}</strong></span></div><footer><span>{openingId === problem.id ? "Открываем…" : "Открыть ситуацию"}</span><b aria-hidden="true">→</b></footer></Card></button>)}</div>}

    {(error || openError) && <div className="inline-error" role="alert"><span>{openError || "Не удалось загрузить ситуации."}</span><button onClick={() => void reload()}>Повторить</button></div>}
    {!loading && !visible.length && <EmptyState title={filter === "resolved" ? "Решённых ситуаций пока нет" : "В этом разделе всё спокойно"} description={filter === "resolved" ? "Здесь появятся ситуации после подтверждённого решения." : "Ventrix продолжает мониторинг и покажет новый риск после проверки контекста."} />}
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
