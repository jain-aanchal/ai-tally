import { NextResponse } from "next/server";
import { projection } from "@/lib/estimate";

export function GET() {
  return NextResponse.json(projection);
}
