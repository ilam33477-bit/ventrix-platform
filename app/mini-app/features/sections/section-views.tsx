"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type * as React from "react";

import type { VentrixClientApi } from "../../api/client";
import {
  AnimatedNumber,
  Button,
  Card,
  EmptyState,
  SectionHeading,
  Skeleton,
  StatusBadge,
} from "../../components/ui";
import { Icon } from "../../components/icons";
import type { IconName } from "../../components/icons";
import { ConnectionManager } from "../connections/connection-manager";
import { useResource } from "../../hooks/use-resource";
import type { ClientSettings, ReportDetail, TabId } from "../../types";

export function ReportsView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.reports(), [api]);
  const { data, loading, error, reload } = useResource(loader);
  const [detail, setDetail] = useState<ReportDetail | null>(null);
  const [openingId, setOpeningId] = useState("");
  const [detailError, setDetailError] = useState("");
  const reports = useMemo(
    () => [...(data ?? [])].sort((left, right) => reportTimestamp(right) - reportTimestamp(left)),
    [data],
  );

  async function openReport(reportId: string) {
    setOpeningId(reportId);
    setDetailError("");
    try {
      setDetail(await api.report(reportId));
    } catch (cause) {
      setDetailError(cause instanceof Error ? cause.message : "Не удалось открыть сводку");
    } finally {
      setOpeningId("");
    }
  }

  if (detail) {
    const company =
      detail.sections.find((item) => item.key === "company_report")?.data ?? {};
    const employees =
      detail.sections.find((item) => item.key === "employee_report")?.data ??
      {};
    const clients =
      detail.sections.find((item) => item.key === "client_report")?.data ?? {};
    const recommendations =
      detail.sections.find((item) => item.key === "recommendations")?.data ?? {};
    const narrative =
      detail.sections.find((item) => item.key === "ai_narrative")?.data ?? {};
    const headlineMetrics = reportMetrics(detail, company);
    return (
      <section className="report-detail-view">
        <button className="text-action section-back" onClick={() => setDetail(null)}>
          ← Все сводки
        </button>
        <Card className="report-detail-hero">
          <div className="report-detail-heading">
            <StatusBadge tone="success">Готова</StatusBadge>
            <small>{formatReportPeriod(detail.period.start, detail.period.end)}</small>
          </div>
          <h2>Итоги работы команды</h2>
          <p>{humanReportSummary(detail.summary)}</p>
        </Card>
        {headlineMetrics.length > 0 && (
          <div className="report-metric-strip">
            {headlineMetrics.map((item) => (
              <Metric key={item.label} value={item.value} label={item.label} />
            ))}
          </div>
        )}
        <ReportNarrative value={narrative} />
        <div className="report-detail-grid">
          <Card className="report-summary-panel">
            <h3>Компания</h3>
            <p>Состояние рабочих ситуаций и обязательств за выбранный период.</p>
            <div className="summary-rows">
              <ReportRow label="Открытые ситуации" value={company.unresolved_problems} />
              <ReportRow label="Решено" value={company.resolved_problems} />
              <ReportRow label="Открытые обещания" value={company.open_commitments} />
              <ReportRow label="Рабочие диалоги" value={company.clients} />
              <ReportRow label="Активные группы" value={company.active_groups} />
            </div>
          </Card>
          <Card className="report-summary-panel">
            <h3>Команда</h3>
            <p>Только показатели, которые рассчитаны в этой сводке.</p>
            <ReportEmployees value={employees} />
          </Card>
        </div>
        <ReportClients value={clients} />
        <ReportRecommendations value={recommendations} />
      </section>
    );
  }
  const latest = reports[0];
  const previous = reports.slice(1);
  return (
    <section className="reports-view">
      <SectionHeading
        eyebrow="ОТЧЁТЫ"
        title="Рабочие сводки"
        description="Периодические итоги по рабочим ситуациям, обязательствам и команде. Пустые технические запуски здесь не показываются."
      />
      {loading ? (
        <div className="report-loading"><Skeleton lines={4} /></div>
      ) : latest ? (
        <>
          <button
            className="section-button featured-report-button"
            disabled={openingId === latest.id}
            onClick={() => void openReport(latest.id)}
          >
            <Card className="featured-report">
              <div className="featured-report-mark"><Icon name="report" /></div>
              <div className="featured-report-copy">
                <div><StatusBadge tone="success">Последняя сводка</StatusBadge><small>{reportDate(latest)}</small></div>
                <h3>Итоги последнего периода</h3>
                <p>{humanReportSummary(latest.summary)}</p>
                <strong>{openingId === latest.id ? "Открываем…" : "Посмотреть итоги"}<span>→</span></strong>
              </div>
            </Card>
          </button>
          {previous.length > 0 && (
            <section className="report-archive">
              <header><h3>Предыдущие сводки</h3><span>{previous.length}</span></header>
              <div className="report-timeline">
                {previous.map((item) => (
                  <button key={item.id} disabled={openingId === item.id} onClick={() => void openReport(item.id)}>
                    <i aria-hidden="true" />
                    <span><strong>{reportDate(item)}</strong><small>{humanReportSummary(item.summary)}</small></span>
                    <b>{openingId === item.id ? "…" : "→"}</b>
                  </button>
                ))}
              </div>
            </section>
          )}
        </>
      ) : (
        <EmptyState
          title="Сводок пока нет"
          description="Ventrix создаст отчёт, когда появятся новые рабочие сообщения."
        />
      )}
      {(error || detailError) && <div className="inline-error"><p>{detailError || error}</p><Button onClick={() => void reload()}>Повторить</Button></div>}
    </section>
  );
}

