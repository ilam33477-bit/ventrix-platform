import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import Script from "next/script";
import "./globals.css";

const title = "Ventrix — рабочие коммуникации под контролем";
const description = "Проблемы, обязательства и контроль исполнения в рабочих Telegram-коммуникациях.";

export const viewport: Viewport = {
  colorScheme: "light dark",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f3f5f2" },
    { media: "(prefers-color-scheme: dark)", color: "#101513" },
  ],
  viewportFit: "cover",
};

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") ?? requestHeaders.get("host") ?? "localhost:3000";
  const protocol = requestHeaders.get("x-forwarded-proto") ?? (host.startsWith("localhost") ? "http" : "https");
  const origin = `${protocol}://${host}`;
  return {
    metadataBase: new URL(origin),
    title,
    description,
    openGraph: { title, description, images: [{ url: `${origin}/og.png`, width: 1200, height: 630 }] },
    twitter: { card: "summary_large_image", title, description, images: [`${origin}/og.png`] },
  };
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const themeBootstrap = `(function(){try{var m=localStorage.getItem('ventrix-theme-mode')||'telegram';var r=m==='telegram'?(localStorage.getItem('ventrix-resolved-theme')||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light')):m;document.documentElement.dataset.themeMode=m;document.documentElement.dataset.theme=r;document.documentElement.style.colorScheme=r}catch(e){}})()`;
  return <html lang="ru" suppressHydrationWarning><body><Script src="https://telegram.org/js/telegram-web-app.js" strategy="beforeInteractive" /><Script id="ventrix-theme" strategy="beforeInteractive">{themeBootstrap}</Script>{children}</body></html>;
}
