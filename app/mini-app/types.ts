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
  | "mini_guide"
  | "telegram_connection"
  | "monitoring_started"
  | "employees"
  | "notifications"
  | "reports"
  | "groups"
  | "final_review"
  | "completed";

export type ClientOnboarding = {
  step: OnboardingStep;
  completed: boolean;
  completed_at: string | null;
  steps: OnboardingStep[];
  statuses: Partial<Record<OnboardingStep, "completed" | "skipped">>;
};

export type DashboardSummary = {
  problems: number;
  signals: number;
  commitments: number;
  reports: number;
  employees: number;
  connections: number;
  groups: number;
  ai_usage: { calls_today: number };
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
  status: ProblemStatus;
  responsible_employee_id: string | null;
  deadline_at: string | null;
  occurred_at: string;
};

export type ProblemStatus =
  | "new" | "needs_confirmation" | "acknowledged" | "assigned"
  | "in_progress" | "waiting" | "resolved" | "auto_resolved"
  | "false_positive" | "ignored" | "reopened";

export type ProblemDetail = Problem & {
  responsible_employee_name: string | null;
  context_messages: Array<{
    id: string;
    text: string;
    outgoing: boolean;
    sender_role: string | null;
    sent_at: string;
    is_source: boolean;
  }>;
  closed_reason: string | null;
  resolution_evidence: string | null;
  transitions: Array<{
    from_status: string;
    to_status: string;
    actor_type: string;
    reason: string;
    evidence: string | null;
    occurred_at: string;
  }>;
  verifications: Array<{
    outcome: string;
    confidence: number;
    method: string;
    reason: string;
    evidence_message_ids: string[];
    checked_at: string;
  }>;
};

export type Commitment = {
  id: string;
  type: string;
  status: string;
  expected_action: string;
  deadline_at: string | null;
  employee_id: string | null;
  dialog_id: string;
  confidence: number;
  linked_problem_id: string | null;
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
  code_delivery_method?: string;
  next_code_delivery_method?: string | null;
  resend_available_in?: number;
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
  access_status?: string | null;
};

export type GroupIntegration = {
  id: string;
  telegram_chat_id: number;
  title: string;
  status: string;
  participants_count: number;
  notifications_enabled: boolean;
  minimum_criticality: number;
  reminder_cooldown_minutes?: number;
};

export type ReportSummary = {
  id: string;
  status: string;
  summary: string;
  period_start?: string;
  period_end?: string;
  created_at?: string;
};

export type ReportDetail = ReportSummary & {
  period: { start: string; end: string };
  sections: Array<{ key: string; position: number; data: Record<string, unknown> }>;
  metrics: Record<string, number>;
  problem_ids: string[];
};

export type ClientSettings = {
  timezone: string;
  daily_report_time: string;
  analysis_enabled: boolean;
  enabled_days: number[];
  history_window_days: number;
  signal_report_threshold: number;
  signal_problem_threshold: number;
  signal_immediate_threshold: number;
  manager_notification_threshold: number;
  employee_notification_threshold: number;
  group_notification_threshold: number;
  notification_immediate_threshold: number;
  employee_notifications_enabled: boolean;
  group_reminders_enabled: boolean;
  next_analysis_at: string | null;
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
  tenant: {
    id: string;
    name: string;
    owner_name: string;
    owner_username: string | null;
    niche: string;
    business_description: string;
    target_audience: string;
    monitoring_priorities: string[];
    welcome: {
      headline: string;
      message: string;
      benefits: string[];
    };
  };
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
  employee_count: number;
  group_count: number;
  problems: Problem[];
};

export type Folder = { id: number; title: string; chat_count: number };

export type TelegramSource = {
  id: string;
  canonical_peer_id: string;
  type: "group" | "channel";
  title: string;
  enabled: boolean;
  added_via: string;
  metadata: { participants_count?: number | null };
};

export type TelegramSourcePreview = {
  kind: "group" | "folder";
  token: string;
  requires_join: boolean;
  peers: Array<{
    canonical_peer_id: string;
    title: string;
    source_type: "group" | "channel";
    participants_count?: number | null;
  }>;
};

export type AsyncJob<T = Record<string, unknown>> = {
  id: string;
  status: string;
  result: T | null;
  last_error: string | null;
};

export type TabId =
  | "dashboard"
  | "problems"
  | "commitments"
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
