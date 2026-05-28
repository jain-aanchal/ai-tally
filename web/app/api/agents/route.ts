import { NextResponse } from "next/server";
import { agents, runs } from "@/lib/agents";

export function GET() {
  return NextResponse.json({ agents, runs });
}
