"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { VentrixClientApi } from "../../api/client";
import { Button, Card, EmptyState, Skeleton, StatusBadge } from "../../components/ui";
import { useResource } from "../../hooks/use-resource";
import { cleanExplanation, formatRelativeAge, priorityLabel, priorityTone, problemPerson, problemStatusLabel, problemTitle, sortProblemsByPriority, verificationOutcomeLabel } from "../../lib/problem-presentation";
import type { Problem, ProblemDetail, ProblemStatus } from "../../types";

type ProblemFilter = "active" | "urgent" | "resolved";

const NEXT_ACTIONS: Partial<Record<ProblemStatus, Array<[ProblemStatus, string, "primary" | "secondary" | "ghost"]>>> = {
  new: [["needs_confirmation", "Проверить ситуацию", "primary"]],
  needs_confirmation: [["false_positive", "Не проблема", "ghost"]],
  assigned: [["in_progress", "Взять в работу", "primary"], ["false_positive", "Не проблема", "ghost"]],
  in_progress: [["resolved", "Отметить решённой", "primary"], ["waiting", "Отложить", "secondary"], ["false_positive", "Не проблема", "ghost"]],
  waiting: [["in_progress", "Вернуть в работу", "primary"], ["false_positive", "Не проблема", "ghost"]],
  resolved: [["reopened", "Открыть снова", "secondary"]],
  auto_resolved: [["reopened", "Открыть снова", "secondary"]],
  false_positive: [["reopened", "Вернуть как проблему", "secondary"]],
  reopened: [["in_progress", "Взять в работу", "primary"]],
};

const CLOSED_STATUSES = new Set<ProblemStatus>(["resolved", "auto_resolved", "false_positive"]);

