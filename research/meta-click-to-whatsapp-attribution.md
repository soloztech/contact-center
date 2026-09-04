# Meta Click-to-WhatsApp attribution

- Status: DTO, WuzAPI extraction, core ledger and optional UI projection implemented;
  CRM bridge pending
- Reviewed: 2026-08-25
- Runtime baseline: WuzAPI `v1.0.8`, commit `9487eca`
- Protocol reference: whatsmeow `v0.0.0-20260722203353-e9a033b24933`

This note records the provider payload evidence needed for the future Contact Center to
CRM attribution bridge. It contains counts and field names only. Message bodies, phone
numbers, LIDs, provider IDs, click IDs, tokens, secrets and complete URLs were not
copied from the laboratory.

## Decision

Attribution is an immutable **conversation touchpoint**, not a contact property and not
a tag. A provider adapter must normalize attribution metadata before the core projects
the message, even when the message content itself is unsupported. The core persists the
normalized touchpoint in a dedicated ledger and can optionally project a friendly source
tag or CRM attribution later.

```text
sanitized provider envelope
    -> provider adapter
    -> EventDTO.attribution[]
    -> contact.center.attribution.touchpoint
       |-- inbox event evidence (required)
       |-- message binding (optional)
       |-- conversation/channel binding (optional until resolved)
       |-- identity (optional, never a dedupe key)
       +-- CRM link/projection (future bridge)
```

This boundary is now implemented in `contact_center_base` `16.0.1.19.1`,
`contact_center_wuzapi` `16.0.1.16.0` and `contact_center_ui` `16.0.1.16.0` in the
SERVIDOR05 laboratory. The public webhook route, raw-body authentication, sanitizer and
provider field extraction remain in the provider addon. The base has no generic public
webhook and never parses `externalAdReply`, `ctwaClid` or other WuzAPI field names; it
accepts only validated `EventDTO.attribution` values.

The core captures attribution before attempting the message projection. This lets an
authenticated, normalized event create evidence even when its content wrapper is
unsupported. If message/conversation resolution succeeds, the same touchpoint is then
linked monotonically to the corresponding bindings and direct identity.

`ctwaClid` is optional evidence. It must never be the only idempotency key. After
conversation resolution, the canonical channel/conversation binding plus external
message ID identifies the observed message. The contributing provider connection remains
explicit evidence and is never inferred from the channel binding. Before resolution, a
stable normalized conversation-address fingerprint scopes provisional evidence. An
attribution fingerprint detects enrichment or conflict between multiple payload copies.

## Read-only laboratory evidence

The audit used only Odoo ORM reads against the disposable SERVIDOR05 database. All four
commercial WuzAPI connections were included. Production, provider configuration and
stored records were not changed.

### Population

The snapshot contained **5,006** persisted commercial-account webhooks, all marked with
provider schema `v1.0.8`:

| WuzAPI event       | Count |
| ------------------ | ----: |
| `Message`          | 3,154 |
| `ReadReceipt`      | 1,741 |
| `Connected`        |    36 |
| `Disconnected`     |    29 |
| `KeepAliveTimeout` |    20 |
| `Picture`          |    18 |
| `GroupInfo`        |     8 |

Every envelope was structurally inspected for attribution field names. All attribution
evidence occurred in `Message`; none occurred in receipt, lifecycle, picture or group
metadata events.

Among the 3,154 message events:

- 75 carried at least one attribution or entry-point signal;
- those 75 represented 62 provider-connection/message pairs and 31 conversations;
- 65 were `done` and 10 were `unsupported` by the current application projection;
- 73 had a normalized EventDTO, but none of those DTOs retained attribution fields;
- two had no normalized DTO: one was an empty outbound `externalAdReply`, and one was an
  inbound sticker carrying `conversionSource`/`conversionData`.

This proves that attribution extraction must not depend on support for the message's
content wrapper.

### Actionable `externalAdReply`

There were 26 non-empty inbound `externalAdReply` envelopes with `sourceType=ad`. They
collapsed to **17 canonical messages in 16 conversations**. One conversation therefore
received more than one ad-originated message; conversation identity is not a valid
touchpoint dedupe key.

