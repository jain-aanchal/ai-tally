// SPDX-License-Identifier: Apache-2.0
"use client";

// Inline value-event configuration for /features (CTO-140). Wires the two previously-dead CTAs:
//   - the per-row "configure value event →" link opens a modal to pin one feature's value event;
//   - "Finish setup" walks a sequential modal over every unconfigured feature, then a success state.
// The modal lists the tenant's distinct business events (observed live over the last 30d via
// /api/features/value-events) plus a free-text option; confirming POSTs to the same route, which
// forwards an idempotent change_id to the gateway. Saved events update in place — the banner clears
// itself once every feature is configured.

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { Card } from "@/components/Card";
import { type FeatureEconomics, margin } from "@/lib/features";
import { formatUSD } from "@/lib/types";

interface ObservedEvent {
  name: string;
  count: number;
}

type SaveState = "idle" | "saving" | "error";

export function FeatureValueEvents({ initialFeatures }: { initialFeatures: FeatureEconomics[] }) {
  const router = useRouter();
  const [features, setFeatures] = useState<FeatureEconomics[]>(initialFeatures);

  // Observed events are fetched once, lazily, the first time a modal opens.
  const [observed, setObserved] = useState<ObservedEvent[] | null>(null);
  const [observedAvailable, setObservedAvailable] = useState(true);
  const [observedLoaded, setObservedLoaded] = useState(false);

  // Modal queue: features still to configure in this session. Single-row config is a queue of one.
  const [queue, setQueue] = useState<string[] | null>(null);
  const [finishDone, setFinishDone] = useState(false);

  const unconfigured = features.filter((f) => f.valueEvent === null);

  const loadObserved = useCallback(async () => {
    if (observedLoaded) return;
    try {
      const res = await fetch("/api/features/value-events");
      const body = (await res.json()) as {
        observedEvents?: ObservedEvent[];
        observedAvailable?: boolean;
      };
      setObserved(body.observedEvents ?? []);
      setObservedAvailable(body.observedAvailable ?? true);
    } catch {
      setObserved([]);
      setObservedAvailable(false);
    } finally {
      setObservedLoaded(true);
    }
  }, [observedLoaded]);

  function openSingle(feature: string) {
    setFinishDone(false);
    setQueue([feature]);
    void loadObserved();
  }

  function openFinishSetup() {
    const pending = unconfigured.map((f) => f.feature);
    if (pending.length === 0) return;
    setFinishDone(false);
    setQueue(pending);
    void loadObserved();
  }

  function closeModal() {
    setQueue(null);
  }

  function applySaved(feature: string, eventName: string) {
    setFeatures((fs) =>
      fs.map((f) => (f.feature === feature ? { ...f, valueEvent: eventName } : f)),
    );
    router.refresh();
  }

  function advance() {
    setQueue((q) => {
      if (!q) return null;
      const rest = q.slice(1);
      if (rest.length === 0) {
        setFinishDone(true);
        return null;
      }
      return rest;
    });
  }

  const activeFeature = queue?.[0] ?? null;
  const remaining = queue?.length ?? 0;

  return (
    <>
      {unconfigured.length > 0 && (
        <FinishSetupBanner count={unconfigured.length} onStart={openFinishSetup} />
      )}

      <Card title="Unit economics — per feature">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase text-muted">
              <tr>
                <th className="py-1 text-left font-medium">Feature</th>
                <th className="py-1 text-right font-medium">Cost/user</th>
                <th className="py-1 text-right font-medium">Value/user</th>
                <th className="py-1 text-right font-medium">Margin</th>
                <th className="py-1 text-right font-medium">Payback</th>
                <th className="py-1 text-right font-medium">Attribution rate</th>
                <th className="py-1 pl-3 text-left font-medium">Value event</th>
              </tr>
            </thead>
            <tbody>
              {features.map((f) => {
                const m = margin(f);
                return (
                  <tr key={f.feature} className="border-t border-edge">
                    <td className="py-2 font-medium">{f.feature}</td>
                    <td className="py-2 text-right tabular-nums">{formatUSD(f.costPerUserMicroUsd)}</td>
                    <td className="py-2 text-right tabular-nums">
                      {f.valuePerUserMicroUsd === null ? <Dash /> : formatUSD(f.valuePerUserMicroUsd)}
                    </td>
                    <td className="py-2 text-right tabular-nums">
                      {m === null ? <Dash /> : <MarginCell m={m} />}
                    </td>
                    <td className="py-2 text-right tabular-nums">
                      {f.paybackDays === null ? <Dash /> : `${f.paybackDays}d`}
                    </td>
                    <td className="py-2 text-right tabular-nums">
                      {f.attributionRate === null ? <Dash /> : `${Math.round(f.attributionRate * 100)}%`}
                    </td>
                    <td className="py-2 pl-3">
                      {f.valueEvent === null ? (
                        <button
                          type="button"
                          onClick={() => openSingle(f.feature)}
                          className="text-warn text-xs hover:underline"
                        >
                          configure value event →
                        </button>
                      ) : (
                        <span className="font-mono text-xs text-muted">{f.valueEvent}</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      {activeFeature && (
        <ConfigureModal
          feature={activeFeature}
          remaining={remaining}
          observed={observed}
          observedAvailable={observedAvailable}
          observedLoaded={observedLoaded}
          onSaved={(eventName) => {
            applySaved(activeFeature, eventName);
            advance();
          }}
          onClose={closeModal}
        />
      )}

      {finishDone && <FinishDoneModal onClose={() => setFinishDone(false)} />}
    </>
  );
}

function FinishSetupBanner({ count, onStart }: { count: number; onStart: () => void }) {
  const noun = count === 1 ? "feature" : "features";
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-warn/40 bg-warn/10 px-4 py-3 text-sm">
      <div className="text-warn">
        <span className="font-medium">Partial data. </span>
        <span>
          {count} {noun} {count === 1 ? "has" : "have"} no value event yet — ROI can’t be attributed
          until you pick one.
        </span>
      </div>
      <button
        type="button"
        onClick={onStart}
        className="inline-flex items-center rounded-md border border-accent/50 bg-accent/15 px-3 py-1.5 text-sm font-medium text-accent hover:bg-accent/25"
      >
        Finish setup
      </button>
    </div>
  );
}

function ConfigureModal({
  feature,
  remaining,
  observed,
  observedAvailable,
  observedLoaded,
  onSaved,
  onClose,
}: {
  feature: string;
  remaining: number;
  observed: ObservedEvent[] | null;
  observedAvailable: boolean;
  observedLoaded: boolean;
  onSaved: (eventName: string) => void;
  onClose: () => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [freeText, setFreeText] = useState("");
  const [useFreeText, setUseFreeText] = useState(false);
  const [save, setSave] = useState<SaveState>("idle");

  // Reset the picker whenever we advance to a different feature in the sequential flow.
  useEffect(() => {
    setSelected(null);
    setFreeText("");
    setUseFreeText(false);
    setSave("idle");
  }, [feature]);

  const chosen = useFreeText ? freeText.trim() : selected;
  const canSave = !!chosen && save !== "saving";
  const hasObserved = (observed?.length ?? 0) > 0;

  async function confirm() {
    if (!chosen) return;
    setSave("saving");
    try {
      const res = await fetch("/api/features/value-events", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ feature, eventName: chosen }),
      });
      if (!res.ok) {
        setSave("error");
        return;
      }
      onSaved(chosen);
    } catch {
      setSave("error");
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`Configure value event for ${feature}`}
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-xl border border-edge bg-panel p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-1 flex items-baseline justify-between gap-2">
          <h2 className="text-base font-semibold">
            Value event for <span className="font-mono text-accent">{feature}</span>
          </h2>
          {remaining > 1 && (
            <span className="text-xs text-muted">{remaining} left</span>
          )}
        </div>
        <p className="mb-4 text-xs text-muted">
          Pick the business event that represents value for this feature. ROI is attributed against it.
        </p>

        {!observedLoaded ? (
          <p className="py-6 text-center text-sm text-muted">Loading observed events…</p>
        ) : (
          <div className="space-y-3">
            {!observedAvailable ? (
              <p className="rounded-md border border-edge bg-ink/40 px-3 py-2 text-xs text-muted">
                Couldn’t reach the event store right now — enter a value event name below and it’ll be
                saved once things reconnect.
              </p>
            ) : !hasObserved ? (
              <p className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn">
                No business events yet — wire Stripe (RUNNING.md §7) to start capturing value events.
                You can still type a value event name below to configure it ahead of time.
              </p>
            ) : (
              <ul className="max-h-56 space-y-1 overflow-y-auto">
                {observed!.map((e) => (
                  <li key={e.name}>
                    <label className="flex cursor-pointer items-center justify-between gap-2 rounded-md border border-edge px-3 py-2 text-sm hover:border-accent">
                      <span className="flex items-center gap-2">
                        <input
                          type="radio"
                          name="value-event"
                          checked={!useFreeText && selected === e.name}
                          onChange={() => {
                            setUseFreeText(false);
                            setSelected(e.name);
                          }}
                        />
                        <span className="font-mono">{e.name}</span>
                      </span>
                      <span className="tabular-nums text-xs text-muted">
                        {e.count.toLocaleString()} events / 30d
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            )}

            <label className="flex cursor-pointer items-center gap-2 rounded-md border border-dashed border-edge px-3 py-2 text-sm hover:border-accent">
              <input
                type="radio"
                name="value-event"
                checked={useFreeText}
                onChange={() => setUseFreeText(true)}
              />
              <span className="text-muted">Other:</span>
              <input
                aria-label="custom value event name"
                value={freeText}
                placeholder="event_name"
                onFocus={() => setUseFreeText(true)}
                onChange={(e) => {
                  setUseFreeText(true);
                  setFreeText(e.target.value);
                }}
                className="flex-1 rounded border border-edge bg-ink px-2 py-1 font-mono text-sm"
              />
            </label>
          </div>
        )}

        {save === "error" && (
          <p className="mt-3 text-xs text-bad">Couldn’t save — try again.</p>
        )}

        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-edge px-3 py-1.5 text-sm text-muted hover:text-fg"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!canSave}
            onClick={confirm}
            className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:cursor-not-allowed disabled:opacity-40"
          >
            {save === "saving" ? "Saving…" : "Save value event"}
          </button>
        </div>
      </div>
    </div>
  );
}

function FinishDoneModal({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Setup complete"
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm rounded-xl border border-edge bg-panel p-6 text-center shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-2 text-2xl">✓</div>
        <h2 className="text-base font-semibold text-good">Setup complete</h2>
        <p className="mt-1 text-sm text-muted">
          Every feature now has a value event. ROI will populate as attribution runs.
        </p>
        <button
          type="button"
          onClick={onClose}
          className="mt-5 rounded-md bg-accent px-4 py-1.5 text-sm font-medium text-ink"
        >
          Done
        </button>
      </div>
    </div>
  );
}

function Dash() {
  return <span className="text-muted">—</span>;
}

function MarginCell({ m }: { m: number }) {
  const pct = Math.round(m * 100);
  return <span className={m > 0.5 ? "text-good" : m > 0 ? "" : "text-bad"}>{pct}%</span>;
}
