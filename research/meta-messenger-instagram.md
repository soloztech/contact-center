# Meta Messenger and Instagram Messaging

- Status: Phases 6.1-6.5 implemented in the laboratory; shared Meta runtime and a
  greenfield Contact Center installation validated end to end
- Reviewed: 2026-09-01
- Graph API baseline: `v26.0`

This document records the official Meta messaging contracts, the initial read-only
inventory and the dedicated laboratory connection completed on 2026-08-25. Normative
implementation decisions remain in [`plan.md`](../plan.md). Secrets, access tokens,
verify tokens, cookies and raw conversations are not stored in this repository.

## Implementation status

`contact_center_meta` `16.0.2.0.0`, backed by `meta_api_base` `16.0.1.1.0` and
`meta_webhook_base` `16.0.1.1.1`, implements durable ingress, Graph transport,
subscription reconciliation, health, inbound/outbound text and media, replies,
quick/story replies, self-message echoes, mutations and receipts in the SERVIDOR05
laboratory. Messenger inbound/echo uses PSID/Page orientation; Instagram uses
IGSID/IG-account orientation. Both normalize to the canonical identity, guest, channel,
message, reply and external-device paths. Contradictory endpoint evidence fails closed
before identity creation.

Message referral evidence is converted to the provider-neutral `AttributionDTO` and
immutable touchpoint ledger. A standalone `messaging[].referral`, without a message or
`mid`, becomes `attribution.observed` and creates no guest, channel or `mail.message`.
If a later message resolves the same exact account/address, the pending touchpoint may
link to its identity/channel but never claims that later message as the original
evidence. Unsupported media carrying a valid referral records only that acquisition
evidence; it never projects incomplete message content. Reply received before its target
retries and later preserves the canonical parent relationship.

Media locators are sanitized before persistence and fetched through the shared Graph
transport. Subscription and health work runs through `queue_job`, with fail-fast
terminal detection and idempotent reconciliation. A dedicated Meta Business App and both
laboratory assets exist and are connected. The remaining gates are external to the
addon: a representative sender without an app role, publication/App Review and a managed
production credential.

## Decision summary

`contact_center_meta` will be one provider addon with adapter key `meta`. Provider and
platform remain separate dimensions:

| Contact Center platform | Meta transport mode                                    | Initial scope |
| ----------------------- | ------------------------------------------------------ | ------------- |
| `messenger`             | Facebook Page through Messenger Platform               | yes           |
| `instagram`             | Professional Instagram account linked to a Page        | yes           |
| `instagram`             | Professional Instagram account through Instagram Login | later         |

The first two modes share a Meta Business App, Messenger Platform webhooks, Page access
and `graph.facebook.com`. Instagram Login is a different authorization and token
lifecycle on `graph.instagram.com`; it must not be hidden behind an accidental fallback.

Each Page inbox and each Instagram professional inbox is a distinct
`contact.center.account`. A linked Page and Instagram account may share one provider
authorization, but never one logical account or one identity namespace.

## Laboratory access inventory

On 2026-08-25 the dedicated Meta Business App `Soloz Contact Center` (App ID
`1588399876398758`), pinned to Graph `v26.0`, was connected without changing the legacy
Lead Ads app or its `leadgen` subscription. The laboratory assets are the Page
`Soloz Industrial - Estrutura Solar` (`117145694671129`) and the linked professional
Instagram account `@solozindustrial` (`17841459160634663`). Odoo stores the App Secret
and Page access token in the shared `meta.api.app` and `meta.webhook.page` records, with
secrets mounted into the container; no credential is stored in this repository.

Graph read-back, last repeated on 2026-09-01, confirmed the dedicated App subscription
set. At App level the object `page` uses `messages`, `message_echoes`,
`message_deliveries`, `message_reads`, `message_edits`, `message_reactions`,
`messaging_postbacks` and `messaging_referrals`; the object `instagram` uses `messages`,
`message_reactions`, `messaging_postbacks`, `messaging_referral` and `messaging_seen`.
The Facebook Page `subscribed_apps` edge receives only the eight `page` fields.
Instagram fields must never be mixed into that Page request; they remain in the
App-level `instagram` subscription. A separate `debug_token` read-back proved that the
Page token is valid, has type `PAGE` and belongs to the dedicated Contact Center app.
The issued authorization covers `pages_show_list`, `pages_manage_metadata`,
`pages_messaging`, `instagram_basic` and `instagram_manage_messages`, with granular
scope restricted to the selected Page and Instagram assets. `pages_read_engagement` is
not present in the issued token; this limits optional metadata discovery but does not
block identity proof, Graph health, inbound callbacks or outbound messaging.

A signed direct-text smoke sent one synthetic Messenger event and one synthetic
Instagram event through the public callback. Both deliveries were routed on the first
attempt to their respective accounts and projected as direct inbound text messages.

A second signed smoke on 2026-08-26 used standalone referrals. Both were routed to the
correct account and created one immutable touchpoint each. The Messenger `ADS` evidence
normalized to `source_type=ad`; Instagram's observed `SHORTLINKS` value normalized to
`source_type=shortlink`. Exact Messenger replay reused the same delivery. Neither event
created an identity alias, guest, channel or message projection.