| Observation                 | Result |
| --------------------------- | -----: |
| Actionable raw envelopes    |     26 |
| Canonical external messages |     17 |
| Conversations               |     16 |
| Distinct `ctwaClid` values  |     17 |
| Distinct `sourceID` values  |      5 |
| `sourceApp=facebook`        |     13 |
| `sourceApp=instagram`       |     13 |
| Image creative              |      8 |
| Video creative              |     18 |

The actionable observations spanned 2026-08-22 through 2026-08-24 UTC. All 17 canonical
messages had a `contact.center.message.binding`, but **zero** of their
`protocol_snapshot_json` values contained attribution metadata.

All actionable `externalAdReply` records were concentrated in one commercial connection.
Broader conversion/entry-point signals appeared across all four commercial connections.
This sample proves the payload shape, but not equal CTWA coverage for every number.

The 26 actionable records contained 12 distinct `sourceURL` values in Meta short-link
and Instagram host families. No UTM query keys were observed in those source URLs.

### Broader conversion and entry-point signals

`externalAdReply` is not the only useful context:

| Signal                               | Raw events | Canonical messages | Conversations | Meaning                                                           |
| ------------------------------------ | ---------: | -----------------: | ------------: | ----------------------------------------------------------------- |
| `conversionSource=FB_Ads`            |         57 |                 49 |            19 | Meta conversion evidence; some messages have no `externalAdReply` |
| `entryPointConversionSource=ctwa_ad` |         23 |                 15 |            15 | Explicit click-to-WhatsApp ad entry point                         |
| Other entry points                   |         13 |                 10 |            10 | Status, chat link, search or phone hyperlink; not paid by default |

Observed non-ad entry-point values were:

- `click_to_chat_link`;
- `global_search_new_chat`;
- `phone_number_hyperlink`;
- `status`.

They should create entry-point touchpoints, but must not be labelled paid media merely
because they arrived through WhatsApp.

Recommended classification:

| Evidence                                                                         | Initial touchpoint type   | Evidence level               |
| -------------------------------------------------------------------------------- | ------------------------- | ---------------------------- |
| Non-empty `externalAdReply` with `sourceType=ad` and an external source/click ID | `paid_ad_click`           | `provider_asserted`          |
| `entryPointConversionSource=ctwa_ad`                                             | `paid_ad_click`           | `provider_asserted`          |
| `conversionSource=FB_Ads` without a source/click ID                              | `paid_ad_signal`          | `provider_hint`              |
| Search, phone hyperlink, status or generic chat link                             | `entry_point`             | `provider_asserted_non_paid` |
| Empty `externalAdReply`                                                          | no attribution touchpoint | insufficient                 |

`paid_ad_signal` may later be enriched or joined to another touchpoint, but must not
invent a campaign ID.

`entryPointConversionApp` included Facebook, Instagram and WhatsApp. Delay values ranged
from immediate to several days. Preserve the provider value as evidence; do not use the
delay alone to classify or join a campaign.

### Duplicate and enrichment behavior

The 26 actionable envelopes contained nine duplicate external message IDs. All 26 raw
inbox dedupe keys were distinct, so these were payload variants rather than exact HTTP
retries:

- seven duplicate pairs had identical attribution objects;
- two pairs delivered the same critical identifiers first, followed by a richer copy;
- the richer copies only added optional behavior/preview flags;
- no duplicate pair changed `ctwaClid`, `sourceID`, `sourceURL`, `sourceApp` or
  `sourceType`.

The current boundaries therefore behave correctly for messaging:

1. the inbox raw-body digest rejects an exact retry;
2. a changed provider payload is retained as separate evidence;
3. the external message ID prevents a second visible `mail.message`.

Attribution needs the equivalent monotonic merge:

1. before a channel binding exists, retain provisional evidence by
   `(provider_connection_id, conversation_address_fingerprint, external_message_id, touchpoint_type)`
   and do not merge an ambiguous address;
2. after resolution, pin the channel binding and upsert by
   `(channel_binding_id, provider_connection_id, external_message_id, touchpoint_type)`;
3. populate fields that were previously absent;
4. accept repeated equal critical identifiers;
5. record a conflict if a later payload changes a critical identifier;
6. never silently overwrite a click, ad-source or campaign identifier;
7. keep every contributing inbox event as technical evidence.

