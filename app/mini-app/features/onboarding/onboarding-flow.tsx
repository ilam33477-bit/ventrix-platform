"use client";

import { useCallback, useState, type PropsWithChildren } from "react";

import type { VentrixClientApi } from "../../api/client";
import type { Bootstrap, OnboardingStep } from "../../types";
import { Card, EmptyState, SectionHeading, Skeleton, StatusBadge } from "../../components/ui";
import { ConnectionManager } from "../connections/connection-manager";
import { useResource } from "../../hooks/use-resource";

const stepNumber: Record<OnboardingStep, number> = {
  welcome: 1,
  telegram_connection: 2,
  scope_selection: 3,
  employees_review: 4,
  completed: 5,
};

export function OnboardingFlow({ api, bootstrap, onAdvance }: {
  api: VentrixClientApi;
  bootstrap: Bootstrap;
  onAdvance: (step: OnboardingStep) => Promise<void>;
}) {
  const step = bootstrap.onboarding.step;
  const [showComplete, setShowComplete] = useState(false);
  const employeeLoader = useCallback(() => api.employees(), [api]);
  const employees = useResource(employeeLoader, step === "employees_review");

  if (step === "welcome") {
    return <OnboardingFrame step={1} title="Добро пожаловать в Ventrix"><Card className="onboarding-hero"><span className="onboarding-mark">V</span><h2>Не теряйте клиентов, обещания и важные задачи</h2><p>Ventrix анализирует выбранные рабочие Telegram-коммуникации, находит риски и собирает их в понятную панель проекта.</p><button className="primary-action" onClick={() => void onAdvance("telegram_connection")}>Начать настройку</button></Card></OnboardingFrame>;
  }

  if (step === "telegram_connection" || step === "scope_selection") {
    return <OnboardingFrame step={stepNumber[step]} title={step === "telegram_connection" ? "Подключите рабочий Telegram" : "Выберите папку для анализа"}><ConnectionManager api={api} connections={bootstrap.connections} onboardingStep={step} onOnboardingStep={onAdvance} /></OnboardingFrame>;
  }

  if (step === "employees_review" && !showComplete) {
    return <OnboardingFrame step={4} title="Проверьте сотрудников"><SectionHeading eyebrow="КОМАНДА" title="Кто работает с клиентами" description="Убедитесь, что рабочие аккаунты и сотрудники относятся к вашему проекту." />{employees.loading ? <Skeleton lines={4} /> : employees.data?.length ? <div className="section-list">{employees.data.map((employee) => <Card key={employee.id} className="person-row"><span>{employee.name.slice(0, 2).toUpperCase()}</span><div><h3>{employee.name}</h3><p>{employee.telegram_username ? `@${employee.telegram_username}` : "Без Telegram username"}</p></div><StatusBadge tone={employee.status === "active" ? "success" : "neutral"}>{employee.role}</StatusBadge></Card>)}</div> : <EmptyState title="Список сотрудников пуст" description="Это не мешает завершить настройку — сотрудников можно добавить позже." />}<button className="primary-action onboarding-next" onClick={() => setShowComplete(true)}>Всё верно</button></OnboardingFrame>;
  }

  return <OnboardingFrame step={5} title="Ventrix готов к работе"><Card className="onboarding-complete"><span>✓</span><h2>Настройка завершена</h2><p>Первичный анализ продолжится в фоне. Теперь при каждом открытии Mini App вы сразу попадёте в dashboard своего проекта.</p><button className="primary-action" onClick={() => void onAdvance("completed")}>Перейти в панель</button></Card></OnboardingFrame>;
}

function OnboardingFrame({ step, title, children }: PropsWithChildren<{ step: number; title: string }>) {
  return <main className="onboarding-shell"><header className="onboarding-header"><div><span className="onboarding-logo">V</span><strong>Ventrix</strong></div><small>Шаг {step} из 5</small><div className="onboarding-progress"><i style={{ width: `${step * 20}%` }} /></div><h1>{title}</h1></header><section className="onboarding-body">{children}</section><footer className="onboarding-security">Данные рабочих сессий защищены шифрованием. Коды подтверждения и пароль 2FA не сохраняются.</footer></main>;
}