Phase 6.3 persists cumulative delivery/read high-watermarks by provider connection and
direct conversation. A receipt can arrive before the provider echo that
creates/correlates the outbound message. The later echo replays the cursor without
reprocessing the webhook, while exact message IDs and cumulative watermarks in the same
payload are both honored.

The app remains in Development mode and this user/Page authorization is not the
production credential baseline. Lifecycle monitoring and Graph reconciliation are
implemented. A system-user or equivalent managed long-lived credential, publication/App
Review and a representative sender without an app role remain pending.

## Official authorization modes

### Messenger and Page-linked Instagram

The shared Messenger Platform path uses Facebook Login for Business and a Page access
token. The current documentation lists these permissions:

- `pages_show_list`;
- `pages_manage_metadata`;
- `pages_messaging`;
- `pages_read_engagement`;
- `business_management`;
- additionally for linked Instagram, `instagram_basic` and `instagram_manage_messages`.

The authorizing principal needs the Page `MESSAGING` task and needs the appropriate
moderation/management task to subscribe the app. A system-user authorization is
preferred for continuous server-to-server operation; a personal user token is not the
operational baseline.

### Instagram Login without a Page

Only Instagram Business or Creator accounts are eligible. The documented scopes are:

- `instagram_business_basic`;
- `instagram_business_manage_messages`.

The authorization code is single-use and valid for one hour. The short-lived token is
valid for one hour and can be exchanged for a long-lived token valid for 60 days. A
valid long-lived token can be refreshed before expiry. This lifecycle requires an
explicit refresh and expiry monitor and is deferred until the Page-linked path is
stable.

### Access level

Standard Access is limited to assets and users covered by the app's development roles.
Advanced Access requires App Review and Business Verification for external assets or
users. Meta publishes a review path specifically for a custom inbox, including apps used
by their own business. Acceptance must include a sender with no app role; a test
performed only by administrators is not evidence that production delivery works.

## Webhook contract

The callback must use HTTPS and a valid certificate.

### Verification request

The `GET` handler must:

1. locate the configured Meta App through an opaque local routing key;
2. require `hub.mode=subscribe`;
3. compare `hub.verify_token` in constant time with the configured value;
4. return `hub.challenge` as plain content only on success.

### Event request

The `POST` handler must:

1. read the exact bounded request bytes;
2. verify `X-Hub-Signature-256` as HMAC-SHA256 with the App Secret before decoding JSON;
3. compare signatures in constant time;
4. validate the object and apply an allow-list sanitizer;
5. durably persist one app-level delivery and enqueue fan-out;
6. return `200` quickly.

Meta does not provide webhook history for replay. Delivery is at least once, retries may
arrive out of order and prolonged callback failure can disable delivery. The local
ledger is therefore the replay source.

One callback belongs to the Meta App, not to a single Contact Center connection. It may
contain multiple `entry[]` and messaging items. The background fan-out resolves each
atomic event by object and destination asset, then creates one canonical
`contact.center.inbox.event` per event and connection.

Unknown signed assets are acknowledged but remain visible in the app-level delivery
ledger as `unrouted`; they must not be assigned to a default inbox.

## Routing and identifiers

Facebook Messenger payloads use:

- `object=page`;
- `entry.id=PAGE_ID`;
- `sender.id=PSID`;
- `message.mid` as the message identifier.

Instagram payloads use:

- `object=instagram`;
- `entry.id=IG_ID`;
- `recipient.id=IG_ID`;
- `sender.id=IGSID`;
- `message.mid` as the message identifier.

PSIDs are scoped to a Page and IGSIDs are scoped to an Instagram professional account.
They are opaque strings, not global person IDs and not cross-platform linkage evidence.
The initial namespaces are:

```text
meta.messenger.psid
meta.instagram.igsid
meta.messenger.page
meta.instagram.account
```

Alias uniqueness remains scoped by `contact.center.account`. The same human talking to
Messenger and Instagram starts as two guests unless an operator explicitly links or
promotes the identities.

The preferred inbound dedupe key is the platform, destination asset and `message.mid`.
Receipts and events without a stable event ID use their semantic target/watermark and
timestamp; a canonical JSON digest is only the last fallback.

## Events and capabilities

The initial subscription is deliberately small and expands only with an implemented
normalizer.

| Capability          | Messenger webhook fields                     | Instagram webhook fields                    |
| ------------------- | -------------------------------------------- | ------------------------------------------- |
| messages and echoes | `messages`, `message_echoes`                 | `messages` (`is_echo`)                      |
| replies/postbacks   | `messaging_postbacks`, `messaging_referrals` | `messaging_postbacks`, `messaging_referral` |
| reactions           | `message_reactions`                          | `message_reactions`                         |
| edit/delete         | `message_edits` and message flags            | message edit/delete flags where exposed     |
| delivery/read       | `message_deliveries`, `message_reads`        | `messaging_seen`; no delivery receipt       |

