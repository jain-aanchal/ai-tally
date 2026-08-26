// SPDX-License-Identifier: Apache-2.0
"use client";

// How an account names itself, on the table and on the detail view (CTO-188 D2, CTO-190 D4).
//
// Extracted from AccountTable when the detail view landed, because "label where one exists, short
// hash otherwise, full hash on hover and on copy" is a rule the two surfaces have to agree on
// exactly. A reader who clicks "Acme Corp" must land on a page headed "Acme Corp", and a reader who
// clicks a shortened hash must land on the same shortened hash; two copies of that rule would drift
// the first time one of them was tweaked.
//
// Client module for the copy control: the clipboard is a browser API.

import Link from "next/link";
import { useState } from "react";

import { accountDisplayName } from "@/lib/accounts";

/** Where an account's detail view lives. One function so the table and the page cannot disagree. */
export function accountHref(accountIdHash: string): string {
  return `/cost-per-customer/${encodeURIComponent(accountIdHash)}`;
}

/**
 * Copies the FULL hash, never the shortened form.
 *
 * The short form is a display convenience and nothing else: it is not what the label API accepts,
 * not what the lookup endpoint returns, and not what anyone can paste into a support conversation.
 * Copying what is on screen rather than what identifies the account would hand people a string that
 * silently fails everywhere they try to use it.
 */
export function CopyHashButton({
  accountIdHash,
  className = "",
}: {
  accountIdHash: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(accountIdHash);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can be refused (insecure origin, denied permission). The hash is already
      // selectable in the title attribute, so there is nothing to recover and nothing to shout at
      // the user about.
      setCopied(false);
    }
  };

  return (
    <button
      type="button"
      onClick={copy}
      title={`Copy the full account hash: ${accountIdHash}`}
      className={`rounded border border-edge px-1.5 py-0.5 text-[11px] text-muted hover:text-white ${className}`.trim()}
    >
      {copied ? "Copied" : "Copy hash"}
    </button>
  );
}

/**
 * The Account cell: label where the tenant set one, shortened hash otherwise, full hash on hover
 * and on copy, linking through to the account's detail view.
 *
 * The full hash is what every other surface takes (the label API, a support conversation), so it
 * has to be retrievable from the row. The short form is for width only.
 */
export function AccountCell({
  accountIdHash,
  label,
  labelsUnavailable,
}: {
  accountIdHash: string;
  label: string | undefined;
  labelsUnavailable: boolean;
}) {
  return (
    <span className="inline-flex items-center gap-2">
      <Link
        href={accountHref(accountIdHash)}
        title={accountIdHash}
        className={`text-accent hover:underline ${label ? "font-medium" : "font-mono text-xs"}`}
      >
        {accountDisplayName(accountIdHash, label)}
      </Link>
      {!label && !labelsUnavailable ? (
        <span className="text-[11px] text-muted" title="no label set for this account">
          unlabelled
        </span>
      ) : null}
      <CopyHashButton accountIdHash={accountIdHash} />
    </span>
  );
}
