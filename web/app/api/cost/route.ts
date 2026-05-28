import { NextResponse } from "next/server";
import { costSeries, featureRows, hiddenCostAlerts } from "@/lib/cost";

export function GET() {
  return NextResponse.json({ series: costSeries, featureRows, alerts: hiddenCostAlerts });
}
