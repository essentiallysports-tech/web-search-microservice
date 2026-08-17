/**
 * List and create tokens. Thin proxy — all policy lives in the microservice.
 *
 * The create response is the only place a secret ever appears. It is passed straight
 * through to the browser once and never persisted here.
 */

import { NextResponse } from "next/server";

import { ServiceError, createToken, listTokens } from "@/lib/service";
import { requireAdminKey } from "@/lib/session";

function errorResponse(error: unknown) {
  const status =
    error instanceof ServiceError
      ? error.status
      : ((error as { status?: number }).status ?? 500);
  const message = error instanceof Error ? error.message : "unexpected error";
  return NextResponse.json({ error: message }, { status });
}

export async function GET() {
  try {
    const adminKey = await requireAdminKey();
    return NextResponse.json(await listTokens(adminKey));
  } catch (error) {
    return errorResponse(error);
  }
}

export async function POST(request: Request) {
  try {
    const adminKey = await requireAdminKey();

    const body = (await request.json()) as {
      name?: unknown;
      expiresInDays?: unknown;
    };
    const name = typeof body.name === "string" ? body.name.trim() : "";
    if (!name) {
      return NextResponse.json(
        { error: "Give the token a name so you know which app it belongs to." },
        { status: 400 },
      );
    }

    // null / 0 / "" all mean "no expiry". Anything else must be a sane day count;
    // the service enforces 1..365 as well, this just avoids a pointless round trip.
    const rawDays = Number(body.expiresInDays);
    const expiresInDays =
      Number.isFinite(rawDays) && rawDays >= 1 ? Math.floor(rawDays) : null;

    return NextResponse.json(await createToken(adminKey, name, expiresInDays), {
      status: 201,
    });
  } catch (error) {
    return errorResponse(error);
  }
}
