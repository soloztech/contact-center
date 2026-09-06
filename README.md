# Contact Center

[![Pre-commit Status](https://github.com/soloztech/contact-center/actions/workflows/pre-commit.yml/badge.svg?branch=16.0)](https://github.com/soloztech/contact-center/actions/workflows/pre-commit.yml?query=branch%3A16.0)
[![Build Status](https://github.com/soloztech/contact-center/actions/workflows/test.yml/badge.svg?branch=16.0)](https://github.com/soloztech/contact-center/actions/workflows/test.yml?query=branch%3A16.0)

Provider-neutral contact center for Odoo 16, with a standalone Owl inbox, WhatsApp
through WuzAPI, and Messenger and Page-linked Instagram through Meta.

This is the **1.0 greenfield pre-release baseline**. All six addons use version
`16.0.1.0.0`. The supported starting point is a clean installation; older development
snapshots require their own explicit laboratory procedure. Historical migrations are not
part of this baseline. Persistent schema or data changes after the first production
release must include normal cumulative migrations.

The [current audit](reviews/2026-09-05-greenfield-audit.md) records changes,
verification and remaining limits. Historical test counts, deployment hashes and phase
milestones remain in [reviews/](reviews/); they do not establish readiness for a new
deployment.

## Addons

| Addon                   | Responsibility                                                                                             |
| ----------------------- | ---------------------------------------------------------------------------------------------------------- |
| `contact_center_base`   | Accounts, identities, conversations, provider contracts, inbox/outbox, media, attribution and productivity |
| `contact_center_ui`     | Standalone Owl inbox using the versioned local UiDTO API                                                   |
| `contact_center_wuzapi` | WhatsApp transport, guided setup, health, group metadata and media                                         |
| `contact_center_meta`   | Messenger and Page-linked Instagram messaging through shared Meta foundations                              |
| `contact_center_kanban` | Optional service cases, pipelines, stages, transition history and case follow-ups                          |
| `contact_center_crm`    | Optional explicit synchronization of service cases, pipelines and teams with native CRM                    |

Base has no CRM or Kanban dependency. The CRM bridge requires Kanban. Meta's technical
foundations, `meta_api_base` and `meta_webhook_base`, are distributed in
[Marketing Center](https://github.com/soloztech/marketing-center), independently of its
functional marketing addons.

## Accounts, identities and access

An account is one logical inbox. It owns conversations, owner/team scope, assignments
and operational policy. A provider connection is one transport attached to that account.
Connections have `primary`, `standby`, `migration` or `historical` roles. Only the
active primary admits traffic; sending also requires outbound enablement and verified
health. Standby and migration connections support monitoring and controlled cutover.

A primary switch drains active outbound work and changes ingress/egress ownership
atomically. Existing provider message references retain their original connection.
Replies and mutations against a former provider cannot be sent through the replacement
with an unrelated external message ID.

`mail.channel` and `mail.message` are canonical. Remote people have durable `mail.guest`
identities, with optional explicit `res.partner` links. A trusted WhatsApp phone-number
alias can identify the same person across inboxes within one company. LID, opaque JID,
PSID and IGSID remain account-scoped. Each inbox retains its own channel and state;
companies remain isolated. Manual names and contact links take precedence over provider
profile updates.

Agent and Supervisor roles grant operations; account ownership and explicit team rosters
determine scope. Shared inboxes use a team, while an owner can operate an exclusive
inbox. Roster changes reconcile native channel memberships and remove historical access
for users who lose their scope. Administrators have privileged, company-scoped
configuration and technical-ledger access.

## Operator capabilities

- Inbox, state, responsibility and search filters; paged history, unread pointers,
  realtime updates, compact desktop density and responsive mobile layout.
- Text and media composition, provider-supported replies and mutations, dispatch and
  delivery status, tags, assignments, identity naming and explicit contact linking.
- Scoped quick replies, immutable internal notes and scheduled provider messages.
  Optional Kanban adds service cases and case follow-ups; CRM binds those explicitly.
- Conversation states `open`, `resolved` and `archived`. New inbound messages reopen
  resolved conversations; archived conversations stay archived until explicitly
  restored.
- Per-inbox deleted-message display: a tombstone by default, or an attenuated retained
  snapshot when configured. Tombstone mode removes operational body, reactions and
  media.
- Private profile/group avatars and aggregate group metadata. Roster synchronization
  creates technical participant records without creating contacts or memberships in
  bulk.

The UI uses Contact Center APIs and bus events. It does not patch private Discuss or
Live Chat JavaScript. Conversation ordering follows local arrival; provider timestamps
remain display metadata. Reconnect recovery preserves reading position and uses bounded
forward pagination, with older history available through backward pagination.

## Provider capabilities and limits

| Capability | WuzAPI / WhatsApp                                                           | Meta / Messenger and Page-linked Instagram                                                   |
| ---------- | --------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Inbound    | Text, supported media and structured-message summaries                      | Direct text, replies, echoes and supported media                                             |
| Outbound   | Text, image, audio, video and document                                      | Direct text and replies within the standard 24-hour response window                          |
| Groups     | Metadata, participant receipts and opt-in sending/mutations                 | Not supported                                                                                |
| Mutations  | Replies, reactions, edits and deletes subject to ownership and capabilities | Inbound state/mutation events by platform; outbound reactions, edits and deletes unavailable |
| Profiles   | Bounded name/avatar fetch and group metadata synchronization                | Bounded profile/avatar fetch and token/scope/subscription health                             |

WuzAPI is pinned to `v1.0.8`, commit `9487eca`. Keep it configured with
`--skipmedia=true`: inbound files are downloaded asynchronously and stored as private
Odoo attachments. Its native S3 delivery is not the attachment store. The adapter limits
image/audio to 16 MiB and video/document to 50 MiB, with one attachment per outbound
message and no audio caption. Outbound encoding is bounded but is not true streaming.

Each WhatsApp inbox separately enables inbound groups, outbound groups and agent-name
signatures. Group replies and mutations require current own/target participant evidence;
ambiguous or stale PN/LID mappings block dispatch. Group read/delivery receipts are
published as aggregate counts. Browser recording negotiates supported formats: OGG/Opus
is a voice note, while audio-only MP4 is ordinary audio. The server validates the actual
container, duration, size and hash.

Meta uses company-scoped Apps, endpoints, Page authorizations and distinct messaging
assets. Credentials are resolved through protected external references. Both Messenger
and Page-linked Instagram sends use the authorizing Page's route and token. Sending
requires authenticated inbound-user evidence within 24 hours; the window is checked
again immediately before dispatch. Text limits are 2,000 characters for Messenger and
1,000 UTF-8 bytes for Instagram. Meta inbound media is limited to 8 MiB images, 16 MiB
audio and 25 MiB video/documents; documents are PDF-only. Signed CDN URLs stay in a
short-lived private vault, while shared ledgers retain opaque references.

Meta permissions, App Review, credentials and observed subscriptions must be valid for
the configured assets. An unavailable or contradictory route remains blocked. Campaign
broadcasts, automated journeys, history imports and group-participant management are
outside this baseline. Provider contracts and research are in [research/](research/).

## Persistence, retries and recovery

Authenticated webhooks produce durable sanitized evidence before asynchronous
projection. Duplicate deliveries and atomic events converge through scoped dedupe keys.
Meta uses one shared callback and dispatcher, with no default-inbox fallback for unknown
or ambiguous assets. WuzAPI verifies HMAC on bounded request bytes before admission.

The composer creates the message, bindings, media and outbox atomically using a UUID
request key. It never calls a provider. Queue workers persist a dispatch boundary and
revalidate ownership, capabilities, identity, health and configuration before external
I/O. Mutation ordering is monotonic, reactions are actor-scoped and deletion is
terminal.

Rate limits share a durable connection cooldown; eligible retries honor `Retry-After`.
WuzAPI outbound starts are paced at one second per connection. Quotas shared across an
entire provider App are not coordinated by this connection-local mechanism.

An ambiguous send remains `uncertain` and is never blindly resent. Positive provider
evidence may reconcile it. Explicit resend is restricted to unequivocally failed `dead`
sends and creates a new message/outbox linked to the terminal source. Automatic recovery
resumes only eligible work that has not crossed the dispatch boundary. Technical views
provide supervised recovery for failed media and other supported retry paths.

Health checks run through `queue_job`; scheduler and refresh actions perform no provider
I/O. Stale health and identity mismatches block outbound. A connection lifecycle event
alone cannot reopen sending: fresh verified identity evidence must clear the safety
latch. Guided WuzAPI setup makes session connection/QR operations explicit. Credential
or expected-identity changes require outbound to be disabled and in-flight work drained.

Media is served through authenticated local routes with access checks, private caching
and content protections. Operational projections omit credentials, private provider
locators and raw payloads. Provider errors are sanitized before entering logs or
ledgers. Attribution is append-only evidence; its optional UI projection exposes safe
labels. CRM mappings require explicit administration, and bridge-managed role grants
retain provenance so removal does not revoke pre-existing manual permissions.

## Installation and operation

1. Use Odoo 16/OCB and the OCA `queue` repository on branch `16.0`. Pin dependency
   commits with the selected release, and make each repository root available on
   `addons_path`.
2. Install the Python requirements declared by the selected addons and their
   prerequisites. For this repository, use `python -m pip install -r requirements.txt`
   in the Odoo environment.
3. Install `queue_job`, `contact_center_base`, a provider addon and `contact_center_ui`.
   Add `contact_center_kanban` for service pipelines and `contact_center_crm` for CRM.
   Meta also requires the matching `meta_api_base` and `meta_webhook_base` sources.
4. Load `queue_job` server-wide and configure an active JobRunner. Keep cron and bus
   routing available. Never enable `QUEUE_JOB__NO_DELAY` outside focused tests.
5. Configure companies, account owners/teams and provider connections with outbound
   disabled. Verify credentials, identity, callback/subscriptions and health before
   activating the primary route and enabling sending. If the inbox imports messages sent
   from another device, set its **Technical Author** and verify an external-device echo
   plus its receipt. This author is optional and is not supplied by guided provider
   setup; without it, those echoes remain unsupported.

Example JobRunner configuration, to size against the intended workload:

```ini
[options]
server_wide_modules = web,queue_job

[queue_job]
channels = root:4,root.contact_center.health:2,root.contact_center.media:1
```

Provider settings extend the generic connection form. New adapters must use the shared
adapter/DTO contracts. Do not bypass the outbox or pass credentials through DTOs,
browser responses, queue arguments or request snapshots.

Use the [resilience and capacity drill](operations/resilience-capacity-drill.md) for
controlled failure/load checks and the
[cutover and rollback procedure](operations/production-cutover-rollback.md) for release
planning. Dates, approvals, dependency pins, backups and provider canaries must be
verified for the actual deployment; local tests do not establish production readiness or
external Meta approval.

## Development and verification

Run `pre-commit run --all-files` for repository checks. Odoo suites cover contracts,
permissions, state transitions, concurrency, provider failures and recovery; QUnit
covers the inbox model and technical JSON viewer. The
[CI workflow](.github/workflows/test.yml) documents the OCB/PostgreSQL test environment
and private cross-repository dependency checkout. Use disposable test databases and
synthetic provider responses; provider integration checks require their own evidence.

See the [current audit](reviews/2026-09-05-greenfield-audit.md) for this working tree
and [historical reviews](reviews/) for previous laboratory results and design decisions.

## License

[AGPL-3.0](LICENSE). Each addon's `__manifest__.py` declares its license.
