/**
 * Shared channel-form validators.
 *
 * Single source of truth for the rules the backend enforces in
 * `app/routes/channels_routes.py` — keeping the SPA's wizard
 * (AddChannelDialog) and the detail editor (ChannelDetail ConfigTab)
 * in lockstep so they never disagree on what a valid allow-list or
 * E.164 number looks like.
 */

/** E.164: +<country><subscriber>, 7-15 digits total. Mirrors the
 * backend's `_E164_RE`. */
export const E164_RE = /^\+[1-9]\d{6,14}$/;

export function isE164(value: string): boolean {
  return E164_RE.test(value.trim());
}

/** Split a comma/whitespace separated text input into clean entries. */
export function parseListInput(raw: string): string[] {
  return raw
    .split(/[,\s]+/)
    .map((v) => v.trim())
    .filter(Boolean);
}

/** Sender allow-list: lowercase, deduplicated. Entries may be bare
 * domains ("acme.com") or full addresses ("person@acme.com"). */
export function parseDomains(raw: string): string[] {
  return Array.from(new Set(parseListInput(raw).map((d) => d.toLowerCase())));
}

/** Numbers allow-list. Entries may be full E.164 or a prefix like
 * "+34" (the backend allows prefix matching). */
export function parseNumbers(raw: string): string[] {
  return Array.from(new Set(parseListInput(raw)));
}

/** Notification recipients (email channels): lowercase, deduplicated
 * email addresses. Mirrors the backend's `notify_to` validation. */
export function parseEmails(raw: string): string[] {
  return Array.from(new Set(parseListInput(raw).map((e) => e.toLowerCase())));
}

/** True when every entry is a plausible number/prefix (starts with +
 * followed by digits). */
export function numbersListValid(entries: string[]): boolean {
  return entries.length > 0 && entries.every((n) => /^\+\d{1,15}$/.test(n));
}
