This addon connects ``contact_center_base`` to Messenger and Instagram messaging.

The shared Meta core owns the App, credentials, callback, Page/Instagram assets,
subscription read-back and immutable webhook delivery ledger. This addon owns only
the Contact Center adapter, normalized messaging DTOs, provider-specific routing,
private media locators, outbound messaging and identity profiles.

A provider connection binds to exactly one ``meta.webhook.asset``. Its App, Page,
transport mode and target identity are read-only projections of that asset. Messenger
PSIDs and Instagram IGSIDs remain scoped to the logical Contact Center account.

Inbound messages, echoes, replies, referrals, media, receipts, reactions, edits,
unsends and postbacks enter the canonical Contact Center inbox pipeline. Shared posts,
reels and stories expose context cards with safe permalinks or private media when
available. Direct text and private media outbound are admitted only inside the
standard 24-hour response window. Signed media URLs remain in a short-lived private
locator vault and never enter DTOs or bus data.

Outbound media uses a private multipart upload followed by an attachment-ID send;
the original Odoo attachment is never made public. Each command sends one attachment
without a caption. Ambiguous uploads can be retried; ambiguous message sends stay
uncertain and are not automatically repeated.

Messaging and marketing are separate integrations
=================================================

Dependencies are ``contact_center_base``, ``meta_api_base`` and ``meta_webhook_base``.
The two shared foundations are supplied by
`Marketing Center <https://github.com/soloztech/marketing-center/tree/16.0>`_.
Their presence does not install the functional ``marketing_center_meta`` addon.

``contact_center_meta`` handles Messenger and Page-linked Instagram conversations.
``marketing_center_meta`` handles marketing catalog/reporting and native Meta lead
form submissions; its optional CRM intake bridge is ``marketing_center_meta_crm``.
A message or referral handled here is not a lead-form submission and does not trigger
that bridge. Referral identifiers remain evidence until a separate authorized
attribution integration resolves them.
