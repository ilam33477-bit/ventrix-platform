import type { TabId } from "../types";

export const primaryTabs: Array<{ id: TabId; label: string; icon: string }> = [
  { id: "dashboard", label: "Главная", icon: "⌂" },
  { id: "problems", label: "Проблемы", icon: "!" },
  { id: "statistics", label: "Статистика", icon: "⌁" },
  { id: "employees", label: "Команда", icon: "◎" },
  { id: "more", label: "Ещё", icon: "•••" },
];

export const allSections: Array<{ id: TabId; label: string; icon: string }> = [
  ...primaryTabs.slice(0, 4),
  { id: "reports", label: "Отчёты", icon: "▤" },
  { id: "connections", label: "Telegram", icon: "↔" },
  { id: "groups", label: "Группы", icon: "◫" },
  { id: "settings", label: "Настройки", icon: "⚙" },
];
