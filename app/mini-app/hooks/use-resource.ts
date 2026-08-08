"use client";

import { useCallback, useEffect, useState } from "react";

export function useResource<T>(loader: () => Promise<T>, enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    if (!enabled) return;
    setLoading(true);
    setError("");
    try {
      setData(await loader());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось загрузить данные");
    } finally {
      setLoading(false);
    }
  }, [enabled, loader]);

  useEffect(() => {
    const timer = window.setTimeout(() => void reload(), 0);
    return () => window.clearTimeout(timer);
  }, [reload]);
  return { data, loading, error, reload };
}
