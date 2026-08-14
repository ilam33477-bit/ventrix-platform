import type { TabId } from "../types";
import type { IconName } from "../components/icons";

export const primaryTabs: Array<{ id: TabId; label: string; icon: IconName }> = [
  { id: "dashboard", label: "Главная", icon: "home" },
  { id: "problems", label: "Проблемы", icon: "alert" },
  { id: "statistics", label: "Статистика", icon: "chart" },
  { id: "employees", label: "Команда", icon: "team" },
  { id: "more", label: "Ещё", icon: "more" },
];

export const allSections: Array<{ id: TabId; label: string; icon: IconName }> = [
  ...primaryTabs.slice(0, 4),
  { id: "reports", label: "Отчёты", icon: "report" },
  { id: "connections", label: "Telegram", icon: "telegram" },
  { id: "groups", label: "Группы", icon: "groups" },
  { id: "settings", label: "Настройки", icon: "settings" },
];