function ReportNarrative({ value }: { value: Record<string, unknown> }) {
  const highlights = Array.isArray(value.highlights) ? value.highlights : [];
  const risks = Array.isArray(value.risks) ? value.risks : [];
  const employeeNotes = Array.isArray(value.employee_notes) ? value.employee_notes as Array<Record<string, unknown>> : [];
  if (!highlights.length && !risks.length && !employeeNotes.length) return null;
  return (
    <Card className="report-list-panel report-narrative-panel">
      <header><div><h3>Управленческий вывод</h3><p>Факты и наблюдения по рабочим перепискам за период.</p></div></header>
      {highlights.length > 0 && <div><strong>Главное</strong><ul>{highlights.map((item, index) => <li key={`h-${index}`}>{String(item)}</li>)}</ul></div>}
      {risks.length > 0 && <div><strong>Требует внимания</strong><ul>{risks.map((item, index) => <li key={`r-${index}`}>{String(item)}</li>)}</ul></div>}
      {employeeNotes.length > 0 && <div><strong>По сотрудникам</strong><ul>{employeeNotes.map((item, index) => <li key={String(item.employee_id ?? index)}><b>{String(item.name ?? "Сотрудник")}:</b> {String(item.summary ?? "")}</li>)}</ul></div>}
    </Card>
  );
}

function Metric({ value, label }: { value: number; label: string }) {
  return (
    <Card className="report-metric-card">
      <strong className="report-metric"><AnimatedNumber value={value} /></strong>
      <p>{label}</p>
    </Card>
  );
}
function ReportRow({ label, value }: { label: string; value: unknown }) {
  if (value === undefined || value === null) return null;
  return (
    <p>
      <span>{label}</span>
      <strong>{String(value ?? 0)}</strong>
    </p>
  );
}
function ReportEmployees({ value }: { value: Record<string, unknown> }) {
  const rawRows = value.employees ?? value.rows;
  const rows = Array.isArray(rawRows)
    ? (rawRows as Array<Record<string, unknown>>)
    : [];
  return rows.length ? (
    <div className="report-employee-list">
      {rows.map((row, index) => (
        <div key={String(row.employee_id ?? index)}>
          <strong>{String(row.name ?? "Сотрудник")}</strong>
          <ReportRow label="Открытые обещания" value={row.open_promises} />
          <ReportRow label="Клиенты ждут ответа" value={row.clients_waiting} />
          <ReportRow label="Пропущенные сроки" value={row.missed_deadlines} />
          <ReportRow label="Решено" value={row.resolved} />
        </div>
      ))}
    </div>
  ) : (
    <p className="muted-copy">Данных по сотрудникам пока нет.</p>
  );
}

function ReportClients({ value }: { value: Record<string, unknown> }) {
  const rows = Array.isArray(value.dialogs) ? value.dialogs as Array<Record<string, unknown>> : [];
  if (!rows.length) return null;
  return <Card className="report-list-panel"><header><div><h3>Рабочие диалоги</h3><p>Диалоги, где в периоде были открытые вопросы или обязательства.</p></div><StatusBadge tone="neutral">{rows.length}</StatusBadge></header><div>{rows.map((row, index) => <div className="report-client-row" key={String(row.dialog_id ?? index)}><strong>{String(row.title ?? "Диалог")}</strong><span>{Number(row.unresolved_problems ?? 0)} ситуаций · {Number(row.open_commitments ?? 0)} обещаний</span></div>)}</div></Card>;
}

