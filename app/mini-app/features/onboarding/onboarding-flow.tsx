"use client";

import { useEffect, useState, type PropsWithChildren } from "react";

import type { VentrixClientApi } from "../../api/client";
import { AnimatedNumber, Card, Skeleton, StatusBadge } from "../../components/ui";
import { Icon, type IconName } from "../../components/icons";
import type { Bootstrap, ClientSettings, OnboardingStep } from "../../types";
import { ConnectionManager } from "../connections/connection-manager";

type Advance = (step: OnboardingStep, status?: "completed" | "skipped") => Promise<void>;

const stageByStep: Record<OnboardingStep, number> = {
  welcome: 1,
  telegram_connection: 1,
  monitoring_started: 2,
  reports: 3,
  groups: 4,
  notifications: 4,
  mini_guide: 5,
  employees: 5,
  final_review: 5,
  completed: 5,
};

const stages = ["Telegram", "Синхронизация", "Отчёты", "Группы", "Готово"];

export function OnboardingFlow({ api, bootstrap, onAdvance }: {
  api: VentrixClientApi;
  bootstrap: Bootstrap;
  onAdvance: Advance;
}) {
  const step = bootstrap.onboarding.step;
  const tenant = bootstrap.tenant;

  if (step === "welcome") {
    const firstName = tenant.owner_name.trim().split(/\s+/)[0] || "Здравствуйте";
    return <OnboardingFrame step={step} title={`Добро пожаловать, ${firstName}`}>
      <Card className="onboarding-hero onboarding-welcome">
        <div className="welcome-brand"><span className="onboarding-mark">V</span><span>Ventrix для {tenant.name}</span></div>
        <h2>{tenant.welcome.headline}</h2>
        <p className="welcome-message">{tenant.welcome.message}</p>
        <div className="welcome-context">
          <span>Компания</span><strong>{tenant.name}</strong>
          {tenant.niche && <><span>Направление</span><strong>{tenant.niche}</strong></>}
        </div>
        <ul className="welcome-benefits">
          {tenant.welcome.benefits.slice(0, 3).map((benefit) => <li key={benefit}>{benefit}</li>)}
        </ul>
        <p className="setup-duration">Настройка займёт несколько минут. Её можно продолжить позже.</p>
        <button className="primary-action" onClick={() => void onAdvance("telegram_connection")}>Настроить Ventrix</button>
      </Card>
    </OnboardingFrame>;
  }

  if (step === "telegram_connection") {
    return <OnboardingFrame step={step} title="Рабочий Telegram">
      <p className="onboarding-lead">Подключите аккаунт, в котором команда общается с клиентами. Личные рабочие диалоги будут найдены автоматически.</p>
      <ConnectionManager
        api={api}
        connections={bootstrap.connections}
        onboardingStep={step}
        mode="onboarding_connection"
        onOnboardingStep={onAdvance}
        onSkip={() => void onAdvance("monitoring_started", "skipped")}
      />
    </OnboardingFrame>;
  }

  if (step === "monitoring_started") return <InitialSyncStep api={api} initial={bootstrap} onAdvance={onAdvance} />;
  if (step === "reports") return <ReportSetup api={api} onAdvance={onAdvance} />;

  if (step === "groups") {
    return <OnboardingFrame step={step} title="Рабочие группы">
      <Card className="onboarding-explainer">
        <Icon name="message" />
        <div><h2>Личные диалоги уже включены</h2><p>Группы — дополнительный источник. Добавьте ссылку на группу или общую папку Telegram, если backend сможет её открыть.</p></div>
      </Card>
      {bootstrap.connections.length > 0
        ? <ConnectionManager api={api} connections={bootstrap.connections} mode="onboarding_groups" />
        : <Card className="warning-card"><h3>Telegram пока не подключён</h3><p>Группы можно добавить позже в разделе «Источники».</p></Card>}
      <div className="onboarding-actions"><button className="primary-action" onClick={() => void onAdvance("notifications")}>Продолжить</button><button className="text-action" onClick={() => void onAdvance("notifications", "skipped")}>Пропустить этот шаг</button></div>
    </OnboardingFrame>;
  }

  if (step === "notifications") {
    return <OnboardingFrame step={step} title="Ventrix в рабочей группе">
      <Card className="onboarding-hero bot-group-guide">
        <span className="guide-icon"><Icon name="telegram" /></span>
        <h2>Напоминания и сводки — прямо в Telegram</h2>
        <p>Можно добавить клиентского Ventrix bot в рабочую группу и разрешить ему отправлять сообщения. Это канал уведомлений, а не источник мониторинга.</p>
        <ol className="guide-list"><li>Добавьте Ventrix bot в нужную группу.</li><li>Разрешите ему отправлять сообщения.</li><li>Включите уведомления группы в настройках.</li></ol>
        <button className="primary-action" onClick={() => void onAdvance("mini_guide")}>Продолжить</button>
      </Card>
    </OnboardingFrame>;
  }

  if (step === "mini_guide") {
    const capabilities: Array<[IconName, string, string]> = [
      ["clock", "Клиент без ответа", "Покажет незакрытый вопрос, когда команда не ответила вовремя."],
      ["check", "Просроченное обещание", "Напомнит о договорённости, срок которой уже наступил."],
      ["repeat", "Повторное обращение", "Свяжет новый запрос с предыдущим незавершённым разговором."],
      ["alert", "Жалоба", "Выделит недовольство, которое требует реакции руководителя."],
      ["document", "Важный документ", "Не даст потерять счёт, договор или файл, по которому ждут действие."],
      ["briefcase", "Сделка без следующего шага", "Покажет интерес клиента, который остался без продолжения."],
    ];
    return <OnboardingFrame step={step} title="Что Ventrix будет замечать">
      <div className="analysis-showcase">{capabilities.map(([icon, title, text]) => <Card className="analysis-capability" key={title}><span><Icon name={icon} /></span><div><h3>{title}</h3><p>{text}</p></div></Card>)}</div>
      <Card className="onboarding-note"><p>Ventrix учитывает историю разговора и последующие сообщения, чтобы не превращать завершённый диалог в ложную тревогу.</p></Card>
      <button className="primary-action onboarding-next" onClick={() => void onAdvance("employees")}>Продолжить</button>
    </OnboardingFrame>;
  }

  if (step === "employees") {
    const items: Array<[IconName, string, string]> = [
      ["home", "Главная", "Состояние проекта и то, что требует внимания."],
      ["alert", "Ситуации", "Контекст, ответственный, срок и действия."],
      ["report", "Отчёты", "Регулярные итоги по компании и команде."],
      ["team", "Команда", "Сотрудники, их доступ и рабочие аккаунты."],
      ["telegram", "Источники", "Telegram-аккаунты и подключённые группы."],
      ["settings", "Настройки", "Расписание, чувствительность и уведомления."],
    ];
    return <OnboardingFrame step={step} title="Где что находится">
      <div className="quick-guide">{items.map(([icon, title, text]) => <div key={title}><Icon name={icon} /><span><strong>{title}</strong><small>{text}</small></span></div>)}</div>
      <button className="primary-action onboarding-next" onClick={() => void onAdvance("final_review")}>Перейти к проверке</button>
    </OnboardingFrame>;
  }

  if (step === "final_review") return <FinalReview api={api} initial={bootstrap} onAdvance={onAdvance} />;
  return <OnboardingFrame step="completed" title="Открываем панель"><Card><p>Настройка сохранена. При следующем запуске откроется панель проекта.</p></Card></OnboardingFrame>;
}