function ProblemReplyPanel({ api, problem, onChanged }: {
  api: VentrixClientApi;
  problem: ProblemDetail;
  onChanged: () => Promise<void>;
}) {
  const loader = useCallback(() => api.problemConversation(problem.id), [api, problem.id]);
  const { data, loading, error: loadError, reload } = useResource(loader);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState("");
  const [optimistic, setOptimistic] = useState<{ requestId: string; text: string } | null>(null);
  const requestId = useRef<string | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void reload();
    }, optimistic ? 3_000 : 15_000);
    return () => window.clearInterval(timer);
  }, [optimistic, reload]);

  useEffect(() => {
    if (!optimistic || !data) return;
    const command = data.outbound_commands.find((item) => item.client_request_id === optimistic.requestId);
    const delivered = command?.telegram_message_id != null
      && data.messages.some((item) => item.telegram_message_id === command.telegram_message_id);
    if (command?.status !== "failed" && !delivered) return;
    const timer = window.setTimeout(() => {
      setOptimistic(null);
      requestId.current = null;
      if (command?.status === "failed") {
        setSendError("Не удалось отправить сообщение. Проверьте рабочую Telegram-сессию и попробуйте ещё раз.");
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [data, optimistic]);

  async function send() {
    const message = text.trim();
    if (!message || sending) return;
    requestId.current ??= window.crypto.randomUUID();
    const activeRequestId = requestId.current;
    setOptimistic({ requestId: activeRequestId, text: message });
    setSending(true);
    setSendError("");
    try {
      const result = await api.replyToProblem(problem.id, message, activeRequestId);
      if (result.status === "failed") throw new Error("Telegram не принял сообщение");
      setText("");
      await reload();
      await onChanged();
    } catch {
      setOptimistic(null);
      requestId.current = null;
      setSendError("Не удалось отправить сообщение. Проверьте рабочую Telegram-сессию и попробуйте ещё раз.");
    } finally {
      setSending(false);
    }
  }

  return <Card className="reply-panel">
    <header><div><p className="detail-label">ОТВЕТ КЛИЕНТУ</p><h3>Переписка и ответ</h3></div>{data && <StatusBadge tone={data.can_reply ? "success" : "neutral"}>{data.can_reply ? "Сессия доступна" : "Только просмотр"}</StatusBadge>}</header>
    {loading && !data ? <Skeleton lines={3} /> : data ? <>
      <div className="reply-route"><span><small>Клиент</small><strong>{data.client.username ? `@${data.client.username}` : data.client.title}</strong></span><span><small>Отправитель</small><strong>{data.connection.username ? `@${data.connection.username}` : data.connection.name ?? "Рабочий аккаунт"}</strong></span></div>
      <div className="reply-thread" aria-live="polite">{data.messages.map((message) => <div className={message.outgoing ? "outgoing" : "incoming"} key={message.id}><small>{message.outgoing ? "Сотрудник" : data.client.username ? `@${data.client.username}` : data.client.title} · {new Date(message.sent_at).toLocaleString("ru-RU")}</small><p>{message.text || "Сообщение без текста"}</p></div>)}{optimistic && <div className="outgoing optimistic" key={optimistic.requestId}><small>Сотрудник · сейчас</small><p>{optimistic.text}</p></div>}</div>
      {data.can_reply ? <div className="reply-composer"><label htmlFor={`problem-reply-${problem.id}`}>Ответить клиенту</label><textarea id={`problem-reply-${problem.id}`} rows={3} maxLength={4096} value={text} disabled={sending} placeholder="Введите сообщение от имени рабочего аккаунта" onChange={(event) => setText(event.target.value)} /><div><small>{text.length}/4096</small><Button variant="primary" disabled={sending || !text.trim()} onClick={() => void send()}>{sending ? "Отправляем…" : "Отправить"}</Button></div></div> : <p className="reply-unavailable">Ответ недоступен: проверьте права и состояние рабочей Telegram-сессии.</p>}
    </> : null}
    {loadError && <div className="inline-error" role="alert"><span>Не удалось обновить переписку.</span><button onClick={() => void reload()}>Повторить</button></div>}
    {sendError && <p className="form-error" role="alert">{sendError}</p>}
  </Card>;
}

function ProblemDetailPanel({ api, problem, onChanged, onClose }: {
  api: VentrixClientApi;
  problem: ProblemDetail;
  onChanged: () => Promise<void>;
  onClose: () => void;
}) {
  const [deadline, setDeadline] = useState(problem.deadline_at?.slice(0, 16) ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [confirmingClose, setConfirmingClose] = useState(false);

  async function transition(status: ProblemStatus, label: string) {
    const reason = status === "false_positive"
      ? "Пользователь отметил карточку как не проблему."
      : status === "reopened"
        ? "Пользователь вернул карточку в работу."
        : `Действие в Mini App: ${label}.`;
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      if (status === "in_progress") {
        await api.startProblem(problem.id);
      } else if (status === "false_positive") {
        await api.markProblemFalsePositive(problem.id);
      } else {
        await api.transitionProblem(problem.id, {
          status,
          reason,
          deadline_at: deadline ? new Date(deadline).toISOString() : undefined,
        });
      }
      setSuccess(label);
      await onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Не удалось изменить ситуацию");
    } finally {
      setBusy(false);
    }
  }

  async function closeProblem() {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await api.resolveProblem(problem.id);
      setConfirmingClose(false);
      setSuccess("Ситуация завершена");
      await onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Не удалось завершить ситуацию");
    } finally {
      setBusy(false);
    }
  }

  return <article className="problem-detail-view">
    <header className="problem-detail-nav"><button className="text-action" onClick={onClose}>← Все ситуации</button><StatusBadge tone={priorityTone(problem.priority)}>{priorityLabel(problem.priority)}</StatusBadge></header>

    <Card className="problem-detail-hero">
      <div className="problem-detail-kicker"><span>{problemTitle(problem)}</span><small>{formatRelativeAge(problem.occurred_at)}</small></div>
      <h2>{problem.evidence}</h2>
      <p className="problem-detail-explanation">{cleanExplanation(problem.explanation)}</p>
      <div className="problem-identity"><div><strong>{problem.dialog_title ?? "Клиент"}</strong><span>{problemPerson(problem)}</span></div><StatusBadge tone={problem.status === "resolved" || problem.status === "auto_resolved" ? "success" : "neutral"}>{problemStatusLabel(problem.status)}</StatusBadge></div>
    </Card>

    <section className="problem-detail-flow">
      <ProblemReplyPanel api={api} problem={problem} onChanged={onChanged} />

      <div className="detail-section assignment-section"><div className="assignment-heading"><div><p className="detail-label">ОТВЕТСТВЕННЫЙ</p><h3>{problem.responsible_employee_name ?? (problem.connection_username ? `@${problem.connection_username}` : "Рабочий аккаунт")}</h3></div><small>Назначен автоматически</small></div><div className="problem-routing"><div><span>Аккаунт</span><strong>{problem.connection_username ? `@${problem.connection_username}` : problem.connection_name ?? "Не определён"}</strong></div><div><span>Клиент</span><strong>{problem.dialog_username ? `@${problem.dialog_username}` : problem.dialog_title ?? "Не определён"}</strong></div></div><label className="compact-deadline"><span>Срок решения</span><input disabled={busy} type="datetime-local" value={deadline} onChange={(event) => setDeadline(event.target.value)} /></label></div>

      {(problem.transitions.length > 0 || problem.verifications.length > 0) && <details className="problem-history"><summary>История ситуации <span>{problem.transitions.length + problem.verifications.length}</span></summary><div className="timeline">{problem.transitions.map((item, index) => <div key={`${item.occurred_at}-${index}`}><i /><p><strong>{problemStatusLabel(item.from_status)} → {problemStatusLabel(item.to_status)}</strong><span>{item.reason}</span><small>{new Date(item.occurred_at).toLocaleString("ru-RU")}</small></p></div>)}{problem.verifications.map((item) => <div key={item.checked_at}><i /><p><strong>Проверка исправления: {verificationOutcomeLabel(item.outcome)}</strong><span>{item.reason}</span><small>{new Date(item.checked_at).toLocaleString("ru-RU")}</small></p></div>)}</div></details>}

      <div className="detail-section action-section"><p className="detail-label">ЗАВЕРШЕНИЕ СИТУАЦИИ</p>{error && <p className="form-error" role="alert">{error}</p>}{success && <p className="action-success" role="status"><span aria-hidden="true">✓</span>{success}</p>}{!CLOSED_STATUSES.has(problem.status) && <div className="detail-quick-close">{confirmingClose ? <><p>Подтвердите, что ситуация действительно завершена.</p><div><Button variant="primary" disabled={busy} onClick={() => void closeProblem()}>{busy ? "Закрываем…" : "Да, завершить"}</Button><Button variant="ghost" disabled={busy} onClick={() => setConfirmingClose(false)}>Отмена</Button></div></> : <Button variant="secondary" disabled={busy} onClick={() => setConfirmingClose(true)}>Завершить ситуацию</Button>}</div>}<div className="problem-actions">{(NEXT_ACTIONS[problem.status] ?? []).map(([status, label, variant]) => <Button variant={variant} key={status} disabled={busy} onClick={() => void transition(status, label)}>{busy ? "Сохраняем…" : label}</Button>)}</div></div>
    </section>
  </article>;
}

export function ProblemsView({ api, initialProblemId }: { api: VentrixClientApi; initialProblemId?: string }) {
  const [filter, setFilter] = useState<ProblemFilter>("active");
  const loader = useCallback(() => api.problems(), [api]);
  const { data, loading, error, reload } = useResource(loader);
  const [resolved, setResolved] = useState<Problem[]>([]);
  const [resolvedLoading, setResolvedLoading] = useState(false);
  const [resolvedLoaded, setResolvedLoaded] = useState(false);
  const [resolvedError, setResolvedError] = useState("");
  const resolvedRequested = useRef(false);
  const [detail, setDetail] = useState<ProblemDetail | null>(null);
  const [openingId, setOpeningId] = useState("");
  const [confirmingCloseId, setConfirmingCloseId] = useState("");
  const [closingId, setClosingId] = useState("");
  const [openError, setOpenError] = useState("");
  const openedInitialProblem = useRef<string | null>(null);

  const active = useMemo(() => data ?? [], [data]);
  const visible = useMemo(() => sortProblemsByPriority(
    filter === "resolved"
      ? resolved
      : filter === "urgent"
        ? active.filter((item) => ["critical", "high"].includes(item.priority))
        : active,
  ), [active, filter, resolved]);
  const criticalCount = active.filter((item) => item.priority === "critical").length;
  const highCount = active.filter((item) => item.priority === "high").length;
  const initialLoading = loading && !data;

  useEffect(() => {
    if (filter !== "resolved" || resolvedLoaded || resolvedRequested.current) return;
    resolvedRequested.current = true;
    setResolvedLoading(true);
    setResolvedError("");
    void Promise.all([api.problems("resolved"), api.problems("auto_resolved")])
      .then(([manual, automatic]) => {
        setResolved([...manual, ...automatic]);
        setResolvedLoaded(true);
      })
      .catch((cause) => {
        resolvedRequested.current = false;
        setResolvedError(cause instanceof Error ? cause.message : "Не удалось загрузить завершённые ситуации");
      })
      .finally(() => setResolvedLoading(false));
  }, [api, filter, resolvedLoaded]);

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

  async function quickClose(problemId: string) {
    setClosingId(problemId);
    setOpenError("");
    try {
      await api.resolveProblem(problemId);
      setConfirmingCloseId("");
      await reload();
    } catch (cause) {
      setOpenError(cause instanceof Error ? cause.message : "Не удалось завершить ситуацию");
    } finally {
      setClosingId("");
    }
  }

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
    { value: "active", label: "В работе", count: active.length },
    { value: "urgent", label: "Срочные", count: criticalCount + highCount },
    { value: "resolved", label: "Завершённые", count: resolved.length },
  ];

  return <div className="problems-workspace">
    <header className="problems-summary"><div><h2>{initialLoading ? "Обновляем ситуации" : active.length ? `${active.length} ${situationWord(active.length)} в работе` : "Сейчас всё под контролем"}</h2><p>{initialLoading ? "Получаем актуальные статусы." : active.length ? "Сначала показаны самые важные и давно ожидающие реакции." : "Новых подтверждённых рисков в рабочих диалогах нет."}</p></div>{!initialLoading && (criticalCount + highCount > 0) && <div className="problems-summary-facts"><span><i className="critical" />Срочные<strong>{criticalCount + highCount}</strong></span></div>}</header>

    {!initialLoading && <div className="problem-filters" role="tablist" aria-label="Фильтр ситуаций">{filters.map((item) => <button role="tab" aria-selected={filter === item.value} className={filter === item.value ? "active" : ""} key={item.value} onClick={() => setFilter(item.value)}><span>{item.label}</span>{item.count !== undefined && <small>{item.count}</small>}</button>)}</div>}

    {(loading || (filter === "resolved" && resolvedLoading)) ? <div className="problem-list-loading"><Skeleton lines={4} /><Skeleton lines={3} /></div> : <div className="problem-cards" key={filter}>{visible.map((problem) => <Card className={`problem-card priority-${problem.priority}`} key={problem.id}><button className="problem-card-open" onClick={() => void open(problem.id)} disabled={openingId === problem.id}><div className="problem-card-top"><StatusBadge tone={priorityTone(problem.priority)}>{priorityLabel(problem.priority)}</StatusBadge><span className="problem-age">{formatRelativeAge(problem.occurred_at)}</span></div><div className="problem-card-person"><span>{problemPerson(problem).replace("@", "").slice(0, 2).toUpperCase()}</span><div><strong>{problemPerson(problem)}</strong><small>{problem.responsible_employee_name ? `Ответственный: ${problem.responsible_employee_name}` : problem.connection_username ? `Ответственный: @${problem.connection_username}` : "Ответственный определяется"}</small></div></div><div className="problem-card-copy"><p className="problem-type">{problemTitle(problem)}</p><h3>{problem.evidence}</h3><p>{cleanExplanation(problem.explanation)}</p></div><footer><span>{openingId === problem.id ? "Открываем…" : "Открыть"}</span><b aria-hidden="true">→</b></footer></button><div className="problem-quick-close">{confirmingCloseId === problem.id ? <><span>Завершить эту ситуацию?</span><Button variant="primary" disabled={closingId === problem.id} onClick={() => void quickClose(problem.id)}>{closingId === problem.id ? "Закрываем…" : "Да, завершить"}</Button><Button variant="ghost" disabled={closingId === problem.id} onClick={() => setConfirmingCloseId("")}>Отмена</Button></> : <Button variant="secondary" onClick={() => setConfirmingCloseId(problem.id)}>Завершить</Button>}</div></Card>)}</div>}

    {(error || resolvedError || openError) && <div className="inline-error" role="alert"><span>{openError || resolvedError || "Не удалось загрузить ситуации."}</span><button onClick={() => { if (filter === "resolved") { resolvedRequested.current = false; setResolvedLoaded(false); setResolvedError(""); } else { void reload(); } }}>Повторить</button></div>}
    {!loading && !resolvedLoading && !visible.length && <EmptyState title={filter === "resolved" ? "Решённых ситуаций пока нет" : "В этом разделе всё спокойно"} description={filter === "resolved" ? "Здесь появятся ситуации после подтверждённого решения." : "Ventrix продолжает мониторинг и покажет новый риск после проверки контекста."} />}
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
