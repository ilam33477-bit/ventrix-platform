import type { Problem } from "../types";

export const PROBLEM_STATUS_LABELS: Record<string, string> = {
  new: "Новая",
  needs_confirmation: "Нужно проверить",
  acknowledged: "Подтверждена",
  assigned: "Назначена",
  in_progress: "В работе",
  waiting: "Ожидает ответа",
  resolved: "Решена",
  auto_resolved: "Исправление подтверждено",
  false_positive: "Не проблема",
  ignored: "Скрыта",
  reopened: "Открыта снова",
};

const PROBLEM_TYPE_LABELS: Record<string, string> = {
  client_without_answer: "Клиент ждёт ответа",
  customer_complaint: "Жалоба клиента",
  commitment_risk: "Обещание под риском",
  overdue_commitment: "Просрочено обязательство",
  broken_commitment: "Обещание не выполнено",
  lost_lead: "Риск потерять клиента",
  customer_question: "Вопрос без ответа",
  waiting_customer: "Ожидается ответ клиента",
  payment_risk: "Риск по оплате",
  deal_risk: "Сделка под риском",
  churn_risk: "Риск потери клиента",
  conflict: "Конфликт в переписке",
  task_risk: "Рабочая задача под риском",
  operational_risk: "Рабочая ситуация требует внимания",
};

export function problemTitle(problem: Pick<Problem, "type" | "explanation">) {
  return PROBLEM_TYPE_LABELS[problem.type] ?? humanize(problem.type) ?? cleanExplanation(problem.explanation);
}

export function cleanExplanation(value: string) {
  return value.replaceAll("SLA", "установленного времени ответа");
}

export function problemPerson(problem: Pick<Problem, "dialog_username">) {
  return problem.dialog_username ? `@${problem.dialog_username}` : "Клиент";
}

export function formatRelativeAge(value: string) {
  const time = new Date(value).getTime();
  if (!Number.isFinite(time)) return "Время не указано";
  const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
  if (seconds < 60) return "Только что";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} мин. назад`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч. назад`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} дн. назад`;
  return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" }).format(new Date(time));
}

export function priorityLabel(priority: Problem["priority"]) {
  if (priority === "critical") return "Критично";
  if (priority === "high") return "Важно";
  return "Средний приоритет";
}

export function priorityTone(priority: Problem["priority"]): "danger" | "warning" | "info" {
  if (priority === "critical") return "danger";
  if (priority === "high") return "warning";
  return "info";
}

export function sortProblemsByPriority(items: Problem[]) {
  const weight: Record<Problem["priority"], number> = { critical: 0, high: 1, medium: 2 };
  return [...items].sort((left, right) => {
    const severity = weight[left.priority] - weight[right.priority];
    if (severity !== 0) return severity;
    return new Date(left.occurred_at).getTime() - new Date(right.occurred_at).getTime();
  });
}

function humanize(value: string) {
  const text = value.replaceAll("_", " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}
