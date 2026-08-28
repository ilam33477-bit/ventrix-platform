"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export function useResource<T>(loader: () => Promise<T>, enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState("");
  const hasLoaded = useRef(false);
  const inFlight = useRef<Promise<void> | null>(null);

  const reload = useCallback(() => {
    if (!enabled) return Promise.resolve();
    if (inFlight.current) return inFlight.current;

    const request = (async () => {
      if (!hasLoaded.current) setLoading(true);
      setError("");
      try {
        setData(await loader());
        hasLoaded.current = true;
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Не удалось загрузить данные");
      } finally {
        setLoading(false);
        inFlight.current = null;
      }
    })();
    inFlight.current = request;
    return request;
  }, [enabled, loader]);

  useEffect(() => {
    const timer = window.setTimeout(() => void reload(), 0);
    return () => window.clearTimeout(timer);
  }, [reload]);
  return { data, loading, error, reload };
}