function InitialSyncStep({ api, initial, onAdvance }: { api: VentrixClientApi; initial: Bootstrap; onAdvance: Advance }) {
  const [snapshot, setSnapshot] = useState(initial);
  const [refreshError, setRefreshError] = useState("");
  useEffect(() => {
    if (!initial.connections.length) return;
    let cancelled = false;
    let timer = 0;
    const refresh = async () => {
      try {
        const value = await api.bootstrap();
        if (!cancelled) { setSnapshot(value); setRefreshError(""); }
        if (!cancelled && value.progress?.status !== "completed") timer = window.setTimeout(refresh, 3000);
      } catch {
        if (!cancelled) { setRefreshError("Не удалось обновить прогресс. Синхронизация продолжает работать в фоне."); timer = window.setTimeout(refresh, 6000); }
      }
    };
    timer = window.setTimeout(refresh, 1200);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [api, initial.connections.length]);
  const progress = snapshot.progress;
  if (!snapshot.connections.length) {
    return <OnboardingFrame step="monitoring_started" title="Можно продолжить без подключения"><Card className="warning-card connection-later"><Icon name="telegram" /><h2>Рабочий Telegram пока не подключён</h2><p>Ventrix пока нечего анализировать. Подключить аккаунт можно в любой момент в разделе «Источники».</p><button className="primary-action" onClick={() => void onAdvance("reports")}>Продолжить настройку</button></Card></OnboardingFrame>;
  }
  const connected = snapshot.connections.some((item) => ["connected", "syncing", "ready"].includes(item.status));
  const percent = progress ? Math.max(0, Math.min(100, progress.percent)) : null;
  return <OnboardingFrame step="monitoring_started" title="Первичная синхронизация">
    <Card className="sync-progress-card">
      <span className="success-check"><Icon name="check" /></span>
      <h2>{connected ? "Telegram подключён" : "Проверяем подключение"}</h2>
      <p>{progress?.status === "completed" ? "Первичная обработка завершена." : "Ventrix собирает диалоги и начинает анализировать рабочий контекст."}</p>
      <div className={`sync-progress ${percent === null ? "indeterminate" : ""}`} role="progressbar" aria-label="Прогресс первичной синхронизации" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined}><i style={percent === null ? undefined : { transform: `scaleX(${percent / 100})` }} /></div>
      <div className="sync-status-line"><strong>{percent === null ? "Ожидаем первые данные" : `${percent}%`}</strong><span>{syncStageLabel(progress?.stage, progress?.status)}</span></div>
      <div className="sync-metrics">
        <Summary label="Найдено диалогов" value={snapshot.dialog_counts.personal ?? progress?.dialogs_total} />
        <Summary label="Диалогов обработано" value={progress?.dialogs_completed} />
        <Summary label="Сообщений собрано" value={progress?.messages_loaded} />
        <Summary label="Первые ситуации" value={numericMetric(progress?.metrics, "problems_created")} />
      </div>
      {refreshError && <p className="inline-warning">{refreshError}</p>}
      <p className="muted-copy">Можно продолжить: синхронизация не остановится и завершится в фоне.</p>
      <button className="primary-action" onClick={() => void onAdvance("reports")}>Продолжить</button>
    </Card>
  </OnboardingFrame>;
}

