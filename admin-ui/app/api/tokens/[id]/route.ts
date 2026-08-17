/** Revoke one token. Takes effect on the microservice immediately. */

import { NextResponse } from "next/server";

import { ServiceError, revokeToken } from "@/lib/service";
import { requireAdminKey } from "@/lib/session";

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const adminKey = await requireAdminKey();
    const { id } = await params;
    await revokeToken(adminKey, id);
    return new NextResponse(null, { status: 204 });
  } catch (error) {
    const status =
      error instanceof ServiceError
        ? error.status
        : ((error as { status?: number }).status ?? 500);
    const message = error instanceof Error ? error.message : "unexpected error";
    return NextResponse.json({ error: message }, { status });
  }
}
