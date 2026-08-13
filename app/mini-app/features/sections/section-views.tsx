"use client";

import { useCallback, useState } from "react";
import type * as React from "react";

import type { VentrixClientApi } from "../../api/client";
import {
  Card,
  EmptyState,
  SectionHeading,
  Skeleton,
  StatusBadge,
} from "../../components/ui";
import { ConnectionManager } from "../connections/connection-manager";
import { useResource } from "../../hooks/use-resource";
import type { ClientSettings, ReportDetail, TabId } from "../../types";

export function ReportsView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.reports(), [api]);
  const { data, loading, error } = useResource(loader);
  const [detail, setDetail] = useState<ReportDetail | null>(null);
  if (detail) {
    const company =
      detail.sections.find((item) => item.key === "company_report")?.data ?? {};
    const employees =
      detail.sections.find((item) => item.key === "employee_report")?.data ??
      {};
    return (
      <>
        <SectionHeading
          eyebrow="РАБОЧАЯ СВОДКА"
          title={`Итоги за ${new Date(detail.period.start).toLocaleDateString("ru-RU")} — ${new Date(detail.period.end).toLocaleDateString("ru-RU")}`}
        />
        <button className="text-action" onClick={() => setDetail(null)}>
          ← Все отчёты
        </button>
        <div className="statistics-grid">
          <Metric
            value={detail.metrics.messages ?? 0}
            label="Сообщений изучено"
          />
          <Metric
            value={detail.metrics.problems ?? 0}
            label="Ситуаций найдено"
          />
          <Metric value={detail.metrics.high ?? 0} label="Высокий приоритет" />
          <Metric
            value={Number(company.resolved_problems ?? 0)}
            label="Решено"
          />
        </div>
        <Card>
          <h3>Итог по компании</h3>
          <div className="summary-rows">
            <ReportRow
              label="Открытые ситуации"
              value={company.unresolved_problems}
            />
            <ReportRow
              label="Открытые обещания"
              value={company.open_commitments}
            />
            <ReportRow
              label="Клиенты и рабочие диалоги"
              value={company.clients}
            />
            <ReportRow label="Активные группы" value={company.active_groups} />
          </div>
        </Card>
        <Card>
          <h3>Команда</h3>
          <ReportObject value={employees} />
        </Card>
      </>
    );
  }
  return (
    <>
      <SectionHeading
        eyebrow="ОТЧЁТЫ"
        title="Рабочие сводки"
        description="Итоги компании и сотрудников. Одинаковые и пустые технические отчёты скрыты."
      />
      {loading ? (
        <Skeleton lines={4} />
      ) : data?.length ? (
        <div className="section-list">
          {data.map((item) => (
            <button
              className="section-button"
              key={item.id}
              onClick={() => void api.report(item.id).then(setDetail)}
            >
              <Card className="report-card">
                <div>
                  <StatusBadge tone="success">Готов</StatusBadge>
                  <small>
                    {new Date(
                      item.period_end ?? item.period_start ?? "",
                    ).toLocaleDateString("ru-RU")}
                  </small>
                </div>
                <h3>Рабочая сводка</h3>
                <p>
                  {item.summary
                    .replace("Обработано сообщений", "Изучено сообщений")
                    .replace("Проблем", "Ситуаций")}
                </p>
                <span>Открыть подробности →</span>
              </Card>
            </button>
          ))}
        </div>
      ) : (
        <EmptyState
          title="Сводок пока нет"
          description="Ventrix создаст отчёт, когда появятся новые рабочие сообщения."
        />
      )}
      {error && <p className="form-error">{error}</p>}
    </>
  );
}