function ReportSetup({ api, onAdvance }: { api: VentrixClientApi; onAdvance: Advance }) {
  const [settings, setSettings] = useState<ClientSettings | null>(null);
  const [frequency, setFrequency] = useState<"daily" | "weekdays" | "weekly">("weekdays");
  const [time, setTime] = useState("19:00");
  const [timezone, setTimezone] = useState("Europe/Moscow");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const retryLoad = () => {
    setError("");
    void api.settings().then((value) => {
      setSettings(value); setTime(value.daily_report_time.slice(0, 5)); setTimezone(value.timezone);
      setFrequency(value.enabled_days.length === 7 ? "daily" : value.enabled_days.length === 1 ? "weekly" : "weekdays");
    }).catch(() => setError("Не удалось загрузить расписание. Проверьте соединение и повторите."));
  };
  useEffect(() => {
    let cancelled = false;
    void api.settings().then((value) => {
      if (cancelled) return;
      setSettings(value); setTime(value.daily_report_time.slice(0, 5)); setTimezone(value.timezone);
      setFrequency(value.enabled_days.length === 7 ? "daily" : value.enabled_days.length === 1 ? "weekly" : "weekdays");
    }).catch(() => !cancelled && setError("Не удалось загрузить расписание. Проверьте соединение и повторите."));
    return () => { cancelled = true; };
  }, [api]);
  async function save() {
    setBusy(true); setError("");
    try {
      const enabled_days = frequency === "daily" ? [0, 1, 2, 3, 4, 5, 6] : frequency === "weekdays" ? [0, 1, 2, 3, 4] : [0];
      await api.updateSettings({ daily_report_time: time, timezone, enabled_days });
      await onAdvance("groups");
    } catch { setError("Не удалось сохранить расписание. Настройки не потеряны — попробуйте ещё раз."); } finally { setBusy(false); }
  }
  if (!settings && !error) return <OnboardingFrame step="reports" title="Регулярные отчёты"><Card className="control-form"><Skeleton lines={4} /></Card></OnboardingFrame>;
  return <OnboardingFrame step="reports" title="Регулярные отчёты"><Card className="control-form report-onboarding"><div><h2>Когда присылать сводку?</h2><p>Ventrix отправит отчёт только когда появятся новые рабочие сообщения.</p></div><div className="choice-grid" role="radiogroup" aria-label="Частота отчёта">{(["daily", "weekdays", "weekly"] as const).map((value) => <button role="radio" aria-checked={frequency === value} className={frequency === value ? "selected" : ""} key={value} onClick={() => setFrequency(value)}>{value === "daily" ? "Каждый день" : value === "weekdays" ? "По будням" : "Раз в неделю"}</button>)}</div><div className="report-time-fields"><label>Время<input type="time" value={time} onChange={(event) => setTime(event.target.value)} /></label><label>Часовой пояс<select value={timezoneOffset(timezone)} onChange={(event) => setTimezone(offsetTimezone(Number(event.target.value)))}>{UTC_OPTIONS.map((offset) => <option key={offset} value={offset}>{utcLabel(offset)}</option>)}</select><small>Для Москвы — UTC+3</small></label></div>{error && <div className="form-error" role="alert"><span>{error}</span>{!settings && <button onClick={retryLoad}>Повторить</button>}</div>}<button className="primary-action" disabled={busy || !settings} onClick={() => void save()}>{busy ? "Сохраняем расписание…" : "Сохранить и продолжить"}</button></Card></OnboardingFrame>;
}

