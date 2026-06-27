import { useEffect, useState } from "react";

type Theme = "dark" | "light";
const KEY = "pgbench_theme";

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = localStorage.getItem(KEY);
    return saved === "light" || saved === "dark" ? saved : "dark";
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(KEY, theme);
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "dark" ? "light" : "dark"))];
}
