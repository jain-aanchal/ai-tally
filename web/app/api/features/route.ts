import { NextResponse } from "next/server";
import { diagnostics, features } from "@/lib/features";

export function GET() {
  return NextResponse.json({ features, diagnostics });
}
