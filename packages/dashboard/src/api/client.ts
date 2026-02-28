import { collectNDJSON } from './ndjson.ts';

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api/v1';

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(response: Response, detail?: string) {
    super(`API Error ${response.status}: ${detail ?? response.statusText}`);
    this.status = response.status;
    this.detail = detail ?? response.statusText;
  }
}

export async function apiFetch(
  path: string,
  options: RequestInit & { rawResponse: true },
): Promise<Response>;
export async function apiFetch<T>(
  path: string,
  options?: RequestInit & { rawResponse?: false },
): Promise<T>;
export async function apiFetch<T>(
  path: string,
  options?: RequestInit & { rawResponse?: boolean },
): Promise<T | Response> {
  const url = `${API_BASE}${path}`;
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });

  if (!response.ok) {
    let detail: string | undefined;
    try {
      const body = await response.json();
      detail = body.detail;
    } catch {
      // ignore parse errors
    }
    throw new ApiError(response, detail);
  }

  if (options?.rawResponse) {
    return response;
  }

  const contentType = response.headers.get('content-type') ?? '';
  if (contentType.includes('x-ndjson')) {
    return collectNDJSON(response) as Promise<T>;
  }

  // Handle empty responses (204 No Content)
  if (response.status === 204) {
    return undefined as T;
  }

  return response.json();
}

export const api = {
  get: <T>(path: string) => apiFetch<T>(path),
  post: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  put: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'PUT', body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'PATCH', body: body ? JSON.stringify(body) : undefined }),
  delete: <T>(path: string) => apiFetch<T>(path, { method: 'DELETE' }),
  getRaw: (path: string) => apiFetch(path, { rawResponse: true }),
};
