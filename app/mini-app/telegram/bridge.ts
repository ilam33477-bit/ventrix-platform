type TelegramTheme = Record<string, string | undefined>;
type TelegramInsets = { top?: number; right?: number; bottom?: number; left?: number };

type TelegramWebApp = {
  initData?: string;
  colorScheme?: "light" | "dark";
  themeParams?: TelegramTheme;
  viewportStableHeight?: number;
  safeAreaInset?: TelegramInsets;
  contentSafeAreaInset?: TelegramInsets;
  ready?: () => void;
  expand?: () => void;
  onEvent?: (event: string, callback: () => void) => void;
  offEvent?: (event: string, callback: () => void) => void;
};

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}

function setCssVariable(name: string, value: string | number | undefined) {
  if (value === undefined) return;
  document.documentElement.style.setProperty(name, typeof value === "number" ? `${value}px` : value);
}

export function initializeTelegramMiniApp() {
  const webApp = window.Telegram?.WebApp;
  webApp?.ready?.();
  webApp?.expand?.();

  const applyViewport = () => {
    setCssVariable("--tg-viewport-stable-height", webApp?.viewportStableHeight ?? window.innerHeight);
    const safe = webApp?.safeAreaInset;
    const content = webApp?.contentSafeAreaInset;
    setCssVariable("--tg-safe-top", safe?.top ?? 0);
    setCssVariable("--tg-safe-right", safe?.right ?? 0);
    setCssVariable("--tg-safe-bottom", safe?.bottom ?? 0);
    setCssVariable("--tg-safe-left", safe?.left ?? 0);
    setCssVariable("--tg-content-safe-top", content?.top ?? 0);
    setCssVariable("--tg-content-safe-bottom", content?.bottom ?? 0);
  };
  applyViewport();
  webApp?.onEvent?.("viewportChanged", applyViewport);

  const theme = webApp?.themeParams ?? {};
  setCssVariable("--tg-theme-bg", theme.bg_color);
  setCssVariable("--tg-theme-text", theme.text_color);
  setCssVariable("--tg-theme-hint", theme.hint_color);
  setCssVariable("--tg-theme-button", theme.button_color);
  setCssVariable("--tg-theme-button-text", theme.button_text_color);
  document.documentElement.dataset.telegramTheme = webApp?.colorScheme ?? "light";

  return {
    initData: webApp?.initData ?? "",
    cleanup: () => webApp?.offEvent?.("viewportChanged", applyViewport),
  };
}
