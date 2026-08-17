/**
 * Sign in / sign out.
 *
 * Signing in means: take the admin key the operator pasted, prove it works by making a
 * real authenticated call, and only then store it in the cookie. Storing an unverified
 * key would leave someone "signed in" to a session that fails on every action.
 */

import { NextResponse } from "next/server";

import { ServiceError, verifyAdminKey } from "@/lib/service";
import { clearSession, getAdminKey, setSession } from "@/lib/session";

export async function GET() {
  return NextResponse.json({ signedIn: (await getAdminKey()) !== null });
}

export async function POST(request: Request) {
  let adminKey: string;
  try {
    const body = (await request.json()) as { adminKey?: unknown };
    adminKey = typeof body.adminKey === "string" ? body.adminKey.trim() : "";
  } catch {
    return NextResponse.json({ error: "malformed request" }, { status: 400 });
  }

  if (!adminKey) {
    return NextResponse.json({ error: "Enter an admin key." }, { status: 400 });
  }

  try {
    await verifyAdminKey(adminKey);
  } catch (error) {
    const status = error instanceof ServiceError ? error.status : 500;
    const message =
      error instanceof Error ? error.message : "could not verify that key";
    // 502/503/500 are service problems, not bad credentials — say which, or the
    // operator will assume they typed the key wrong and re-type it forever.
    return NextResponse.json({ error: message }, { status: status === 401 ? 401 : status });
  }

  await setSession(adminKey);
  return NextResponse.json({ signedIn: true });
}

export async function DELETE() {
  await clearSession();
  return NextResponse.json({ signedIn: false });
}
