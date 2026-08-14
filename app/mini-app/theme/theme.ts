"use client";

export type ThemeMode = "light" | "dark" | "telegram";
export type ResolvedTheme = "light" | "dark";

const MODE_KEY = "ventrix-theme-mode";
const RESOLVED_KEY = "ventrix-resolved-theme";
const THEME_EVENT = "ventrix:theme-change";

function telegramScheme(): ResolvedTheme | null {
  return window.Telegram?.WebApp?.colorScheme ?? null;
}
export function getThemeMode(): ThemeMode {
  const saved = window.localStorage.getItem(MODE_KEY);
  return saved === "light" || saved === "dark" || saved === "telegram" ? saved : "telegram";
}

export function resolveTheme(mode: ThemeMode): ResolvedTheme {
  if (mode !== "telegram") return mode;
  return telegramScheme()
    ?? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

export function applyTheme(mode: ThemeMode = getThemeMode()) {
  const resolved = resolveTheme(mode);
  const root = document.documentElement;
  root.dataset.themeMode = mode;
  root.dataset.theme = resolved;
  root.style.colorScheme = resolved;
  window.localStorage.setItem(MODE_KEY, mode);
  window.localStorage.setItem(RESOLVED_KEY, resolved);
  window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: { mode, resolved } }));
  return { mode, resolved };
}

export function setThemeMode(mode: ThemeMode) {
  return applyTheme(mode);
}

export function subscribeToTheme(callback: (mode: ThemeMode, resolved: ResolvedTheme) => void) {
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const refresh = () => {
    const value = applyTheme();
    callback(value.mode, value.resolved);
  };
  const handleThemeEvent = (event: Event) => {
    const detail = (event as CustomEvent<{ mode: ThemeMode; resolved: ResolvedTheme }>).detail;
    callback(detail.mode, detail.resolved);
  };
  media.addEventListener("change", refresh);
  window.addEventListener(THEME_EVENT, handleThemeEvent);
  return () => {
    media.removeEventListener("change", refresh);
    window.removeEventListener(THEME_EVENT, handleThemeEvent);
  };
}
