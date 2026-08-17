/**
 * Session handling: the admin key lives in an httpOnly cookie, nothing else.
 *
 * Deliberately not a Vercel environment variable. An env var would mean this
 * deployment permanently holds a credential that can mint API tokens, so anyone with
 * access to the Vercel project — or to a build log that echoed it — would have it.
 * A per-session cookie keeps the blast radius at one browser.
 *
 * httpOnly so client JavaScript cannot read it, sameSite=lax so another site cannot
 * ride the session, and secure in production so it never crosses plain HTTP.
 */

import { cookies } from "next/headers";

const COOKIE = "wss_admin_key";

/** Eight hours. Long enough for a working session, short enough that a forgotten
 *  open tab is not a standing credential. */
const MAX_AGE_S = 8 * 60 * 60;

export async function setSession(adminKey: string): Promise<void> {
  const jar = await cookies();
  jar.set(COOKIE, adminKey, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: MAX_AGE_S,
  });
}

export async function clearSession(): Promise<void> {
  const jar = await cookies();
  jar.delete(COOKIE);
}

export async function getAdminKey(): Promise<string | null> {
  const jar = await cookies();
  return jar.get(COOKIE)?.value ?? null;
}

/** For route handlers that require a session. Throws a 401-shaped error if absent. */
export async function requireAdminKey(): Promise<string> {
  const key = await getAdminKey();
  if (!key) {
    const err = new Error("not signed in") as Error & { status?: number };
    err.status = 401;
    throw err;
  }
  return key;
}
