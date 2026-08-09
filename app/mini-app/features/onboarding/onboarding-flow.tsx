"use client";

import { useEffect, useState, type PropsWithChildren } from "react";

import type { VentrixClientApi } from "../../api/client";
import type { Bootstrap, ClientSettings, OnboardingStep } from "../../types";
import { Card, StatusBadge } from "../../components/ui";
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

export function OnboardingFlow({ api, bootstrap, onAdvance }: {
  api: VentrixClientApi;
  bootstrap: Bootstrap;
  onAdvance: Advance;
}) {
  const step = bootstrap.onboarding.step;
  const tenant = bootstrap.tenant;

  if (step === "welcome") {
    return <OnboardingFrame step={step} title={`Добро пожаловать, ${tenant.owner_name.split(" ")[0]}`}>
      <Card className="onboarding-hero onboarding-welcome">
        <span className="onboarding-mark">V</span>
        <h2>Ventrix настроен под {tenant.name}</h2>
        {tenant.owner_username && <p className="welcome-username">@{tenant.owner_username}</p>}
        <div className="profile-summary">
          <Summary label="Компания" value={tenant.name} />
          <Summary label="Ниша" value={tenant.niche} />
          <Summary label="Целевая аудитория" value={tenant.target_audience} />
          <Summary label="Что отслеживаем" value={tenant.monitoring_priorities.slice(0, 3).join(" · ") || "Клиентов без ответа, обещания и важные ситуации"} />
        </div>
        <button className="primary-action" onClick={() => void onAdvance("telegram_connection")}>Начать настройку</button>
      </Card>
    </OnboardingFrame>;
  }

  if (step === "telegram_connection") {
    return <OnboardingFrame step={step} title="Подключите рабочий Telegram">
      <p className="onboarding-lead">Подключите свой рабочий аккаунт или аккаунт сотрудника. Дополнительные аккаунты можно добавить позже.</p>
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

  if (step === "monitoring_started") {
    return <InitialSyncStep api={api} initial={bootstrap} onAdvance={onAdvance} />;
  }

  if (step === "reports") {
    return <ReportSetup api={api} onAdvance={onAdvance} />;
  }

  if (step === "groups") {
    return <OnboardingFrame step={step} title="Рабочие группы">
      <p className="onboarding-lead">Личные рабочие диалоги уже отслеживаются автоматически. Группы можно добавить отдельно.</p>
      {bootstrap.connections.length > 0
        ? <ConnectionManager api={api} connections={bootstrap.connections} mode="onboarding_groups" />
        : <Card><h3>Сначала нужен Telegram-аккаунт</h3><p>Группы можно подключить позже в разделе «Аккаунты».</p></Card>}
      <div className="onboarding-actions"><button className="primary-action" onClick={() => void onAdvance("notifications")}>Продолжить</button><button className="text-action" onClick={() => void onAdvance("notifications", "skipped")}>Пропустить</button></div>
    </OnboardingFrame>;
  }

  if (step === "notifications") {
    return <OnboardingFrame step={step} title="Ventrix bot в рабочей группе">
      <Card className="onboarding-hero">
        <h2>Напоминания там, где работает команда</h2>
        <ol className="guide-list"><li>Добавьте клиентского Ventrix bot в рабочую группу.</li><li>Дайте ему право отправлять сообщения.</li><li>Включите напоминания для группы в настройках.</li></ol>
        <p>Бот сможет напоминать о просроченных обещаниях, клиентах без ответа и важных нерешённых вопросах. Личные сообщения в группу не публикуются.</p>
        <button className="primary-action" onClick={() => void onAdvance("mini_guide")}>Продолжить</button>
      </Card>
    </OnboardingFrame>;
  }

  if (step === "mini_guide") {
    const examples = tenant.monitoring_priorities.length ? tenant.monitoring_priorities.slice(0, 5) : ["Клиент написал и не получил ответ", "Сотрудник пропустил обещанный срок", "Клиент повторно напоминает о себе", "Появилась жалоба", "Сделка осталась без следующего шага"];
    return <OnboardingFrame step={step} title="Что Ventrix будет замечать">
      <div className="section-list">{examples.map((item, index) => <Card className="person-row" key={item}><span>{index + 1}</span><div><h3>{item}</h3></div></Card>)}</div>
      <Card className="onboarding-note"><p>Ventrix анализирует контекст, время, историю диалога и роли участников. Ситуация создаётся только при достаточных основаниях.</p></Card>
      <button className="primary-action onboarding-next" onClick={() => void onAdvance("employees")}>Продолжить</button>
    </OnboardingFrame>;
  }

  if (step === "employees") {
    const items = [["Главная", "Состояние проекта и ключевые показатели"], ["Важное", "Сигналы, которые требуют реакции"], ["Ситуации", "Ответственные, сроки и evidence"], ["Обязательства", "Открытые и просроченные обещания"], ["Отчёты", "Регулярные итоги"], ["Команда", "Сотрудники и доступ"], ["Аккаунты", "Telegram-сессии и группы"]];
    return <OnboardingFrame step={step} title="Короткий гид">
      <div className="guide-carousel">{items.map(([title, text]) => <Card key={title}><h3>{title}</h3><p>{text}</p></Card>)}</div>
      <button className="primary-action onboarding-next" onClick={() => void onAdvance("final_review")}>Понятно</button>
    </OnboardingFrame>;
  }

  if (step === "final_review") {
    return <FinalReview api={api} initial={bootstrap} onAdvance={onAdvance} />;
  }

  return <OnboardingFrame step="completed" title="Открываем панель"><Card><p>Настройка сохранена. При следующем запуске откроется dashboard проекта.</p></Card></OnboardingFrame>;
}

function InitialSyncStep({ api, initial, onAdvance }: { api: VentrixClientApi; initial: Bootstrap; onAdvance: Advance }) {
  const [snapshot, setSnapshot] = useState(initial);
  useEffect(() => {
    if (!initial.connections.length) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void api.bootstrap().then((value) => { if (!cancelled) setSnapshot(value); });
    }, 2500);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [api, initial.connections.length]);
  const progress = snapshot.progress;
  if (!snapshot.connections.length) {
    return <OnboardingFrame step="monitoring_started" title="Telegram можно подключить позже"><Card className="warning-card"><h2>Рабочий Telegram пока не подключён</h2><p>Ventrix пока нечего анализировать. После onboarding откройте раздел «Аккаунты» и подключите первый аккаунт.</p><button className="primary-action" onClick={() => void onAdvance("reports")}>Продолжить</button></Card></OnboardingFrame>;
  }
  return <OnboardingFrame step="monitoring_started" title="Первичная синхронизация">
    <Card className="sync-progress-card"><span className="success-check">✓</span><h2>Подключение готово</h2><div className="sync-progress"><i style={{ width: `${progress?.percent ?? 10}%` }} /></div><p>{progress?.status === "completed" ? "Первичный анализ завершён" : "Загружаем рабочие диалоги и начинаем анализ"}</p><div className="sync-metrics"><Summary label="Личных диалогов" value={String(snapshot.dialog_counts.personal ?? progress?.dialogs_total ?? 0)} /><Summary label="Сообщений собрано" value={String(progress?.messages_loaded ?? 0)} />{typeof progress?.metrics?.signals_created === "number" && <Summary label="Первые сигналы" value={String(progress.metrics.signals_created)} />}{typeof progress?.metrics?.problems_created === "number" && <Summary label="Открытые ситуации" value={String(progress.metrics.problems_created)} />}</div><p className="muted-copy">Ventrix продолжит анализ в фоне. Ждать завершения не нужно.</p><button className="primary-action" onClick={() => void onAdvance("reports")}>Продолжить</button></Card>
  </OnboardingFrame>;
}

