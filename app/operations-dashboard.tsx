"use client";

import { useEffect, useMemo, useState } from "react";

type Problem = {
  id: string | number;
  priority: "critical" | "high" | "medium";
  type: string;
  title: string;
  person: string;
  age: string;
  amount?: string;
  quote: string;
  confidence: number;
  action: string;
};

type Bootstrap = {
  tenant: { id: string; name: string };
  onboarding_state: "not_connected" | "connecting" | "folder_selection" | "chat_selection" | "synchronization" | "ready" | "reauthorization_required";
  menu: string[];
  connection: null | { status: string; account: string | null; folder: string | null; history_days: number; personal_dialogs_consent: boolean };
  connections: Array<{ id: string; status: string; account: string | null; health_status: string; last_incremental_sync_at: string | null }>;
  progress: null | { status: string; stage: string; percent: number; dialogs_total: number; dialogs_completed: number; failed_dialogs: number; messages_loaded: number; metrics: Record<string, number | string | null> };
  problems: Array<{ id: string; type: string; priority: "critical" | "high" | "medium"; confidence: number; evidence: string; explanation: string; recommended_action: string; occurred_at: string }>;
};

type MiniAppAuth = {
  tenant_id: string;
  tenant_name: string;
  user: { telegram_user_id: number; first_name: string | null; last_name: string | null; username: string | null; role: string };
  permissions: string[];
  project_context: { status: string; timezone: string | null; client_bot: { id: string; username: string }; onboarding_state: Bootstrap["onboarding_state"] };
  dashboard_summary: {
    problems: number;
    signals: number;
    commitments: number;
    reports: number;
    employees: number;
    connections: number;
    groups: number;
    ai_usage: { tokens_today: number; calls_today: number };
  };
};

const nav = ["Сводка", "Важное", "Диалоги", "Сотрудники", "Отчёты", "Подключения", "Настройки"];