function Metric({ value, label }: { value: number; label: string }) {
  return (
    <Card>
      <strong className="report-metric">{value.toLocaleString("ru-RU")}</strong>
      <p>{label}</p>
    </Card>
  );
}
function ReportRow({ label, value }: { label: string; value: unknown }) {
  return (
    <p>
      <span>{label}</span>
      <strong>{String(value ?? 0)}</strong>
    </p>
  );
}
function ReportObject({ value }: { value: Record<string, unknown> }) {
  const rows = Array.isArray(value.rows)
    ? (value.rows as Array<Record<string, unknown>>)
    : [];
  return rows.length ? (
    <div className="report-employee-list">
      {rows.map((row, index) => (
        <div key={String(row.employee_id ?? index)}>
          <strong>{String(row.name ?? "Сотрудник")}</strong>
          <span>Открытые обещания: {String(row.open_promises ?? 0)}</span>
          <span>Клиенты ждут: {String(row.clients_waiting ?? 0)}</span>
          <span>Решено: {String(row.resolved ?? 0)}</span>
        </div>
      ))}
    </div>
  ) : (
    <p className="muted-copy">Данных по сотрудникам пока нет.</p>
  );
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
  const loader = useCallback(() => api.employees(), [api]);
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
        title="Сотрудники и доступ"
        description="Новый сотрудник добавляется по номеру телефона. Данные профиля определяются после входа в Telegram."
      />
      <button
        className="primary-action section-action"
        onClick={() => setAdding(true)}
      >
        Добавить сотрудника по номеру
      </button>
      {loading ? (
        <Skeleton lines={4} />
      ) : data?.length ? (
        <div className="section-list">
          {data.map((item) => (
            <Card className="employee-card" key={item.id}>
              <div className="person-row">
                <span>{item.name.slice(0, 2).toUpperCase()}</span>
                <div>
                  <h3>{item.name}</h3>
                  <p>
                    {item.telegram_username
                      ? `@${item.telegram_username}`
                      : "username не указан"}{" "}
                    · роль: сотрудник
                  </p>
                </div>
                <StatusBadge tone={item.connection_id ? "success" : "warning"}>
                  {item.connection_id ? "подключён" : "нет сессии"}
                </StatusBadge>
              </div>
              <div
                className={`employee-actions ${deleting === item.id ? "" : "single-action"}`}
              >
                {item.connection_id ? (
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
          ))}
        </div>
      ) : (
        <EmptyState
          title="Сотрудники ещё не добавлены"
          description="Нажмите «Добавить сотрудника по номеру» и подтвердите вход в Telegram."
        />
      )}
      {error && <p className="form-error">{error}</p>}
    </>
  );
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
  async function toggle(id: string, enabled: boolean) {
    await api.updateGroup(id, { notifications_enabled: enabled });
    await reload();
  }
  return (
    <>
      <SectionHeading
        eyebrow="РАБОЧИЕ ГРУППЫ"
        title="Группы и напоминания"
        description="Разрешённые назначения, пороги и cooldown для групповых уведомлений."
      />
      {loading ? (
        <Skeleton />
      ) : data?.length ? (
        <div className="section-list">
          {data.map((item) => (
            <Card key={item.id}>
              <h3>{item.title}</h3>
              <p>
                {item.participants_count ?? 0} участников · порог{" "}
                {item.minimum_criticality}
              </p>
              <label className="inline-check">
                <input
                  type="checkbox"
                  checked={item.notifications_enabled}
                  onChange={(event) =>
                    void toggle(item.id, event.target.checked)
                  }
                />{" "}
                Уведомления включены
              </label>
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState
          title="Группы не подключены"
          description="Добавьте клиентского бота в рабочую группу и зарегистрируйте chat ID."
        />
      )}
      {error && <p className="form-error">{error}</p>}
    </>
  );
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
  const patch = <K extends keyof ClientSettings>(
    key: K,
    next: ClientSettings[K],
  ) => setValue((current) => ({ ...current, [key]: next }));
  async function save() {
    try {
      saved(
        await api.updateSettings({
          timezone: value.timezone,
          daily_report_time: value.daily_report_time,
          analysis_enabled: value.analysis_enabled,
          history_window_days: value.history_window_days,
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
        }),
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Не удалось сохранить");
    }
  }
  const controls = [
    {
      key: "signal_problem_threshold" as const,
      title: "Создавать рабочую ситуацию",
      note: "Чем выше значение, тем строже Ventrix отсеивает сомнительные случаи.",
    },
    {
      key: "notification_immediate_threshold" as const,
      title: "Присылать срочно",
      note: "Ситуации выше этого уровня сразу приходят в Telegram.",
    },
    {
      key: "employee_notification_threshold" as const,
      title: "Уведомлять сотрудника",
      note: "Минимальная важность для персонального уведомления ответственному.",
    },
  ];
  return (
    <div className="section-list">
      <Card className="control-form">
        <h3>Регулярная сводка</h3>
        <p className="settings-note">
          Отчёт формируется только при наличии новых рабочих сообщений.
        </p>
        <label>
          Время отправки
          <input
            type="time"
            value={value.daily_report_time.slice(0, 5)}
            onChange={(event) => patch("daily_report_time", event.target.value)}
          />
        </label>
        <label>
          Часовой пояс
          <select
            value={timezoneOffset(value.timezone)}
            onChange={(event) =>
              patch("timezone", offsetTimezone(Number(event.target.value)))
            }
          >
            {UTC_OPTIONS.map((offset) => (
              <option key={offset} value={offset}>
                {utcLabel(offset)}
              </option>
            ))}
          </select>
          <small>Выберите разницу с UTC. Для Москвы — UTC+3.</small>
        </label>
        <label>
          Период анализа
          <select
            value={value.history_window_days}
            onChange={(event) =>
              patch("history_window_days", Number(event.target.value))
            }
          >
            <option value="7">Последние 7 дней</option>
            <option value="14">Последние 14 дней</option>
            <option value="30">Последние 30 дней</option>
          </select>
        </label>
        <label className="inline-check">
          <input
            type="checkbox"
            checked={value.analysis_enabled}
            onChange={(event) =>
              patch("analysis_enabled", event.target.checked)
            }
          />{" "}
          Регулярный анализ включён
        </label>
      </Card>
      <Card className="control-form threshold-settings">
        <h3>Чувствительность</h3>
        <p className="settings-note">
          Чем выше значение, тем строже отбор и тем меньше сомнительных
          уведомлений.
        </p>
        {controls.map((control) => (
          <label key={control.key}>
            <span>
              {control.title}
              <strong>{value[control.key]}/100</strong>
            </span>
            <small>{control.note}</small>
            <input
              type="range"
              min="30"
              max="95"
              value={value[control.key]}
              style={
                {
                  "--range-progress": `${((value[control.key] - 30) / 65) * 100}%`,
                } as React.CSSProperties
              }
              onChange={(event) =>
                patch(control.key, Number(event.target.value))
              }
            />
          </label>
        ))}
        <label className="inline-check">
          <input
            type="checkbox"
            checked={value.employee_notifications_enabled}
            onChange={(event) =>
              patch("employee_notifications_enabled", event.target.checked)
            }
          />{" "}
          Уведомлять ответственных сотрудников
        </label>
        <label className="inline-check">
          <input
            type="checkbox"
            checked={value.group_reminders_enabled}
            onChange={(event) =>
              patch("group_reminders_enabled", event.target.checked)
            }
          />{" "}
          Разрешить напоминания в рабочих группах
        </label>
      </Card>
      {error && <p className="form-error">{error}</p>}
      <button className="primary-action" onClick={() => void save()}>
        Сохранить настройки
      </button>
    </div>
  );
}

export function SettingsView({ api }: { api: VentrixClientApi }) {
  const loader = useCallback(() => api.settings(), [api]);
  const { data, loading, error, reload } = useResource(loader);
  return (
    <>
      <SectionHeading
        eyebrow="НАСТРОЙКИ"
        title="Расписание и уведомления"
        description="Настройте время сводки и насколько строго Ventrix отбирает рабочие ситуации."
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
      {error && <p className="form-error">{error}</p>}
    </>
  );
}

export function MoreView({ onNavigate }: { onNavigate: (tab: TabId) => void }) {
  const items: Array<[TabId, string, string]> = [
    ["commitments", "Обязательства", "Обещания и дедлайны"],
    ["reports", "Отчёты", "Consolidated-сводки"],
    ["connections", "Telegram-аккаунты", "Сессии и папки анализа"],
    ["groups", "Рабочие группы", "Группы и напоминания"],
    ["settings", "Настройки", "Политика и расписание"],
  ];
  return (
    <>
      <SectionHeading eyebrow="ЕЩЁ" title="Управление проектом" />
      <div className="more-grid">
        {items.map(([id, title, note]) => (
          <button key={id} onClick={() => onNavigate(id)}>
            <span>→</span>
            <strong>{title}</strong>
            <small>{note}</small>
          </button>
        ))}
      </div>
    </>
  );
}
