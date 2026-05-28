import { NextResponse } from "next/server";
import { comparison } from "@/lib/compare";

export function GET() {
  return NextResponse.json(comparison);
}
