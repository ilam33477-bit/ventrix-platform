"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { ClientApiError, VentrixClientApi } from "../api/client";
import { initializeTelegramMiniApp } from "../telegram/bridge";
import type { ClientSession, LaunchState, OnboardingStep } from "../types";

export function useMiniAppSession() {
  const [launchState, setLaunchState] = useState<LaunchState>("checking");
  const [session, setSession] = useState<ClientSession | null>(null);
  const [error, setError] = useState("");
  const [api, setApi] = useState<VentrixClientApi | null>(null);
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";

  const load = useCallback(async (client: VentrixClientApi) => {
    setLaunchState("authenticating");
    setError("");
    try {
      setSession(await client.loadSession());
      setLaunchState("authenticated");
    } catch (reason) {
      if (reason instanceof ClientApiError && [401, 403].includes(reason.status)) {
        setLaunchState("denied");
      } else {
        setError(reason instanceof Error ? reason.message : "Не удалось открыть проект");
        setLaunchState("error");
      }
    }
  }, []);

  useEffect(() => {
    let cleanup: (() => void) | undefined;
    const timer = window.setTimeout(() => {
      const telegram = initializeTelegramMiniApp();
      cleanup = telegram.cleanup;
      if (!telegram.initData) {
        setLaunchState("outside_telegram");
        return;
      }
      const client = new VentrixClientApi(apiBase, telegram.initData);
      setApi(client);
      void load(client);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      cleanup?.();
    };
  }, [apiBase, load]);

  const advanceOnboarding = useCallback(async (step: OnboardingStep) => {
    if (!api || !session) return;
    const onboarding = await api.updateOnboarding(step);
    setSession({
      ...session,
      auth: {
        ...session.auth,
        project_context: { ...session.auth.project_context, onboarding },
      },
      bootstrap: { ...session.bootstrap, onboarding },
    });
  }, [api, session]);

  const refresh = useCallback(async () => {
    if (api) await load(api);
  }, [api, load]);

  return useMemo(() => ({
    launchState,
    session,
    api,
    error,
    refresh,
    advanceOnboarding,
  }), [advanceOnboarding, api, error, launchState, refresh, session]);
}