function FinalReview({ api, initial, onAdvance }: { api: VentrixClientApi; initial: Bootstrap; onAdvance: Advance }) {
  const [snapshot, setSnapshot] = useState(initial);
  const [settings, setSettings] = useState<ClientSettings | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => { let cancelled = false; void Promise.all([api.bootstrap(), api.settings()]).then(([next, nextSettings]) => { if (!cancelled) { setSnapshot(next); setSettings(nextSettings); } }).finally(() => !cancelled && setLoading(false)); return () => { cancelled = true; }; }, [api]);
  const connected = snapshot.connections.some((item) => ["connected", "syncing", "ready"].includes(item.status));
  const syncReady = snapshot.progress?.status === "completed";
  return <OnboardingFrame step="final_review" title="Проверим настройку">
    <Card className="onboarding-complete">
      <span><Icon name="check" /></span><h2>Ventrix готов к работе</h2><p>Все выбранные настройки сохранены на уровне проекта.</p>
      {loading ? <Skeleton lines={5} /> : <div className="final-status">
        <StatusRow label="Рабочий Telegram" value={connected ? "подключён" : "подключите позже"} ok={connected} />
        <StatusRow label="Первичная синхронизация" value={connected ? (syncReady ? "готова" : "идёт в фоне") : "ожидает Telegram"} ok={syncReady} />
        <StatusRow label="Сотрудники" value={String(snapshot.employee_count)} />
        <StatusRow label="Рабочие группы" value={String(snapshot.group_count)} />
        <StatusRow label="Регулярный отчёт" value={settings ? reportSchedule(settings) : "расписание сохранено"} ok={Boolean(settings)} />
        <StatusRow label="Мониторинг" value={connected ? "активен" : "ожидает подключения"} ok={connected} />
      </div>}
      <button className="primary-action" onClick={() => void onAdvance("completed")}>Завершить настройку</button>
      <p className="completion-note">Ventrix продолжит обработку данных в фоне. При следующем открытии вы сразу увидите главную панель.</p>
    </Card>
  </OnboardingFrame>;
}

