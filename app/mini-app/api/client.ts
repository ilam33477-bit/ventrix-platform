import type {
  Bootstrap,
  ClientOnboarding,
  Employee,
  Folder,
  GroupIntegration,
  MiniAppAuth,
  OnboardingStep,
  ReportSummary,
  TelegramConnection,
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

  updateOnboarding(step: OnboardingStep) {
    return this.request<ClientOnboarding>("/onboarding", {
      method: "PATCH",
      body: JSON.stringify({ step }),
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

  startTelegramLogin(phone: string, employeeId: string | null) {
    return this.request<{ id: string }>("/connections/login/start", {
      method: "POST",
      body: JSON.stringify({ phone, employee_id: employeeId }),
    });
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
}
