# Ad origin previews

Ad previews are part of `contact_center_base` and `contact_center_ui`. Incoming WuzAPI
ad context and Meta Messenger/Instagram referrals can supply a bounded title, body,
public Facebook/Instagram link and thumbnail. Available fields vary by provider and
event; missing details are not inferred from tracking IDs.

The card appears next to the original message and in the attribution panel only when it
has presentable content (title, body, public link or thumbnail) or its touchpoint has a
concrete ad identifier: a numeric `sourceID`/`ad_id` or a CTWA click ID. Rows without
either stay internal and are never shown; historical `paid_ad_signal` touchpoints
without identifiers (lone `conversionSource` markers) stay in the ledger but are left
out of the agent panel. Standalone referrals appear only in the panel until a provider
supplies a real message relationship. The customer's text, acquisition identity and
attribution evidence remain unchanged. Duplicates fill only missing presentation fields;
they cannot replace the first observed copy.

## Configure

1. Upgrade the Contact Center base, UI and installed provider addons. No separate
   preview addon is needed. Ad details obey the inbox's existing attribution visibility
   flag and conversation membership/company permissions.
2. Give `root.contact_center.ad_preview` a small capacity in the existing queue runner.
   Webhook ingestion and UI reads perform no thumbnail or Marketing HTTP.
3. Optionally upgrade the installed Marketing bridge and Meta addons. Enable **Anúncios
   de origem → Completar dados pelo Marketing Center** for the inbox only when catalog
   enrichment is wanted. The actual field label may follow the database language.
   Enrichment defaults to disabled.
4. The optional lookup needs an already mapped, current and unambiguous catalog ad plus
   an active authorized Meta Ads reader. It uses the existing Marketing credentials;
   there is no new credential field in the ad card. An unrelated `source_id` is never
   assumed to be an ad ID.
5. The administrative retry processes at most 100 incomplete previews still inside their
   existing 30-day acquisition window. It does not search old webhook payloads or
   reconstruct thumbnails previously erased by age. Stored images have no age TTL.

When a missing Marketing link or catalog match becomes available, its exact revision can
wake an incomplete preview once. **Tentar completar prévias novamente** lets an
administrator retry up to 100 incomplete previews after fixing access or a previous
failure. Active jobs are deduplicated; expired or deleted content stays erased.

Later catalog details fill missing fields only and carry a visible **Prévia consultada
posteriormente** label. They describe the later lookup, not proof that the current
creative was displayed at the time of the message.

## Content and lifecycle

- Titles are limited to 256 characters and bodies to 2,000. HTML is rendered as text.
  Public links accept only HTTPS Facebook/Instagram URLs with public query keys and
  reject private parameters, fragments, account, login and messaging paths. A stored
  link counts as card content, and reaches the browser, only if it also passes the
  browser rule: ASCII characters the browser keeps unchanged, no repeated keys, no `#`,
  no `.`/`..` segments, valid path encoding and at most 2,047 characters. Text counts
  only when it has a visible character, and a thumbnail only when the preview is ready.
- Signed thumbnail URLs stay in an internal locator vault scoped to the exact connection
  and event. Operational payloads use opaque references. Locators cannot be used after
  one day. Successful use clears their URLs; unused URLs are cleared at the next daily
  cleanup, which can be almost another day later. This storage delay also applies to
  regular backups; daily cleanup is not an erasure of earlier backup copies.
- The worker validates CDN hosts, public DNS addresses and every redirect, pins the
  selected address for TLS, and downloads at most 2 MiB within a bounded timeout. JPEG,
  PNG and WebP are converted to a metadata-free JPEG derivative of at most 640 × 360
  pixels. No remote thumbnail URL reaches the browser.
- Thumbnail attachments are private and served through a route that rechecks inbox
  policy, company and conversation access on every request. Deleted, missing or
  inaccessible previews return no thumbnail.
- Saved copy and thumbnails remain with their message. `expires_at` on a preview limits
  acquisition/enrichment of missing content to the existing 30-day window; it does not
  hide or delete an image already saved. Images erased by the old age cleanup are not
  reconstructed. Message redaction/deletion and conversation retention still erase
  private derived content and fence pending downloads.
- The existing cron now clears only ad thumbnail locators, daily at 03:25 São Paulo.
  Upload receipt cleanup runs at 03:20 and Meta media locator cleanup at 03:30. Each
  drains the fixed initial cutoff in batches, without a total item/time cap in the
  method. A failed transaction is retried by the scheduler; infrastructure worker limits
  still apply. Custom cron intervals and activation are preserved on upgrade. Physical
  file collection remains Odoo's existing GC.
- Message redaction and conversation retention remove derived previews. Deletion after a
  remote fetch is fenced before attachment creation. Historical preview jobs participate
  in the existing conversation retention workflow.
- Thumbnail failures leave the available text/link usable. Transient failures retry with
  a bounded queue policy; a missing terminal job does not leave the interface showing a
  permanent processing state.

## Validation

Pure transport checks use generated images and fake HTTP responses:

```bash
python3 -m unittest discover -s contact_center_base/tests \
  -p test_ad_preview_transport_standalone.py
```

Native tests carry the `contact_center_ad_origin` and `marketing_ad_preview` tags. The
local runner `operations/ad_origin_preview_qa.py` pins both source commits, creates a
fresh synthetic PostgreSQL cluster and Odoo data directory, blocks external networking
and records every selected test. It never opens an existing database or creates an Odoo
backup.

UI QUnit coverage is under `contact_center_ui ad origin`. Real provider payload
availability, account permissions and signed CDN expiry still require a pilot against
the chosen inbox. No external API call is needed for the test suite.

## Recovery after a large backlog

These methods have no total item/time cap. Infrastructure limits still apply. A
hard-killed cron worker rolls back every batch in its transaction; it has not yet
advanced `nextcall`. The scheduler can therefore pick it up at its next poll, repeat the
same work and hit the limit again, rather than wait until the next night. A retry alone
does not guarantee progress.

If that happens, pause only the affected cron under a separately authorized maintenance
operation. Drain small, verified ORM batches with a commit per operator invocation
before re-enabling its daily schedule. Meta already accepts an explicit `limit`; ad
locators support a bounded recordset followed by `_consume()`. Upload recovery needs a
delimited ORM executor preserving the same pending-attachment capture, unlink and
`.exists()` fallback as `_cron_cleanup_expired`; consumed message media must stay. Do
not raise global worker limits or change unrelated indexes blindly. No such production
recovery was performed in this delivery.

Standalone referral/event previews have no message binding. Their saved image stays in
the attribution panel until the conversation is deleted; message-level redaction and
message-history retention do not select those panel-only previews. The removed age cron
used to erase them. Adding another lifecycle trigger is a separate decision, not part of
this cleanup.