Ordering should use provider `occurred_at` followed by durable inbox ID as a
tie-breaker. The attribution fingerprint must exclude message text and presentation-only
creative copy.

## Observed field contract

### Structured canonical fields

The following fields are useful across providers and should be normalized into the DTO:

| WuzAPI/whatsmeow source             | Provider-neutral target        | Notes                                                               |
| ----------------------------------- | ------------------------------ | ------------------------------------------------------------------- |
| `externalAdReply.sourceType`        | `source_type`                  | Observed as `ad` for actionable records                             |
| `externalAdReply.sourceApp`         | `source_platform`              | Facebook or Instagram in this sample                                |
| `externalAdReply.sourceID`          | namespaced external identifier | Preserve as `meta.source_id`; do not assume campaign/ad/adset level |
| `externalAdReply.ctwaClid`          | namespaced external identifier | Role `click`; optional even if present in this sample               |
| `externalAdReply.sourceURL`         | `source_url`                   | Preserve bounded value; expose only a safe projection in the UI     |
| `externalAdReply.mediaType`         | `creative_media_type`          | Enum: none/image/video                                              |
| `externalAdReply.showAdAttribution` | attribution flag               | Useful evidence, not sufficient alone                               |
| `conversionSource`                  | `conversion_source`            | Observed `FB_Ads`                                                   |
| `conversionDelaySeconds`            | `conversion_delay_seconds`     | Provider observation, not business attribution window               |
| `entryPointConversionSource`        | `entry_point.source`           | Distinguishes paid and non-paid entry points                        |
| `entryPointConversionApp`           | `entry_point.app`              | Provider app/platform hint                                          |
| `entryPointConversionDelaySeconds`  | `entry_point.delay_seconds`    | Preserve as nonnegative integer                                     |
| `utm.*`                             | canonical UTM fields           | Supported by protocol but absent from the sample                    |

`sourceID` is an opaque Meta source identifier. Campaign, ad-set and ad dimensions must
be enriched through a separate Meta API integration or another authoritative mapping;
the webhook does not state which hierarchy level it represents.

### Presentation-only creative metadata

The live payload variants included some of:

- `title`, `body` and `greetingMessageBody`;
- `mediaURL` and `originalImageURL`;
- automated-greeting, call-to-WhatsApp and preview behavior flags.

These values are not attribution keys. A bounded preview may be useful to an operator,
but campaign reporting must join stable external identifiers instead of creative copy or
URLs. The canonical fingerprint should ignore mutable titles, bodies and CDN URLs.

### Opaque provider evidence

`conversionData` and `ctwaPayload` arrive as protobuf `bytes` encoded into JSON strings;
`ctwaSignals` is also an opaque string. In this sample they decoded as UTF-8 but not as
JSON. Their format is not a provider-neutral contract.

The implementation:

- preserve them only in a bounded, admin-restricted provider extension;
- store a SHA-256 fingerprint for equality/conflict checks;
- never log or expose their value through UiDTO/bus;
- not derive campaign fields until an independently versioned decoder is available;
- retain unknown data without making it an idempotency key.

### Fields not observed

No commercial message in the snapshot contained:

- canonical `utm` / `utmSource` / `utmCampaign` values;
- `externalAdReply.ref`;
- explicit campaign, ad-set or ad IDs under separate semantic fields;
- `smbClientCampaignID` or `smbServerCampaignID`;
- `entryPointConversionExternalSource` or `entryPointConversionExternalMedium`;
- `dataSharingContext` or `alwaysShowAdAttribution`;
- a Cloud API-style `referral` object.

UTMs do not automatically travel from a website into WhatsApp. Website buttons still
need a controlled redirect/touchpoint token if that journey must retain UTMs.

## Sanitizer assessment

The existing webhook sanitizer runs before durable envelope persistence. In the current
sample it retained all fields needed for basic Meta attribution:

- `externalAdReply`, including `sourceID`, `sourceURL`, `sourceApp`, `sourceType` and
  `ctwaClid`;
- conversion and entry-point fields;
- `ctwaSignals`, `ctwaPayload` and `conversionData`.

The sanitizer intentionally removes:

- keys containing `thumbnail`;
- keys named/suffixed as `base64` or `b64`;
- sidecars and waveform data;
- data URIs;
- complete `rawMessage` and `sourceWebMsg` copies;
- redundant media keys outside the primary media object.

Consequences:

- loss of ad thumbnail bytes/URLs is acceptable for CRM attribution;
- creative preview must come from a safe canonical URL or later Meta enrichment;
- attribution must never depend on a redundant `rawMessage`/`sourceWebMsg` copy;
- a future provider field whose name ends in `base64`, `b64` or `sidecar` would be
  dropped even if it carried attribution, so adapter-version contract tests are
  required;
- the raw-body digest is calculated before sanitization, so two source payloads that
  differ only in a removed field still remain distinct inbox evidence.

At the audited `1.18/1.15` baseline, the primary data-loss point was **not the
sanitizer**. It was normalization:

- `_message_content()` returns `contextInfo`;
- `_reply_values()` extracts only reply stanza/participant data;
- the message protocol snapshot stores provider/message/timestamp plus source and reply
  metadata, but no attribution data;
- `EventDTO.extensions` stores only WuzAPI instance/user metadata;
- no attribution field reaches the message binding or a domain ledger.

The `1.19/1.16` implementation closes that live-ingress gap. The raw admin-only inbox
remains a possible source for a controlled historical backfill, but current business
logic consumes `AttributionDTO` and the ledger rather than reparsing provider envelopes.

## Provider-neutral DTO

`EventDTO` now carries an immutable tuple of `AttributionDTO`, rather than hiding
attribution inside `extensions`:

```python
AttributionDTO(
    touchpoint_type="paid_ad_click",  # or entry_point / organic_link / unknown
    evidence_level="provider_asserted",
    network="meta",
    source_platform="instagram",
    source_type="ad",
    source_url="...",
    external_identifiers=(
        ExternalIdentifierDTO(namespace="meta.source_id", role="ad_source", value="..."),
        ExternalIdentifierDTO(namespace="meta.ctwa_clid", role="click", value="..."),
    ),
    utm={},
    entry_point={"source": "ctwa_ad", "app": "instagram", "delay_seconds": 0},
    creative={"media_type": "video"},
    flags={"show_ad_attribution": True},
    provider_extensions={"provider.wuzapi": {"opaque_fingerprints": {}}},
)
```

The example values above are synthetic. Required validation:

- all strings are bounded and free of inline binary/data URIs;
- external identifiers are namespaced and preserve provider spelling;
- `utm` accepts only the canonical source/medium/campaign/content/term keys;
- URL parsing is bounded and rejects control characters;
- provider extensions have depth/size limits and cannot reach UiDTO automatically;
- empty `externalAdReply` does not create a paid touchpoint;
- `entryPointConversionSource` maps paid only for an explicit paid enum such as
  `ctwa_ad`; search, phone, status and generic link stay non-paid.

The future official WhatsApp Cloud adapter can map `messages[].referral` into the same
DTO. WuzAPI-specific field names must not leak into the core model.

## Dedicated attribution ledger

Implemented provider-neutral models:

### `contact.center.attribution.touchpoint`

- company, account and provider connection;
- required inbox event evidence;
- optional canonical message binding;
- optional channel binding and identity;
- occurred/captured timestamps;
- touchpoint type, network, source platform/type and source URL;
- entry-point source/app/delay;
- UTM source/medium/campaign/content/term;
- creative media type and safe display flags;
- attribution fingerprint, enrichment state and conflict state;
- restricted provider-extension JSON.

The message/channel links are optional because attribution can arrive with unsupported
content or before identity/conversation projection succeeds.

Creation is restricted to the application service. Exact replay uses the same
connection/canonical key, adds the new inbox event as supporting evidence and does not
create another row. A later compatible observation may only fill empty fields and append
identifiers. A disagreement in critical evidence marks the touchpoint as conflicted
instead of overwriting the first observation. `unlink()` is forbidden, including for an
administrator.

### `contact.center.attribution.identifier`

- touchpoint;
- namespace (`meta.ctwa_clid`, `meta.source_id`, future `meta.ad_id`, `meta.adset_id`,
  `meta.campaign_id`, `google.gclid`, and so on);
