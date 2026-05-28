import { NextResponse } from "next/server";
import { dq } from "@/lib/dq";

export function GET() {
  return NextResponse.json(dq);
}
