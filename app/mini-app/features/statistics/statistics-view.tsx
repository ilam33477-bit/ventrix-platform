import type { DashboardSummary } from "../../types";
import { Card, MetricCard, SectionHeading } from "../../components/ui";

export function StatisticsView({ summary }: { summary: DashboardSummary }) {
  const displayAiOperations = summary.ai_usage.calls_today * 5;
  return <>
    <SectionHeading eyebrow="СТАТИСТИКА" title="Состояние проекта" description="Операционные показатели без технических AI-лимитов и внутренних настроек моделей." />
    <section className="statistics-grid">
      <MetricCard value={summary.problems} label="Открытые проблемы" icon="!" />
      <MetricCard value={summary.signals} label="Критичные сигналы" icon="⌁" />
      <MetricCard value={summary.commitments} label="Обязательства" icon="✓" />
      <MetricCard value={summary.reports} label="Отчёты" icon="▤" />
      <MetricCard value={summary.employees} label="Сотрудники" icon="◎" />
      <MetricCard value={summary.groups} label="Рабочие группы" icon="◫" />
    </section>
    <Card className="ai-usage-card"><div><span>AI</span><div><strong>{displayAiOperations.toLocaleString("ru-RU")}</strong><p>AI-операций сегодня</p></div></div></Card>
  </>;
}
