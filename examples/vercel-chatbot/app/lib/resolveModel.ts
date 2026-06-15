// SPDX-License-Identifier: Apache-2.0
// CTO-109: read the gateway's auto-discovered model cache so the chatbot stops
// hardcoding SKUs that the provider may retire (e.g. claude-3-5-haiku-latest
// got sunset and broke the demo). The Python gateway writes this file on boot;
// here we just look up the current best id per (provider, family).

import fs from "node:fs";
import path from "node:path";

type Provider = "openai" | "anthropic";
type Family = "haiku" | "sonnet" | "opus" | "mini" | "flagship" | "embedding" | "other";

interface CachedModel {
  provider: Provider;
  id: string;
  family: Family;
  created_at: string | null;
  deprecated_at: string | null;
}

const DATE_SUFFIX = /-\d{8}$/;

// Best-effort load. If the cache is missing or malformed, callers fall back to
// their hardcoded defaults — the whole feature is quality-of-life, not critical
// path. Honors TALLY_MODELS_CACHE so the demo can point at the gateway's mount.
function loadCache(): CachedModel[] | null {
  const overridePath = process.env.TALLY_MODELS_CACHE;
  const candidates = overridePath
    ? [overridePath]
    : [
        path.join(process.cwd(), ".tally", "models.json"),
        // The chatbot demo runs from examples/vercel-chatbot/app; the gateway
        // writes its cache at the repo root. Walk up so we find it from either cwd.
        path.join(process.cwd(), "..", "..", "..", ".tally", "models.json"),
      ];
  for (const p of candidates) {
    try {
      const raw = fs.readFileSync(p, "utf-8");
      const parsed = JSON.parse(raw) as CachedModel[];
      if (Array.isArray(parsed)) return parsed;
    } catch {
      // try the next candidate
    }
  }
  return null;
}

export function resolveLatest(
  provider: Provider,
  family: Family,
  fallback: string,
): string {
  const cache = loadCache();
  if (!cache) return fallback;
  const candidates = cache.filter(
    (m) => m.provider === provider && m.family === family && !m.deprecated_at,
  );
  if (candidates.length === 0) return fallback;
  // Same tiebreaker as tally.models.latest(): undated alias beats date-stamped snapshot,
  // then newer created_at wins.
  candidates.sort((a, b) => {
    const aDated = DATE_SUFFIX.test(a.id) ? 1 : 0;
    const bDated = DATE_SUFFIX.test(b.id) ? 1 : 0;
    if (aDated !== bDated) return aDated - bDated;
    const aTs = a.created_at ? Date.parse(a.created_at) : 0;
    const bTs = b.created_at ? Date.parse(b.created_at) : 0;
    return bTs - aTs;
  });
  return candidates[0].id;
}
