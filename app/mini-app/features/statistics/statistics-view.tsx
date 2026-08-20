"use client";

import { useCallback, useMemo } from "react";
import type { CSSProperties } from "react";

import type { VentrixClientApi } from "../../api/client";
import { AnimatedNumber, ChartContainer, MetricCard, SectionHeading, Skeleton } from "../../components/ui";
import { Icon } from "../../components/icons";
import { useResource } from "../../hooks/use-resource";
import { problemTitle } from "../../lib/problem-presentation";
import type { DashboardSummary, Problem } from "../../types";

type StatisticsData = { active: Problem[]; resolved: Problem[] };
type DistributionItem = { label: string; value: number; color: string };

export function StatisticsView({ api, summary }: { api: VentrixClientApi; summary: DashboardSummary }) {
  const loader = useCallback(async (): Promise<StatisticsData> => {
    const [active, resolved, autoResolved] = await Promise.all([
      api.problems(),
      api.problems("resolved"),
      api.problems("auto_resolved"),
    ]);
    return { active, resolved: [...resolved, ...autoResolved] };
  }, [api]);
  const { data, loading, error, reload } = useResource(loader);
  const activeCount = data?.active.length ?? summary.problems;
  const resolvedCount = data?.resolved.length ?? 0;
  const statusDistribution: DistributionItem[] = [
    { label: "В работе", value: activeCount, color: "var(--chart-secondary)" },
    { label: "Решено", value: resolvedCount, color: "var(--success)" },
  ];
  const statusTotal = statusDistribution.reduce((sum, item) => sum + item.value, 0);
  const typeDistribution = useMemo(() => rankBy(data?.active ?? [], (problem) => problem.type, (problem) => problemTitle(problem)), [data?.active]);
  const teamDistribution = useMemo(() => rankBy(
    data?.active ?? [],
    (problem) => problem.responsible_employee_id ?? "unassigned",
    (problem) => problem.responsible_employee_name ?? "Не назначено",
  ), [data?.active]);
  const coverage = [
    { label: "Сотрудники", value: summary.employees },
    { label: "Telegram-аккаунты", value: summary.connections },
    { label: "Рабочие группы", value: summary.groups },
  ];
  const coverageMax = Math.max(1, ...coverage.map((item) => item.value));

  return <>
    <SectionHeading eyebrow="СТАТИСТИКА" title="Пульс рабочих коммуникаций" description="Текущий срез ситуаций, обязательств и подключённой команды. Только реальные рабочие показатели." />
    <section className="statistics-grid" aria-label="Ключевые показатели">
      <MetricCard value={summary.problems} label="Ситуации в работе" note="требуют управленческого решения" icon={<Icon name="alert" />} />
      <MetricCard value={summary.commitments} label="Открытые обещания" note="действия и договорённости" icon={<Icon name="report" />} />
      <MetricCard value={summary.reports} label="Готовые сводки" note="доступны для просмотра" icon={<Icon name="document" />} />
      <MetricCard value={summary.ai_usage.calls_today} label="AI-проверок сегодня" note="фактические обращения к анализу" icon={<Icon name="chart" />} />
    </section>

    {error && <div className="inline-error statistics-error" role="alert"><span>Не удалось обновить детализацию статистики.</span><button onClick={() => void reload()}>Повторить</button></div>}

    <section className="statistics-charts">
      <ChartContainer title="Открыто и решено" description="Статусы доступных вам рабочих ситуаций.">
        {loading && !data ? <ChartLoading /> : <div className="donut-chart-layout">
          <div className="donut-chart" role="img" aria-label={`Ситуаций всего: ${statusTotal}; в работе: ${statusDistribution[0].value}; решено: ${statusDistribution[1].value}`}>
            <svg viewBox="0 0 120 120">
              <circle className="donut-track" cx="60" cy="60" r="44" />
              {donutSegments(statusDistribution).map((item) => <circle key={item.label} className="donut-segment" cx="60" cy="60" r="44" pathLength="100" stroke={item.color} strokeDasharray={`${item.length} ${100 - item.length}`} strokeDashoffset={-item.offset} />)}
            </svg>
            <div><strong><AnimatedNumber value={statusTotal} /></strong><span>ситуаций</span></div>
          </div>
          <ChartLegend items={statusDistribution} />
        </div>}
      </ChartContainer>

      <ChartContainer title="Ситуации по типам" description="Какие рабочие риски сейчас встречаются чаще.">
        {loading && !data ? <ChartLoading /> : typeDistribution.length ? <RankedBars items={typeDistribution} /> : <ChartEmpty title="Активных ситуаций нет" description="Распределение появится после подтверждённых рабочих событий." />}
      </ChartContainer>

      <ChartContainer title="Нагрузка команды" description="Активные ситуации по ответственным сотрудникам.">
        {loading && !data ? <ChartLoading /> : teamDistribution.length ? <RankedBars items={teamDistribution} /> : <ChartEmpty title="Нагрузка не сформирована" description="Здесь появятся ответственные после назначения ситуаций." />}
      </ChartContainer>

      <ChartContainer title="Контур мониторинга" description="Люди и источники, подключённые к проекту.">
        <div className="bar-chart" role="img" aria-label="Подключённые сотрудники, Telegram-аккаунты и рабочие группы">{coverage.map((item) => <div key={item.label}><header><span>{item.label}</span><strong><AnimatedNumber value={item.value} /></strong></header><div><i style={{ "--bar-value": `${(item.value / coverageMax) * 100}%` } as CSSProperties} /></div></div>)}</div>
      </ChartContainer>
    </section>
  </>;
}

