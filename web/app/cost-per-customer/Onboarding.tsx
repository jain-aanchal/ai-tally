// SPDX-License-Identifier: Apache-2.0
"use client";

// The /cost-per-customer onboarding empty state (CTO-191, plan D5).
//
// This tab ships dark. Nothing emits `account_id` yet, so on release every existing tenant lands
// here with an empty table, having never seen the page before and with no way to tell "you have not
// switched this on" apart from "this product is broken". The empty state does two jobs, in order:
// say what the page is, then say how to turn it on.
//
// Client module only for the copy button on the snippet. Everything else is static.
//
// WHY THERE IS NO PREVIEW TABLE. The house pattern for a pre-data surface is
// SyntheticPreviewBanner, which wraps invented numbers in a "SAMPLE DATA" label. It is deliberately
// not used here, for the reason page.tsx and lib/accounts.ts both state: a plausible list of fake
// customer names is the single most misleading thing this page could show, and this is the one
// surface where a reader could not tell the fixture from their own book of business. So "what it
// will look like once data arrives" is answered by naming the columns and what each one will hold,
// which is concrete without inventing a customer.

import { useState } from "react";

/**
 * The snippet, kept in one place so the copy button and the rendered block cannot drift.
 *
 * This MUST match the API that shipped in CTO-181 (sdk/python/README.md, "Tagging spend with a
 * customer account"): context-scoped via `with_account`, with an optional wire-only label. A
 * copy-pasteable snippet that does not match the shipped API is worse than no snippet, because it
 * fails after the reader has already decided to trust us.
 */
const SNIPPET = `from tally.context import start_trace, with_account

# In request middleware: resolve the customer once, wrap the handler.
with start_trace(), with_account("acct_northwind", label="Northwind Traders"):
    client.record_llm_call(provider="openai", model="gpt-4o", usage=usage)
    client.record_tool_call(provider="openai", tool="web_search")
    # ...every span emitted inside this block carries the same account.`;

/** What each column of the table will hold. Named, rather than mocked up with invented rows. */
const COLUMNS: readonly { name: string; body: string }[] = [
  {
    name: "Account",
    body: "one row per customer, shown by the label you set, or by a shortened hash if you set none.",
  },
  {
    name: "Users",
    body: "how many distinct people were seen under that account in the window.",
  },
  {
    name: "Direct cost",
    body: "what that customer's LLM, tool, vector and embedding calls cost you over the window.",
  },
  {
    name: "Cost per user",
    body: "direct cost divided by those users, left blank on accounts too small for the ratio to mean anything.",
  },
];

/**
 * The full explainer, shown when not one span in the window carried an account.
 *
 * Deliberately not phrased as an error and not phrased as a promise that data exists. The
 * surrounding page still states the unattributed share above this block, so the reader gets the
 * honest number first and the explanation second.
 */
export function OnboardingEmptyState({ windowDays }: { windowDays: number }) {
  return (
    <section className="rounded-xl border border-edge bg-panel p-5">
      <h2 className="text-sm font-medium uppercase tracking-wide text-muted">
        Nothing is tagged with an account yet
      </h2>

      <div className="mt-4 space-y-3">
        <h3 className="text-base font-semibold">What this page is for</h3>
        <p className="max-w-prose text-sm text-muted">
          It breaks your AI spend down by <span className="text-white">your own customers</span>, so
          you can see which accounts are expensive to serve and which ones are worth what they pay.
          Everywhere else in this dashboard groups spend by model, feature or provider. This is the
          only tab that groups it by who you are serving.
        </p>
        <p className="max-w-prose text-sm text-muted">
          Spend is grouped by the account id your own code puts on each span, so nothing can appear
          here until your spans carry one. That is why this page is empty rather than wrong.
        </p>

        <div className="rounded-lg border border-edge bg-ink/40 p-4">
          <p className="text-xs uppercase tracking-wide text-muted">
            Once accounts arrive, the table below will show
          </p>
          <dl className="mt-3 space-y-2 text-sm">
            {COLUMNS.map((c) => (
              <div key={c.name} className="flex flex-wrap gap-x-2">
                <dt className="font-medium">{c.name}:</dt>
                <dd className="max-w-prose flex-1 text-muted">{c.body}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-3 max-w-prose text-xs text-muted">
            Ranked most expensive first, over the same {windowDays} days the rest of this page
            covers. Compute and egress stay out of it: no span carries an account for them, so they
            are reported separately rather than split by a rule nobody agreed to.
          </p>
        </div>
      </div>

      <div className="mt-6 space-y-3">
        <h3 className="text-base font-semibold">How to turn it on</h3>
        <p className="max-w-prose text-sm text-muted">
          Set the account once per request and every span inside the block picks it up. A single
          call can pass <code className="rounded bg-ink px-1 py-0.5 text-xs">account_id=</code> to
          override the scope, which is what a batch job walking several customers needs.
        </p>

        <Snippet code={SNIPPET} />

        <ul className="max-w-prose list-disc space-y-1.5 pl-5 text-sm text-muted">
          <li>
            <span className="text-white">The label is optional.</span> Leave it out and the account
            shows as a shortened hash, which is a supported way to run. The label is wire-only: it
            is stored against the hash and never written to the telemetry store, so no customer name
            lands in your span data.
          </li>
          <li>
            <span className="text-white">The raw account id never leaves your process.</span> It is
            HMAC-SHA256&apos;d under your per-tenant key at emit time, exactly like a user id, which
            is also why the table can only show you a hash you already know.
          </li>
          <li>
            <span className="text-white">
              Rows appear as soon as the next tagged spans land.
            </span>{" "}
            The hash is written at emit time, so spans already recorded stay unattributed. The
            window fills forward from your deploy rather than backfilling.
          </li>
        </ul>

        <p className="max-w-prose text-xs text-muted">
          Full reference, including{" "}
          <code className="rounded bg-ink px-1 py-0.5 text-xs">with_account(None)</code> to stop a
          background task inheriting its caller&apos;s customer, is in{" "}
          <code className="text-xs">sdk/python/README.md</code>.
        </p>
      </div>
    </section>
  );
}

/**
 * The same snippet in a compact disclosure, for the partial state.
 *
 * The partial state already has a banner saying most spend is untagged (D2's UnattributedNotice),
 * so repeating the whole explainer under a table the reader can plainly see would be noise. What
 * they are missing is the call to finish the job, and the code to do it, one click away.
 */
export function HowToTagDetails() {
  return (
    <details className="rounded-lg border border-edge bg-ink/40 p-4">
      <summary className="cursor-pointer text-sm font-medium">
        How to tag the rest of your spend
      </summary>
      <p className="mt-3 max-w-prose text-sm text-muted">
        Set the account once per request and every span inside the block picks it up. The label is
        optional, and rows appear as soon as the next tagged spans land: nothing is backfilled.
      </p>
      <div className="mt-3">
        <Snippet code={SNIPPET} />
      </div>
    </details>
  );
}

/** Code block with a copy control. Copy failure is silent for the same reason it is in AccountCell:
 * the text is already selectable, so there is nothing to recover and nothing to shout about. */
function Snippet({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="relative">
      <pre className="overflow-x-auto rounded-lg border border-edge bg-ink p-4 pr-24 text-xs leading-relaxed">
        <code>{code}</code>
      </pre>
      <button
        type="button"
        onClick={copy}
        className="absolute right-2 top-2 rounded border border-edge bg-panel px-2 py-1 text-[11px] text-muted hover:text-white"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