export function OperationsDashboard() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [auth, setAuth] = useState<MiniAppAuth | null>(null);
  const [launchState, setLaunchState] = useState<"checking" | "outside_telegram" | "authenticating" | "authenticated" | "denied">("checking");
  const [loadError, setLoadError] = useState(false);
  const [active, setActive] = useState("Сводка");
  const [priority, setPriority] = useState("Все");
  const [selectedId, setSelectedId] = useState<string | number | null>(null);
  const [done, setDone] = useState<Array<string | number>>([]);
  const [initData, setInitData] = useState("");
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";
  useEffect(() => {
    const telegram = (window as typeof window & { Telegram?: { WebApp?: { initData?: string; ready?: () => void; expand?: () => void } } }).Telegram?.WebApp;
    telegram?.ready?.();
    telegram?.expand?.();
    const initData = telegram?.initData;
    const launchTimer = window.setTimeout(() => {
      if (!initData) {
        setLaunchState("outside_telegram");
        return;
      }
      setInitData(initData);
      setLaunchState("authenticating");
      const headers = { Authorization: `tma ${initData}` };
      fetch(`${apiBase}/api/v1/client/mini-app/auth`, { method: "POST", headers })
        .then(async (response) => {
          if (response.status === 401 || response.status === 403) {
            setLaunchState("denied");
            return;
          }
          if (!response.ok) throw new Error("auth failed");
          const authenticated = await response.json() as MiniAppAuth;
          const bootstrapResponse = await fetch(`${apiBase}/api/v1/client/bootstrap`, { headers });
          if (!bootstrapResponse.ok) throw new Error("bootstrap failed");
          setAuth(authenticated);
          setBootstrap(await bootstrapResponse.json() as Bootstrap);
          setLaunchState("authenticated");
        })
        .catch(() => setLoadError(true));
    }, 0);
    return () => window.clearTimeout(launchTimer);
  }, [apiBase]);
  const liveProblems = useMemo<Problem[]>(() => (bootstrap?.problems ?? []).map((item) => ({
    id: item.id,
    priority: item.priority,
    type: item.type.replaceAll("_", " "),
    title: item.explanation,
    person: "Telegram-диалог",
    age: new Date(item.occurred_at).toLocaleString("ru-RU"),
    quote: item.evidence,
    confidence: Math.round(item.confidence * 100),
    action: item.recommended_action,
  })), [bootstrap]);
  const selected = liveProblems.find((item) => item.id === selectedId) ?? liveProblems[0] ?? null;
  const visible = useMemo(
    () => liveProblems.filter((item) => priority === "Все" || item.priority === priority),
    [liveProblems, priority],
  );

  if (loadError) {
    return <main className="state-shell"><section className="state-card"><p className="eyebrow">ОШИБКА ПОДКЛЮЧЕНИЯ</p><h1>Не удалось открыть проект</h1><p>Вернитесь в клиентский бот и откройте панель снова. Если Telegram-сессия истекла, бот предложит повторную авторизацию.</p><button onClick={() => location.reload()}>Повторить</button></section></main>;
  }
  if (launchState === "outside_telegram") {
    return <main className="state-shell"><section className="state-card"><p className="eyebrow">VENTRIX MINI APP</p><h1>Откройте Ventrix через вашего Telegram-бота</h1><p>Эта панель работает внутри Telegram и безопасно определяет ваш проект по подписанным данным запуска. Откройте клиентского бота и нажмите «Открыть панель».</p><div className="launch-hint"><span>1</span><p>Откройте бот вашего проекта</p><span>2</span><p>Нажмите «Открыть панель»</p></div></section></main>;
  }
  if (launchState === "denied") {
    return <main className="state-shell"><section className="state-card"><p className="eyebrow">ДОСТУП НЕ ПОДТВЕРЖДЁН</p><h1>У вас нет доступа к этому проекту</h1><p>Вернитесь в клиентский бот, к которому вы добавлены, и откройте Ventrix повторно.</p></section></main>;
  }
  if (!bootstrap) {
    return <main className="state-shell"><section className="state-card"><p className="eyebrow">VENTRIX</p><h1>{launchState === "authenticating" ? "Проверяем доступ к проекту…" : "Открываем Mini App…"}</h1><div className="state-progress"><i /></div></section></main>;
  }
  if (bootstrap.onboarding_state !== "ready" && active !== "Сотрудники") {
    const progress = bootstrap.progress;
    const copy = {
      not_connected: ["Подключите рабочий Telegram", "Создайте отдельную рабочую папку в Telegram, затем безопасно подключите аккаунт через клиентский бот."],
      connecting: ["Подключение аккаунта", "Завершите ввод кода или 2FA в клиентском боте. Секретные данные здесь не отображаются."],
      folder_selection: ["Выберите рабочую папку", "В анализ войдут только группы, каналы и чаты из выбранной папки."],
      chat_selection: ["Проверьте область анализа", "Подтвердите личные диалоги и выберите глубину истории: 3, 7, 14 или 30 дней."],
      synchronization: ["Первичный анализ выполняется", "История загружается небольшими пакетами. Прогресс сохранится после перезапуска."],
      reauthorization_required: ["Нужна повторная авторизация", "Telegram-сессия больше не действует. Переподключите аккаунт через клиентский бот."],
    }[bootstrap.onboarding_state];
    return <main className="state-shell"><section className="state-card wide"><p className="eyebrow">{bootstrap.tenant.name}</p><h1>{copy[0]}</h1><p>{copy[1]}</p>{progress && <><div className="state-progress"><i style={{ width: `${progress.percent}%` }} /></div><strong>{progress.percent}% · {progress.messages_loaded.toLocaleString("ru-RU")} сообщений</strong><small>{progress.dialogs_completed} из {progress.dialogs_total} диалогов · ошибок отдельных чатов: {progress.failed_dialogs}</small></>}<nav><button onClick={() => setActive("Сотрудники")}>Подключить рабочий аккаунт</button>{bootstrap.menu.slice(1).map((item) => <button key={item}>{item}</button>)}</nav><p className="security-note">Сессия хранится в зашифрованном виде. Код и пароль 2FA не сохраняются.</p></section></main>;
  }
  const readyMetrics = bootstrap.progress?.metrics ?? {};
  const summary = auth?.dashboard_summary;
  const liveMetrics = [
    [String(summary?.problems ?? 0), "Открытые проблемы", "операционный фокус"],
    [String(summary?.signals ?? 0), "Критичные сигналы", "требуют проверки"],
    [String(summary?.commitments ?? 0), "Обязательства", "открытые"],
    [String(summary?.reports ?? 0), "Отчёты", "доступные"],
    [String(summary?.employees ?? 0), "Сотрудники", "активные"],
    [String(summary?.connections ?? 0), "Telegram connections", "подключения"],
    [String(summary?.groups ?? 0), "Рабочие группы", "интеграции"],
    [String(summary?.ai_usage.tokens_today ?? 0), "AI usage", `${summary?.ai_usage.calls_today ?? 0} запросов сегодня`],
  ];
  const criticalCount = liveProblems.filter((item) => item.priority === "critical").length;
  const highCount = liveProblems.filter((item) => item.priority === "high").length;
  const mediumCount = liveProblems.filter((item) => item.priority === "medium").length;

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">V</span><span>Ventrix</span></div>
        <p className="tenant-label">КОМПАНИЯ</p>
        <button className="tenant-switch">{bootstrap.tenant.name} <span>⌄</span></button>
        <nav aria-label="Основная навигация">
          {nav.map((item, index) => (
            <button key={item} className={active === item ? "nav-item active" : "nav-item"} onClick={() => setActive(item)}>
              <span className="nav-icon">{["⌂", "!", "◌", "◎", "▤", "↔", "⚙"][index]}</span>{item}
              {item === "Важное" && <b>{liveProblems.length}</b>}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <button className="nav-item"><span className="nav-icon">⚙</span>Настройки</button>
          <div className="profile"><span>{(auth?.user.first_name ?? "V").slice(0, 2).toUpperCase()}</span><div><strong>{[auth?.user.first_name, auth?.user.last_name].filter(Boolean).join(" ") || "Пользователь проекта"}</strong><small>{auth?.user.username ? `@${auth.user.username}` : auth?.user.role}</small></div><button aria-label="Открыть профиль">•••</button></div>
        </div>
      </aside>

      <section className="content">
        <header className="topbar">
          <div><p className="eyebrow">РАБОЧАЯ СВОДКА</p><h1>{bootstrap.tenant.name}</h1></div>
          <div className="top-actions"><button aria-label="Поиск">⌕</button><button className="notification" aria-label="Уведомления">♢<i /></button></div>
        </header>

        {active === "Сотрудники" ? (
          <EmployeeManagement initData={initData} apiBase={apiBase} initialConnections={bootstrap.connections} />
        ) : <>

        <section className="attention-banner">
          <div className="pulse-ring"><span>!</span></div>
          <div><p>ТРЕБУЕТ ВНИМАНИЯ</p><h2>{liveProblems.length} ситуаций требуют проверки</h2><span>Каждый вывод содержит исходное сообщение и уверенность</span></div>
          <button onClick={() => { setPriority("critical"); document.getElementById("problems")?.scrollIntoView({ behavior: "smooth" }); }}>Разобрать сейчас <span>→</span></button>
        </section>

        <section className="metrics" aria-label="Ключевые показатели">
          {liveMetrics.map(([value, label, note], index) => <article key={label}><div className="metric-head"><span className={`metric-icon metric-${index % 4}`}>{["!", "⌁", "✓", "▤", "◎", "↔", "◫", "AI"][index]}</span><small>{note}</small></div><strong>{value}</strong><p>{label}</p></article>)}
        </section>

        <section className="workspace" id="problems">
          <div className="problem-list-panel">
            <div className="section-head"><div><p className="eyebrow">ОПЕРАЦИОННЫЙ ФОКУС</p><h2>Что требует решения</h2></div><button>Все проблемы <span>→</span></button></div>
            <div className="filters" role="group" aria-label="Фильтр приоритета">
              {["Все", "critical", "high", "medium"].map((item) => <button key={item} className={priority === item ? "selected" : ""} onClick={() => setPriority(item)}>{item === "Все" ? `Все ${liveProblems.length}` : item === "critical" ? `Критичные ${criticalCount}` : item === "high" ? `Высокие ${highCount}` : `Средние ${mediumCount}`}</button>)}
            </div>
            <div className="problem-list">
              {visible.map((problem) => <button key={problem.id} className={`problem-row ${selected?.id === problem.id ? "current" : ""} ${done.includes(problem.id) ? "resolved" : ""}`} onClick={() => setSelectedId(problem.id)}>
                <span className={`priority-dot ${problem.priority}`} />
                <span className="problem-copy"><small>{problem.type}</small><strong>{problem.title}</strong><span>{problem.person}</span></span>
                <span className="problem-meta"><small>{problem.age}</small>{problem.amount && <b>{problem.amount}</b>}<i>→</i></span>
              </button>)}
              {visible.length === 0 && <div className="empty-problems"><strong>Проблем этого приоритета нет</strong><span>Список обновится после следующего анализа.</span></div>}
            </div>
          </div>

          {selected && <aside className="evidence-card">
            <div className="evidence-top"><span className={`priority-pill ${selected.priority}`}>{selected.priority === "critical" ? "КРИТИЧНО" : selected.priority === "high" ? "ВЫСОКИЙ РИСК" : "СРЕДНИЙ РИСК"}</span><button aria-label="Закрыть карточку">×</button></div>
            <h3>{selected.title}</h3>
            <p className="person">{selected.person}</p>
            <div className="confidence"><span>Уверенность AI</span><div><i style={{ width: `${selected.confidence}%` }} /></div><b>{selected.confidence}%</b></div>
            <p className="block-label">ДОКАЗАТЕЛЬСТВО</p>
            <blockquote>“{selected.quote}”<footer>Telegram · сегодня, 12:41</footer></blockquote>
            <p className="block-label">РЕКОМЕНДУЕМОЕ ДЕЙСТВИЕ</p>
            <div className="recommended"><span>→</span><p>{selected.action}</p></div>
            <div className="card-actions">
              <button className="primary" disabled={done.includes(selected.id)} onClick={() => setDone((items) => [...items, selected.id])}>{done.includes(selected.id) ? "Отмечено в работе" : "Взять в работу"}</button>
              <button className="secondary">Назначить</button>
            </div>
            <button className="not-problem">Это не проблема</button>
          </aside>}
        </section>

        <footer className="value-strip"><div><span>↗</span><p>Первичный анализ загрузил <strong>{bootstrap.progress?.messages_loaded ?? 0} сообщений</strong> и создал <strong>{readyMetrics.problems_created ?? 0} проблем</strong> с исходными доказательствами</p></div><button>Смотреть эффект →</button></footer>
        </>}
      </section>

      <nav className="mobile-nav" aria-label="Мобильная навигация">
        {nav.slice(0, 5).map((item, index) => <button key={item} className={active === item ? "active" : ""} onClick={() => setActive(item)}><span>{["⌂", "!", "◌", "✓", "▤"][index]}</span>{item}</button>)}
      </nav>
    </main>
  );
}

type Employee = { id: string; name: string; telegram_username: string | null; role: string; status: string };
type Connection = { id: string; status: string; account: string | null; username?: string | null; health_status?: string };
type Folder = { id: number; title: string; chat_count: number };
type ConnectStep = "phone" | "code" | "password" | "folders" | "syncing" | "done";

function EmployeeManagement({ initData, apiBase, initialConnections }: { initData: string; apiBase: string; initialConnections: Connection[] }) {
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [connections, setConnections] = useState<Connection[]>(initialConnections);
  const [employeeId, setEmployeeId] = useState("");
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [connectionId, setConnectionId] = useState("");
  const [step, setStep] = useState<ConnectStep>("phone");
  const [folders, setFolders] = useState<Folder[]>([]);
  const [folderId, setFolderId] = useState<number | null>(null);
  const [historyDays, setHistoryDays] = useState(7);
  const [personalConsent, setPersonalConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const request = async <T,>(path: string, options?: RequestInit): Promise<T> => {
    const response = await fetch(`${apiBase}/api/v1/client${path}`, {
      ...options,
      headers: { "Content-Type": "application/json", Authorization: `tma ${initData}`, ...(options?.headers ?? {}) },
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({ detail: "Не удалось выполнить запрос" }));
      throw new Error(typeof payload.detail === "string" ? payload.detail : "Не удалось выполнить запрос");
    }
    return response.json() as Promise<T>;
  };

  useEffect(() => {
    if (!initData) return;
    Promise.all([request<Employee[]>("/employees"), request<Connection[]>("/connections")])
      .then(([employeeRows, connectionRows]) => { setEmployees(employeeRows); setConnections(connectionRows); })
      .catch((reason: Error) => setError(reason.message));
  // request is intentionally scoped to the signed Telegram launch payload.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initData]);

  const run = async (operation: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await operation(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось выполнить запрос"); }
    finally { setBusy(false); }
  };

  const startLogin = () => run(async () => {
    const result = await request<{ id: string }>("/connections/login/start", { method: "POST", body: JSON.stringify({ phone, employee_id: employeeId || null }) });
    setConnectionId(result.id); setStep("code");
  });

  const loadFolders = async (id: string) => {
    const catalog = await request<{ folders: Folder[] }>(`/connections/${id}/catalog`, { method: "POST" });
    setFolders(catalog.folders); setFolderId(catalog.folders[0]?.id ?? null); setStep("folders");
  };

  const completeLogin = (withPassword = false) => run(async () => {
    const result = await request<{ id: string; requires_2fa: boolean }>(`/connections/${connectionId}/login/complete`, {
      method: "POST",
      body: JSON.stringify(withPassword ? { password } : { code }),
    });
    setCode(""); setPassword("");
    if (result.requires_2fa) { setStep("password"); return; }
    await loadFolders(result.id);
  });

  const startSync = () => run(async () => {
    if (folderId === null) throw new Error("Выберите рабочую папку");
    setStep("syncing");
    await request(`/connections/${connectionId}/scope`, {
      method: "POST",
      body: JSON.stringify({ folder_ids: [folderId], history_days: historyDays, personal_dialogs_consent: personalConsent }),
    });
    const rows = await request<Connection[]>("/connections");
    setConnections(rows); setStep("done");
  });

  const reset = () => { setStep("phone"); setPhone(""); setCode(""); setPassword(""); setConnectionId(""); setFolders([]); setError(""); };
  const cancel = () => run(async () => {
    if (connectionId) await request(`/connections/${connectionId}/login/cancel`, { method: "POST" });
    reset();
  });

  return <section className="employee-management">
    <div className="management-heading"><div><p className="eyebrow">УПРАВЛЕНИЕ СОТРУДНИКАМИ</p><h2>Рабочие Telegram-аккаунты</h2><p>Подключите аккаунт сотрудника, чтобы Ventrix отслеживал только выбранную рабочую папку.</p></div><span className="secure-badge">Зашифрованная сессия</span></div>
    {!initData && <div className="management-alert">Откройте Mini App из клиентского Telegram-бота. В обычном браузере подключение аккаунта отключено.</div>}
    <div className="management-grid">
      <article className="connection-wizard">
        <div className="wizard-progress"><span className={step === "phone" ? "current" : "done"}>1</span><i/><span className={["code", "password"].includes(step) ? "current" : ["folders", "syncing", "done"].includes(step) ? "done" : ""}>2</span><i/><span className={step === "folders" ? "current" : ["syncing", "done"].includes(step) ? "done" : ""}>3</span></div>
        {step === "phone" && <><h3>Кому принадлежит аккаунт</h3><label>Сотрудник<select value={employeeId} onChange={(event) => setEmployeeId(event.target.value)}><option value="">Общий аккаунт компании</option>{employees.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label>Номер Telegram<input type="tel" autoComplete="tel" placeholder="+7 999 000-00-00" value={phone} onChange={(event) => setPhone(event.target.value)} /></label><button className="wizard-primary" disabled={busy || !initData || phone.length < 8} onClick={startLogin}>{busy ? "Отправляем код…" : "Получить код в Telegram"}</button></>}
        {step === "code" && <><h3>Введите код Telegram</h3><p>Код пришёл в официальный чат Telegram на подключаемом аккаунте.</p><label>Код<input inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} /></label><button className="wizard-primary" disabled={busy || !code} onClick={() => completeLogin(false)}>{busy ? "Проверяем…" : "Подтвердить код"}</button></>}
        {step === "password" && <><h3>Требуется пароль 2FA</h3><p>Пароль используется только для авторизации Telegram и не сохраняется.</p><label>Облачный пароль<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label><button className="wizard-primary" disabled={busy || !password} onClick={() => completeLogin(true)}>{busy ? "Проверяем…" : "Подключить аккаунт"}</button></>}
        {step === "folders" && <><h3>Выберите рабочую папку</h3><p>Диалоги вне этой папки не попадут в анализ.</p><div className="folder-list">{folders.map((folder) => <label className={folderId === folder.id ? "selected" : ""} key={folder.id}><input type="radio" checked={folderId === folder.id} onChange={() => setFolderId(folder.id)} /><span><strong>{folder.title}</strong><small>{folder.chat_count} чатов</small></span></label>)}</div><div className="scope-options"><label>История<select value={historyDays} onChange={(event) => setHistoryDays(Number(event.target.value))}>{[3, 7, 14, 30].map((days) => <option value={days} key={days}>{days} дней</option>)}</select></label><label className="checkbox"><input type="checkbox" checked={personalConsent} onChange={(event) => setPersonalConsent(event.target.checked)} />Разрешить предварительную проверку личных диалогов</label></div><button className="wizard-primary" disabled={busy || folderId === null} onClick={startSync}>Запустить синхронизацию</button></>}
        {step === "syncing" && <><h3>Запускаем синхронизацию…</h3><div className="state-progress"><i /></div><p>Можно закрыть Mini App — прогресс сохранится.</p></>}
        {step === "done" && <div className="wizard-success"><span>✓</span><h3>Аккаунт подключён</h3><p>Первичная история загружается, затем включится incremental-мониторинг.</p><button className="wizard-primary" onClick={reset}>Подключить ещё аккаунт</button></div>}
        {error && <p className="wizard-error">{error}</p>}
        {step !== "phone" && step !== "done" && <button className="wizard-cancel" disabled={busy} onClick={cancel}>Отменить</button>}
      </article>
      <aside className="managed-accounts"><div className="managed-head"><h3>Подключённые аккаунты</h3><b>{connections.length}</b></div>{connections.length ? connections.map((item) => <div className="managed-row" key={item.id}><span>{(item.account ?? "TG").slice(0, 2).toUpperCase()}</span><div><strong>{item.account ?? "Telegram account"}</strong><small>{item.username ? `@${item.username}` : item.status}</small></div><i className={["connected", "ready", "syncing"].includes(item.status) ? "online" : ""}/></div>) : <div className="managed-empty"><strong>Пока нет аккаунтов</strong><p>Первый аккаунт появится здесь после подтверждения кода.</p></div>}<div className="privacy-card"><strong>Что хранит Ventrix</strong><p>Только зашифрованную Telegram-сессию, выбранную область анализа и служебные checkpoints. Код и 2FA-пароль не сохраняются.</p></div></aside>
    </div>
  </section>;
}
