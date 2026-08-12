import type {
  Bootstrap,
  ClientSettings,
  ClientOnboarding,
  Commitment,
  Employee,
  Folder,
  GroupIntegration,
  MiniAppAuth,
  OnboardingStep,
  Problem,
  ProblemDetail,
  ProblemStatus,
  ReportDetail,
  ReportSummary,
  TelegramConnection,
  TelegramSource,
  AsyncJob,
} from "../types";

export class ClientApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export class VentrixClientApi {
  constructor(
    private readonly baseUrl: string,
    private readonly initData: string,
  ) {}

  private async request<T>(path: string, options: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.baseUrl}/api/v1/client${path}`, {
      ...options,
      headers: {
        Authorization: `tma ${this.initData}`,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers ?? {}),
      },
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null) as { detail?: string } | null;
      throw new ClientApiError(payload?.detail ?? "Не удалось выполнить запрос", response.status);
    }
    return response.json() as Promise<T>;
  }

  authenticate() {
    return this.request<MiniAppAuth>("/mini-app/auth", { method: "POST" });
  }

  bootstrap() {
    return this.request<Bootstrap>("/bootstrap");
  }

  async loadSession() {
    const auth = await this.authenticate();
    const bootstrap = await this.bootstrap();
    return { auth, bootstrap };
  }

  updateOnboarding(step: OnboardingStep, status: "completed" | "skipped" = "completed") {
    return this.request<ClientOnboarding>("/onboarding", {
      method: "PATCH",
      body: JSON.stringify({ step, status }),
    });
  }

  employees() {
    return this.request<Employee[]>("/employees");
  }

  connections() {
    return this.request<TelegramConnection[]>("/connections");
  }

  groups() {
    return this.request<GroupIntegration[]>("/group-integrations");
  }

  reports() {
    return this.request<ReportSummary[]>("/reports");
  }

  report(reportId: string) {
    return this.request<ReportDetail>(`/reports/${reportId}`);
  }

  problems(status?: string) {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    return this.request<Problem[]>(`/problems${query}`);
  }

  problem(problemId: string) {
    return this.request<ProblemDetail>(`/problems/${problemId}`);
  }

  transitionProblem(problemId: string, value: {
    status: ProblemStatus;
    reason: string;
    evidence?: string;
    responsible_employee_id?: string | null;
    deadline_at?: string | null;
  }) {
    return this.request<{ id: string; status: ProblemStatus }>(`/problems/${problemId}`, {
      method: "PATCH",
      body: JSON.stringify(value),
    });
  }

  commitments() {
    return this.request<Commitment[]>("/commitments");
  }

  updateCommitment(commitmentId: string, status: "completed" | "cancelled", reason: string) {
    return this.request<{ id: string; status: string }>(`/commitments/${commitmentId}`, {
      method: "PATCH",
      body: JSON.stringify({ status, reason }),
    });
  }

  createEmployee(value: {
    display_name: string;
    telegram_user_id?: number | null;
    telegram_username?: string | null;
    role: "manager" | "employee" | "observer";
    notifications_enabled: boolean;
    criticality_threshold: number;
  }) {
    return this.request<{ id: string; name: string }>("/employees", {
      method: "POST",
      body: JSON.stringify(value),
    });
  }

  updateEmployee(employeeId: string, value: Partial<{
    display_name: string;
    telegram_user_id: number | null;
    telegram_username: string | null;
    role: "manager" | "employee" | "observer";
    status: "active" | "inactive";
    notifications_enabled: boolean;
    criticality_threshold: number;
  }>) {
    return this.request<Employee>(`/employees/${employeeId}`, {
      method: "PATCH",
      body: JSON.stringify(value),
    });
  }

  settings() {
    return this.request<ClientSettings>("/settings");
  }

  updateSettings(value: Partial<ClientSettings>) {
    return this.request<ClientSettings>("/settings", {
      method: "PATCH",
      body: JSON.stringify(value),
    });
  }

  updateGroup(groupId: string, value: Partial<{
    title: string;
    status: "pending" | "active" | "disabled";
    notifications_enabled: boolean;
    minimum_criticality: number;
    reminder_cooldown_minutes: number;
  }>) {
    return this.request<GroupIntegration>(`/group-integrations/${groupId}`, {
      method: "PATCH",
      body: JSON.stringify(value),
    });
  }

  startTelegramLogin(phone: string, employeeId: string | null) {
    return this.request<{
      id: string;
      phone_masked: string;
      code_delivery_method: string;
      next_code_delivery_method: string | null;
      resend_available_in: number;
    }>("/connections/login/start", {
      method: "POST",
      body: JSON.stringify({ phone, employee_id: employeeId }),
    });
  }

  resendTelegramLogin(connectionId: string) {
    return this.request<{
      id: string;
      phone_masked: string;
      code_delivery_method: string;
      next_code_delivery_method: string | null;
      resend_available_in: number;
    }>(`/connections/${connectionId}/login/resend`, { method: "POST" });
  }

  completeTelegramLogin(connectionId: string, value: { code?: string; password?: string }) {
    return this.request<{ id: string; status: string; requires_2fa: boolean }>(
      `/connections/${connectionId}/login/complete`,
      { method: "POST", body: JSON.stringify(value) },
    );
  }

  folderCatalog(connectionId: string) {
    return this.request<{ folders: Folder[] }>(`/connections/${connectionId}/catalog`, {
      method: "POST",
    });
  }

  selectScope(
    connectionId: string,
    folderId: number,
    historyDays: number,
    personalDialogsConsent: boolean,
  ) {
    return this.request<{ analysis_run_id: string }>(`/connections/${connectionId}/scope`, {
      method: "POST",
      body: JSON.stringify({
        folder_ids: [folderId],
        history_days: historyDays,
        personal_dialogs_consent: personalDialogsConsent,
      }),
    });
  }

  cancelTelegramLogin(connectionId: string) {
    return this.request<{ cancelled: boolean }>(`/connections/${connectionId}/login/cancel`, {
      method: "POST",
    });
  }

  sources(connectionId: string) {
    return this.request<TelegramSource[]>(`/connections/${connectionId}/sources`);
  }

  previewSource(connectionId: string, link: string) {
    return this.request<{ job_id: string }>(`/connections/${connectionId}/sources/preview`, {
      method: "POST",
      body: JSON.stringify({ link }),
    });
  }

  confirmSources(connectionId: string, previewJobId: string, peerIds: string[], join: boolean) {
    return this.request<{ job_id: string }>(`/connections/${connectionId}/sources/confirm`, {
      method: "POST",
      body: JSON.stringify({
        preview_job_id: previewJobId,
        selected_peer_ids: peerIds,
        join,
      }),
    });
  }

  job<T = Record<string, unknown>>(jobId: string) {
    return this.request<AsyncJob<T>>(`/sync/${jobId}`);
  }
}
