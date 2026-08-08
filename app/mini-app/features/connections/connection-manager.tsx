"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { VentrixClientApi } from "../../api/client";
import type { Employee, Folder, OnboardingStep, TelegramConnection } from "../../types";
import { Card, EmptyState, SectionHeading, StatusBadge } from "../../components/ui";

type ConnectStep = "phone" | "code" | "password" | "folders" | "syncing" | "done";

function initialConnectStep(
  onboardingStep: OnboardingStep | undefined,
  connection: TelegramConnection | undefined,
): ConnectStep {
  if (onboardingStep === "scope_selection") return "folders";
  if (connection?.status === "awaiting_code") return "code";
  if (connection?.status === "awaiting_2fa") return "password";
  if (connection?.status === "connected") return "folders";
  if (["syncing", "ready"].includes(connection?.status ?? "")) return "done";
  return "phone";
}

export function ConnectionManager({ api, connections: initialConnections, onboardingStep, onOnboardingStep }: {
  api: VentrixClientApi;
  connections: TelegramConnection[];
  onboardingStep?: OnboardingStep;
  onOnboardingStep?: (step: OnboardingStep) => Promise<void>;
}) {
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [connections, setConnections] = useState(initialConnections);
  const [employeeId, setEmployeeId] = useState("");
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [connectionId, setConnectionId] = useState(initialConnections[0]?.id ?? "");
  const [step, setStep] = useState<ConnectStep>(
    initialConnectStep(onboardingStep, initialConnections[0]),
  );
  const [folders, setFolders] = useState<Folder[]>([]);
  const [folderId, setFolderId] = useState<number | null>(null);
  const [historyDays, setHistoryDays] = useState(7);
  const [personalConsent, setPersonalConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const resumedScope = useRef(false);

  const loadFolders = useCallback(async (id: string) => {
    const catalog = await api.folderCatalog(id);
    setFolders(catalog.folders);
    setFolderId(catalog.folders[0]?.id ?? null);
    setStep("folders");
    await onOnboardingStep?.("scope_selection");
  }, [api, onOnboardingStep]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.employees(), api.connections()]).then(([employeeRows, connectionRows]) => {
      if (!cancelled) { setEmployees(employeeRows); setConnections(connectionRows); }
    }).catch((reason: Error) => !cancelled && setError(reason.message));
    let scopeTimer: number | undefined;
    if (step === "folders" && connectionId && !resumedScope.current) {
      resumedScope.current = true;
      scopeTimer = window.setTimeout(() => {
        void loadFolders(connectionId).catch((reason: Error) => setError(reason.message));
      }, 0);
    }
    return () => {
      cancelled = true;
      if (scopeTimer !== undefined) window.clearTimeout(scopeTimer);
    };
  }, [api, connectionId, loadFolders, step]);

  const run = async (operation: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await operation(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось выполнить запрос"); }
    finally { setBusy(false); }
  };

  const startLogin = () => run(async () => {
    const result = await api.startTelegramLogin(phone, employeeId || null);
    setConnectionId(result.id); setStep("code");
  });

  const completeLogin = (withPassword = false) => run(async () => {
    const result = await api.completeTelegramLogin(connectionId, withPassword ? { password } : { code });
    setCode(""); setPassword("");
    if (result.requires_2fa) { setStep("password"); return; }
    await loadFolders(result.id);
  });

  const startSync = () => run(async () => {
    if (folderId === null) throw new Error("Выберите рабочую папку");
    setStep("syncing");
    await api.selectScope(connectionId, folderId, historyDays, personalConsent);
    setConnections(await api.connections());
    setStep("done");
    await onOnboardingStep?.("employees_review");
  });

  const cancel = () => run(async () => {
    if (connectionId) await api.cancelTelegramLogin(connectionId);
    setStep("phone"); setConnectionId(""); setError("");
  });

  return <section className="connection-feature">
    <SectionHeading eyebrow="TELEGRAM CONNECTIONS" title="Рабочие Telegram-аккаунты" description="Ventrix анализирует только выбранную рабочую папку и не сохраняет код подтверждения или пароль 2FA." />
    <div className="connection-layout">
      <Card className="connection-wizard">
        <div className="step-dots"><span className={step === "phone" ? "active" : "done"}>1</span><i /><span className={["code", "password"].includes(step) ? "active" : ["folders", "syncing", "done"].includes(step) ? "done" : ""}>2</span><i /><span className={step === "folders" ? "active" : ["syncing", "done"].includes(step) ? "done" : ""}>3</span></div>
        {step === "phone" && <><h3>Подключить рабочий аккаунт</h3><p>Код придёт в официальный чат Telegram подключаемого аккаунта.</p><label>Сотрудник<select value={employeeId} onChange={(event) => setEmployeeId(event.target.value)}><option value="">Общий аккаунт компании</option>{employees.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label>Номер Telegram<input type="tel" autoComplete="tel" placeholder="+7 999 000-00-00" value={phone} onChange={(event) => setPhone(event.target.value)} /></label><button className="primary-action" disabled={busy || phone.length < 8} onClick={startLogin}>{busy ? "Отправляем код…" : "Получить код"}</button></>}
        {step === "code" && <><h3>Код подтверждения</h3><p>Введите код из Telegram. Ventrix использует его один раз и не сохраняет.</p><label>Код<input inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} /></label><button className="primary-action" disabled={busy || !code} onClick={() => completeLogin(false)}>Подтвердить</button></>}
        {step === "password" && <><h3>Пароль 2FA</h3><p>Пароль передаётся только для входа в Telegram и не сохраняется.</p><label>Облачный пароль<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label><button className="primary-action" disabled={busy || !password} onClick={() => completeLogin(true)}>Подключить аккаунт</button></>}
        {step === "folders" && <><h3>Выберите рабочую папку</h3><p>Чаты вне выбранной папки не попадут в анализ.</p><div className="folder-list">{folders.map((folder) => <label className={folderId === folder.id ? "selected" : ""} key={folder.id}><input type="radio" checked={folderId === folder.id} onChange={() => setFolderId(folder.id)} /><span><strong>{folder.title}</strong><small>{folder.chat_count} чатов</small></span></label>)}</div><label>Глубина истории<select value={historyDays} onChange={(event) => setHistoryDays(Number(event.target.value))}>{[3, 7, 14, 30].map((days) => <option value={days} key={days}>{days} дней</option>)}</select></label><label className="inline-check"><input type="checkbox" checked={personalConsent} onChange={(event) => setPersonalConsent(event.target.checked)} />Включить выбранные личные рабочие диалоги</label><button className="primary-action" disabled={busy || folderId === null} onClick={startSync}>Начать анализ</button></>}
        {step === "syncing" && <><h3>Запускаем анализ</h3><div className="state-progress"><i /></div><p>Mini App можно закрыть — прогресс сохранится.</p></>}
        {step === "done" && <div className="connection-done"><span>✓</span><h3>Аккаунт подключён</h3><p>Ventrix начал загружать выбранную рабочую историю.</p></div>}
        {error && <p className="form-error">{error}</p>}
        {["code", "password", "folders"].includes(step) && <button className="text-action" disabled={busy} onClick={cancel}>Отменить подключение</button>}
      </Card>
      <Card className="managed-connections"><div className="card-title"><h3>Подключённые аккаунты</h3><StatusBadge tone="neutral">{connections.length}</StatusBadge></div>{connections.length ? connections.map((item) => <div className="connection-row" key={item.id}><span>{(item.account ?? "TG").slice(0, 2).toUpperCase()}</span><div><strong>{item.account ?? "Telegram account"}</strong><small>{item.folder ?? item.username ?? item.status}</small></div><StatusBadge tone={["connected", "ready", "syncing"].includes(item.status) ? "success" : "warning"}>{item.status}</StatusBadge></div>) : <EmptyState title="Аккаунтов пока нет" description="Первый аккаунт появится после подтверждения Telegram-кода." />}<p className="security-message">Данные рабочих сессий защищены шифрованием. Коды подтверждения и пароль 2FA не сохраняются.</p></Card>
    </div>
  </section>;
}
