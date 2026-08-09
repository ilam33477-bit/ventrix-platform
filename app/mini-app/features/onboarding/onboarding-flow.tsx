"use client";

import type { PropsWithChildren } from "react";

import type { VentrixClientApi } from "../../api/client";
import type { Bootstrap, OnboardingStep } from "../../types";
import { Card } from "../../components/ui";
import { ConnectionManager } from "../connections/connection-manager";

const stepNumber: Record<OnboardingStep, number> = {
  welcome: 1,
  mini_guide: 2,
  telegram_connection: 3,
  monitoring_started: 4,
  employees: 5,
  notifications: 6,
  reports: 7,
  groups: 8,
  final_review: 9,
  completed: 10,
};

type Advance = (
  step: OnboardingStep,
  status?: "completed" | "skipped",
) => Promise<void>;

export function OnboardingFlow({ api, bootstrap, onAdvance }: {
  api: VentrixClientApi;
  bootstrap: Bootstrap;
  onAdvance: Advance;
}) {
  const step = bootstrap.onboarding.step;
  const connection = bootstrap.connections[0];
  if (step === "welcome") {
    return <OnboardingFrame step={1} title="Добро пожаловать в Ventrix"><Card className="onboarding-hero"><span className="onboarding-mark">V</span><h2>Не теряйте клиентов, обещания и важные задачи</h2><p>Ventrix следит за рабочими переписками, замечает риски, уведомляет ответственных и формирует понятные отчёты.</p><button className="primary-action" onClick={() => void onAdvance("mini_guide")}>Настроить Ventrix</button></Card></OnboardingFrame>;
  }
  if (step === "mini_guide") {
    return <OnboardingFrame step={2} title="Как Ventrix помогает"><div className="section-list">{[["1", "Ventrix наблюдает", "Находит клиентов без ответа, обещания и важные документы."], ["2", "Ventrix реагирует", "Уведомляет владельца и нужного сотрудника."], ["3", "Ventrix подводит итоги", "Собирает проблемы, результаты и регулярные отчёты."]].map(([mark, title, text]) => <Card key={mark} className="person-row"><span>{mark}</span><div><h3>{title}</h3><p>{text}</p></div></Card>)}</div><button className="primary-action onboarding-next" onClick={() => void onAdvance("telegram_connection")}>Продолжить</button></OnboardingFrame>;
  }
  if (step === "telegram_connection" || step === "monitoring_started") {
    return <OnboardingFrame step={stepNumber[step]} title={step === "telegram_connection" ? "Подключите рабочий Telegram" : "Мониторинг начался"}><ConnectionManager api={api} connections={bootstrap.connections} onboardingStep={step} onOnboardingStep={onAdvance} /></OnboardingFrame>;
  }
  if (step === "employees") {
    return <SimpleStep step={5} title="Кто работает с клиентами?" text="Добавьте сотрудников позже в разделе «Команда», чтобы Ventrix назначал ответственного и отправлял персональные уведомления." next="notifications" onAdvance={onAdvance} optional />;
  }
  if (step === "notifications") {
    return <SimpleStep step={6} title="Критические уведомления" text="По умолчанию владелец получает только подтверждённые важные события. Порог и тихие часы можно изменить в настройках." next="reports" onAdvance={onAdvance} />;
  }
  if (step === "reports") {
    return <SimpleStep step={7} title="Регулярные отчёты" text="Ventrix подготовит сводку по проблемам, обещаниям, клиентам без ответа и результатам команды. Расписание можно изменить в настройках." next="groups" onAdvance={onAdvance} />;
  }
  if (step === "groups") {
    return <SimpleStep step={8} title="Рабочие группы" text="Группы не подключаются автоматически. Позже вставьте ссылку на группу или общую папку Telegram в разделе «Источники» и подтвердите выбор." next="final_review" onAdvance={onAdvance} optional />;
  }
  if (step === "final_review") {
    return <OnboardingFrame step={9} title="Ventrix готов"><Card className="onboarding-complete"><span>✓</span><h2>Настройка завершена</h2><p>Рабочий Telegram: {connection?.account ?? "подключён"}</p><p>Личные диалоги: все новые и существующие</p><p>История: 7 дней · Live monitoring: активно</p><button className="primary-action" onClick={() => void onAdvance("completed")}>Открыть панель</button></Card></OnboardingFrame>;
  }
  return <OnboardingFrame step={10} title="Открываем панель"><Card><p>Настройка сохранена. При следующем запуске откроется dashboard проекта.</p></Card></OnboardingFrame>;
}

function SimpleStep({ step, title, text, next, onAdvance, optional = false }: {
  step: number;
  title: string;
  text: string;
  next: OnboardingStep;
  onAdvance: Advance;
  optional?: boolean;
}) {
  return <OnboardingFrame step={step} title={title}><Card className="onboarding-hero"><p>{text}</p><button className="primary-action" onClick={() => void onAdvance(next)}>Использовать настройки по умолчанию</button>{optional && <button className="text-action" onClick={() => void onAdvance(next, "skipped")}>Пропустить сейчас</button>}</Card></OnboardingFrame>;
}

function OnboardingFrame({ step, title, children }: PropsWithChildren<{ step: number; title: string }>) {
  return <main className="onboarding-shell"><header className="onboarding-header"><div><span className="onboarding-logo">V</span><strong>Ventrix</strong></div><small>Шаг {step} из 10</small><div className="onboarding-progress"><i style={{ width: `${step * 10}%` }} /></div><h1>{title}</h1></header><section className="onboarding-body">{children}</section><footer className="onboarding-security">Данные рабочих сессий защищены шифрованием. Коды подтверждения и пароль 2FA не сохраняются.</footer></main>;
}
