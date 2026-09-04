This addon connects ``contact_center_base`` to Messenger and Instagram messaging.

The shared Meta core owns the App, credentials, callback, Page/Instagram assets,
subscription read-back and immutable webhook delivery ledger. This addon owns only
the Contact Center adapter, normalized messaging DTOs, provider-specific routing,
private media locators, outbound messaging and identity profiles.

A provider connection binds to exactly one ``meta.webhook.asset``. Its App, Page,
transport mode and target identity are read-only projections of that asset. Messenger
PSIDs and Instagram IGSIDs remain scoped to the logical Contact Center account.

Inbound messages, echoes, replies, referrals, media, receipts, reactions, edits,
unsends and postbacks enter the canonical Contact Center inbox pipeline. Direct text
outbound is admitted only inside the standard 24-hour response window. Signed media
URLs remain in a short-lived private locator vault and never enter DTOs or bus data.
