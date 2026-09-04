# WuzAPI group metadata

- Status: Phase 5.2 deployed and validated in the disposable SERVIDOR05 laboratory;
  general pilot pending
- Reviewed: 2026-08-24
- Baseline: WuzAPI `v1.0.8`, commit `9487eca`

## Reviewed provider contract

The pinned WuzAPI revision exposes the authoritative group snapshot through:

- `GET /group/info?groupJID=<group-jid>` for the group name, participant roster,
  participant roles and opaque participant revision;
- `POST /user/avatar` with `Phone=<group-jid>` and `Preview=true` for the current
  picture locator and opaque picture revision.

The effective routes in the pinned source take precedence over the examples in `API.md`:
`/group/info` reads the case-sensitive `groupJID` query parameter and `/user/avatar` is
a JSON `POST`. The adapter must not reproduce the obsolete GET-body examples from the
provider documentation.

Observed group references are not limited to an all-numeric local part. The adapter
accepts the current numeric shape and the legacy `numeric-hyphen-numeric@g.us` shape.
Both remain group conversation references; the hyphen is not a device separator and must
not be rewritten as a participant PN or LID. Other non-reviewed local-part shapes and
`@broadcast` remain rejected.

WuzAPI can also publish `GroupInfo`, `JoinedGroup` and `Picture`. These events are only
change hints. Their payload shape is not a complete, stable snapshot and therefore must
never directly replace the canonical profile or roster. A valid hint identifies the
group, schedules the same idempotent asynchronous pull and may be coalesced with an
already active pull. Ordinary inbound group messages can create/backfill the profile, as
can an authenticated, uncorrelated human group message sent from the connected account
on an external device. Neither path waits for provider I/O and neither continuously
supersedes an active initial or TTL job.

Primary source references at the pinned revision:

- [effective HTTP routes](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/routes.go);
- [`GetGroupInfo` and `GetAvatar` handlers](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/handlers.go);
- [supported webhook event names](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/constants.go);
- [provider API examples](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/API.md).

## Provider-neutral persistence boundary

Every group conversation has at most one provider-neutral group profile attached to its
existing `contact.center.channel.binding`. The profile owns only operational group
metadata:

- safe display name and private local avatar attachment;
- metadata state, last synchronization time and next synchronization time;
- aggregate participant/admin counts and the connected account's own group role;
- technical participant entries and their observed aliases.

The synchronized roster is deliberately separate from person resolution. Pulling a
snapshot must not create `mail.guest`, `contact.center.identity`, `res.partner` or
`mail.channel.member` records. Only authors actually observed in inbound messages keep
using the Phase 5.1 identity/guest path. This prevents a large WhatsApp group from
turning every roster entry into an Odoo person or conversation member.

A participant is reconciled by a provider-neutral participant reference plus all
observed addresses. WhatsApp PN and LID identifiers remain separate aliases with their
namespace, normalized value, source field, role and confidence. Neither identifier is
assumed to be a permanent universal person key. A later snapshot may connect PN and LID
to the same technical participant without rewriting historical message authors.

## Complete and partial snapshots

The adapter marks whether the returned roster is complete. Reconciliation follows two
different rules:

- a **complete** snapshot may mark previously active, absent roster entries as removed
  and publishes authoritative participant/admin counts;
- a **partial** snapshot only upserts entries that were observed. It must not infer
  removals or expose old aggregate counts as current.

Participant order does not affect the snapshot hash. Replaying the same logical snapshot
is idempotent. Provider revisions such as `ParticipantVersionID` are opaque: equality is
useful, but numeric or lexical ordering is not assumed.

## Scheduling and provider fences

The metadata pull runs only in OCA `queue_job`; webhook and UI transactions never call
WuzAPI. A complete snapshot is refreshed after a six-hour TTL. A partial snapshot stays
stale and is retried after 15 minutes. The periodic cron only schedules missing, due or
lost jobs and does no provider I/O itself.

Before and after provider I/O, the job verifies the account, connection, channel,
binding, conversation reference, health freshness and connection configuration revision.
A response started for an archived, switched, unhealthy or reconfigured scope cannot
project into the group profile. Queue identity and monotonic sync revisions prevent an
older job from overwriting a newer request.

## Avatar boundary

`POST /user/avatar` returns a provider/CDN locator, not the image that the Contact
Center may expose. The asynchronous adapter downloads the preview without forwarding the
WuzAPI token to the CDN, rejects redirects and non-public destinations, and accepts at
most **2 MiB** of sniffed JPEG, PNG or WebP content. The remote URL is never persisted
or returned to the browser.

The validated bytes become a private `ir.attachment`. The UiDTO exposes only the local
authenticated avatar route. An absent picture removes the previous attachment; a
temporarily unavailable picture does not erase a known good avatar.

