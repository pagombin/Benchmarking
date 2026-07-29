// Typed client over the existing FastAPI JSON API.
//
// Auth: the browser carries the Secure httpOnly session cookie automatically;
// state-changing calls double-submit the readable `pgbench_csrf` cookie as an
// X-CSRF-Token header (the server's existing CSRF check). A 401 means the
// session lapsed — bounce to the server-rendered /login.

export function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)pgbench_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
  const res = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401) {
    if (window.location.pathname !== "/login") window.location.href = "/login";
    throw new ApiError(401, "authentication required");
  }
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    if (data && typeof data === "object" && "detail" in data) {
      detail = String((data as Record<string, unknown>).detail);
    }
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export const api = {
  get: <T>(url: string) => request<T>("GET", url),
  post: <T>(url: string, body?: unknown) => request<T>("POST", url, body ?? {}),
  del: <T>(url: string) => request<T>("DELETE", url),
  raw: (url: string) => fetch(url, { credentials: "same-origin" }),
};

export { ApiError };