The active Graph contract was confirmed by read-back: Messenger uses the plural
`messaging_referrals`, while Instagram uses the singular `messaging_referral`.
Subscription remains a capability probe and future additions require their normalizer
and fixture before activation.

Group conversations are unsupported and fail closed. A missing or malformed capability
never enables an operation.

## Conversation window and outbound policy

The user must initiate the conversation. Normal replies are allowed within the standard
24-hour window after an eligible user interaction.

The Human Agent path permits a genuine manual response for up to seven days only when
the feature is approved. It is not valid for automation or promotional content and is
not part of the first outbound phase. The adapter must never silently attach that tag.

The legacy message tags `CONFIRMED_EVENT_UPDATE`, `ACCOUNT_UPDATE` and
`POST_PURCHASE_UPDATE` have returned an API error since 2026-04-27 and are excluded.

Provider-window timestamps must be kept separately from the local conversation status.
The Contact Center can keep a conversation open while Meta has already closed its send
window; in that case the composer must fail closed with a clear reason.

## Media

The official APIs cover images, audio, video and PDF. Current documented Instagram
limits are 8 MiB for an image and 25 MiB for audio, video and PDF. Provider capabilities
must remain stricter than or equal to both Meta and core limits.

Meta CDN URLs can expire or disappear after deletion. Inbound media must be scheduled
immediately after the message commit and stored as a private `ir.attachment` only after
MIME, size and hash validation. A remote URL or access token must never enter UiDTO,
bus, logs or a generic request snapshot. If a signed URL has to be retained briefly, it
belongs to a provider-private opaque locator and is cleared after success or terminal
expiry.

Outbound media is deferred until the provider upload/fetch contract is validated with
the dedicated app. An authenticated internal Odoo content URL must never be sent to Meta
as if it were public.

## Rate limiting and health

Rate limiting is per destination asset, not one global adapter bucket. Current official
documentation includes a Conversations API limit of 2 requests/second per Page or
Instagram account and lower Send API throughput for audio/video than for text. The
adapter must classify 429/613 and provider retry hints without blocking unrelated
connections.

Health checks inspect, on a provider-specific cadence:

- token validity and expiry;
- granted and granular scopes;
- Page tasks;
- expected Page/Instagram asset association;
- effective subscriptions and callback drift;
- app mode and configured Graph version.

The generic one-minute health scheduler must not make a Graph request for every asset on
every pass. Results are cached and refreshed with jitter. Authentication loss or asset
mismatch blocks outbound without attempting OAuth automatically.

Server-to-server calls use an explicitly versioned Graph URL and `appsecret_proof` where
supported. Credentials never enter URLs, DTOs, raw envelopes, logs or frontend data.

## Current API transitions to tolerate

Graph API `v26.0` was released on 2026-07-29. The adapter pins the configured version in
every request and records both Graph and provider-schema versions for replay.

- During the Messenger sticker transition ending 2026-08-30, one sticker may be
  represented as both `image` and `sticker`; normalize it once.
- Instagram post sharing is moving from `share` to `ig_post`; accept both shapes.
- Official references use both `SHORTLINK` and `SHORTLINKS` as referral source values.
  The normalizer accepts both and stores canonical `shortlink`. Documentation also
  differs on GIF/sticker delivery and whether the Instagram text limit is bytes or
  characters. Use a conservative 1,000-byte limit until fixtures prove the active
  contract.

These variations require anonymized fixtures from the dedicated sandbox before a
normalizer is declared complete.

## Official sources

- [Messenger Platform overview](https://developers.facebook.com/docs/messenger-platform/overview/)
- [Messenger Send API](https://developers.facebook.com/docs/messenger-platform/send-messages/)
- [Messenger webhooks](https://developers.facebook.com/docs/messenger-platform/webhooks/)
- [Messenger standalone referral event](https://developers.facebook.com/documentation/business-messaging/messenger-platform/webhooks/webhook-events/messaging_referrals/)
- [Messenger policy](https://developers.facebook.com/docs/messenger-platform/policy/policy-overview/)
- [Messenger rate limiting](https://developers.facebook.com/documentation/business-messaging/messenger-platform/overview/rate-limiting/)
- [Instagram Messaging Send API](https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/send-message/)
- [Instagram Messaging webhooks](https://developers.facebook.com/documentation/business-messaging/instagram-messaging/webhooks/)
- [Instagram Platform webhooks](https://developers.facebook.com/documentation/instagram-platform/webhooks/)
- [App Review for a custom inbox](https://developers.facebook.com/documentation/business-messaging/instagram-messaging/app-review/apps-for-your-own-business/)
- [Instagram Platform overview](https://developers.facebook.com/docs/instagram-platform/overview/)
- [Business Login for Instagram](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login/)
- [Instagram Login Messaging API](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api/)
- [Instagram Login webhooks](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/webhooks/)
- [Graph API versioning](https://developers.facebook.com/docs/apps/versions/)
- [Graph API changelog](https://developers.facebook.com/docs/graph-api/changelog/)
- [Messenger Platform changelog](https://developers.facebook.com/docs/messenger-platform/changelog/)
