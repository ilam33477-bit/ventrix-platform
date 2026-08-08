import type { PropsWithChildren, ReactNode } from "react";

export function MotionSurface({ children, className = "" }: PropsWithChildren<{ className?: string }>) {
  return <div className={`motion-surface ${className}`.trim()}>{children}</div>;
}

export function Card({ children, className = "" }: PropsWithChildren<{ className?: string }>) {
  return <MotionSurface className={`ui-card ${className}`.trim()}>{children}</MotionSurface>;
}

export function MetricCard({ value, label, note, icon }: {
  value: string | number;
  label: string;
  note?: string;
  icon: ReactNode;
}) {
  return <Card className="ui-metric"><span className="ui-metric-icon">{icon}</span><strong>{value}</strong><p>{label}</p>{note && <small>{note}</small>}</Card>;
}

export function StatusBadge({ children, tone = "neutral" }: PropsWithChildren<{ tone?: "success" | "warning" | "danger" | "neutral" }>) {
  return <span className={`status-badge ${tone}`}>{children}</span>;
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return <div className="skeleton" aria-label="Загрузка">{Array.from({ length: lines }, (_, index) => <i key={index} />)}</div>;
}

export function EmptyState({ title, description }: { title: string; description: string }) {
  return <Card className="empty-state"><span>○</span><strong>{title}</strong><p>{description}</p></Card>;
}

export function ErrorState({ message, retry }: { message: string; retry: () => void }) {
  return <main className="state-shell"><Card className="state-card"><p className="eyebrow">ОШИБКА ПОДКЛЮЧЕНИЯ</p><h1>Не удалось открыть проект</h1><p>{message}</p><button onClick={retry}>Повторить</button></Card></main>;
}

export function SectionHeading({ eyebrow, title, description }: { eyebrow: string; title: string; description?: string }) {
  return <header className="mini-section-heading"><p className="eyebrow">{eyebrow}</p><h2>{title}</h2>{description && <p>{description}</p>}</header>;
}
