"use client";

import { useEffect, useState } from "react";

/**
 * True only after the first client-side render. Needed wherever a
 * component reads state that can legitimately differ between server and
 * client — next-themes' resolved theme, in this foundation's current
 * caller (`components/theme-toggle.tsx`) — since rendering that state
 * before mount produces a hydration mismatch React will warn about.
 */
export function useMounted(): boolean {
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  return mounted;
}