function FinalReview({ api, initial, onAdvance }: { api: VentrixClientApi; initial: Bootstrap; onAdvance: Advance }) {
  const [snapshot, setSnapshot] = useState(initial);
  useEffect(() => { void api.bootstrap().then(setSnapshot); }, [api]);
  const connection = snapshot.connections[0];
  return <OnboardingFrame step="final_review" title="Ventrix настроен">
    <Card className="onboarding-complete">
      <span>✓</span><h2>Можно начинать работу</h2>
      <div className="final-status">
        <StatusRow label="Telegram account" value={connection ? connection.status : "не подключён"} ok={Boolean(connection)} />
        <StatusRow label="Первичный анализ" value={snapshot.progress ? `${snapshot.progress.percent}%` : "ожидает подключения"} ok={snapshot.progress?.status === "completed"} />
        <StatusRow label="Сотрудники" value={String(snapshot.employee_count)} />
        <StatusRow label="Группы" value={String(snapshot.group_count)} />
        <StatusRow label="Отчёт" value={reportLabel(snapshot)} ok />
        <StatusRow label="Уведомления" value="включены" ok />
      </div>
      <div className="bot-guide"><h3>Client bot</h3><p>Используйте кнопки «Открыть панель», «Важные ситуации», «Отчёты», «Сотрудники» и «Настройки».</p></div>
      <button className="primary-action" onClick={() => void onAdvance("completed")}>Завершить настройку</button>
      <p>Ventrix продолжит обработку данных в фоне и сообщит, когда первичный анализ завершится.</p>
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
  useEffect(() => { void api.settings().then((value) => { setSettings(value); setTime(value.daily_report_time.slice(0, 5)); setTimezone(value.timezone); if (value.enabled_days.length === 7) setFrequency("daily"); else if (value.enabled_days.length === 1) setFrequency("weekly"); }).catch((reason: Error) => setError(reason.message)); }, [api]);
  async function save() {
    setBusy(true); setError("");
    try {
      const enabled_days = frequency === "daily" ? [0, 1, 2, 3, 4, 5, 6] : frequency === "weekdays" ? [0, 1, 2, 3, 4] : [0];
      await api.updateSettings({ daily_report_time: time, timezone, enabled_days });
      await onAdvance("groups");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сохранить расписание"); } finally { setBusy(false); }
  }
  return <OnboardingFrame step="reports" title="Регулярный отчёт"><Card className="control-form"><h2>Когда присылать отчёт?</h2><div className="choice-grid">{(["daily", "weekdays", "weekly"] as const).map((value) => <button className={frequency === value ? "selected" : ""} key={value} onClick={() => setFrequency(value)}>{value === "daily" ? "Каждый день" : value === "weekdays" ? "По будням" : "Раз в неделю"}</button>)}</div><label>Время<input type="time" value={time} onChange={(event) => setTime(event.target.value)} /></label><label>Часовой пояс<input value={timezone} onChange={(event) => setTimezone(event.target.value)} /></label>{error && <p className="form-error">{error}</p>}<button className="primary-action" disabled={busy || !settings} onClick={() => void save()}>{busy ? "Сохраняем…" : "Сохранить и продолжить"}</button></Card></OnboardingFrame>;
}

function Summary({ label, value }: { label: string; value: string }) { return <div><small>{label}</small><strong>{value || "Не указано"}</strong></div>; }
function StatusRow({ label, value, ok = false }: { label: string; value: string; ok?: boolean }) { return <div><span>{label}</span><StatusBadge tone={ok ? "success" : "neutral"}>{value}</StatusBadge></div>; }
function reportLabel(bootstrap: Bootstrap) { return bootstrap.onboarding.statuses.reports === "completed" ? "расписание сохранено" : "по расписанию"; }

function OnboardingFrame({ step, title, children }: PropsWithChildren<{ step: OnboardingStep; title: string }>) {
  const stage = stageByStep[step];
  return <main className="onboarding-shell"><header className="onboarding-header"><div><span className="onboarding-logo">V</span><strong>Ventrix</strong></div><small>Этап {stage} из 5</small><div className="onboarding-progress"><i style={{ width: `${stage * 20}%` }} /></div><h1>{title}</h1></header><section className="onboarding-body onboarding-step-enter">{children}</section><footer className="onboarding-security">Данные рабочих сессий защищены шифрованием. Коды подтверждения и пароль 2FA не сохраняются.</footer></main>;
}
