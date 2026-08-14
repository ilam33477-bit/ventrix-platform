"use client";

import { useEffect, useRef, useState } from "react";

import { ClientApiError, type VentrixClientApi } from "../../api/client";
import type { Employee, OnboardingStep, TelegramConnection, TelegramSource, TelegramSourcePreview } from "../../types";
import { Card, EmptyState, SectionHeading, StatusBadge } from "../../components/ui";

type ConnectStep = "phone" | "confirm_phone" | "code" | "password" | "syncing" | "done";
type ConnectionError = { title: string; message: string; action?: "resend" | "restart" };

function initialConnectStep(
  onboardingStep: OnboardingStep | undefined,
  connection: TelegramConnection | undefined,
): ConnectStep {
  if (onboardingStep === "monitoring_started") return "done";
  if (connection?.status === "awaiting_code") return "code";
  if (connection?.status === "awaiting_2fa") return "password";
  if (connection?.status === "connected") return "syncing";
  if (["syncing", "ready"].includes(connection?.status ?? "")) return "done";
  return "phone";
}

export function ConnectionManager({ api, connections: initialConnections, onboardingStep, onOnboardingStep, mode = "manage", onSkip, assignedEmployee, createEmployee = false, onComplete }: {
  api: VentrixClientApi;
  connections: TelegramConnection[];
  onboardingStep?: OnboardingStep;
  onOnboardingStep?: (step: OnboardingStep) => Promise<void>;
  mode?: "manage" | "onboarding_connection" | "onboarding_groups";
  onSkip?: () => void;
  assignedEmployee?: Pick<Employee, "id" | "name">;
  createEmployee?: boolean;
  onComplete?: () => void;
}) {
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [connections, setConnections] = useState(initialConnections);
  const [employeeId, setEmployeeId] = useState(assignedEmployee?.id ?? "");
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [connectionId, setConnectionId] = useState(initialConnections[0]?.id ?? "");
  const [step, setStep] = useState<ConnectStep>(
    mode === "manage" ? "phone" : initialConnectStep(onboardingStep, initialConnections[0]),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ConnectionError | null>(null);
  const [resendAvailableIn, setResendAvailableIn] = useState(
    initialConnections[0]?.resend_available_in ?? 0,
  );
  const [deliveryMethod, setDeliveryMethod] = useState(
    initialConnections[0]?.code_delivery_method ?? "telegram_app",
  );
  const [codePhoneMasked, setCodePhoneMasked] = useState(
    initialConnections[0]?.account ?? "",
  );
  const [sourceLink, setSourceLink] = useState("");
  const [sourcePreview, setSourcePreview] = useState<TelegramSourcePreview | null>(null);
  const [previewJobId, setPreviewJobId] = useState("");
  const [selectedPeerIds, setSelectedPeerIds] = useState<string[]>([]);
  const [sources, setSources] = useState<TelegramSource[]>([]);
  const onboardingRecoveryStarted = useRef(false);
  useEffect(() => {
    let cancelled = false;
    Promise.all([api.employees(), api.connections()]).then(([employeeRows, connectionRows]) => {
      if (!cancelled) {
        setEmployees(employeeRows); setConnections(connectionRows);
        if (mode === "onboarding_connection") {
          const resumed = connectionRows.find((item) => ["awaiting_code", "awaiting_2fa", "connected", "syncing", "ready"].includes(item.status));
          if (resumed) {
            setConnectionId(resumed.id);
            setCodePhoneMasked(resumed.account ?? "");
            setDeliveryMethod(resumed.code_delivery_method ?? "telegram_app");
            setResendAvailableIn(resumed.resend_available_in ?? 0);
            if (resumed.status === "awaiting_code") setStep("code");
            else if (resumed.status === "awaiting_2fa") setStep("password");
            else if (["connected", "syncing", "ready"].includes(resumed.status)) setStep("done");
          }
        }
        const active = connectionRows.find((item) =>
          ["connected", "syncing", "ready"].includes(item.status),
        );
        if (active) void api.sources(active.id).then((rows) => !cancelled && setSources(rows));
      }
    }).catch(() => !cancelled && setError({ title: "Не удалось обновить данные", message: "Проверьте соединение и попробуйте ещё раз." }));
    return () => {
      cancelled = true;
    };
  }, [api, mode]);

  useEffect(() => {
    if (step !== "code" || resendAvailableIn <= 0) return;
    const timer = window.setInterval(() => {
      setResendAvailableIn((current) => Math.max(0, current - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [resendAvailableIn, step]);

  useEffect(() => {
    if (
      mode !== "onboarding_connection"
      || onboardingStep !== "telegram_connection"
      || !onOnboardingStep
      || onboardingRecoveryStarted.current
    ) return;
    const connected = connections.find((item) =>
      ["connected", "syncing", "ready"].includes(item.status),
    );
    if (!connected) return;
    onboardingRecoveryStarted.current = true;
    void onOnboardingStep("monitoring_started").catch((reason: unknown) => {
      onboardingRecoveryStarted.current = false;
      setError({ title: "Не удалось продолжить настройку", message: reason instanceof Error ? reason.message : "Обновите Mini App и повторите." });
    });
  }, [connections, mode, onOnboardingStep, onboardingStep]);

  const run = async (operation: () => Promise<void>, phase: "start" | "code" | "password" | "resend" | "cancel" | "source" = "source") => {
    setBusy(true); setError(null);
    try { await operation(); return true; } catch (reason) {
      setError(presentConnectionError(reason, phase));
      if (phase === "code") setCode("");
      if (phase === "password") setPassword("");
      return false;
    } finally { setBusy(false); }
  };

  const startLogin = () => run(async () => {
    const result = await api.startTelegramLogin(phone, employeeId || null, createEmployee);
    setConnectionId(result.id);
    setCodePhoneMasked(result.phone_masked);
    setDeliveryMethod(result.code_delivery_method);
    setResendAvailableIn(result.resend_available_in);
    setStep("code");
  }, "start");

  const completeLogin = (withPassword = false) => run(async () => {
    const result = await api.completeTelegramLogin(connectionId, withPassword ? { password } : { code });
    setCode(""); setPassword("");
    if (result.requires_2fa) { setStep("password"); return; }
    setStep("syncing");
    setConnections(await api.connections());
    setStep("done");
    if (onOnboardingStep) {
      await new Promise((resolve) => window.setTimeout(resolve, 520));
      await onOnboardingStep("monitoring_started");
    }
    onComplete?.();
  }, withPassword ? "password" : "code");

  const cancel = () => run(async () => {
    if (connectionId) await api.cancelTelegramLogin(connectionId);
    setStep("phone"); setConnectionId(""); setError(null); setCode(""); setPassword("");
  }, "cancel");

  const resendCode = () => run(async () => {
    if (!connectionId) throw new Error("Начните подключение заново");
    const result = await api.resendTelegramLogin(connectionId);
    setCodePhoneMasked(result.phone_masked);
    setDeliveryMethod(result.code_delivery_method);
    setResendAvailableIn(result.resend_available_in);
    setCode(""); setStep("code");
  }, "resend");

  const waitForJob = async <T,>(jobId: string): Promise<T> => {
    for (let attempt = 0; attempt < 30; attempt += 1) {
      const job = await api.job<T>(jobId);
      if (job.status === "completed" && job.result) return job.result;
      if (["failed", "dead", "cancelled"].includes(job.status)) {
        throw new Error(job.last_error ?? "Операция Telegram не выполнена");
      }
      await new Promise((resolve) => window.setTimeout(resolve, 500));
    }
    throw new Error("Telegram отвечает дольше обычного. Попробуйте ещё раз.");
  };

  const previewSource = () => run(async () => {
    if (!connectionId) throw new Error("Сначала подключите Telegram-аккаунт");
    const operation = await api.previewSource(connectionId, sourceLink);
    const preview = await waitForJob<TelegramSourcePreview>(operation.job_id);
    setPreviewJobId(operation.job_id);
    setSourcePreview(preview);
    setSelectedPeerIds(preview.peers.map((item) => item.canonical_peer_id));
  });

  const addSources = () => run(async () => {
    if (!sourcePreview || !previewJobId) return;
    const operation = await api.confirmSources(
      connectionId, previewJobId, selectedPeerIds, sourcePreview.requires_join,
    );
    await waitForJob(operation.job_id);
    setSources(await api.sources(connectionId));
    setSourceLink(""); setSourcePreview(null); setPreviewJobId("");
  });

  const showConnection = mode !== "onboarding_groups";
  const visibleConnections = connections.filter((item) =>
    ["connected", "syncing", "ready", "reauthorization_required"].includes(item.status),
  );
  const showSources = mode !== "onboarding_connection" && visibleConnections.length > 0;
  const heading = mode === "onboarding_groups"
    ? ["РАБОЧИЕ ГРУППЫ", "Добавьте группы", "Вставьте ссылку на группу или общую папку Telegram. Можно добавить несколько групп."]
    : assignedEmployee
      ? ["TELEGRAM СОТРУДНИКА", `Аккаунт · ${assignedEmployee.name}`, "Подключите рабочую Telegram-сессию сотрудника по номеру телефона."]
      : ["TELEGRAM", "Рабочий Telegram", "Личные диалоги подключаются автоматически. Дополнительные аккаунты можно добавить позже."];

  return <section className={`connection-feature ${mode}`}>
    {mode === "manage" && <SectionHeading eyebrow={heading[0]} title={heading[1]} description={heading[2]} />}
    <div className="connection-layout">
      {showConnection && <Card className="connection-wizard">
        <div className="step-dots"><span className={["phone", "confirm_phone"].includes(step) ? "active" : "done"}>1</span><i /><span className={["code", "password"].includes(step) ? "active" : ["syncing", "done"].includes(step) ? "done" : ""}>2</span><i /><span className={["syncing", "done"].includes(step) ? "active" : ""}>3</span></div>
        {step === "phone" && <><h3>{createEmployee ? "Подключить нового сотрудника" : "Подключить рабочий аккаунт"}</h3><p>{createEmployee ? "Введите номер рабочего Telegram. Имя и username Ventrix получит из подтверждённой сессии автоматически." : "Укажите номер именно того Telegram-аккаунта, рабочие диалоги которого нужно анализировать."}</p>{mode === "manage" && !createEmployee && (assignedEmployee ? <div className="fixed-employee"><small>Сотрудник</small><strong>{assignedEmployee.name}</strong><span>Роль: сотрудник</span></div> : <label>Сотрудник<select value={employeeId} onChange={(event) => setEmployeeId(event.target.value)}><option value="">Общий аккаунт компании</option>{employees.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>)}<label>Номер Telegram<input type="tel" autoComplete="tel" inputMode="tel" placeholder="+7 999 000-00-00" value={phone} onChange={(event) => { setPhone(event.target.value); setError(null); }} /></label><button className="primary-action" disabled={busy || !normalizePhoneInput(phone)} onClick={() => { const normalized = normalizePhoneInput(phone); if (normalized) { setPhone(normalized); setStep("confirm_phone"); setError(null); } }}>Продолжить</button>{mode === "onboarding_connection" && <button className="text-action" disabled={busy} onClick={onSkip}>Настроить позже</button>}</>}
        {step === "confirm_phone" && <><h3>Проверьте номер</h3><p>Telegram отправит код для этого аккаунта:</p><div className="phone-confirmation">{phone}</div><p>Обычно код приходит в служебный чат «Telegram» на уже авторизованных устройствах. Способ доставки выбирает Telegram.</p><button className="primary-action" disabled={busy} onClick={startLogin}>{busy ? "Отправляем код…" : "Да, отправить код"}</button><button className="text-action" disabled={busy} onClick={() => setStep("phone")}>Изменить номер</button></>}
        {step === "code" && <><h3>Введите код из Telegram</h3><p>{deliveryDescription(deliveryMethod)} Ventrix использует код один раз и не сохраняет.</p><div className="delivery-hint"><strong>Код отправлен{codePhoneMasked ? ` для ${codePhoneMasked}` : ""}</strong><span>Откройте официальный служебный чат «Telegram» на уже авторизованном устройстве. После повторной отправки вводите только самый новый код.</span></div><label>Код подтверждения<input className="code-input" inputMode="numeric" autoComplete="one-time-code" enterKeyHint="done" maxLength={8} value={code} onChange={(event) => { setCode(event.target.value.replace(/\D/g, "")); setError(null); }} /></label><button className="primary-action" disabled={busy || code.length < 4} onClick={() => completeLogin(false)}>{busy ? "Проверяем код…" : "Подтвердить код"}</button><div className="secondary-actions"><button disabled={busy || !code} onClick={() => { setCode(""); setError(null); }}>Ввести заново</button><button disabled={busy || resendAvailableIn > 0} onClick={resendCode}>{resendAvailableIn > 0 ? `Новый код через ${resendAvailableIn} сек.` : "Отправить новый код"}</button></div></>}
        {step === "password" && <><h3>Защита 2FA</h3><p>У аккаунта включён облачный пароль. Он нужен только для входа и не сохраняется.</p><label>Облачный пароль<input type="password" autoComplete="current-password" enterKeyHint="done" value={password} onChange={(event) => { setPassword(event.target.value); setError(null); }} /></label><button className="primary-action" disabled={busy || !password} onClick={() => completeLogin(true)}>{busy ? "Проверяем пароль…" : "Подключить аккаунт"}</button></>}
        {step === "syncing" && <><h3>Запускаем анализ</h3><div className="state-progress"><i /></div><p>Mini App можно закрыть — прогресс сохранится.</p></>}
        {step === "done" && <div className="connection-done"><span aria-hidden="true">✓</span><h3>Аккаунт подключён</h3><p>Вход подтверждён. Ventrix запускает первичную синхронизацию личных рабочих диалогов.</p></div>}
        {error && <div className="form-error connection-error" role="alert" aria-live="assertive"><strong>{error.title}</strong><span>{error.message}</span>{error.action === "resend" && <button disabled={busy || resendAvailableIn > 0} onClick={resendCode}>Запросить новый код</button>}{error.action === "restart" && <button disabled={busy} onClick={cancel}>Начать подключение заново</button>}</div>}
        {["code", "password"].includes(step) && <button className="text-action" disabled={busy} onClick={cancel}>Отменить подключение</button>}
      </Card>}
      {showConnection && !assignedEmployee && !createEmployee && <Card className="managed-connections">
        <div className="card-title"><div><h3>Подключённые аккаунты</h3><p>Состояние синхронизации и сегодняшняя активность.</p></div><StatusBadge tone="neutral">{visibleConnections.length}</StatusBadge></div>
        {visibleConnections.length ? visibleConnections.map((item) => <div className="connection-row connection-overview" key={item.id}>
          <span>{(item.username ?? item.account ?? "TG").slice(0, 2).toUpperCase()}</span>
          <div><strong>{item.username ? `@${item.username}` : item.account ?? "Telegram-аккаунт"}</strong><small>{item.employee_name ?? "Общий аккаунт компании"}</small><div className="connection-stats"><em>Диалогов <b>{item.personal_dialogs ?? 0}</b></em><em>Новых сегодня <b>{item.new_contacts_today ?? 0}</b></em><em>Сообщений сегодня <b>{item.messages_today ?? 0}</b></em></div>{(item.last_incremental_sync_at ?? item.last_sync_at) && <small className="connection-sync">Синхронизация: {formatConnectionDate(item.last_incremental_sync_at ?? item.last_sync_at!)}</small>}</div>
          <StatusBadge tone={connectionStatusTone(item.status)}>{connectionStatusLabel(item.status)}</StatusBadge>
        </div>) : <EmptyState title="Аккаунтов пока нет" description="Аккаунт появится только после ввода номера и успешного входа в Telegram." />}
        <p className="security-message">Данные рабочих сессий защищены шифрованием. Коды подтверждения и пароль 2FA не сохраняются.</p>
      </Card>}
      {showSources && <Card className="managed-sources"><div className="card-title"><div><h3>Рабочие группы</h3><p>Дополнительные источники подключаются отдельно.</p></div><StatusBadge tone="neutral">{sources.length}</StatusBadge></div><p>Личные диалоги уже включены. Вставьте ссылку на группу или общую папку Telegram — сначала Ventrix покажет, что будет подключено.</p><label>Ссылка Telegram<input type="url" placeholder="https://t.me/+… или https://t.me/addlist/…" value={sourceLink} onChange={(event) => setSourceLink(event.target.value)} /></label><button className="primary-action" disabled={busy || sourceLink.length < 5} onClick={previewSource}>Проверить ссылку</button>{sourcePreview && <div className="folder-list">{sourcePreview.peers.map((peer) => <label className={selectedPeerIds.includes(peer.canonical_peer_id) ? "selected" : ""} key={peer.canonical_peer_id}><input type="checkbox" checked={selectedPeerIds.includes(peer.canonical_peer_id)} onChange={(event) => setSelectedPeerIds((current) => event.target.checked ? [...current, peer.canonical_peer_id] : current.filter((id) => id !== peer.canonical_peer_id))} /><span><strong>{peer.title}</strong><small>{sourceTypeLabel(peer.source_type)}{peer.participants_count ? ` · ${peer.participants_count} участников` : ""}</small></span></label>)}</div>}{sourcePreview && <button className="primary-action" disabled={busy || selectedPeerIds.length === 0} onClick={addSources}>{sourcePreview.requires_join ? "Вступить и подключить" : "Подключить выбранное"}</button>}{sources.map((source) => <div className="connection-row" key={source.id}><span>#</span><div><strong>{source.title}</strong><small>{sourceTypeLabel(source.type)}</small></div><StatusBadge tone={source.enabled ? "success" : "neutral"}>{source.enabled ? "Анализируется" : "Выключено"}</StatusBadge></div>)}</Card>}
    </div>
  </section>;
}

function deliveryDescription(method: string) {
  if (method === "sms") return "Telegram отправил код по SMS на подключаемый номер.";
  if (["call", "flash_call", "missed_call"].includes(method)) return "Telegram отправляет код через телефонный звонок.";
  return "Код отправлен в официальный служебный чат Telegram на подключаемом аккаунте.";
}

function connectionStatusLabel(status: string) {
  if (status === "ready") return "Готов";
  if (status === "connected") return "Подключён";
  if (status === "syncing") return "Синхронизация";
  if (status === "reauthorization_required") return "Нужен вход";
  return "Не подключён";
}

function connectionStatusTone(status: string): "success" | "warning" | "danger" | "neutral" {
  if (["ready", "connected"].includes(status)) return "success";
  if (status === "reauthorization_required") return "danger";
  if (status === "syncing") return "warning";
  return "neutral";
}

function formatConnectionDate(value: string) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(date) : "дата не указана";
}

function sourceTypeLabel(type: string) {
  return type === "channel" ? "Канал" : "Группа";
}

function normalizePhoneInput(value: string) {
  const raw = value.trim();
  let digits = raw.replace(/\D/g, "");
  if (raw.startsWith("00")) digits = digits.slice(2);
  else if (!raw.startsWith("+")) {
    if (digits.length === 10) digits = `7${digits}`;
    else if (digits.length === 11 && digits.startsWith("8")) digits = `7${digits.slice(1)}`;
  }
  if (digits.length < 8 || digits.length > 15 || digits.startsWith("0")) return "";
  return `+${digits}`;
}

function presentConnectionError(reason: unknown, phase: "start" | "code" | "password" | "resend" | "cancel" | "source"): ConnectionError {
  const message = reason instanceof Error ? reason.message : "";
  const normalized = message.toLowerCase();
  if (normalized.includes("подождать") || normalized.includes("через") && normalized.includes("сек")) {
    return { title: "Telegram ограничил частоту запросов", message: message || "Подождите указанное время и повторите попытку." };
  }
  if (normalized.includes("код не подош") || normalized.includes("неверн") && phase === "code") {
    return { title: "Код не подошёл", message: "Проверьте код в служебном чате Telegram и введите его ещё раз." };
  }
  if (normalized.includes("код истёк")) {
    return { title: "Срок действия кода закончился", message: "Запросите новый код. Старый код больше не сработает.", action: "resend" };
  }
  if (normalized.includes("пароль 2fa не подош") || normalized.includes("password") && phase === "password") {
    return { title: "Пароль 2FA не подошёл", message: "Введите облачный пароль Telegram ещё раз. Пароль не сохраняется." };
  }
  if (normalized.includes("перезапуск") || normalized.includes("начните подключение заново") || normalized.includes("сессия входа завершена")) {
    return { title: "Нужно начать вход заново", message: "Предыдущая попытка входа завершена. Номер и настройки проекта сохранятся.", action: "restart" };
  }
  if (normalized.includes("заблокирован")) return { title: "Аккаунт заблокирован", message: "Telegram не разрешает вход для этого номера." };
  if (normalized.includes("формат номера") || normalized.includes("не принял номер")) return { title: "Проверьте номер", message: "Введите номер в международном формате, например +79991234567." };
  if (normalized.includes("не ответил") || normalized.includes("сеть") || normalized.includes("timeout")) return { title: "Telegram не отвечает", message: "Проверьте соединение и повторите попытку. Настройка проекта не сброшена." };
  if (phase === "source") return { title: "Не удалось проверить источник", message: "Проверьте ссылку и доступ аккаунта к этой группе, затем повторите." };
  if (phase === "cancel") return { title: "Не удалось отменить подключение", message: "Обновите Mini App и попробуйте ещё раз." };
  if (reason instanceof ClientApiError && reason.status === 429) return { title: "Слишком много попыток", message: message || "Подождите и повторите запрос позже." };
  return { title: "Не удалось продолжить вход", message: "Telegram временно не подтвердил запрос. Повторите текущий шаг — начинать весь onboarding заново не нужно." };
}
