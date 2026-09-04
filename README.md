# Contact Center

<!-- /!\ Non OCA Context : Set here the badge of your runbot / runboat instance. -->

[![Pre-commit Status](https://github.com/soloztech/contact-center/actions/workflows/pre-commit.yml/badge.svg?branch=16.0)](https://github.com/soloztech/contact-center/actions/workflows/pre-commit.yml?query=branch%3A16.0)
[![Build Status](https://github.com/soloztech/contact-center/actions/workflows/test.yml/badge.svg?branch=16.0)](https://github.com/soloztech/contact-center/actions/workflows/test.yml?query=branch%3A16.0)

<!-- /!\ Non OCA Context : Set here the badge of your translation instance. -->

<!-- /!\ do not modify above this line -->

Provider-agnostic omnichannel contact center for Odoo 16. WhatsApp via WuzAPI is the
first complete adapter; `contact_center_meta` provides fail-closed inbound, controlled
direct-text/reply outbound and managed Graph health for Messenger and Page-linked
Instagram.
Telegram and other providers can be added without changing the core contract.

## Current status

Phases 2.1, 3, Phase 4 code hardening, 5.1, 5.2, 5.3 and Meta 6.1-6.5 have
validated laboratory evidence. The first greenfield baseline validated on SERVIDOR05
pins OCA `queue_job` `16.0.3.0.2`, `contact_center_base` `16.0.1.43.5`,
`contact_center_wuzapi` `16.0.1.29.2`, `contact_center_meta` `16.0.2.1.1`,
`contact_center_crm` `16.0.2.4.6`, and `contact_center_ui` `16.0.1.26.5`.
The atomic release passed 505 Base tests, 199 WuzAPI tests and 895 integrated tests,
plus the Base viewer and UI QUnit suites in minified and debug-assets modes. Its exact
tree hash is `83b88b22773067c34e370d3784ec05f1bd0b2b09f08a478230475bbe16bb1f9e`.
The complete current evidence and publication disposition are recorded in the
[GitHub release baseline](reviews/2026-09-04-github-release-baseline.md); the detailed
greenfield decisions remain in the
[Base and CRM closeout](reviews/2026-09-03-contact-base-crm-greenfield-review.md).
The shared provider boundary is documented in the
[Marketing Center architecture](https://github.com/soloztech/marketing-center/blob/16.0/ARCHITECTURE.md).
Phase 5.3 evidence is recorded in the
[group-events validation](reviews/2026-08-24-phase5-3-group-events-validation.md), and
the direct-avatar follow-up in the
[avatar/alignment validation](reviews/2026-08-24-direct-avatar-alignment-validation.md).
The Phase 4 pilot webhook hardening is documented in the
[Phase 4 validation](reviews/2026-08-24-phase4-pilot-hardening-validation.md).
Meta 6.3 inbound media and state-event evidence is in the
[6.3 validation](reviews/2026-08-26-meta-phase6-3-hardening-validation.md), and the
corresponding UI inbox projection in the
[Meta inbox visibility validation](reviews/2026-08-26-meta-inbox-visibility.md).
The 2026-08-28 release validation is recorded in the
[2026-08-28 release review](reviews/2026-08-28-release-readiness-validation.md).
The subsequent R3 audit remediation is recorded in the
[R3 remediation validation](reviews/2026-08-28-audit-r3-remediation-validation.md).
The subsequent application-service split and its laboratory release are recorded in the
[application-service refactor validation](reviews/2026-08-29-application-service-refactor.md).
The 2026-08-30 provider-coverage, reply-scope and timeline increment is recorded in the
[2026-08-30 validation](reviews/2026-08-30-provider-coverage-and-timeline-validation.md).
The person-first identity and company-context UX is recorded in the
[2026-09-01 validation](reviews/2026-09-01-person-company-identity-ux-validation.md).
The per-inbox deleted-message display policy is recorded in the
[deleted-message policy validation](reviews/2026-09-01-deleted-message-display-policy-validation.md).
The inbox-density, two-state workflow and inbound-reopen increment is recorded in the
[2026-09-01 validation](reviews/2026-09-01-inbox-density-two-state-reopen-validation.md).
The final architectural review is recorded in the
[independent-audit disposition](reviews/2026-08-25-independent-audit-disposition.md).
Production was not accessed or changed.

The release contract is intentionally narrower than the historical changelog. All
pre-production migrations from Base, CRM and WuzAPI were removed after the laboratory
database had been materialized and validated at the versions above. The first
production-target baseline is therefore a clean install or an exact current schema;
older lab snapshots are rejected instead of being treated as implicitly migratable. Once this
baseline enters production, every later persistent schema/data change must again ship
with a normal cumulative migration.

The atomic release also inventories every Python test module and every QUnit source;
an undeclared test file or a UI source count below the pinned 144-case floor fails
before any deployment mutation. Installed-schema validation also requires the
scheduled intent's immutable `outbound_request_id`, the CRM tombstone's
`lead_record_id_snapshot`, the
productivity ledgers, CRM role-grant ledger, the internal
`contact.center.crm.catalog.authority` concurrency fence and the health-state inbox
fence. The catalog authority is deliberately technical: it has no user menu or ACL
because only bridge services use it to serialize first-binding decisions.

An account is one logical inbox, not one provider credential. It owns the canonical
conversations, routing policy, team and operational state. A provider connection is a
transport/session attached to that inbox. Connections have explicit `primary`,
`standby`, `migration` or `historical` roles: only the active primary can receive
webhooks, and it is the only connection that may dispatch when outbound is enabled.
Standby is configured and monitored without traffic, migration is a staged cutover
candidate, and historical is archived while retaining durable references. This is not
load balancing. A controlled primary switch drains active outbound work, changes the
single ingress/egress owner atomically and keeps provider-scoped message references on
their original connection. Consequently, a new plain message uses the new primary,
while reply/edit/reaction/delete against content from the former provider fails closed
instead of sending an invalid external ID to a different provider.

The standalone Owl client provides the conversation list, state/account/search filters,
paged timeline, unread pointers, text and media composer, replies, reactions, edits,
deletes, delivery/dispatch status, assignments, tags, guest aliases, and explicit
contact link/create/unlink, scoped native quick replies, immutable internal notes,
native follow-up activities, and provider-neutral scheduled sends. Conversation cards
prioritize the logical inbox name over
repeated transport metadata, and the selected-conversation header keeps platform,
provider and inbox together. A persisted compact desktop density narrows the list and
collapses its filters without changing the responsive mobile layout. The operational
workflow has only `open` and `resolved` states, exposed as one contextual Resolve or
Reopen action. Image, audio, video and document messages are rendered in the timeline.
Each inbox chooses whether a deleted message keeps an attenuated, struck-through
content snapshot or exposes only the deletion tombstone; the default is the tombstone
with operational body, reactions and attachments removed. Desktop and mobile layouts
use only the versioned local UiDTO API and
Contact Center bus events; no private Discuss or Live Chat JavaScript is imported or
patched. The composer persists `mail.message`, binding, media and outbox atomically and
never calls a provider. Group conversations display a safe name, private local avatar
and aggregate metadata; opted-in accounts can send text/media, replies and supported
mutations.

Direct conversations now project a provider-neutral, account-scoped profile avatar on
their canonical channel binding. WuzAPI `Picture` events only invalidate an existing
binding; they never create a guest or conversation. An OCA job performs the bounded
provider fetch, validates MIME and size, stores a private attachment and exposes only an
authenticated local conversation URL. The avatar remains independent from optional
`res.partner` promotion.

Agent and Supervisor roles grant operations, while explicit team rosters grant their
operational scope. An account's default team is its authorization and routing boundary.
A team with several agents represents a shared inbox; a team with one agent represents
an exclusive inbox, with any explicitly assigned supervisors. Removing an operational
user from a roster reconciles native channel memberships and immediately removes
historical conversation access. The Administrator role is an accepted privileged
exception for company-scoped configuration and technical records, including media and
mutation ledgers.

When a team is bound to CRM, the bridge records role-grant provenance per binding,
user and group. It revokes only a membership it demonstrably introduced and only after
no active binding requires it; manual/pre-existing roles remain unmanaged. Team,
pipeline, stage, lead and case synchronization uses the same revision-fenced lock graph
as the core, including first-binding creation, so concurrent CRM and Contact Center
changes cannot commit a mixed roster or stage projection.

`mail.channel` and `mail.message` remain canonical. Remote people remain durable
`mail.guest` identities even after an optional `res.partner` link. A trusted protocol
`whatsapp.pn` reuses one identity/guest across the inboxes of the same company, while
each account keeps its own direct channel and operational state. LID, opaque JID, PSID
and IGSID stay account-scoped, and different companies remain isolated. UI sends use a
canonical UUID request key, making repeated submissions idempotent before dispatch.
For a direct WhatsApp identity without a reliable name, the core prefers a trusted
`whatsapp.pn` alias and renders it as a human-readable telephone number instead of an
opaque LID. The WuzAPI profile job performs a bounded, targeted `/user/check` lookup and
projects a safe `VerifiedName` when one exists. Manual names, linked contacts and both
the original LID and PN aliases remain authoritative and preserved.
Conversation ordering follows local message arrival (`create_date` plus local message
ID), while the provider timestamp remains display metadata. Group roster entries are
technical records with PN/LID aliases; synchronizing them does not create guests,
contacts or channel memberships. Group replies and mutations persist and revalidate
structured own/target protocol participants; an absent, ambiguous, or stale PN/LID
mapping fails closed before provider I/O. Per-participant delivered/read receipts
converge in a dedicated technical ledger and reach the UI only as aggregate counts.

An earlier validated increment passed **408/408** core tests, **204/204** WuzAPI tests and
**742/742** integrated tests with no failures or errors. Its authenticated UI smoke
covered the inbox-priority hierarchy, compact density, contextual state action and
per-inbox inbound-reopen option. The authenticated media route is covered for `206`,
`304`, `416` and deleted-content `404` responses.

The 2026-09-02 native-first increment added two prerequisites.
`contact_center_base 16.0.1.36.0` and
`contact_center_ui 16.0.1.24.0` add a safe, auditable resend: only an unequivocally
failed `dead` send can create one new message/binding/outbox linked by `retry_of`; the
terminal source is never reopened and `uncertain` is never resent blindly.
`contact_center_crm 16.0.2.1.0` adds an administrator-only inventory of explainable CRM
mapping candidates. It never accepts a heuristic match or creates a binding without an
explicit, revalidated administrator action. The atomic SERVIDOR05 release passed
**413/413** base, **204/204** WuzAPI and **762/762** integrated tests; CRM-MAP has
**50** directed tests. Authenticated QUnit passed **114/114** tests and **1062/1062**
assertions in both asset modes, with zero console errors or warnings. Desktop and
compact-density application smokes also completed without console errors or warnings.
The deployed tree hash is
`59e653bcde155f838228424e79e4dcd85343d712f1edbdcc746a7bab4943cd86`; canonical
evidence is in `scans/raw/20260902-cc-send-crm-map-release-r4` and is tracked in the
[native-first first implementation release](reviews/2026-09-02-native-first-first-implementation-release.md).
No automatic UTM write, tracking cutover or CRM binding is enabled, and production was
not touched.

A previous Phase 3 real WuzAPI smoke exercised outbound media, reply, edit, reaction and
delete; every provider request returned HTTP 200 and every outbox command created by
that smoke finished in `done`. No tested media or mutation remained in a failed state.

The MVP supports text, image, audio, video and document messages, with one attachment
per outbound message, plus reply, reaction, edit and delete according to provider
capabilities. Opted-in groups support outbound text/media, replies, reactions, edits and
deletes according to per-conversation capabilities. Participant delivered/read receipts
are kept in a dedicated ledger and exposed to the UI only as aggregate counts.
Campaign broadcasts, automated journeys, history import and participant management
remain outside the pilot. WuzAPI intentionally stays configured with `--skipmedia=true`: inbound media is
fetched asynchronously through authenticated download endpoints and stored as private
Odoo attachments instead of depending on WuzAPI's native S3 path.

## 2026-08-28 Meta Phases 6.1 through 6.5

`contact_center_meta` now provides the fail-closed ingress foundation for Facebook
Messenger and Page-linked Instagram. A company-scoped Meta App owns one opaque callback,
App Secret and verify token; Page authorizations may be shared by distinct operational
connections, while each Page or Instagram asset remains a separate
`contact.center.account`.

The public callback verifies the GET challenge and authenticates POST bytes with
`X-Hub-Signature-256` before JSON decoding. A bounded allow-list sanitizer persists an
immutable app-level delivery and Graph-version snapshot, then an OCA job decomposes
batch entries and routes each atomic event strictly by object, transport mode and asset.
Unknown, inactive or ambiguous assets remain auditable and never fall into a default
inbox. Replays converge at both delivery and atomic-event layers.

Phase 6.2 normalizes direct Messenger and Instagram text, reply correlation and
provider echoes into the same guest, identity, `mail.channel` and `mail.message` core.
PSID/IGSID remain account-scoped, contradictory endpoint orientation fails closed, and
Meta referral evidence enters the immutable attribution ledger. Phase 6.3 adds inbound
image, audio, video and document locators with private asynchronous download, plus
delivery/read/seen, reply, reaction, edit/unsend and postback normalization according to
each platform's capability. Phase 6.4 adds canonical direct text outbound, a strict
24-hour response window, `appsecret_proof`, rate-limit retry and an `uncertain` terminal
path that never resends blindly. Phase 6.5 adds revision-fenced OCA jobs for token,
scope, task, asset and subscription inspection plus explicit apply/read-back. The
runtime correctly remains degraded until the external Meta permission and production
credential gates are satisfied. No legacy Lead Ads app, subscription, token or
production system was changed.
Validation evidence is in the
[6.1 foundation](reviews/2026-08-25-meta-phase6-1-foundation-validation.md) and
[6.2 validation](reviews/2026-08-25-meta-phase6-2-direct-text-validation.md), with the
current state in the
[6.3 validation](reviews/2026-08-26-meta-phase6-3-hardening-validation.md).

## 2026-08-25 Per-inbox controls, webhook subscriptions and voice recording

Each account/inbox now has independent switches for signing agent messages, receiving
group messages and sending to groups. A signed WuzAPI message is rendered only at the
adapter boundary as `*Agent name:*` followed by a line break; the canonical
`mail.message`, command ledger and UI keep the clean body. The operator name is captured
semantically when the command is created, so retries remain stable after a rename or
configuration change.

WuzAPI connections expose the provider's versioned webhook-event catalog in their
existing provider form. Applying or checking a selection schedules an OCA `queue_job`;
the remote write/read-back, retry, configuration fencing and drift result happen outside
the browser request. A real laboratory read-back reported `In Sync` and confirmed the
configured callback URL.

Authorized operators can edit a guest's display name inline without creating or
replacing a contact and without changing PN/LID aliases or historical authorship. The
standalone composer can record audio through `MediaRecorder`, intersecting browser
support with provider capabilities. Recorded OGG/Opus is sent as a WuzAPI voice note;
audio-only MP4 is sent as regular audio. Upload and dispatch validate the container,
content hash, actual server-derived duration and provider limits rather than trusting
browser metadata.

Authenticated Chromium recorded and uploaded a real fragmented MP4 audio stream, showed
its 21-second preview and then removed it without sending a message. The application and
both QUnit asset modes completed with zero console errors. Meta Click-to-WhatsApp
payload research is documented in
[`research/meta-click-to-whatsapp-attribution.md`](research/meta-click-to-whatsapp-attribution.md);
the provider-neutral ledger and safe opt-in UI projection are now implemented, while the
CRM bridge remains deliberately deferred. The complete test and deployment record is in
[`reviews/2026-08-25-per-inbox-webhooks-voice-validation.md`](reviews/2026-08-25-per-inbox-webhooks-voice-validation.md).

## 2026-08-25 Cross-inbox identity and attribution

A protocol-validated `whatsapp.pn` is now a portable company-level identity key. The
same person may use one `contact.center.identity` and `mail.guest` in several inboxes,
with a separate `mail.channel`, team, assignee, state and account-local aliases in each
inbox. LID/JID/PSID/IGSID do not cross that boundary. Concurrent first observations are
serialized, and legacy duplicates merge only when company/state, linked-contact, manual
name and per-inbox conversation checks all pass.

Safe reconciliation redirects retired identities and moves active aliases, bindings and
memberships to the survivor. Historical message authors, reactions, mutation actors and
tombstones retain their original guest references. Because Odoo 16 treats a channel
member's `guest_id` as immutable, the migration creates the survivor membership before
removing the retired active membership.

WuzAPI now converts supported Click-to-WhatsApp/Meta acquisition evidence into the
provider-neutral `AttributionDTO`. The base captures it before message projection in an
append-only ledger, deduplicates exact replays, enriches only missing evidence and marks
critical disagreements as conflicts. Unsupported message content can still create a
touchpoint. The provider addon remains responsible for webhook authentication and
provider field extraction; the base exposes no generic public webhook.

An administrator may opt an account into a bounded right-panel projection. Operators see
safe source/evidence labels for their authorized channel only; external IDs, complete
URLs, provider extensions and raw payloads remain administrative. A later
`contact_center_crm` addon will link these immutable touchpoints to leads/opportunities;
`contact_center_base` deliberately has no CRM dependency.

A controlled replay of three persisted commercial webhooks produced two canonical
touchpoints: two copies of the same provider message converged into one row with two
evidence links, while the distinct entry-point event produced the second row. Repeating
the capture changed no counts. Both touchpoints linked to their existing message,
channel and identity. The account-level UI opt-in was enabled for one laboratory inbox
and Chromium rendered one paid-ad click and one non-paid entry point using only the safe
projection.

## 2026-08-24 Phase 4 pilot webhook hardening

Provider-observed names now converge monotonically from persisted inbound events into
the account-scoped identity, durable guest and managed direct channel. Invalid protocol
fallbacks are ignored, while manual names and promoted partners remain authoritative.
The migration performs a local, deterministic backfill without provider I/O or raw
webhook replay.

An external-device direct message can now create its missing conversation from the
conversation addresses without projecting the account's own `PushName` onto the remote
guest. Self-side direct read evidence is terminally classified as unsupported instead of
being retried as outbound delivery. WuzAPI receipt normalization also distinguishes the
own and remote sides and treats an explicitly empty `MessageIDs` batch as permanent
unsupported input.

The laboratory investigation of a sanitized LID case (`lab-subject-a@lid`) covered all 114 persisted
webhooks. A recovery copy without a name created the initial fallback; the immediately
following canonical message already carried the provider name and PN alias, exposing the
missing convergence step. After the migration and directed replay, the UI shows
the expected contact name, all 45 correlated provider message IDs remain unique, the six
dependent receipts and three early external-device messages are `done`, and three
self-side read receipts are correctly `unsupported`.

Document media accepts vendor MIME types such as `image/vnd.dwg` without weakening the
inline image policy. Two historical DWG downloads converged to private ready
attachments. The exact 104 malformed empty receipt batches were reclassified from `dead`
to `unsupported`; no broad dead-letter replay was performed.

The final isolated suites passed 193/193 base and 290/290 integrated tests. An
authenticated Chromium session found the corrected direct conversation, rendered text,
images, audio and documents, showed five of five connections healthy and emitted no
console error. The disposable lab was upgraded without a backup by explicit instruction;
production remained untouched.

## 2026-08-24 Phase 5.3 completion — group mutations and participant receipts

Group reactions can target remote or own messages. Group edits and deletes remain
restricted to correlated outbound messages owned by the account. EventDTO and CommandDTO
carry explicit `target_from_me`, own participant and target participant evidence;
inbound, echo and dispatch revalidate account opt-in, capability, health, profile,
direction, roster and PN/LID equivalence before any provider-side mutation.

WuzAPI dispatch uses `/chat/react`, `/chat/send/edit` and `/chat/delete`. Mutation
projection remains monotonic, reactions use actor-specific lanes, and delete is
terminal. A `from_me` echo resolves the actor from the account's own participant rather
than from the author of the target message.

`ReadReceipt` events for outbound group messages converge by message, technical
participant and `delivered/read` milestone. They do not create guests, contacts,
identities or memberships, and they never promote the aggregate message state. Mixed
batches preserve known correlations while recording only the count of unknown entries as
technical Inbox metadata. UiDTO exposes `delivered_count` and `read_count` only.

The WuzAPI `16.0.1.12.0` migration refreshed capabilities and scheduled authoritative
group metadata without replaying historical raw mutations or receipts. Automatic replay
is deliberately excluded because it cannot be safe without exact message correlation and
an unambiguous current roster.

The final isolated suites passed 176/176 base tests and 267/267 integrated tests.
Authenticated QUnit passed 51/51 tests and 478/478 assertions for the UI, plus 4/4 tests
and 15/15 assertions for the JSON viewer, in minified and debug asset modes. The
authenticated application loaded direct and group conversations, exposed per-message
actions, showed five of five connections healthy and produced no browser console errors.
No real group message or mutation was sent during validation.

The atomic laboratory release isolated only the exact test route, upgraded offline,
restored the route byte-identically and returned both internal and public HTTP 200.
Database backup was explicitly waived for the disposable SERVIDOR05 lab; production was
not addressed. Evidence is under
`scans/raw/20260824-odoo16-contact-center-phase5-3-group-events/` and
`scans/raw/20260824-odoo16-contact-center-phase5-3-atomic-release/`.

## 2026-08-24 Phase 5.3b laboratory release

Group outbound now supports text, image, audio, video and document messages plus replies
through provider-neutral DTOs. Reply targets carry the canonical protocol participant
selected from the authoritative roster and its WuzAPI `AddressingMode`. Audio captions
remain prohibited and each outbound message accepts at most one attachment.

Dispatch fails closed if account opt-in, capabilities, connection health, profile,
health revision, roster or target participant changed. It validates those invariants
again after provider I/O; only a confirmed response persists the external ID and
participant. An ambiguous response remains `uncertain`. PN/LID alternates are considered
equivalent only when the current roster resolves them to the same participant, and a
conflicting echo or reply target cannot silently become a new message.

The WuzAPI `16.0.1.11.0` migration refreshes capabilities, schedules group metadata,
clears stale sender evidence and backfills historical group-message participants from
protocol snapshots. After upgrade, all 62 group profiles were `ready`, all had a
canonical participant matching the current health revision, and all four historical
outbound group messages with sender evidence were backfilled.

The final isolated suites passed 165/165 base tests and 252/252 integrated tests.
Authenticated QUnit passed 48/48 tests and 438/438 assertions in minified and debug
asset modes. Black, isort, Flake8, mandatory Pylint, Prettier, ESLint, OCA checks,
Python compilation, `node --check`, XML checks and `git diff --check` are clean.

The authenticated UI loaded 159 conversations, displayed group metadata and safely
withheld the composer for an account without group opt-in. The fleet showed five of five
connections healthy and the browser console ended with zero errors and zero warnings. No
real group message was sent during validation. Final deploy, isolated tests and upgrade
evidence is under
`scans/raw/20260824-odoo16-contact-center-phase5-3b-group-media-reply/`.

## 2026-08-24 Phase 5.2 laboratory release

The WuzAPI adapter treats `GroupInfo`, `JoinedGroup`, and `Picture` as change hints and
pulls the authoritative snapshot through `GET /group/info?groupJID=...`. All five
laboratory instances subscribe to those events. Complete snapshots reconcile removals;
partial snapshots remain stale and retry after 15 minutes. The complete snapshot TTL is
six hours. Both current numeric group JIDs and the legacy `numeric-hyphen-numeric@g.us`
shape are accepted.

The final backfill converged with 51/51 group profiles ready, 49 stored avatars, 19,365
technical participants, and 38,697 aliases. The counts of `mail.guest`, `res.partner`,
and `mail.channel.member` remained unchanged during convergence, proving that roster
sync did not create people or memberships in bulk. The guarded administrative
`action_retry_metadata_sync` recovered all 15 initially failed synchronizations.

A later read-only snapshot after UI `16.0.1.8.3`, while new provider data continued to
arrive, showed 53/53 profiles ready, 51 avatars, 19,860 technical participants, 39,686
aliases, and five of five active provider connections connected. This live snapshot is
separate from the controlled backfill invariant above.

The authenticated browser loaded 117 conversations with all five connections healthy,
rendered read-only group name/avatar/aggregates and produced no console errors or
warnings. Thirty-two avatars rendered; the authenticated local route returned
`200 image/jpeg`, `nosniff`, and private cache headers, while a cold anonymous request
was redirected to authentication. Browser artifacts are under
`output/playwright/phase5-2-group-metadata/`.

A final UI handoff has also been integrated for message grouping, an internal
image/video viewer, an audio player, and touch/accessibility actions. Its first asset
rebuild was versioned as `contact_center_ui` `16.0.1.8.3`. The first QUnit pass found
that the internal sanitizer accepted a ready avatar but omitted its state; `8b83773`
corrected the projection and `a1b61f6` forced an unambiguous rebuild. The final QUnit
run passed 40/40 tests and 335/335 assertions in both asset modes at that checkpoint.

The real UI smoke grouped an eight-message sample as one start plus seven continuations,
with the last continuation closing the run; the group remained read-only without a
composer. Eighteen real audio players reached `readyState=4` without error and changed
speed from 1x to 1.5x. The same smoke found that a single-image viewer retained a
three-column grid and squeezed a naturally 1200x1600 image to approximately 13 pixels.
After the `16.0.1.8.3` template/SCSS fix, desktop revalidation measured a 1138-pixel
viewer/stage and a 464x618 rendered image with no navigation controls. At 390x844
mobile, the stage measured 378 pixels and the image 366x488 without overflow. Escape
restored focus and the console remained at zero errors and zero warnings.

The final `16.0.1.9.0` increment groups conversations by inbox, keeps a flat-view
alternative, and moves responsibility scopes (`all`, `mine`, and `unassigned`) to the
server before pagination and totals. Reassignment and realtime refresh reconcile stale
selection. A per-tab deferred image queue gives timeline media priority, starts images
only near the viewport, reserves layout space, and limits requests to two concurrent
downloads. Authenticated QUnit passed 47/47 tests and 416/416 assertions in both asset
modes. A real-browser smoke reported 133 open conversations, global totals of two for
`mine` and 131 for `unassigned`, opened the internal viewer, and measured a maximum of
two concurrent media requests before and after incremental scrolling. The health panel
settled at 5/5 connected and the console remained at zero errors.

The final deploy, base/UI upgrade, and validation evidence is under
`scans/raw/20260824-odoo16-contact-center-phase5-2-group-metadata/`. The upgrade touched
only base/UI module IDs 3817 and 3818; business data remained unchanged and no database
backup was created for the disposable laboratory. The deploy retained only its
automatic, recoverable copy of the previous source tree.

## 2026-08-23 mutation, media and realtime increment

Mutation projections are monotonic by provider occurrence time and local tie-breaker.
Deletes are terminal, reactions use actor-specific lanes, self echoes use the account's
technical author, and targets may correlate through either external or client message
IDs. Native Discuss reactions are blocked for Contact Center messages so all external
mutations continue through the audited ledger and outbox.

Outbound media now reads `ir.attachment.raw`, validates metadata before the read when
possible, and encodes once in the WuzAPI adapter. A 50 MiB isolated measurement reduced
the observed peak from approximately 400 MiB to 281 MiB. This is a bounded mitigation,
not true streaming; a short signed URL/provider-fetch transport remains backlog.

Realtime timeline refreshes merge into the already loaded history instead of resetting
it to the latest 100 entries. The oldest pagination cursor is preserved, notifications
for other channels are ignored, and the component follows the bottom only when the
operator was already near it. Otherwise it keeps the reading position and announces a
new-message count. Reconnect gaps larger than the latest-page window are recovered with
forward-cursor pages from a fenced high-water mark. Automatic catch-up has an aggregate
cap of 20 pages; larger backlogs are reanchored to a fresh head, while older history
remains available through backward pagination.

## 2026-08-23 code-quality increment

The five Python functions and two JavaScript functions that exceeded the repository's
configured complexity limits were decomposed without changing their public contracts.
Shared retry values now have named constants, while the distinct inbox, outbox, media
and health state machines remain separate. Controller translations, deterministic
migrations and the objective formatting/lint findings were also corrected.

Black, isort, Flake8, the mandatory Pylint checks, Prettier, ESLint, `node --check`,
Python compilation and OCA checks are clean. New tests cover stale and racing UI loads,
pagination, malformed/interrupted WuzAPI downloads and the shared retry ceiling. A
physical split of the large Python/JS/SCSS files, a generic job mixin and generic sudo
helpers were deliberately deferred because they would increase churn or hide security
boundaries without improving this release's behavior.

## 2026-08-23 M4 laboratory release

Every minute, an Odoo cron only schedules one idempotent OCA job per due active
connection. It performs no provider I/O. Health jobs use the dedicated
`root.contact_center.health` channel, priority 30, and a deterministic 0–29 second
jitter. Manual refresh is Supervisor/Administrator-only and follows the same queue.

The health contract persists `connected`, `degraded`, `disconnected`, or
`authentication_required`. `unknown` is derived when there is no fresh observation;
`checking` is a transverse flag and never replaces the last-known state. The compact
UiDTO is designed for roughly 20 numbers: it returns a fleet summary and safe items with
the logical account, provider connection, platform/provider, own display address, state,
checking flag, diagnostic code, and timestamps. Agents see only roster-visible inboxes.
Incremental `connection_health_updated` bus events update one item without a full
bootstrap. A monotonic client-side revision fence prevents a slower refresh RPC from
overwriting a newer bus result. If a bus event is lost, bounded fallback polling uses 11
attempts over 180.5 seconds; the Odoo 16 Owl view observes `connectionHealthRevision` so
the fleet converges without a page reload.

UI and outbound dispatch share the same 180-second freshness predicate. Once an
observation is older than that, the public state becomes `unknown` and dispatch is
blocked before the provider boundary. A later fresh health result can make the
connection available and recover eligible outbox work even when the stored `state` was
already `connected`; recovery follows unavailable-to-available, not merely a database
state change. An identity safety latch never masks a fresh `disconnected` or
`authentication_required` observation in the fleet. It remains `degraded` for a
connected or stale observation and always continues to block dispatch until verified.

WuzAPI polling through `GET /session/status` converges with the exact lifecycle events
`Connected`, `KeepAliveRestored`, `Disconnected`, `ConnectFailure`, `StreamReplaced`,
`KeepAliveTimeout`, `StreamError`, `LoggedOut`, `QRTimeout`, `ClientOutdated`, and
`TemporaryBan`. `Connected` and `KeepAliveRestored` are only
`health_confirmation_required` hints: they immediately keep the connection
`degraded/identity_unverified`, enqueue a unique low-priority probe, and never reopen
outbound by themselves.

A configured own identity is compared with the normalized session JID. A mismatch
becomes `identity_mismatch`; a connected session without a JID becomes
`identity_unverified`; no raw JID is published. WuzAPI also treats a missing configured
own identity as `identity_unverified`: it cannot report health `connected` for outbound
until the expected identity exists and matches the session. These negative results set
the durable `identity_mismatch_latched` safety lock. Lifecycle events, intermediate
outages, and health results without explicit identity proof do not clear it. Only a
fresh health result containing both `state=connected` and `identity_matches=true`
releases the lock. Odoo never calls connect, login, or QR generation automatically.

Outbound dispatch requires an active logical account and an active, outbound-enabled,
`connected` provider connection with no identity safety lock. Archiving the account is a
final dispatch guard. Recovery can occur only after the explicit verified match above
and may resume only `pending`/`retry` commands that have not crossed the durable
dispatch boundary. It never automatically resends `processing`, `uncertain`, `dead`,
`done`, or `cancelled` commands.

Provider `Retry-After` is persisted as `health_retry_not_before`. Scheduled and manual
refreshes both honor that durable cooldown, so a supervisor refresh cannot enqueue a
probe before the provider allows it. The endpoint still performs no provider I/O.

WuzAPI service URL, API token, and expected own identity cannot change while outbound is
active or any affected command is `processing`. After outbound is disabled and in-flight
dispatch drains, an accepted change fails closed: it marks
`degraded/identity_unverified`, sets the identity latch, increments the monotonic
`health_configuration_revision`, clears a `health_retry_not_before` cooldown belonging
to the previous configuration, and schedules confirmation. Immediately before the health
HTTP request, the adapter invalidates the ORM cache for service URL, token, and expected
identity and rereads them. A health response started under an older revision is
discarded and can never reopen outbound.

Correctly authenticated webhooks for archived accounts or connections receive a
successful `ignored=inactive` acknowledgement without an Inbox ledger or queue job;
invalid signatures remain rejected. Account/connection archive, default-team changes,
and roster changes invalidate the scoped fleet for current and previous recipients. The
client refreshes its authorized bootstrap and never inserts an unknown bus connection ID
directly into the current fleet.

The laboratory JobRunner uses
`root:4,root.contact_center.health:2,root.contact_center.media:1`. The leaf caps admit
two health probes for the approximately twenty-number fleet, prevent slow downloads from
occupying more than one slot and leave at least one global slot for other queues at the
worst simultaneous health/media load. Capacity must still be measured again for the
eventual pilot workload and production topology.

## 2026-08-22 hardening release

The base addon now includes a reusable read-only JSON inspector for technical ledgers.
Inbound records label the provider payload as **Sanitized Envelope**, and sensitive raw
and normalized JSON is restricted to Contact Center administrators. Outbox records show
the canonical command, a sanitized provider-request snapshot persisted before external
I/O, and the sanitized provider response. Media snapshots contain metadata, byte size
and SHA-256 only; inline binary, base64, credentials and authorization headers are
rejected. Historical outboxes are not backfilled with an invented request.

Audit hardening moves inbound-media and orphan-event retries back to the declarative OCA
`queue_job` retry patterns, increases the media-attempt horizon, adds supervised manual
recovery for failed downloads, forces unsafe document content to download with a
restrictive CSP, and grants administrators company-scoped Inbox/Outbox ledger access.
Connection recovery was subsequently implemented by the M4 laboratory release above.
Mutation ordering/actor semantics and incremental real-time timeline merges were closed
in the 2026-08-23 increment; true streaming remains Phase 4 work. The original audit
disposition and its M4 addendum are recorded in
[`reviews/2026-08-22-phase3-audit-disposition.md`](reviews/2026-08-22-phase3-audit-disposition.md).

## Runtime and development

Use the OCA `queue` repository on branch `16.0` and install modules in this order:
`queue_job`, `contact_center_base`, a provider addon such as `contact_center_wuzapi`,
and `contact_center_ui`. A running deployment must load `queue_job` server-wide and
configure an active JobRunner; size workers and channel capacity before production use.
Do not enable `QUEUE_JOB__NO_DELAY` outside focused tests. Provider addons must
communicate with the core only through the adapter/DTO boundary and extend the generic
provider connection form for their own settings.

`contact_center_meta` additionally depends on the independent `meta_api_base` and
`meta_webhook_base` addons co-located in the `soloztech/marketing-center` repository.
Those foundations own only credential-safe Graph transport, authenticated ingress and
neutral technical errors; the Contact Center facade retains messaging semantics and
translates failures into its adapter contract. This source-level dependency does not
require `marketing_center_base` or any functional Marketing Center addon.

Outbound starts are durably paced per provider connection. The core reserves the next
eligible dispatch instant in the same transaction as the `processing` boundary and
releases every connection lock before provider I/O. Adapters declare only their minimum
interval (`1 s` for WuzAPI, `0` by default); a positive provider `Retry-After` is shared
by sibling commands without consuming their attempt budget. WuzAPI and Meta use a
bounded 60-second fallback for a rate-limit response without a valid header. This
connection-local contract does not replace future App-wide quota coordination for
providers whose quota spans several connections.

Before the general pilot, exercise disconnect, authentication loss, stale/out-of-order
events, reconnect, and an `uncertain` non-requeue canary under the expected workload.
The deployed base `16.0.1.25.1`, WuzAPI `16.0.1.21.1`, Meta `16.0.1.7.1` and UI
`16.0.1.18.2` passed **353/353** base tests and **674/674** integrated tests. QUnit
passed **80/80** UI tests with **700/700** assertions in
minified and `debug=assets` bundles; the JSON viewer passed 4/4 with 15/15. The
authenticated browser reported all five configured WuzAPI sessions connected, both
Meta inboxes projected, realtime active and zero console errors or warnings. The
installed module source tree
`3236382e2d5b333b7f95f0c086b0c02aeaab4aea0be43ecf4b88565881a9bb98` passed the
fresh registry composition gate for `contact.center.application` and
`contact.center.ui.api`. JobRunner capacity
`root:4,root.contact_center.health:2,root.contact_center.media:1` was revalidated at
runtime. Atomic release evidence is in
`scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260830T061716589446Z`;
the isolated suites are in
`scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260830T061817057015Z`;
and the installed-state validation is in
`scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/validate/20260830T062110843001Z`.
Upgrade and final validation succeeded on SERVIDOR05. Production was not accessed or
changed. This laboratory validation does not by itself complete the general pilot or
the external Meta approval gates.

<!-- /!\ do not modify below this line -->

<!-- prettier-ignore-start -->

[//]: # (addons)

This part will be replaced when running the oca-gen-addons-table script from OCA/maintainer-tools.

[//]: # (end addons)

<!-- prettier-ignore-end -->

## Licenses

This repository is licensed under [AGPL-3.0](LICENSE).

However, each module can have a totally different license, as long as they adhere to
Soloz Technologies policy. Consult each module's `__manifest__.py` file, which contains
a `license` key that explains its license.

---

<!-- /!\ Non OCA Context : Set here the full description of your organization. -->
