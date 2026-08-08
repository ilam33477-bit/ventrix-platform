import type { Bootstrap, DashboardSummary } from "../../types";
import { Card, EmptyState, MetricCard, SectionHeading, StatusBadge } from "../../components/ui";

export function DashboardView({ summary, bootstrap, onOpenProblems }: {
  summary: DashboardSummary;
  bootstrap: Bootstrap;
  onOpenProblems: () => void;
}) {
  const metrics = [
    [summary.problems, "Проблемы", "требуют внимания", "!"],
    [summary.signals, "Сигналы", "критичные", "⌁"],
    [summary.commitments, "Обязательства", "открытые", "✓"],
    [summary.reports, "Отчёты", "доступны", "▤"],
  ] as const;
  return <>
    <SectionHeading eyebrow="РАБОЧАЯ СВОДКА" title="Что происходит в коммуникациях" description="Только данные вашего проекта и выбранных рабочих Telegram-источников." />
    <Card className="focus-card">
      <div><StatusBadge tone={summary.problems ? "danger" : "success"}>{summary.problems ? "Требует внимания" : "Всё спокойно"}</StatusBadge><h3>{summary.problems ? `${summary.problems} ситуаций нужно проверить` : "Критичных проблем не найдено"}</h3><p>Каждый вывод Ventrix связан с исходным сообщением и оценкой уверенности.</p></div>
      {summary.problems > 0 && <button onClick={onOpenProblems}>Открыть проблемы</button>}
    </Card>
    <section className="mobile-metrics">{metrics.map(([value, label, note, icon]) => <MetricCard key={label} value={value} label={label} note={note} icon={icon} />)}</section>
    <section className="dashboard-grid">
      <Card><SectionHeading eyebrow="ПОДКЛЮЧЕНИЯ" title="Telegram-контур" /><div className="summary-rows"><p><span>Аккаунты</span><strong>{summary.connections}</strong></p><p><span>Рабочие группы</span><strong>{summary.groups}</strong></p><p><span>Сотрудники</span><strong>{summary.employees}</strong></p></div></Card>
      <Card><SectionHeading eyebrow="АНАЛИЗ" title="Последняя синхронизация" />{bootstrap.progress ? <div className="analysis-progress"><strong>{bootstrap.progress.percent}%</strong><div><i style={{ width: `${bootstrap.progress.percent}%` }} /></div><p>{bootstrap.progress.messages_loaded.toLocaleString("ru-RU")} сообщений · {bootstrap.progress.dialogs_completed} диалогов</p></div> : <EmptyState title="Анализ ещё не запускался" description="После подключения рабочей папки здесь появится прогресс." />}</Card>
    </section>
  </>;
}
