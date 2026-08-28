import type { TabId } from "../types";
import type { IconName } from "../components/icons";

export const primaryTabs: Array<{ id: TabId; label: string; icon: IconName }> = [
  { id: "dashboard", label: "Главная", icon: "home" },
  { id: "problems", label: "Ситуации", icon: "alert" },
  { id: "reports", label: "Отчёты", icon: "report" },
  { id: "more", label: "Ещё", icon: "more" },
];

export const allSections: Array<{ id: TabId; label: string; icon: IconName }> = [
  { id: "dashboard", label: "Главная", icon: "home" },
  { id: "problems", label: "Ситуации", icon: "alert" },
  { id: "reports", label: "Отчёты", icon: "report" },
  { id: "statistics", label: "Статистика", icon: "chart" },
  { id: "employees", label: "Команда", icon: "team" },
  { id: "commitments", label: "Обязательства", icon: "document" },
  { id: "connections", label: "Telegram", icon: "telegram" },
  { id: "groups", label: "Группы", icon: "groups" },
  { id: "settings", label: "Настройки", icon: "settings" },
];
