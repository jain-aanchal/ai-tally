// SPDX-License-Identifier: Apache-2.0
// Bridge Next 15's page `searchParams` record to a URLSearchParams (CTO-226).
//
// Server Components receive the query as a `Record<string, string | string[] | undefined>` (a repeated
// key arrives as an array). The pure filter parser in lib/filters.ts speaks URLSearchParams, so the
// range-aware pages funnel their awaited `searchParams` through this one converter rather than each
// re-implementing the array/undefined handling. Kept tiny and dependency-free so it round-trips the
// same way the client's `new URLSearchParams(searchParams.toString())` does.

export function searchParamsFromRecord(
  record?: Record<string, string | string[] | undefined>,
): URLSearchParams {
  const params = new URLSearchParams();
  if (!record) return params;
  for (const [key, value] of Object.entries(record)) {
    if (Array.isArray(value)) {
      for (const v of value) params.append(key, v);
    } else if (value !== undefined) {
      params.set(key, value);
    }
  }
  return params;
}