## UI boundary

Operational users receive only aggregate group metadata:

- display name and authenticated local avatar URL;
- `participant_count` and `admin_count` when the roster is complete;
- own role, metadata state and last synchronization time.

The participant roster, PN/LID aliases, provider revision, raw group JID, provider URL,
token and webhook payload are not part of UiDTO or bus events. The Phase 5.2 UI remains
inbound/read-only: metadata does not enable composer, reply, reaction, edit, delete,
receipt or group-management commands.

## Laboratory evidence

On 2026-08-24 Phase 5.2 was deployed on SERVIDOR05 with OCA `queue_job` `16.0.3.0.2`,
base `16.0.1.11.0`, WuzAPI `16.0.1.8.0`, and final UI build `16.0.1.9.0`. Base tests
passed 147/147 and integrated tests passed 222/222. QUnit passed 47/47 UI tests with
416/416 assertions; the base viewer passed 4/4 with 15/15 assertions in minified and
`debug=assets` bundles. The final UI handoff adds message grouping, an internal
image/video viewer, an audio player, and touch/accessibility actions.

All five WuzAPI instances subscribed to `GroupInfo`, `JoinedGroup`, and `Picture`. The
backfill converged with 51/51 profiles ready, 49 avatars, 19,365 technical participants,
and 38,697 aliases. `mail.guest`, `res.partner`, and `mail.channel.member` counts stayed
unchanged throughout convergence, confirming the roster boundary. The guarded
administrative `action_retry_metadata_sync` recovered all 15 initially failed pulls.

A later read-only snapshot after UI `16.0.1.8.3`, with provider data still arriving,
showed 53/53 profiles ready, 51 avatars, 19,860 technical participants, 39,686 aliases,
and five of five active provider connections connected. This live snapshot is separate
from the controlled backfill invariant above.

The authenticated browser loaded 117 conversations with five of five connections
healthy, rendered 32 avatars and showed group name/avatar/aggregate metadata without a
console error or warning. The local avatar route returned `200 image/jpeg`, `nosniff`,
and private cache headers; a cold anonymous request was redirected to authentication.
Production was not accessed or changed.

The handoff smoke kept a real eight-message group read-only and grouped it as one start,
seven continuations, and one run end. Eighteen audio players reached `readyState=4`
without error and changed from 1x to 1.5x. A real 1200x1600 image exposed a single-item
viewer grid defect that compressed it to approximately 13 pixels; the template/SCSS
correction in UI `16.0.1.8.3` was revalidated at 464x618 inside a 1138-pixel desktop
stage with no navigation controls. At 390x844 mobile, the stage measured 378 pixels and
the image 366x488 without overflow. Escape restored focus and the browser console
remained at zero errors and zero warnings.

The final UI increment reports global responsibility totals from the server before
pagination, groups conversations by inbox, and reconciles selection after assignment or
realtime changes. A deferred queue assigns image sources only near the viewport and caps
the tab at two concurrent downloads. Its authenticated smoke reported 133 open
conversations, totals of two for `mine` and 131 for `unassigned`, and a measured maximum
of two concurrent media requests before and after incremental scrolling. The internal
viewer opened normally, the health panel settled at five of five connected, and the
console had zero errors.

Evidence is recorded in
[`reviews/2026-08-24-phase5-2-group-metadata-validation.md`](../reviews/2026-08-24-phase5-2-group-metadata-validation.md).
The deployed runtime code tree hash is
`39721ec9cdbb368999f0587b5a20da53f2a7f988f2f70040fb0b7b0e3cd23481`. Final deploy,
base/UI upgrade, and validation evidence is under
`scans/raw/20260824-odoo16-contact-center-phase5-2-group-metadata/`; business data was
unchanged and the disposable laboratory upgrade ran without a database backup. The
deployment retained only its automatic recoverable copy of the previous source tree.

## Revalidation gate

Before changing WuzAPI version, commit or group-event subscriptions:

1. compare the effective routes and event constants with the pinned contract;
2. refresh anonymized fixtures for `GroupInfo`, `JoinedGroup`, `Picture`, a complete
   PN/LID roster, a partial roster and an absent avatar;
3. prove that hints trigger a pull but cannot directly mutate the canonical snapshot;
4. replay reordered and repeated snapshots and verify complete-versus-partial removal;
5. verify the six-hour TTL, 15-minute partial retry and recovery of a lost queue job;
6. exercise avatar absence, MIME mismatch, redirect/private destination and the 2 MiB
   limit;
7. confirm that roster synchronization creates no guest, identity, partner or channel
   membership and that the UI exposes only the aggregate contract.
