import { NextResponse } from "next/server";
import { mockDataQuality, mockOutliers, mockRoi, mockSpend } from "@/lib/mock";

export function GET() {
  return NextResponse.json({
    spend: mockSpend,
    outliers: mockOutliers,
    roi: mockRoi,
    dq: mockDataQuality,
  });
}
