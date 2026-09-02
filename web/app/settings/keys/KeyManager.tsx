// SPDX-License-Identifier: Apache-2.0
// Client-side ingest-key manager (Initiative 1, §7). Lists key METADATA (never a secret), and, for
// admins, mints a key showing its raw token exactly once, rotates a key, and revokes one. Every
// write goes to the server-side /api/keys proxy, which holds the gateway service token and re-checks
// the admin role; this component never sees the service token.
"use client";

import { useCallback, useEffect, useState } from "react";

import { ConnectPanel } from "./ConnectPanel";

interface KeyMeta {
  id: string;
  name: string | null;
  token_prefix: string | null;
  scope: string;
  created_by: string | null;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

interface MintedKey {
  id: string;
  token: string;
  token_prefix: string | null;
  name: string | null;
  scope: string;
}

/** A value we do not know is a blank, never a fabricated one (honest under uncertainty). */
function orDash(v: string | null): string {
  return v && v.trim() ? v : "—";
}

export function KeyManager({ canManage }: { canManage: boolean }) {
  const [keys, setKeys] = useState<KeyMeta[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [scope, setScope] = useState("write");
  const [minted, setMinted] = useState<MintedKey | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/keys", { cache: "no-store" });
      if (!res.ok) {
        setError(`Could not load keys (HTTP ${res.status}).`);
        return;
      }
      const body = (await res.json()) as { keys?: KeyMeta[] };
      setKeys(body.keys ?? []);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function createKey() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/keys", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: name.trim() || undefined, scope }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(body?.error ?? `Mint failed (HTTP ${res.status}).`);
        return;
      }
      setMinted(body as MintedKey);
      setName("");
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function rotateKey(id: string) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/keys/${id}/rotate`, { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(body?.error ?? `Rotate failed (HTTP ${res.status}).`);
        return;
      }
      setMinted(body as MintedKey);
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function revokeKey(id: string) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/keys/${id}`, { method: "DELETE" });
      if (!res.ok && res.status !== 204) {
        const body = await res.json().catch(() => ({}));
        setError(body?.error ?? `Revoke failed (HTTP ${res.status}).`);
        return;
      }
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      {error && (
        <div className="rounded-md border border-edge bg-panel px-3 py-2 text-sm text-fg">
          {error}
        </div>
      )}

      {canManage && (
        <div className="flex flex-wrap items-end gap-3 rounded-md border border-edge bg-panel p-4">
          <label className="flex flex-col gap-1 text-xs text-muted">
            Name
            <input
              className="w-56 rounded border border-edge bg-transparent px-2 py-1 text-sm text-fg"
              placeholder="prod ingest"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted">
            Scope
            <select
              className="rounded border border-edge bg-transparent px-2 py-1 text-sm text-fg"
              value={scope}
              onChange={(e) => setScope(e.target.value)}
            >
              <option value="write">write</option>
              <option value="read">read</option>
              <option value="admin">admin</option>
            </select>
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={() => void createKey()}
            className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            Create key
          </button>
        </div>
      )}

      {minted && (
        <div className="space-y-2 rounded-md border border-accent bg-panel p-4">
          <div className="text-sm font-semibold text-fg">
            Copy this key now. You will not see it again.
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 break-all rounded border border-edge bg-transparent px-2 py-1 text-xs text-fg">
              {minted.token}
            </code>
            <button
              type="button"
              onClick={() => void navigator.clipboard?.writeText(minted.token)}
              className="rounded border border-edge px-2 py-1 text-xs text-fg"
            >
              Copy
            </button>
            <button
              type="button"
              onClick={() => setMinted(null)}
              className="rounded border border-edge px-2 py-1 text-xs text-muted"
            >
              Dismiss
            </button>
          </div>
          {/* One-step connect (Initiative 2, §9): snippets with the real key inlined into this
              one-time view, plus the live first-event indicator. The key is never stored to render
              this later; it lives only in `minted` until dismissed. */}
          <ConnectPanel token={minted.token} />
        </div>
      )}

      <div className="overflow-x-auto rounded-md border border-edge">
        <table className="w-full text-left text-sm">
          <thead className="bg-panel text-xs uppercase tracking-wider text-muted">
            <tr>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">Prefix</th>
              <th className="px-3 py-2">Scope</th>
              <th className="px-3 py-2">Created</th>
              <th className="px-3 py-2">Last used</th>
              <th className="px-3 py-2">Status</th>
              {canManage && <th className="px-3 py-2 text-right">Actions</th>}
            </tr>
          </thead>
          <tbody>
            {keys === null && (
              <tr>
                <td className="px-3 py-3 text-muted" colSpan={canManage ? 7 : 6}>
                  Loading…
                </td>
              </tr>
            )}
            {keys?.length === 0 && (
              <tr>
                <td className="px-3 py-3 text-muted" colSpan={canManage ? 7 : 6}>
                  No keys yet.
                </td>
              </tr>
            )}
            {keys?.map((k) => (
              <tr key={k.id} className="border-t border-edge">
                <td className="px-3 py-2 text-fg">{orDash(k.name)}</td>
                <td className="px-3 py-2 font-mono text-xs text-muted">{orDash(k.token_prefix)}</td>
                <td className="px-3 py-2 text-fg">{k.scope}</td>
                <td className="px-3 py-2 text-muted">{orDash(k.created_at)}</td>
                <td className="px-3 py-2 text-muted">{orDash(k.last_used_at)}</td>
                <td className="px-3 py-2">
                  {k.revoked_at ? (
                    <span className="text-muted">revoked</span>
                  ) : (
                    <span className="text-fg">active</span>
                  )}
                </td>
                {canManage && (
                  <td className="px-3 py-2 text-right">
                    {!k.revoked_at && (
                      <span className="inline-flex gap-2">
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void rotateKey(k.id)}
                          className="rounded border border-edge px-2 py-1 text-xs text-fg disabled:opacity-50"
                        >
                          Rotate
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void revokeKey(k.id)}
                          className="rounded border border-edge px-2 py-1 text-xs text-muted disabled:opacity-50"
                        >
                          Revoke
                        </button>
                      </span>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {!canManage && (
        <p className="text-xs text-muted">
          You have read-only access. Ask an organization admin to mint or rotate keys.
        </p>
      )}
    </div>
  );
}
