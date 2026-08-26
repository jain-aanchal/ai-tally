// SPDX-License-Identifier: Apache-2.0
"use server";

// Server actions for the CSV revenue upload (CTO-198). The browser reads the file and hands the
// text to these; the gateway owns every validation rule, so the same checks apply to a curl call
// as to the form. Nothing is written from the browser and the web app never touches Postgres.
import { revalidatePath } from "next/cache";

import {
  type UploadResult,
  deleteRevenueUpload,
  uploadRevenueCsv,
} from "@/lib/revenueUpload";

/** Guard against a mistaken multi-megabyte paste before it crosses the wire. */
const MAX_CSV_CHARS = 8 * 1024 * 1024;

export async function uploadRevenueCsvAction(
  csv: string,
  filename: string,
): Promise<UploadResult> {
  if (typeof csv !== "string" || !csv.trim()) {
    return { ok: false, error: "The file is empty.", rowErrors: [] };
  }
  if (csv.length > MAX_CSV_CHARS) {
    return { ok: false, error: "The file is larger than 8 MiB.", rowErrors: [] };
  }
  const result = await uploadRevenueCsv(csv, { filename: filename || undefined });
  if (!result.ok) return result;
  revalidatePath("/connectors");
  // Uploaded revenue feeds the margin column, so every surface that reads it is now different.
  revalidatePath("/", "layout");
  return result;
}

export async function deleteRevenueUploadAction(
  period: string,
): Promise<{ ok: true; removed: boolean } | { ok: false; error: string }> {
  const result = await deleteRevenueUpload(period);
  if (!result.ok) return result;
  revalidatePath("/connectors");
  revalidatePath("/", "layout");
  return result;
}
