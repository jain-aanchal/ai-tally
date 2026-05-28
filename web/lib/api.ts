// Typed API fetcher for server components.
//
// Pages fetch over HTTP so swapping to a real backend later is just a base-URL change. In server
// components we derive the base URL from the incoming request (`headers()`); in dev/build with no
// request we fall back to NEXT_PUBLIC_API_BASE_URL or localhost.

import { headers } from "next/headers";

async function baseUrl(): Promise<string> {
  if (process.env.NEXT_PUBLIC_API_BASE_URL) return process.env.NEXT_PUBLIC_API_BASE_URL;
  try {
    const h = await headers();
    const host = h.get("host") ?? "localhost:3217";
    const proto = h.get("x-forwarded-proto") ?? "http";
    return `${proto}://${host}`;
  } catch {
    return process.env.PORT ? `http://localhost:${process.env.PORT}` : "http://localhost:3217";
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const base = await baseUrl();
  const res = await fetch(`${base}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`API ${path} failed: ${res.status}`);
  return (await res.json()) as T;
}
