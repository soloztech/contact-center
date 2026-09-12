# Ad origin previews

Ad previews are part of `contact_center_base` and `contact_center_ui`. Incoming WuzAPI
ad context and Meta Messenger/Instagram referrals can supply a bounded title, body,
public Facebook/Instagram link and thumbnail. Available fields vary by provider and
event; missing details are not inferred from tracking IDs.

The card appears next to the original message and in the attribution panel. Standalone
referrals appear only in the panel until a provider supplies a real message
relationship. The customer's text, acquisition identity and attribution evidence remain
unchanged. Duplicates fill only missing presentation fields; they cannot replace the
first observed copy.

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
5. The history action on that tab processes at most 100 eligible touchpoints per
   invocation. It uses retained sanitized provider evidence and queues necessary work.
   Already complete or expired previews are skipped. Old thumbnails that were never
   retained cannot be reconstructed from historical webhook data.

Later catalog details fill missing fields only and carry a visible **Prévia consultada
posteriormente** label. They describe the later lookup, not proof that the current
creative was displayed at the time of the message.

## Content and lifecycle

- Titles are limited to 256 characters and bodies to 2,000. HTML is rendered as text.
  Public links accept only HTTPS Facebook/Instagram URLs, remove tracking parameters and
  reject account, login and messaging paths.
- Signed thumbnail URLs stay in an internal locator vault scoped to the exact connection
  and event. Operational payloads use opaque references. Locators expire after one day
  and their URLs are erased after use.
- The worker validates CDN hosts, public DNS addresses and every redirect, pins the
  selected address for TLS, and downloads at most 2 MiB within a bounded timeout. JPEG,
  PNG and WebP are converted to a metadata-free JPEG derivative of at most 640 × 360
  pixels. No remote thumbnail URL reaches the browser.
- Thumbnail attachments are private and served through a route that rechecks inbox
  policy, company and conversation access on every request. Expired, missing or
  inaccessible previews return no thumbnail.
- Presentation expires after 30 days. The hourly cleanup erases copy and private
  attachments, clears locators and cancels pending jobs. A retained empty marker
  prevents duplicate webhooks or backfill from resurrecting expired content.
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