function ChartLoading() {
  return <div className="chart-loading"><Skeleton lines={3} /></div>;
}

function ChartEmpty({ title, description }: { title: string; description: string }) {
  return <div className="chart-empty"><span aria-hidden="true">—</span><strong>{title}</strong><p>{description}</p></div>;
}

function ChartLegend({ items }: { items: DistributionItem[] }) {
  return <div className="chart-legend">{items.map((item) => <div key={item.label}><i style={{ background: item.color }} /><span>{item.label}</span><strong>{item.value.toLocaleString("ru-RU")}</strong></div>)}</div>;
}

function RankedBars({ items }: { items: DistributionItem[] }) {
  const max = Math.max(1, ...items.map((item) => item.value));
  return <div className="ranked-bars" role="list">{items.map((item, index) => <div key={item.label} role="listitem"><header><span><b>{index + 1}</b>{item.label}</span><strong>{item.value.toLocaleString("ru-RU")}</strong></header><div><i style={{ "--bar-value": `${item.value / max * 100}%`, "--bar-color": item.color } as CSSProperties} /></div></div>)}</div>;
}

function rankBy(items: Problem[], keyOf: (problem: Problem) => string, labelOf: (problem: Problem) => string): DistributionItem[] {
  const rows = new Map<string, { label: string; value: number }>();
  for (const problem of items) {
    const key = keyOf(problem);
    const current = rows.get(key);
    rows.set(key, { label: current?.label ?? labelOf(problem), value: (current?.value ?? 0) + 1 });
  }
  const colors = ["var(--chart-primary)", "var(--chart-secondary)", "var(--info)", "var(--chart-muted)"];
  return [...rows.values()]
    .sort((left, right) => right.value - left.value || left.label.localeCompare(right.label, "ru"))
    .slice(0, 4)
    .map((item, index) => ({ ...item, color: colors[index] }));
}

function donutSegments(items: DistributionItem[]) {
  const total = items.reduce((sum, item) => sum + item.value, 0);
  if (!total) return [];
  let offset = 0;
  return items.map((item) => {
    const length = item.value / total * 100;
    const result = { ...item, length, offset };
    offset += length;
    return result;
  });
}