function ReportRecommendations({ value }: { value: Record<string, unknown> }) {
  const items = Array.isArray(value.items) ? value.items.filter((item): item is string => typeof item === "string" && item.trim().length > 0) : [];
  if (!items.length) return null;
  return <Card className="report-list-panel"><header><div><h3>Следующие действия</h3><p>Рекомендации по найденным ситуациям.</p></div></header><ol className="report-recommendations">{items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ol></Card>;
}

function reportTimestamp(report: { period_end?: string; period_start?: string; created_at?: string }) {
  const value = report.period_end ?? report.created_at ?? report.period_start;
  const timestamp = value ? new Date(value).getTime() : 0;
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function reportDate(report: { period_end?: string; period_start?: string; created_at?: string }) {
  const value = report.period_end ?? report.created_at ?? report.period_start;
  return value ? new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" }).format(new Date(value)) : "Дата не указана";
}

function formatReportPeriod(start: string, end: string) {
  const format = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", year: "numeric" });
  return `${format.format(new Date(start))} — ${format.format(new Date(end))}`;
}

function humanReportSummary(summary: string) {
  return summary
    .replace("Обработано сообщений", "Изучено сообщений")
    .replace("Проблем", "Рабочих ситуаций");
}

function reportMetrics(detail: ReportDetail, company: Record<string, unknown>) {
  const candidates = [
    ["messages", "Сообщений изучено"],
    ["problems", "Ситуаций найдено"],
    ["high", "Высокий приоритет"],
  ] as const;
  const rows: Array<{ label: string; value: number }> = candidates.flatMap(([key, label]) => typeof detail.metrics[key] === "number" ? [{ label, value: detail.metrics[key] }] : []);
  if (typeof company.resolved_problems === "number") rows.push({ label: "Решено", value: company.resolved_problems });
  return rows;
}

const UTC_OPTIONS = Array.from({ length: 27 }, (_, index) => index - 12);
function offsetTimezone(offset: number) {
  return offset === 0
    ? "Etc/UTC"
    : `Etc/GMT${offset > 0 ? "-" : "+"}${Math.abs(offset)}`;
}
function timezoneOffset(timezone: string) {
  if (timezone === "Etc/UTC" || timezone === "UTC") return 0;
  if (timezone === "Europe/Moscow") return 3;
  const match = timezone.match(/^Etc\/GMT([+-])(\d+)$/);
  return match ? (match[1] === "-" ? Number(match[2]) : -Number(match[2])) : 3;
}
function utcLabel(offset: number) {
  return `UTC${offset >= 0 ? "+" : ""}${offset}`;
}

export function EmployeesView({ api, canManage = true, onOpenGroups }: { api: VentrixClientApi; canManage?: boolean; onOpenGroups?: () => void }) {
  const loader = useCallback(async () => {
    const [employees, connections] = await Promise.all([
      api.employees(), api.connections(),
    ]);
    return { employees, connections };
  }, [api]);
  const { data, loading, error, reload } = useResource(loader);
  const [adding, setAdding] = useState(false);
  const [expandedEmployeeId, setExpandedEmployeeId] = useState<string | null>(null);
  const [connecting, setConnecting] = useState<{
    id: string;
    name: string;
  } | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  async function remove(employeeId: string) {
    if (deleting !== employeeId) {
      setDeleting(employeeId);
      return;
    }
    await api.deleteEmployee(employeeId);
    setDeleting(null);
    await reload();
  }
  if (adding)
    return (
      <>
        <SectionHeading
          eyebrow="КОМАНДА"
          title="Новый сотрудник"
          description="Имя, username и Telegram ID будут получены из подтверждённой сессии."
        />
        <button
          className="text-action section-back"
          onClick={() => setAdding(false)}
        >
          ← Вернуться к сотрудникам
        </button>
        <ConnectionManager
          api={api}
          connections={[]}
          createEmployee
          onComplete={async () => {
            await reload();
            setAdding(false);
          }}
        />
      </>
    );
  if (connecting)
    return (
      <>
        <SectionHeading
          eyebrow="КОМАНДА"
          title={`Telegram · ${connecting.name}`}
          description="Код придёт в официальный служебный чат подключаемого аккаунта."
        />
        <button
          className="text-action section-back"
          onClick={() => {
            setConnecting(null);
            void reload();
          }}
        >
          ← Вернуться к сотрудникам
        </button>
        <ConnectionManager
          api={api}
          connections={[]}
          assignedEmployee={connecting}
          onComplete={async () => {
            await reload();
            setConnecting(null);
          }}
        />
      </>
    );
  return (
    <>
      <SectionHeading
        eyebrow="КОМАНДА"
        title="Команда и ответственность"
        description="Кто подключён к мониторингу, какие ситуации закреплены за сотрудниками и кому приходят уведомления."
      />
      {canManage && <div className="team-management-actions"><button className="primary-action section-action" onClick={() => setAdding(true)}>Добавить сотрудника по номеру</button><button className="secondary-action section-action" onClick={onOpenGroups}>Добавить или настроить рабочую группу</button></div>}
      {loading ? (
        <Skeleton lines={4} />
      ) : data?.employees.length ? (
        <div className="team-roster">
          <div className="team-overview">
            <div><strong><AnimatedNumber value={data.employees.length} /></strong><span>сотрудников</span></div>
            <div><strong><AnimatedNumber value={data.employees.reduce((sum, item) => sum + (item.active_problem_count ?? 0), 0)} /></strong><span>ситуаций в работе</span></div>
          </div>
          <div className="team-list">
          {data.employees.map((item) => {
            const connection = data.connections.find((row) => row.id === item.connection_id || row.employee_id === item.id);
            const assignedProblemCount = item.active_problem_count ?? 0;
            const openCommitmentCount = item.open_commitment_count ?? 0;
            const expanded = expandedEmployeeId === item.id;
            return (
            <Card className={`employee-card${expanded ? " expanded" : ""}`} key={item.id}>
              <div className="person-row">
                <span>{item.name.slice(0, 2).toUpperCase()}</span>
                <div>
                  <h3>{item.name}</h3>
                  <p>
                    {item.telegram_username
                      ? `@${item.telegram_username}`
                      : "Telegram-профиль ещё не определён"}
                  </p>
                </div>
                <StatusBadge tone={connection ? connectionTone(connection.status) : "warning"}>
                  {connection ? connectionStatusLabel(connection.status) : "Не подключён"}
                </StatusBadge>
              </div>
              <div className="employee-card-glance">
                <span><strong>{assignedProblemCount}</strong> в работе</span>
                <span><strong>{openCommitmentCount}</strong> обещаний</span>
              </div>
              <div className="employee-meta">
                <span><small>Доступ</small><strong>{accessStatusLabel(item.access_status ?? item.status)}</strong></span>
                <span><small>Уведомления</small><strong>{item.notifications_enabled ? "Включены" : "Выключены"}</strong></span>
                <span><small>Бот проекта</small><strong>{item.bot_started ? "Запущен" : "Ещё не открыт"}</strong></span>
              </div>
              <button
                type="button"
                className="employee-settings-toggle"
                aria-expanded={expanded}
                aria-controls={`employee-settings-${item.id}`}
                onClick={() => setExpandedEmployeeId(expanded ? null : item.id)}
              >
                <span>{expanded ? "Свернуть настройки сессии" : "Настройки сессии"}</span>
                <i aria-hidden="true" />
              </button>
              <div
                className="employee-card-details"
                id={`employee-settings-${item.id}`}
                hidden={!expanded}
              >
                {connection && canManage && (
                  <ConnectionAnalysisControls
                    key={`${connection.id}-${connection.response_sla_minutes}-${connection.signal_problem_threshold}`}
                    responseMinutes={connection.response_sla_minutes}
                    problemThreshold={connection.signal_problem_threshold}
                    onCommit={(value) => api.updateConnectionAnalysisSettings(connection.id, value)}
                  />
                )}
                {canManage && <label className="toggle-row employee-report-access"><span><strong>Все отчёты проекта</strong><small>По умолчанию сотрудник видит только собственные данные.</small></span><input type="checkbox" checked={item.reports_access_all} onChange={async (event) => { await api.updateEmployee(item.id, { reports_access_all: event.target.checked }); await reload(); }} /></label>}
                {canManage && <div
                  className={`employee-actions ${deleting === item.id ? "" : "single-action"}`}
                >
                  {connection ? (
                    <button
                      className="danger-action"
                      onClick={() => void remove(item.id)}
                    >
                      {deleting === item.id
                        ? "Подтвердить удаление"
                        : "Удалить сотрудника и сессию"}
                    </button>
                  ) : (
                    <button
                      className="primary-action"
                      onClick={() =>
                        setConnecting({ id: item.id, name: item.name })
                      }
                    >
                      Подключить Telegram
                    </button>
                  )}
                  {deleting === item.id && (
                    <button onClick={() => setDeleting(null)}>Отмена</button>
                  )}
                </div>}
              </div>
            </Card>
          );})}
          </div>
        </div>
      ) : (
        <EmptyState
          title="Сотрудники ещё не добавлены"
          description="Нажмите «Добавить сотрудника по номеру» и подтвердите вход в Telegram."
        />
      )}
      {error && <div className="inline-error"><p>{error}</p><Button onClick={() => void reload()}>Повторить</Button></div>}
    </>
  );
}

type ConnectionAnalysisValue = {
  response_sla_minutes: number;
  signal_problem_threshold: number;
};

function sameConnectionAnalysisValue(
  left: ConnectionAnalysisValue,
  right: ConnectionAnalysisValue,
) {
  return (
    left.response_sla_minutes === right.response_sla_minutes &&
    left.signal_problem_threshold === right.signal_problem_threshold
  );
}

function ConnectionAnalysisControls({
  responseMinutes: initialResponseMinutes,
  problemThreshold: initialProblemThreshold,
  onCommit,
}: {
  responseMinutes: number;
  problemThreshold: number;
  onCommit: (value: ConnectionAnalysisValue) => Promise<unknown>;
}) {
  const [responseMinutes, setResponseMinutes] = useState(initialResponseMinutes);
  const [problemThreshold, setProblemThreshold] = useState(initialProblemThreshold);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const latestValue = useRef<ConnectionAnalysisValue>({
    response_sla_minutes: initialResponseMinutes,
    signal_problem_threshold: initialProblemThreshold,
  });
  const savedValue = useRef<ConnectionAnalysisValue>({
    response_sla_minutes: initialResponseMinutes,
    signal_problem_threshold: initialProblemThreshold,
  });
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveInFlight = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (saveTimer.current) clearTimeout(saveTimer.current);
    };
  }, []);

  function scheduleSave(delay = 850) {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => void flushSave(), delay);
  }

  async function flushSave() {
    saveTimer.current = null;
    if (saveInFlight.current) {
      scheduleSave(300);
      return;
    }
    const value = { ...latestValue.current };
    if (sameConnectionAnalysisValue(value, savedValue.current)) return;
    saveInFlight.current = true;
    if (mounted.current) {
      setSaving(true);
      setError("");
    }
    try {
      await onCommit(value);
      savedValue.current = value;
    } catch (cause) {
      if (mounted.current) {
        setError(cause instanceof Error ? cause.message : "Не удалось сохранить настройки");
      }
    } finally {
      saveInFlight.current = false;
      if (mounted.current) setSaving(false);
      if (!sameConnectionAnalysisValue(latestValue.current, value)) scheduleSave(500);
    }
  }

  function updateValue(value: ConnectionAnalysisValue) {
    latestValue.current = value;
    setResponseMinutes(value.response_sla_minutes);
    setProblemThreshold(value.signal_problem_threshold);
    setError("");
    scheduleSave();
  }

  return (
    <div className="connection-analysis-controls" aria-label="Настройки анализа сессии">
      <label>
        <span><strong>Время на ответ</strong><b>{responseMinutes} мин.</b></span>
        <small>Когда отсутствие ответа становится рабочей ситуацией для этой сессии.</small>
        <input
          type="range"
          min="15"
          max="180"
          step="1"
          value={responseMinutes}
          aria-valuetext={`${responseMinutes} минут`}
          style={{ "--range-progress": `${((responseMinutes - 15) / 165) * 100}%` } as React.CSSProperties}
          onChange={(event) =>
            updateValue({
              response_sla_minutes: Number(event.target.value),
              signal_problem_threshold: problemThreshold,
            })
          }
        />
      </label>
      <label>
        <span><strong>Порог создания ситуации</strong><b>{problemThreshold}/100</b></span>
        <small>Чем выше значение, тем строже Ventrix отсеивает сомнительные случаи.</small>
        <input
          type="range"
          min="30"
          max="95"
          value={problemThreshold}
          aria-valuetext={`${problemThreshold} из 100`}
          style={{ "--range-progress": `${((problemThreshold - 30) / 65) * 100}%` } as React.CSSProperties}
          onChange={(event) =>
            updateValue({
              response_sla_minutes: responseMinutes,
              signal_problem_threshold: Number(event.target.value),
            })
          }
        />
      </label>
      <div className="connection-analysis-status" aria-live="polite">
        {error ? <span className="error-text">{error}</span> : saving ? "Сохраняем…" : "Настройки этой сессии"}
      </div>
    </div>
  );
}

