"use client";

import { useCallback, useMemo, useState } from "react";
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

export function EmployeesView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(async () => {
    const [employees, problems, commitments, connections] = await Promise.all([
      api.employees(), api.problems(), api.commitments(), api.connections(),
    ]);
    return { employees, problems, commitments, connections };
  }, [api]);
  const { data, loading, error, reload } = useResource(loader);
  const [adding, setAdding] = useState(false);
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
          onComplete={() => {
            setAdding(false);
            void reload();
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
          onComplete={() => {
            setConnecting(null);
            void reload();
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
      <button
        className="primary-action section-action"
        onClick={() => setAdding(true)}
      >
        Добавить сотрудника по номеру
      </button>
      {loading ? (
        <Skeleton lines={4} />
      ) : data?.employees.length ? (
        <div className="team-roster">
          <div className="team-overview">
            <div><strong><AnimatedNumber value={data.employees.length} /></strong><span>сотрудников</span></div>
            <div><strong><AnimatedNumber value={data.connections.filter((item) => ["connected", "syncing", "ready"].includes(item.status)).length} /></strong><span>сессий активно</span></div>
            <div><strong><AnimatedNumber value={data.problems.filter((item) => !["resolved", "auto_resolved", "false_positive", "ignored"].includes(item.status)).length} /></strong><span>ситуаций в работе</span></div>
          </div>
          <div className="team-list">
          {data.employees.map((item) => {
            const connection = data.connections.find((row) => row.id === item.connection_id || row.employee_id === item.id);
            const assignedProblems = data.problems.filter((row) => row.responsible_employee_id === item.id && !["resolved", "auto_resolved", "false_positive", "ignored"].includes(row.status));
            const openCommitments = data.commitments.filter((row) => row.employee_id === item.id && row.status === "open");
            return (
            <Card className="employee-card" key={item.id}>
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
              <div className="employee-workload">
                <div><strong>{assignedProblems.length}</strong><span>ситуаций в работе</span></div>
                <div><strong>{openCommitments.length}</strong><span>открытых обещаний</span></div>
                <div><strong>{item.criticality_threshold}/100</strong><span>порог уведомлений</span></div>
              </div>
              <div className="employee-meta">
                <span><small>Доступ</small><strong>{accessStatusLabel(item.access_status ?? item.status)}</strong></span>
                <span><small>Уведомления</small><strong>{item.notifications_enabled ? "Включены" : "Выключены"}</strong></span>
                {connection?.last_sync_at && <span><small>Синхронизация</small><strong>{formatRelativeDate(connection.last_sync_at)}</strong></span>}
              </div>
              <div
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
        <Card className="group-connect-guide"><Icon name="groups" /><div><h3>Подключите рабочую группу</h3><ol><li>Добавьте клиентского Ventrix-бота в нужную Telegram-группу.</li><li>Выдайте боту право отправлять сообщения.</li><li>Вернитесь сюда и обновите список.</li></ol><p>После подключения здесь можно разрешить персональные уведомления и регулярные отчёты.</p></div><Button variant="secondary" onClick={() => void reload()}>Обновить список</Button></Card>
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
  const detectionControls = [
    {
      key: "signal_problem_threshold" as const,
      title: "Показывать как рабочую ситуацию",
      note: "Чем выше значение, тем меньше сомнительных случаев попадёт в раздел «Проблемы».",
    },
    {
      key: "signal_immediate_threshold" as const,
      title: "Считать ситуацию срочной",
      note: "Уровень, с которого ситуация получает срочный приоритет.",
    },
  ];
  const notificationControls = [
    {
      key: "manager_notification_threshold" as const,
      title: "Уведомлять руководителя",
      note: "Минимальная важность для личного уведомления владельцу или менеджеру.",
    },
    {
      key: "employee_notification_threshold" as const,
      title: "Уведомлять сотрудника",
      note: "Минимальная важность для персонального уведомления ответственному.",
    },
    {
      key: "group_notification_threshold" as const,
      title: "Уведомлять рабочую группу",
      note: "Минимальная важность для разрешённого уведомления в подключённой группе.",
    },
    {
      key: "notification_immediate_threshold" as const,
      title: "Отправлять сразу",
      note: "Ситуации этого уровня не ждут следующей регулярной сводки.",
    },
  ];
  const weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
  return (
    <div className="settings-workspace">
      <Card className="settings-section schedule-settings">
        <header><div><h3>Регулярная сводка</h3><p>Приходит по расписанию; если событий нет, Ventrix коротко подтвердит продолжение мониторинга.</p></div><StatusBadge tone={value.analysis_enabled ? "success" : "neutral"}>{value.analysis_enabled ? "Включена" : "Выключена"}</StatusBadge></header>
        <div className="settings-fields two-columns">
          <label>Время отправки<input type="time" value={value.daily_report_time.slice(0, 5)} onChange={(event) => patch("daily_report_time", event.target.value)} /></label>
          <label>Часовой пояс<select value={timezoneOffset(value.timezone)} onChange={(event) => patch("timezone", offsetTimezone(Number(event.target.value)))}>{UTC_OPTIONS.map((offset) => <option key={offset} value={offset}>{utcLabel(offset)}</option>)}</select><small>Для Москвы — UTC+3.</small></label>
          <label>Период анализа<select value={value.history_window_days} onChange={(event) => patch("history_window_days", Number(event.target.value))}><option value="7">Последние 7 дней</option><option value="14">Последние 14 дней</option><option value="30">Последние 30 дней</option></select></label>
          <label>Время на ответ клиенту<select value={value.response_sla_minutes} onChange={(event) => patch("response_sla_minutes", Number(event.target.value))}><option value="15">15 минут</option><option value="30">30 минут</option><option value="60">60 минут</option><option value="120">120 минут</option><option value="180">180 минут</option></select><small>Через какое время без ответа Ventrix создаст ситуацию.</small></label>
        </div>
        <fieldset className="weekday-field"><legend>Дни работы</legend><div>{weekdays.map((day, index) => { const valueDay = index + 1; const active = value.enabled_days.includes(valueDay); return <button type="button" aria-pressed={active} className={active ? "active" : ""} key={day} onClick={() => patch("enabled_days", active ? value.enabled_days.filter((item) => item !== valueDay) : [...value.enabled_days, valueDay].sort())}>{day}</button>; })}</div></fieldset>
        <label className="toggle-row"><span><strong>Регулярный анализ</strong><small>Останавливает новые плановые проверки, не удаляя уже собранные данные.</small></span><input type="checkbox" checked={value.analysis_enabled} onChange={(event) => patch("analysis_enabled", event.target.checked)} /></label>
        {value.next_analysis_at && <div className="next-analysis"><span>Следующая проверка</span><strong>{formatRelativeDate(value.next_analysis_at)}</strong></div>}
      </Card>
      <Card className="settings-section threshold-settings">
        <header><div><h3>Отбор ситуаций</h3><p>Определяет, насколько строго Ventrix отделяет рабочие риски от обычного диалога.</p></div></header>
        {detectionControls.map((control) => <ThresholdControl key={control.key} control={control} value={value[control.key]} onChange={(next) => patch(control.key, next)} />)}
      </Card>
      <Card className="settings-section threshold-settings">
        <header><div><h3>Кому и когда сообщать</h3><p>Разные получатели могут иметь собственный минимальный уровень важности.</p></div></header>
        {notificationControls.map((control) => <ThresholdControl key={control.key} control={control} value={value[control.key]} onChange={(next) => patch(control.key, next)} />)}
        <div className="settings-toggles">
          <label className="toggle-row"><span><strong>Ответственные сотрудники</strong><small>Отправлять личные уведомления назначенному сотруднику.</small></span><input type="checkbox" checked={value.employee_notifications_enabled} onChange={(event) => patch("employee_notifications_enabled", event.target.checked)} /></label>
          <label className="toggle-row"><span><strong>Рабочие группы</strong><small>Разрешить напоминания в уже подключённых группах.</small></span><input type="checkbox" checked={value.group_reminders_enabled} onChange={(event) => patch("group_reminders_enabled", event.target.checked)} /></label>
        </div>
      </Card>
      {error && <p className="form-error">{error}</p>}
      {success && <p className="settings-success"><span>✓</span>Настройки сохранены</p>}
      <Button variant="primary" className="settings-save" disabled={saving || value.enabled_days.length === 0} onClick={() => void save()}>{saving ? "Сохраняем…" : "Сохранить изменения"}</Button>
    </div>
  );
}

function ThresholdControl<K extends keyof ClientSettings>({ control, value, onChange }: { control: { key: K; title: string; note: string }; value: number; onChange: (value: number) => void }) {
  return <label className="threshold-control"><span><strong>{control.title}</strong><b>{value}/100</b></span><small>{control.note}</small><input type="range" min="30" max="95" value={value} style={{ "--range-progress": `${((value - 30) / 65) * 100}%` } as React.CSSProperties} onChange={(event) => onChange(Number(event.target.value))} /></label>;
}

export function SettingsView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.settings(), [api]);
  const { data, loading, error, reload } = useResource(loader);
  return (
    <section className="settings-view">
      <SectionHeading
        eyebrow="НАСТРОЙКИ"
        title="Правила мониторинга"
        description="Расписание, чувствительность и маршруты уведомлений — без внутренних AI-параметров и технических лимитов."
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

export function MoreView({ onNavigate }: { onNavigate: (tab: TabId) => void }) {
  const items: Array<{ id: TabId; title: string; note: string; icon: IconName; group: "Работа" | "Подключения" | "Проект" }> = [
    { id: "commitments", title: "Обязательства", note: "Обещания сотрудников и сроки", icon: "alert", group: "Работа" },
    { id: "reports", title: "Отчёты", note: "Периодические итоги команды", icon: "report", group: "Работа" },
    { id: "connections", title: "Telegram-аккаунты", note: "Сессии и источники анализа", icon: "telegram", group: "Подключения" },
    { id: "groups", title: "Рабочие группы", note: "Групповые уведомления", icon: "groups", group: "Подключения" },
    { id: "settings", title: "Настройки проекта", note: "Расписание и правила уведомлений", icon: "settings", group: "Проект" },
  ];
  return (
    <section className="more-view">
      <SectionHeading eyebrow="ЕЩЁ" title="Управление проектом" description="Рабочие разделы, которые нужны реже основной панели." />
      {(["Работа", "Подключения", "Проект"] as const).map((group) => <section className="more-group" key={group}><h3>{group}</h3><div>{items.filter((item) => item.group === group).map((item) => <button key={item.id} onClick={() => onNavigate(item.id)}><span className="more-icon"><Icon name={item.icon} /></span><span><strong>{item.title}</strong><small>{item.note}</small></span><b>→</b></button>)}</div></section>)}
      <p className="more-profile-note">Профиль, срок активности и тема интерфейса открываются по аватару в правом верхнем углу.</p>
    </section>
  );
}
