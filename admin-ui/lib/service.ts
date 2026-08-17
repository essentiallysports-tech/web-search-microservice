/**
 * Server-only client for the search microservice's admin API.
 *
 * Every function here runs in a Next.js route handler, never in the browser. That is
 * the whole point of the split: the admin key must not reach client-side JavaScript,
 * where any visitor could read it out of the bundle or the network tab.
 *
 * The key is not a Vercel environment variable either. It is supplied per-session by
 * whoever signs in and held in an httpOnly cookie, so this deployment stores no
 * long-lived credential at all — a compromised Vercel project leaks nothing.
 */

const BASE_URL = process.env.SEARCH_SERVICE_URL;

export class ServiceError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export type TokenInfo = {
  id: string;
  name: string;
  created_at: number;
  expires_at: number | null;
  last_used_at: number | null;
};

export type TokenList = {
  tokens: TokenInfo[];
  count: number;
  static_keys: number;
};

export type TokenCreated = TokenInfo & { secret: string };

function baseUrl(): string {
  if (!BASE_URL) {
    throw new ServiceError(
      500,
      "SEARCH_SERVICE_URL is not set. Point it at the microservice, e.g. https://search.internal.example.com",
    );
  }
  return BASE_URL.replace(/\/+$/, "");
}

async function call<T>(
  path: string,
  adminKey: string,
  init: RequestInit = {},
): Promise<T | null> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl()}${path}`, {
      ...init,
      headers: {
        "X-API-Key": adminKey,
        "Content-Type": "application/json",
        ...(init.headers ?? {}),
      },
      // Admin views must never show a cached token list — a revoked token still
      // appearing would be actively misleading.
      cache: "no-store",
    });
  } catch (cause) {
    throw new ServiceError(
      502,
      `cannot reach the search service at ${baseUrl()}. Is it running and publicly resolvable?`,
    );
  }

  if (response.status === 204) return null;

  const text = await response.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }

  if (!response.ok) {
    throw new ServiceError(response.status, describe(response.status, body));
  }
  return body as T;
}

/** Turn the service's error shapes into something an operator can act on. */
function describe(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (status === 401) return "That admin key was not accepted.";
  if (status === 403) {
    return "That key is valid but is not an admin key. Admin actions need a key from SERVICE_API_KEYS, not an issued token.";
  }
  if (status === 503) {
    return "The service reached but its token store is unavailable. Tokens live in Redis — check that it is up.";
  }
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const d = detail as { error?: string; hint?: string };
    return [d.error, d.hint].filter(Boolean).join(" — ") || `HTTP ${status}`;
  }
  return `HTTP ${status}`;
}

export function listTokens(adminKey: string) {
  return call<TokenList>("/admin/tokens", adminKey) as Promise<TokenList>;
}

export function createToken(
  adminKey: string,
  name: string,
  expiresInDays: number | null,
) {
  return call<TokenCreated>("/admin/tokens", adminKey, {
    method: "POST",
    body: JSON.stringify({
      name,
      ...(expiresInDays ? { expires_in_days: expiresInDays } : {}),
    }),
  }) as Promise<TokenCreated>;
}

export function revokeToken(adminKey: string, id: string) {
  return call<null>(`/admin/tokens/${encodeURIComponent(id)}`, adminKey, {
    method: "DELETE",
  });
}

/** Cheap credential check used by the sign-in route. */
export async function verifyAdminKey(adminKey: string): Promise<void> {
  await listTokens(adminKey);
}