function accessStatusLabel(status: string | null | undefined) {
  if (status === "active") return "Активен";
  if (status === "pending") return "Ожидает подтверждения";
  if (status === "suspended") return "Приостановлен";
  if (status === "inactive") return "Выключен";
  return "Не связан";
}

function formatRelativeDate(value: string) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "Дата не указана";
  return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(date);
}

function connectionStatusLabel(status: string) {
  if (status === "ready") return "Готов";
  if (status === "connected") return "Подключён";
  if (status === "syncing") return "Синхронизация";
  if (status === "reauthorization_required") return "Нужен повторный вход";
  if (status === "awaiting_code") return "Ожидает код";
  if (status === "awaiting_2fa") return "Ожидает 2FA";
  return "Не подключён";
}

function connectionTone(status: string): "success" | "warning" | "danger" | "neutral" {
  if (["ready", "connected"].includes(status)) return "success";
  if (status === "reauthorization_required") return "danger";
  if (["syncing", "awaiting_code", "awaiting_2fa"].includes(status)) return "warning";
  return "neutral";
}

export function CommitmentsView({
  api,
  onOpenProblem,
}: {
  api: VentrixClientApi;
  onOpenProblem: () => void;
}) {
  const loader = useCallback(() => api.commitments(), [api]);
  const { data, loading, error, reload } = useResource(loader);
  async function complete(id: string) {
    await api.updateCommitment(
      id,
      "completed",
      "Подтверждено пользователем в Mini App",
    );
    await reload();
  }
  return (
    <>
      <SectionHeading
        eyebrow="ОБЯЗАТЕЛЬСТВА"
        title="Обещания и сроки"
        description="Открытые и просроченные действия с ответственным и связанной проблемой."
      />
      {loading ? (
        <Skeleton />
      ) : data?.length ? (
        <div className="section-list">
          {data.map((item) => (
            <Card key={item.id}>
              <div className="problem-card-top">
                <StatusBadge
                  tone={item.status === "open" ? "warning" : "success"}
                >
                  {item.status}
                </StatusBadge>
                <small>
                  {item.deadline_at
                    ? new Date(item.deadline_at).toLocaleString("ru-RU")
                    : "без срока"}
                </small>
              </div>
              <h3>{item.expected_action}</h3>
              <p>Уверенность {Math.round(item.confidence * 100)}%</p>
              {item.status === "open" && (
                <button
                  className="primary-action"
                  onClick={() => void complete(item.id)}
                >
                  Отметить выполненным
                </button>
              )}
              {item.linked_problem_id && (
                <button className="text-action" onClick={onOpenProblem}>
                  Открыть связанную проблему
                </button>
              )}
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState
          title="Открытых обязательств нет"
          description="Обещания появятся после анализа коммуникаций."
        />
      )}
      {error && <p className="form-error">{error}</p>}
    </>
  );
}

export function GroupsView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.groups(), [api]);
  const { data, loading, error, reload } = useResource(loader);
  const [savingId, setSavingId] = useState("");
  const [actionError, setActionError] = useState("");
  async function update(id: string, value: { notifications_enabled?: boolean; minimum_criticality?: number }) {
    setSavingId(id);
    setActionError("");
    try {
      await api.updateGroup(id, value);
      await reload();
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : "Не удалось обновить группу");
    } finally {
      setSavingId("");
    }
  }
  return (
    <section className="groups-view">
      <SectionHeading
        eyebrow="РАБОЧИЕ ГРУППЫ"
        title="Группы и напоминания"
        description="Управляйте уведомлениями только в тех группах, которые уже разрешены и подключены к проекту."
      />
      {loading ? (
        <Skeleton lines={3} />
      ) : data?.length ? (
        <div className="group-list">
          {data.map((item) => (
            <Card className="group-card" key={item.id}>
              <header>
                <div className="group-mark"><Icon name="groups" /></div>
                <div><h3>{item.title}</h3><p>{item.participants_count ?? 0} участников</p></div>
                <StatusBadge tone={item.status === "active" ? "success" : "neutral"}>{groupStatusLabel(item.status)}</StatusBadge>
              </header>
              <div className="group-policy">
                <label className="toggle-row">
                  <span><strong>Уведомления в группе</strong><small>Ventrix сможет отправлять сюда разрешённые напоминания.</small></span>
                <input
                  type="checkbox"
                  checked={item.notifications_enabled}
                  disabled={savingId === item.id}
                  onChange={(event) =>
                    void update(item.id, { notifications_enabled: event.target.checked })
                  }
                />
              </label>
                <GroupThreshold key={`${item.id}-${item.minimum_criticality}`} value={item.minimum_criticality} disabled={savingId === item.id} onCommit={(next) => update(item.id, { minimum_criticality: next })} />
                {typeof item.reminder_cooldown_minutes === "number" && <div className="group-cooldown"><span>Интервал между похожими напоминаниями</span><strong>{item.reminder_cooldown_minutes} мин.</strong></div>}
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <Card className="group-connect-guide"><Icon name="groups" /><div><h3>Подключите рабочую группу</h3><ol><li>Добавьте клиентского Ventrix-бота непосредственно в нужную группу.</li><li>Назначьте его администратором и оставьте право отправлять сообщения.</li><li>Напишите в группе команду <code>/ventrix_connect</code>.</li><li>Вернитесь сюда и обновите список.</li></ol><p>Для приватной группы ссылка-приглашение не требуется: одной ссылки недостаточно — бот должен состоять в группе. После подключения здесь можно включить карточки ситуаций и регулярные отчёты.</p></div><Button variant="secondary" onClick={() => void reload()}>Обновить список</Button></Card>
      )}
      {(error || actionError) && <div className="inline-error"><p>{actionError || error}</p><Button onClick={() => void reload()}>Повторить</Button></div>}
    </section>
  );
}

function groupStatusLabel(status: string) {
  if (status === "active") return "Подключена";
  if (status === "pending") return "Ожидает";
  if (status === "disabled") return "Выключена";
  return status;
}

function GroupThreshold({ value: initialValue, disabled, onCommit }: { value: number; disabled: boolean; onCommit: (value: number) => Promise<void> }) {
  const [value, setValue] = useState(initialValue);
  const commit = () => value !== initialValue ? void onCommit(value) : undefined;
  return <label className="group-threshold"><span><strong>Минимальная важность</strong><b>{value}/100</b></span><small>Ниже этого уровня уведомления в группу не отправляются.</small><input type="range" min="30" max="95" value={value} disabled={disabled} style={{ "--range-progress": `${((value - 30) / 65) * 100}%` } as React.CSSProperties} onChange={(event) => setValue(Number(event.target.value))} onPointerUp={commit} onBlur={commit} onKeyUp={(event) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) commit(); }} /></label>;
}

function SettingsForm({
  api,
  settings,
  saved,
}: {
  api: VentrixClientApi;
  settings: ClientSettings;
  saved: (value: ClientSettings) => void;
}) {
  const [value, setValue] = useState(settings);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [success, setSuccess] = useState(false);
  const patch = <K extends keyof ClientSettings>(
    key: K,
    next: ClientSettings[K],
  ) => {
    setSuccess(false);
    setValue((current) => ({ ...current, [key]: next }));
  };
  async function save() {
    setSaving(true);
    setError("");
    try {
      const next = await api.updateSettings({
          timezone: value.timezone,
          daily_report_time: value.daily_report_time,
          analysis_enabled: value.analysis_enabled,
          enabled_days: value.enabled_days,
          history_window_days: value.history_window_days,
          response_sla_minutes: value.response_sla_minutes,
          signal_problem_threshold: value.signal_problem_threshold,
          signal_immediate_threshold: value.signal_immediate_threshold,
          manager_notification_threshold: value.manager_notification_threshold,
          employee_notification_threshold:
            value.employee_notification_threshold,
          group_notification_threshold: value.group_notification_threshold,
          notification_immediate_threshold:
            value.notification_immediate_threshold,
          employee_notifications_enabled: value.employee_notifications_enabled,
          group_reminders_enabled: value.group_reminders_enabled,
        });
      setValue(next);
      setSuccess(true);
      saved(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Не удалось сохранить");
    } finally {
      setSaving(false);
    }
  }
  const weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
  return (
    <div className="settings-workspace">
      <Card className="settings-section schedule-settings">
        <header><div><h3>Регулярная сводка</h3><p>Приходит по расписанию; если событий нет, Ventrix коротко подтвердит продолжение мониторинга.</p></div><StatusBadge tone={value.analysis_enabled ? "success" : "neutral"}>{value.analysis_enabled ? "Включена" : "Выключена"}</StatusBadge></header>
        <div className="settings-fields two-columns">
          <label>Время отправки<input type="time" value={value.daily_report_time.slice(0, 5)} onChange={(event) => patch("daily_report_time", event.target.value)} /></label>
          <label>Часовой пояс<select value={timezoneOffset(value.timezone)} onChange={(event) => patch("timezone", offsetTimezone(Number(event.target.value)))}>{UTC_OPTIONS.map((offset) => <option key={offset} value={offset}>{utcLabel(offset)}</option>)}</select><small>Для Москвы — UTC+3.</small></label>
          <label>Период анализа<select value={value.history_window_days} onChange={(event) => patch("history_window_days", Number(event.target.value))}><option value="7">Последние 7 дней</option><option value="14">Последние 14 дней</option><option value="30">Последние 30 дней</option></select></label>
        </div>
        <fieldset className="weekday-field"><legend>Дни отправки</legend><div>{weekdays.map((day, valueDay) => { const active = value.enabled_days.includes(valueDay); return <button type="button" aria-pressed={active} className={active ? "active" : ""} key={day} onClick={() => patch("enabled_days", active ? value.enabled_days.filter((item) => item !== valueDay) : [...value.enabled_days, valueDay].sort())}>{day}</button>; })}</div></fieldset>
        <label className="toggle-row"><span><strong>Регулярный анализ</strong><small>Останавливает новые плановые проверки, не удаляя уже собранные данные.</small></span><input type="checkbox" checked={value.analysis_enabled} onChange={(event) => patch("analysis_enabled", event.target.checked)} /></label>
        {value.next_analysis_at && <div className="next-analysis"><span>Следующая проверка</span><strong>{formatRelativeDate(value.next_analysis_at)}</strong></div>}
      </Card>
      {error && <p className="form-error">{error}</p>}
      {success && <p className="settings-success"><span>✓</span>Настройки сохранены</p>}
      <Button variant="primary" className="settings-save" disabled={saving || value.enabled_days.length === 0} onClick={() => void save()}>{saving ? "Сохраняем…" : "Сохранить изменения"}</Button>
    </div>
  );
}

export function SettingsView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.settings(), [api]);
  const { data, loading, error, reload } = useResource(loader);
  return (
    <section className="settings-view">
      <SectionHeading
        eyebrow="НАСТРОЙКИ"
        title="Регулярные отчёты"
        description="Расписание отчётов проекта. Время ответа и строгость отбора настраиваются отдельно для каждой рабочей сессии."
      />
      {loading ? (
        <Skeleton />
      ) : data ? (
        <SettingsForm api={api} settings={data} saved={() => void reload()} />
      ) : (
        <EmptyState
          title="Настройки недоступны"
          description="Для этого раздела нужна роль владельца или менеджера."
        />
      )}
      {error && <div className="inline-error"><p>{error}</p><Button onClick={() => void reload()}>Повторить</Button></div>}
    </section>
  );
}

