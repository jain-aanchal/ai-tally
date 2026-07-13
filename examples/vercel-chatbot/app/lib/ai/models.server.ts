// SPDX-License-Identifier: Apache-2.0
// CTO-170: server-only resolution of the chatbot model picker.
//
// resolveModel.ts reads .tally/models.json via node:fs, so this module — and
// anything that imports it — must stay on the server. The `server-only` import
// makes that a hard build error if a "use client" component ever pulls it in.
// The client imports the plain pinned data shape from ./models directly; the chat
// route, /api/models, and server actions import the discovery-RESOLVED shape here.
//
// Resolution is fail-soft at every step: resolveLatest() returns the pinned
// fallback when .tally/models.json is missing/stale, and the catalog guard below
// rejects any resolved id that isn't priced in the SDK seed catalog. So offline
// (or with TALLY_SKIP_MODEL_REFRESH=1 / TALLY_PINNED_MODELS, which gate run.sh's
// cache refresh) the lineup collapses back to today's literals — never a 404 or an
// unpriced ($0) model. When a provider retires a SKU, the next launch's refreshed
// cache makes resolveLatest() pick the replacement with no code change here.
import "server-only";

import { resolveLatest } from "../resolveModel";
import {
  catalogPricedIds,
  type ChatModel,
  chatModels as pinnedChatModels,
  chatModelSlots,
  DEFAULT_CHAT_MODEL as PINNED_DEFAULT_CHAT_MODEL,
  defaultChatModelSlot,
  type ModelSlot,
  titleModel as pinnedTitleModel,
  titleModelSlot,
} from "./models";

// Resolve one (provider, family) slot against the discovery cache, returning a
// prefixed `<provider>/<id>`. Falls back to the pinned prefixed id when discovery
// has nothing (resolveLatest's own fallback) OR when the resolved id isn't
// catalog-priced — offering an unpriced model would show $0 on the dashboard
// (catalog_miss), which is worse than pinning a slightly-older priced SKU.
function resolveSlot(slot: ModelSlot, pinnedPrefixedId: string): string {
  const slash = pinnedPrefixedId.indexOf("/");
  const pinnedBare =
    slash >= 0 ? pinnedPrefixedId.slice(slash + 1) : pinnedPrefixedId;

  const resolvedBare = resolveLatest(slot.provider, slot.family, pinnedBare);
  const resolvedPrefixed = `${slot.provider}/${resolvedBare}`;

  if (resolvedPrefixed === pinnedPrefixedId) return pinnedPrefixedId;

  if (!catalogPricedIds.has(resolvedPrefixed)) {
    console.warn(
      `[models.server] discovery resolved ${slot.provider}/${slot.family} to ` +
        `"${resolvedPrefixed}", which is not in the SDK seed catalog; falling back ` +
        `to pinned "${pinnedPrefixedId}" to avoid an unpriced (catalog_miss $0) model.`
    );
    return pinnedPrefixedId;
  }
  return resolvedPrefixed;
}

// The picker lineup with each `id` resolved from discovery; display labels,
// provider, and description are preserved from the pinned entry.
export const chatModels: ChatModel[] = pinnedChatModels.map((model, i) => {
  const slot = chatModelSlots[i];
  if (!slot) return model; // defensive: slot list drifted out of sync with chatModels
  const id = resolveSlot(slot, model.id);
  return id === model.id ? model : { ...model, id };
});

export const DEFAULT_CHAT_MODEL = resolveSlot(
  defaultChatModelSlot,
  PINNED_DEFAULT_CHAT_MODEL
);

export const titleModel = (() => {
  const id = resolveSlot(titleModelSlot, pinnedTitleModel.id);
  return id === pinnedTitleModel.id
    ? pinnedTitleModel
    : { ...pinnedTitleModel, id };
})();

// Accept a client-sent model id if it's in the resolved lineup OR still a pinned
// literal. The union matters: the client's first render (and any offline client)
// uses the pinned ids from ./models before /api/models delivers the resolved list,
// so both must validate to avoid a spurious fallback-to-DEFAULT on send.
export const allowedModelIds = new Set<string>([
  ...chatModels.map((m) => m.id),
  ...pinnedChatModels.map((m) => m.id),
]);
