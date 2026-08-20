"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { ButtonHTMLAttributes, PropsWithChildren, ReactNode } from "react";

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
  return <Card className="ui-metric"><span className="ui-metric-icon">{icon}</span><strong>{typeof value === "number" ? <AnimatedNumber value={value} /> : value}</strong><p>{label}</p>{note && <small>{note}</small>}</Card>;
}

export function StatusBadge({ children, tone = "neutral" }: PropsWithChildren<{ tone?: "success" | "warning" | "danger" | "info" | "neutral" }>) {
  return <span className={`status-badge ${tone}`}>{children}</span>;
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return <div className="skeleton" aria-label="Загрузка" aria-live="polite">{Array.from({ length: lines }, (_, index) => <i key={index} />)}</div>;
}

export function EmptyState({ title, description }: { title: string; description: string }) {
  return <Card className="empty-state"><span aria-hidden="true" /><strong>{title}</strong><p>{description}</p></Card>;
}

export function ErrorState({ message, retry }: { message: string; retry: () => void }) {
  return <main className="state-shell"><Card className="state-card"><p className="eyebrow">ОШИБКА ПОДКЛЮЧЕНИЯ</p><h1>Не удалось открыть проект</h1><p>{message}</p><button onClick={retry}>Повторить</button></Card></main>;
}

export function SectionHeading({ eyebrow, title, description }: { eyebrow: string; title: string; description?: string }) {
  return <header className="mini-section-heading"><p className="eyebrow">{eyebrow}</p><h2>{title}</h2>{description && <p>{description}</p>}</header>;
}

export function Button({ variant = "secondary", className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" | "ghost" }) {
  return <button className={`ui-button ${variant} ${className}`.trim()} {...props} />;
}

export function IconButton({ label, className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return <button className={`ui-icon-button ${className}`.trim()} aria-label={label} title={label} {...props} />;
}

export function ProfileButton({ initials, label, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { initials: string; label: string }) {
  return <button className="profile-button" aria-label={label} {...props}><span>{initials}</span></button>;
}

export function SegmentedControl<T extends string>({ value, options, onChange, label }: {
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
  label: string;
}) {
  return <div className="segmented-control" role="radiogroup" aria-label={label}>{options.map((option) => <button key={option.value} role="radio" aria-checked={value === option.value} className={value === option.value ? "active" : ""} onClick={() => onChange(option.value)}>{option.label}</button>)}</div>;
}

export function AnimatedNumber({ value, duration = 520 }: { value: number; duration?: number }) {
  const [display, setDisplay] = useState(value);
  const previous = useRef(value);
  useEffect(() => {
    const from = previous.current;
    previous.current = value;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches || from === value) {
      setDisplay(value);
      return;
    }
    const start = performance.now();
    let frame = 0;
    const tick = (now: number) => {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 4);
      setDisplay(Math.round(from + (value - from) * eased));
      if (progress < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [duration, value]);
  return <>{display.toLocaleString("ru-RU")}</>;
}

export function StatTrend({ value, label, tone = "neutral" }: { value: string; label: string; tone?: "positive" | "negative" | "neutral" }) {
  return <span className={`stat-trend ${tone}`}><strong>{value}</strong><small>{label}</small></span>;
}

export function ChartContainer({ title, description, children, className = "" }: PropsWithChildren<{ title: string; description?: string; className?: string }>) {
  const titleId = useId();
  return <Card className={`chart-container ${className}`.trim()}><header><div><h3 id={titleId}>{title}</h3>{description && <p>{description}</p>}</div></header><div className="chart-area" role="region" aria-labelledby={titleId}>{children}</div></Card>;
}
