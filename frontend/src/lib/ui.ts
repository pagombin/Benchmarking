// Small cross-cutting UI utilities (bug-bash Part 2): per-route document
// titles and client-side CSV export for the analytical tables.

import { useEffect } from "react";

export function usePageTitle(title: string) {
  useEffect(() => {
    const prev = document.title;
    document.title = title ? `${title} · pgbench` : "pgbench console";
    return () => { document.title = prev; };
  }, [title]);
}

/** Download rows as a CSV file, quoting defensively. */
export function exportCsv(filename: string, header: string[],
                          rows: (string | number | null | undefined)[][]) {
  const esc = (v: string | number | null | undefined) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const text = [header, ...rows].map((r) => r.map(esc).join(",")).join("\n");
  const blob = new Blob([text], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}
