/**
 * fetch wrapper for the admin dashboard.
 *
 * Three responsibilities:
 *   1. Always send the cookie (`credentials: 'same-origin'`) — the entire
 *      auth model is the `tm_sid` HttpOnly cookie, no Authorization headers.
 *   2. Always include the CSRF header on writes (`X-Requested-With`) — the
 *      server refuses POST/DELETE without it (see auth_session module).
 *   3. Turn 401 from a deep endpoint into an automatic bounce to the login
 *      page. The Router still renders `<ProtectedRoute>` for the same effect,
 *      this is the safety net for direct background fetches.
 */

const BASE = '';

export class ApiError extends Error {
  status: number;
  payload: unknown;
  constructor(status: number, payload: unknown, message?: string) {
    super(message ?? `HTTP ${status}`);
    this.status = status;
    this.payload = payload;
  }
}

type Method = 'GET' | 'POST' | 'PUT' | 'DELETE';

async function request<T>(method: Method, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = {
    method,
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-Requested-With': 'XMLHttpRequest',
    },
  };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
  }

  const res = await fetch(`${BASE}${path}`, init);

  if (res.status === 401 && !path.endsWith('/admin/ui/login') && !path.endsWith('/admin/ui/me')) {
    if (typeof window !== 'undefined' && !window.location.pathname.endsWith('/admin/ui/login')) {
      window.location.href = '/admin/ui/login';
    }
  }

  const text = await res.text();
  let parsed: unknown = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = text;
  }

  if (!res.ok) {
    throw new ApiError(res.status, parsed);
  }
  return parsed as T;
}

export const api = {
  get: <T = unknown>(path: string) => request<T>('GET', path),
  post: <T = unknown>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T = unknown>(path: string, body?: unknown) => request<T>('PUT', path, body),
  del: <T = unknown>(path: string) => request<T>('DELETE', path),
};
