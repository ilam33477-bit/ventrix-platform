export type LaunchState =
  | "checking"
  | "outside_telegram"
  | "authenticating"
  | "authenticated"
  | "denied"
  | "error";

export type ConnectionState =
  | "not_connected"
  | "connecting"
  | "folder_selection"
  | "chat_selection"
  | "synchronization"
  | "ready"
  | "reauthorization_required";

export type OnboardingStep =
  | "welcome"
  | "telegram_connection"
  | "scope_selection"
  | "employees_review"
  | "completed";

export type ClientOnboarding = {
  step: OnboardingStep;
  completed: boolean;
  completed_at: string | null;
  steps: OnboardingStep[];
};

export type DashboardSummary = {
  problems: number;
  signals: number;
  commitments: number;
  reports: number;
  employees: number;
  connections: number;
  groups: number;
  ai_usage: { tokens_today: number; calls_today: number };
};

export type MiniAppAuth = {
  tenant_id: string;
  tenant_name: string;
  user: {
    telegram_user_id: number;
    first_name: string | null;
    last_name: string | null;
    username: string | null;
    role: string;
  };
  permissions: string[];
  project_context: {
    status: string;
    timezone: string | null;
    client_bot: { id: string; username: string };
    onboarding_state: ConnectionState;
    onboarding: ClientOnboarding;
  };
  dashboard_summary: DashboardSummary;
};

export type Problem = {
  id: string;
  type: string;
  priority: "critical" | "high" | "medium";
  confidence: number;
  evidence: string;
  explanation: string;
  recommended_action: string;
  occurred_at: string;
};

export type TelegramConnection = {
  id: string;
  status: string;
  account: string | null;
  username?: string | null;
  health_status?: string;
  last_incremental_sync_at?: string | null;
  last_health_check_at?: string | null;
  last_sync_at?: string | null;
  folder?: string | null;
};

export type Employee = {
  id: string;
  name: string;
  telegram_user_id: number | null;
  telegram_username: string | null;
  role: string;
  status: string;
  notifications_enabled: boolean;
  criticality_threshold: number;
};

export type GroupIntegration = {
  id: string;
  telegram_chat_id: number;
  title: string;
  status: string;
  participants_count: number;
  notifications_enabled: boolean;
  minimum_criticality: number;
};

export type ReportSummary = {
  id: string;
  status: string;
  summary: string;
  period_start?: string;
  period_end?: string;
  created_at?: string;
};

export type AnalysisProgress = {
  status: string;
  stage: string;
  percent: number;
  dialogs_total: number;
  dialogs_completed: number;
  failed_dialogs: number;
  messages_loaded: number;
  metrics: Record<string, number | string | null>;
};

export type Bootstrap = {
  tenant: { id: string; name: string };
  role: string;
  permissions: string[];
  onboarding_state: ConnectionState;
  onboarding: ClientOnboarding;
  menu: string[];
  connection: null | {
    status: string;
    account: string | null;
    folder: string | null;
    history_days: number;
    personal_dialogs_consent: boolean;
  };
  connections: TelegramConnection[];
  progress: AnalysisProgress | null;
  dialog_counts: Record<string, number>;
  problems: Problem[];
};

export type Folder = { id: number; title: string; chat_count: number };

export type TabId =
  | "dashboard"
  | "problems"
  | "statistics"
  | "reports"
  | "employees"
  | "connections"
  | "groups"
  | "settings"
  | "more";

export type ClientSession = {
  auth: MiniAppAuth;
  bootstrap: Bootstrap;
};
