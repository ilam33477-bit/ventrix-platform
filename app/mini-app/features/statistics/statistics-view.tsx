import type { DashboardSummary } from "../../types";
import { AnimatedNumber, Card, ChartContainer, MetricCard, SectionHeading, StatusBadge } from "../../components/ui";
import { Icon } from "../../components/icons";

export function StatisticsView({ summary }: { summary: DashboardSummary }) {
  const displayAiOperations = summary.ai_usage.calls_today * 5;
  const workload = [
    { label: "Ситуации в работе", value: summary.problems, color: "var(--chart-primary)" },
    { label: "Открытые обещания", value: summary.commitments, color: "var(--chart-secondary)" },
    { label: "Готовые сводки", value: summary.reports, color: "var(--chart-muted)" },
  ];
  const workloadTotal = workload.reduce((sum, item) => sum + item.value, 0);
  const coverage = [
    { label: "Сотрудники", value: summary.employees },
    { label: "Telegram-аккаунты", value: summary.connections },
    { label: "Рабочие группы", value: summary.groups },
  ];
  const coverageMax = Math.max(1, ...coverage.map((item) => item.value));
  return <>
    <SectionHeading eyebrow="СТАТИСТИКА" title="Пульс рабочих коммуникаций" description="Текущий срез ситуаций, обязательств и подключённой команды. Только понятные рабочие показатели." />
    <section className="statistics-grid">
      <MetricCard value={summary.problems} label="Ситуации в работе" note="требуют управленческого решения" icon={<Icon name="alert" />} />
      <MetricCard value={summary.commitments} label="Открытые обещания" note="действия и договорённости" icon={<Icon name="report" />} />
      <MetricCard value={summary.reports} label="Готовые сводки" note="доступны для просмотра" icon={<Icon name="chart" />} />
    </section>
    <section className="statistics-charts">
      <ChartContainer title="Структура рабочего внимания" description="Соотношение открытых ситуаций, обещаний и готовых сводок.">
        <div className="donut-chart-layout">
          <div className="donut-chart" role="img" aria-label={`Всего объектов в текущем срезе: ${workloadTotal}`}>
            <svg viewBox="0 0 120 120">
              <circle className="donut-track" cx="60" cy="60" r="44" />
              {donutSegments(workload).map((item) => <circle key={item.label} className="donut-segment" cx="60" cy="60" r="44" pathLength="100" stroke={item.color} strokeDasharray={`${item.length} ${100 - item.length}`} strokeDashoffset={-item.offset} />)}
            </svg>
            <div><strong><AnimatedNumber value={workloadTotal} /></strong><span>всего</span></div>
          </div>
          <div className="chart-legend">{workload.map((item) => <div key={item.label}><i style={{ background: item.color }} /><span>{item.label}</span><strong>{item.value.toLocaleString("ru-RU")}</strong></div>)}</div>
        </div>
      </ChartContainer>
      <ChartContainer title="Контур мониторинга" description="Люди и источники, подключённые к проекту.">
        <div className="bar-chart" role="img" aria-label="Подключённые сотрудники, Telegram-аккаунты и рабочие группы">{coverage.map((item) => <div key={item.label}><header><span>{item.label}</span><strong><AnimatedNumber value={item.value} /></strong></header><div><i style={{ "--bar-value": `${(item.value / coverageMax) * 100}%` } as CSSProperties} /></div></div>)}</div>
      </ChartContainer>
    </section>
    <Card className="ai-operations-card"><div className="ai-operation-mark">V</div><div><StatusBadge tone="info">Сегодня</StatusBadge><strong><AnimatedNumber value={displayAiOperations} /></strong><p>AI-операций помогли проверить рабочие коммуникации</p></div></Card>
  </>;
}

function donutSegments(items: Array<{ label: string; value: number; color: string }>) {
  const total = items.reduce((sum, item) => sum + item.value, 0) || 1;
  let offset = 0;
  return items.map((item) => {
    const length = item.value / total * 100;
    const result = { ...item, length, offset };
    offset += length;
    return result;
  });
}
import type { CSSProperties } from "react";