export function MoreView({ onNavigate, canManageProject = true, canReadAllReports = true }: { onNavigate: (tab: TabId) => void; canManageProject?: boolean; canReadAllReports?: boolean }) {
  const items: Array<{ id: TabId; title: string; note: string; icon: IconName; group: "Работа" | "Подключения" | "Проект" }> = [
    { id: "statistics", title: "Статистика", note: "Динамика ситуаций и команды", icon: "chart", group: "Работа" },
    { id: "employees", title: "Команда", note: "Сотрудники и ответственность", icon: "team", group: "Работа" },
    { id: "commitments", title: "Обязательства", note: "Обещания сотрудников и сроки", icon: "alert", group: "Работа" },
    { id: "connections", title: "Telegram-аккаунты", note: "Сессии и источники анализа", icon: "telegram", group: "Подключения" },
    { id: "groups", title: "Рабочие группы", note: "Групповые уведомления", icon: "groups", group: "Подключения" },
    { id: "settings", title: "Настройки проекта", note: "Расписание и правила уведомлений", icon: "settings", group: "Проект" },
  ];
  const visibleItems = items.filter((item) =>
    (canManageProject || !["connections", "groups", "settings"].includes(item.id))
    && (canReadAllReports || item.id !== "reports"),
  );
  return (
    <section className="more-view">
      <SectionHeading eyebrow="РАЗДЕЛЫ" title="Ещё" description="Команда, подключения и настройки проекта." />
      {(["Работа", "Подключения", "Проект"] as const).map((group) => visibleItems.some((item) => item.group === group) && <section className="more-group" key={group}><h3>{group}</h3><div>{visibleItems.filter((item) => item.group === group).map((item) => <button key={item.id} onClick={() => onNavigate(item.id)}><span className="more-icon"><Icon name={item.icon} /></span><span><strong>{item.title}</strong><small>{item.note}</small></span><b>→</b></button>)}</div></section>)}
      <p className="more-profile-note">Профиль, срок активности и тема интерфейса открываются по аватару в правом верхнем углу.</p>
    </section>
  );
}