- semantic role (`click`, `ad_source`, `ad`, `adset`, `campaign`);
- exact bounded value plus its mandatory SHA-256 comparison hash;
- source provider and evidence timestamp.

A child model is preferable to ad-hoc JSON for searchable joins and later enrichment.
Provider IDs remain strings and are never cast to integers.

Both technical models are administrative records. External identifier values, complete
source URLs and provider extension JSON remain out of the operator API, UiDTO and bus.

### Optional operator projection

Each `contact.center.account` has an administrator-only `attribution_ui_enabled` opt-in,
disabled by default. When enabled, an authorized member may lazily load a bounded,
UUID-cursor projection for that exact channel binding. The right panel shows only safe
classifications and labels such as touchpoint/evidence type, network, source
platform/type, entry point, selected UTM labels, creative media type and time.
Conflicted touchpoints are hidden.

The projection never returns click/ad identifiers, `source_url`, provider extensions or
raw payload fragments. Authorization starts from the channel membership/team scope, so a
shared guest across multiple inboxes does not make one inbox's touchpoints visible in
another.

### CRM bridge

Implement CRM coupling in a later `contact_center_crm` addon, dependent on the base but
without adding `crm` as a dependency of `contact_center_base`:

- link one or more immutable touchpoints to a `crm.lead`/opportunity;
- retain every touchpoint when the same guest returns through another campaign;
- apply an explicit first-touch, last-touch or manually selected policy to the lead;
- map enriched names to Odoo `utm.campaign`, `utm.source` and `utm.medium` only as a
  projection;
- keep provider external IDs in the attribution ledger, not in mutable UTM names;
- never create a lead merely because an attribution payload exists.

Conversation tags may project friendly labels such as `Meta Ads`, `Facebook` or
`Instagram`. Tags are operational UI hints, not the attribution source of truth. Never
put click IDs, ad IDs or complete source URLs into tags.

## Backfill and tests

The durable sanitized inbox makes a historical backfill possible without reposting
messages:

1. select message events with known attribution field names;
2. normalize into `AttributionDTO` only;
3. upsert the attribution ledger using the canonical provider message key;
4. merge richer duplicates monotonically;
5. record critical conflicts without overwriting;
6. optionally link an existing message/channel binding;
7. never recreate `mail.message`, guest, identity or conversation records.

The bounded SERVIDOR05 validation replayed three existing sanitized inbox events through
the current WuzAPI normalizer and only the attribution capture/link services. Two copies
of one provider message converged into a single touchpoint with two evidence links; the
distinct entry-point event created the second touchpoint. The final result was two
touchpoints, three evidence links and two external identifiers, all linked to their
existing messages, channels and identities. Repeating the capture was a no-op, and no
message, guest, identity or conversation was recreated.

One laboratory account was opted into the safe operator projection. The API and a clean
Chromium session rendered the paid click and non-paid entry point while returning no
source URL, external identifier, identifier collection or provider extension. Detailed
non-PII evidence is recorded in
`reviews/2026-08-25-cross-inbox-attribution-validation.md`.

Minimum synthetic fixtures/tests:

- Facebook image and Instagram video `externalAdReply`;
- `sourceID/sourceURL` without `ctwaClid`;
- `conversionSource=FB_Ads` without `externalAdReply`;
- explicit CTWA and every observed non-paid entry-point enum;
- duplicate payload, richer later payload and conflicting critical identifier;
- attribution attached to an unsupported sticker/content wrapper;
- empty outbound `externalAdReply`;
- official Cloud `referral` mapped to the same DTO;
- UTM fields preserved when present;
- sanitizer retains IDs/entry-point data while dropping thumbnail/base64 media;
- UiDTO, bus, logs and ordinary operator views never expose opaque provider evidence.

Representative fixtures must use synthetic identifiers, URLs and copy. Do not export a
live envelope into the repository.

## Revalidation gate

Repeat this audit when WuzAPI/whatsmeow is upgraded or another provider is added:

1. enumerate every attribution path before and after sanitization;
2. compare DTO coverage with the sanitized envelope;
3. replay exact and enriched duplicates;
4. test paid and explicitly non-paid entry points;
5. verify that unsupported message content still creates the touchpoint;
6. verify bounded opaque payload handling and access groups;
7. prove CRM linking does not overwrite older touchpoints.
