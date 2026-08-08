"use client";

import { useMemo, useState } from "react";

import type { Problem } from "../../types";
import { Card, EmptyState, SectionHeading, StatusBadge } from "../../components/ui";

export function ProblemsView({ problems }: { problems: Problem[] }) {
  const [filter, setFilter] = useState<"all" | Problem["priority"]>("all");
  const visible = useMemo(() => problems.filter((item) => filter === "all" || item.priority === filter), [filter, problems]);
  return <>
    <SectionHeading eyebrow="ПРОБЛЕМЫ И СИГНАЛЫ" title="Что требует решения" description="Приоритет, доказательство и рекомендуемое действие — в одной карточке." />
    <div className="chip-row">{(["all", "critical", "high", "medium"] as const).map((item) => <button key={item} className={filter === item ? "active" : ""} onClick={() => setFilter(item)}>{item === "all" ? "Все" : item === "critical" ? "Критичные" : item === "high" ? "Высокие" : "Средние"}</button>)}</div>
    <div className="problem-cards">{visible.map((problem) => <Card key={problem.id} className="problem-card"><div className="problem-card-top"><StatusBadge tone={problem.priority === "critical" ? "danger" : "warning"}>{problem.priority}</StatusBadge><small>{new Date(problem.occurred_at).toLocaleString("ru-RU")}</small></div><h3>{problem.explanation}</h3><blockquote>{problem.evidence}</blockquote><p><strong>Что сделать:</strong> {problem.recommended_action}</p><footer>Уверенность {Math.round(problem.confidence * 100)}%</footer></Card>)}</div>
    {!visible.length && <EmptyState title="Проблем этого приоритета нет" description="Список обновится после следующего анализа." />}
  </>;
}
