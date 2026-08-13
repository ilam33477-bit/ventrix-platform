"use client";

import { useEffect, useRef, useState } from "react";

import type { VentrixClientApi } from "../../api/client";
import type { Employee, OnboardingStep, TelegramConnection, TelegramSource, TelegramSourcePreview } from "../../types";
import { Card, EmptyState, SectionHeading, StatusBadge } from "../../components/ui";

type ConnectStep = "phone" | "confirm_phone" | "code" | "password" | "syncing" | "done";

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
  const [error, setError] = useState("");
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
        const active = connectionRows.find((item) =>
          ["connected", "syncing", "ready"].includes(item.status),
        );
        if (active) void api.sources(active.id).then((rows) => !cancelled && setSources(rows));
      }
    }).catch((reason: Error) => !cancelled && setError(reason.message));
    return () => {
      cancelled = true;
    };
  }, [api]);

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
      setError(reason instanceof Error ? reason.message : "Не удалось продолжить настройку");
    });
  }, [connections, mode, onOnboardingStep, onboardingStep]);

  const run = async (operation: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await operation(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось выполнить запрос"); }
    finally { setBusy(false); }
  };

  const startLogin = () => run(async () => {
    const result = await api.startTelegramLogin(phone, employeeId || null, createEmployee);
    setConnectionId(result.id);
    setCodePhoneMasked(result.phone_masked);
    setDeliveryMethod(result.code_delivery_method);
    setResendAvailableIn(result.resend_available_in);
    setStep("code");
  });

  const completeLogin = (withPassword = false) => run(async () => {
    const result = await api.completeTelegramLogin(connectionId, withPassword ? { password } : { code });
    setCode(""); setPassword("");
    if (result.requires_2fa) { setStep("password"); return; }
    setStep("syncing");
    setConnections(await api.connections());
    setStep("done");
    await onOnboardingStep?.("monitoring_started");
    onComplete?.();
  });

  const cancel = () => run(async () => {
    if (connectionId) await api.cancelTelegramLogin(connectionId);
    setStep("phone"); setConnectionId(""); setError("");
  });

  const resendCode = () => run(async () => {
    if (!connectionId) throw new Error("Начните подключение заново");
    const result = await api.resendTelegramLogin(connectionId);
    setCodePhoneMasked(result.phone_masked);
    setDeliveryMethod(result.code_delivery_method);
    setResendAvailableIn(result.resend_available_in);
    setCode(""); setStep("code");
  });

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
      ? ["TELEGRAM СОТРУДНИКА", `Аккаунт @${assignedEmployee.name}`, "Подключите рабочую Telegram-сессию сотрудника по номеру телефона."]
    : ["TELEGRAM", "Рабочий Telegram", "Личные диалоги подключаются автоматически. Дополнительные аккаунты можно добавить позже."];

  return <section className={`connection-feature ${mode}`}>
    {mode === "manage" && <SectionHeading eyebrow={heading[0]} title={heading[1]} description={heading[2]} />}
    <div className="connection-layout">
      {showConnection && <Card className="connection-wizard">
        <div className="step-dots"><span className={["phone", "confirm_phone"].includes(step) ? "active" : "done"}>1</span><i /><span className={["code", "password"].includes(step) ? "active" : ["syncing", "done"].includes(step) ? "done" : ""}>2</span><i /><span className={["syncing", "done"].includes(step) ? "active" : ""}>3</span></div>
        {step === "phone" && <><h3>{createEmployee ? "Подключить нового сотрудника" : "Подключить рабочий аккаунт"}</h3><p>{createEmployee ? "Введите номер рабочего Telegram. Имя и username Ventrix получит из подтверждённой сессии автоматически." : "Укажите номер именно того Telegram-аккаунта, рабочие диалоги которого нужно анализировать."}</p>{mode === "manage" && !createEmployee && (assignedEmployee ? <div className="fixed-employee"><small>Сотрудник</small><strong>{assignedEmployee.name}</strong><span>Роль: сотрудник</span></div> : <label>Сотрудник<select value={employeeId} onChange={(event) => setEmployeeId(event.target.value)}><option value="">Общий аккаунт компании</option>{employees.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>)}<label>Номер Telegram<input type="tel" autoComplete="tel" placeholder="+7 999 000-00-00" value={phone} onChange={(event) => setPhone(event.target.value)} /></label><button className="primary-action" disabled={busy || !normalizePhoneInput(phone)} onClick={() => { const normalized = normalizePhoneInput(phone); if (normalized) { setPhone(normalized); setStep("confirm_phone"); } }}>Продолжить</button>{mode === "onboarding_connection" && <button className="text-action" disabled={busy} onClick={onSkip}>Настроить позже</button>}</>}
        {step === "confirm_phone" && <><h3>Проверьте номер</h3><p>Telegram отправит код для этого аккаунта:</p><div className="phone-confirmation">{phone}</div><p>Обычно код приходит в служебный чат «Telegram» на уже авторизованных устройствах. Способ доставки выбирает Telegram.</p><button className="primary-action" disabled={busy} onClick={startLogin}>{busy ? "Отправляем код…" : "Да, отправить код"}</button><button className="text-action" disabled={busy} onClick={() => setStep("phone")}>Изменить номер</button></>}
        {step === "code" && <><h3>Код подтверждения</h3><p>{deliveryDescription(deliveryMethod)} Ventrix использует код один раз и не сохраняет.</p><div className="delivery-hint"><strong>Код запрошен{codePhoneMasked ? ` для ${codePhoneMasked}` : ""}</strong><span>Если уведомления выключены, откройте на этом аккаунте чат «Telegram» со служебными сообщениями. Старые коды после повторной отправки не действуют.</span></div><label>Код<input inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} /></label><button className="primary-action" disabled={busy || !code} onClick={() => completeLogin(false)}>Подтвердить</button><div className="secondary-actions"><button disabled={busy} onClick={() => setCode("")}>Очистить код</button><button disabled={busy || resendAvailableIn > 0} onClick={resendCode}>{resendAvailableIn > 0 ? `Новый код через ${resendAvailableIn} сек.` : "Отправить новый код"}</button></div></>}
        {step === "password" && <><h3>Пароль 2FA</h3><p>Пароль передаётся только для входа в Telegram и не сохраняется.</p><label>Облачный пароль<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label><button className="primary-action" disabled={busy || !password} onClick={() => completeLogin(true)}>Подключить аккаунт</button><button className="text-action" disabled={busy} onClick={() => setPassword("")}>Попробовать ещё раз</button></>}
        {step === "syncing" && <><h3>Запускаем анализ</h3><div className="state-progress"><i /></div><p>Mini App можно закрыть — прогресс сохранится.</p></>}
        {step === "done" && <div className="connection-done"><span>✓</span><h3>Аккаунт подключён</h3><p>Ventrix начал загружать личные рабочие диалоги с глубиной истории, заданной владельцем платформы. Рабочие группы можно добавить позже.</p></div>}
        {error && <p className="form-error">{error}</p>}
        {["code", "password"].includes(step) && <button className="text-action" disabled={busy} onClick={cancel}>Отменить подключение</button>}
      </Card>}
      {showConnection && !assignedEmployee && !createEmployee && <Card className="managed-connections"><div className="card-title"><h3>Подключённые аккаунты</h3><StatusBadge tone="neutral">{visibleConnections.length}</StatusBadge></div>{visibleConnections.length ? visibleConnections.map((item) => <div className="connection-row connection-overview" key={item.id}><span>{(item.account ?? "TG").slice(0, 2).toUpperCase()}</span><div><strong>{item.account ?? "Telegram account"}</strong><small>{item.username ? `@${item.username}` : item.employee_name ?? "Общий аккаунт компании"}</small><div className="connection-stats"><em>Диалогов <b>{item.personal_dialogs ?? 0}</b></em><em>Новых сегодня <b>{item.new_contacts_today ?? 0}</b></em><em>Сообщений сегодня <b>{item.messages_today ?? 0}</b></em></div></div><StatusBadge tone={["connected", "ready", "syncing"].includes(item.status) ? "success" : "warning"}>{item.status === "ready" ? "готов" : item.status}</StatusBadge></div>) : <EmptyState title="Аккаунтов пока нет" description="Аккаунт появится только после ввода номера и запроса Telegram-кода." />}<p className="security-message">Данные рабочих сессий защищены шифрованием. Коды подтверждения и пароль 2FA не сохраняются.</p></Card>}
      {showSources && <Card className="managed-sources"><div className="card-title"><h3>Рабочие группы</h3><StatusBadge tone="neutral">{sources.length}</StatusBadge></div><p>Личные диалоги уже включены. Вставьте ссылку на группу или общую папку Telegram. Сначала Ventrix покажет preview.</p><label>Ссылка Telegram<input type="url" placeholder="https://t.me/+… или https://t.me/addlist/…" value={sourceLink} onChange={(event) => setSourceLink(event.target.value)} /></label><button className="primary-action" disabled={busy || sourceLink.length < 5} onClick={previewSource}>Проверить ссылку</button>{sourcePreview && <div className="folder-list">{sourcePreview.peers.map((peer) => <label className={selectedPeerIds.includes(peer.canonical_peer_id) ? "selected" : ""} key={peer.canonical_peer_id}><input type="checkbox" checked={selectedPeerIds.includes(peer.canonical_peer_id)} onChange={(event) => setSelectedPeerIds((current) => event.target.checked ? [...current, peer.canonical_peer_id] : current.filter((id) => id !== peer.canonical_peer_id))} /><span><strong>{peer.title}</strong><small>{peer.source_type}{peer.participants_count ? ` · ${peer.participants_count} участников` : ""}</small></span></label>)}</div>}{sourcePreview && <button className="primary-action" disabled={busy || selectedPeerIds.length === 0} onClick={addSources}>{sourcePreview.requires_join ? "Вступить и подключить" : "Подключить выбранное"}</button>}{sources.map((source) => <div className="connection-row" key={source.id}><span>#</span><div><strong>{source.title}</strong><small>{source.type} · {source.added_via}</small></div><StatusBadge tone={source.enabled ? "success" : "neutral"}>{source.enabled ? "анализируется" : "выключено"}</StatusBadge></div>)}</Card>}
    </div>
  </section>;
}

function deliveryDescription(method: string) {
  if (method === "sms") return "Telegram отправил код по SMS на подключаемый номер.";
  if (["call", "flash_call", "missed_call"].includes(method)) return "Telegram отправляет код через телефонный звонок.";
  return "Код отправлен в официальный служебный чат Telegram на подключаемом аккаунте.";
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