function Summary({ label, value }: { label: string; value: number | undefined }) { return <div><small>{label}</small><strong>{typeof value === "number" ? <AnimatedNumber value={value} /> : "—"}</strong></div>; }
function StatusRow({ label, value, ok = false }: { label: string; value: string; ok?: boolean }) { return <div><span>{label}</span><StatusBadge tone={ok ? "success" : "neutral"}>{value}</StatusBadge></div>; }
function numericMetric(metrics: Bootstrap["progress"] extends infer _ ? Record<string, number | string | null> | undefined : never, key: string) { const value = metrics?.[key]; return typeof value === "number" ? value : undefined; }
function syncStageLabel(stage?: string, status?: string) { if (status === "completed") return "Готово"; if (!stage) return "Подготавливаем синхронизацию"; if (stage.includes("message")) return "Собираем сообщения"; if (stage.includes("analysis")) return "Анализируем диалоги"; if (stage.includes("dialog")) return "Находим рабочие диалоги"; return "Синхронизация продолжается"; }
function reportSchedule(settings: ClientSettings) { const frequency = settings.enabled_days.length === 7 ? "ежедневно" : settings.enabled_days.length === 1 ? "раз в неделю" : "по будням"; return `${frequency}, ${settings.daily_report_time.slice(0, 5)}`; }

const UTC_OPTIONS = Array.from({ length: 27 }, (_, index) => index - 12);
function offsetTimezone(offset: number) { return offset === 0 ? "Etc/UTC" : `Etc/GMT${offset > 0 ? "-" : "+"}${Math.abs(offset)}`; }
function timezoneOffset(timezone: string) { if (timezone === "Etc/UTC" || timezone === "UTC") return 0; if (timezone === "Europe/Moscow") return 3; const match = timezone.match(/^Etc\/GMT([+-])(\d+)$/); return match ? (match[1] === "-" ? Number(match[2]) : -Number(match[2])) : 3; }
function utcLabel(offset: number) { return `UTC${offset >= 0 ? "+" : ""}${offset}`; }

function OnboardingFrame({ step, title, children }: PropsWithChildren<{ step: OnboardingStep; title: string }>) {
  const stage = stageByStep[step];
  return <main className="onboarding-shell"><header className="onboarding-header"><div className="onboarding-brand"><span className="onboarding-logo">V</span><strong>Ventrix</strong></div><div className="onboarding-stage-copy"><small>Шаг {stage} из 5</small><strong>{stages[stage - 1]}</strong></div><div className="onboarding-progress" role="progressbar" aria-label="Прогресс настройки" aria-valuemin={1} aria-valuemax={5} aria-valuenow={stage}><i style={{ transform: `scaleX(${stage / 5})` }} /></div><h1>{title}</h1></header><section className="onboarding-body onboarding-step-enter" key={step}>{children}</section><footer className="onboarding-security">Данные рабочих сессий защищены шифрованием. Коды подтверждения и пароль 2FA не сохраняются.</footer></main>;
}
