# Contact Center

[![Pre-commit Status](https://github.com/soloztech/contact-center/actions/workflows/pre-commit.yml/badge.svg?branch=16.0)](https://github.com/soloztech/contact-center/actions/workflows/pre-commit.yml?query=branch%3A16.0)
[![Build Status](https://github.com/soloztech/contact-center/actions/workflows/test.yml/badge.svg?branch=16.0)](https://github.com/soloztech/contact-center/actions/workflows/test.yml?query=branch%3A16.0)

Provider-neutral contact center for Odoo 16, with a standalone Owl inbox, WhatsApp
through WuzAPI, and Messenger and Page-linked Instagram through Meta.

Addon versions advance with their changes: Base and UI **`16.0.1.6.0`**, WuzAPI and Meta
**`16.0.1.3.0`**, CRM **`16.0.1.2.1`**, and Kanban **`16.0.1.2.0`**. Clean installations
need no data handoff. Upgrades from the `16.0.1.0.0` production baseline use the
cumulative [inbox-access migration](operations/account-access-upgrade.md), which
preserves existing users, teams and conversation assignments. Older development
snapshots still require their explicit laboratory procedures before this upgrade.

## Documentation

This README is the project entry point. Topic guides have descriptive names:

| Guide                                                          | Purpose                                                             |
| -------------------------------------------------------------- | ------------------------------------------------------------------- |
| [Architecture — Português](docs/architecture.md)               | Current module boundaries, identity, access and transport contracts |
| [CRM and attribution — Português](docs/crm-and-attribution.md) | Operator flow, commercial actions, evidence and diagnosis           |
| [Current roadmap](docs/roadmap.md)                             | Open work, research and environment-specific validation             |
| [Operational procedures](operations/)                          | Installation, upgrades, conversation actions, audio and recovery    |
| [Technical research](research/)                                | Protocol and framework evidence; preserve the context of each study |
| [History](docs/history/index.md)                               | Dated plans, reviews and validation evidence                        |

Historical test counts, deployment hashes and phase milestones do not establish
readiness for a new deployment. Current contracts live in the guides and addon READMEs.

## Addons

| Addon                   | Responsibility                                                                                             |
| ----------------------- | ---------------------------------------------------------------------------------------------------------- |
| `contact_center_base`   | Accounts, identities, conversations, provider contracts, inbox/outbox, media, attribution and productivity |
| `contact_center_ui`     | Standalone Owl inbox using the versioned local UiDTO API                                                   |
| `contact_center_wuzapi` | WhatsApp transport, guided setup, health, group metadata and media                                         |
| `contact_center_meta`   | Messenger and Page-linked Instagram messaging through shared Meta foundations                              |
| `contact_center_kanban` | Optional service cases, pipelines and CRM stage synchronization                                            |
| `contact_center_crm`    | Customer opportunities, quotations, orders and invoices in the chat; usable without Kanban                 |

Base has no CRM or Kanban dependency. CRM depends on the UI and native CRM; Kanban
extends CRM. Meta's technical foundations, `meta_api_base` and `meta_webhook_base`, are
distributed in
[Marketing Center](https://github.com/soloztech/marketing-center/tree/16.0),
independently of its functional marketing addons.

## Conversations, CRM actions and attribution

Contact Center owns the conversation and its operational lifecycle. Native CRM owns
leads and opportunities. Marketing Center owns its acquisition evidence, resolution and
optional attribution policies. The
[operator guide in Portuguese](docs/crm-and-attribution.md) explains configuration,
examples and diagnosis; the
[Marketing Center intake and attribution contract](https://github.com/soloztech/marketing-center/blob/16.0/docs/crm-intake-and-attribution.md)
describes the cross-project boundary.

| Operation                                                        | Owner                                         | Effect                                                                        |
| ---------------------------------------------------------------- | --------------------------------------------- | ----------------------------------------------------------------------------- |
| Receive or answer a message                                      | Base and provider adapter                     | Conversation/message and durable transport evidence                           |
| Link a contact                                                   | Base and UI                                   | Explicit identity-to-partner relationship                                     |
| Browse customer documents                                        | CRM customer panel                            | Reads the customer's permitted records; does not associate the conversation   |
| Explicitly link a CRM record                                     | CRM association API; optional Kanban actions  | Maintains the conversation-to-CRM association                                 |
| Create a lead/opportunity from an Atendimento                    | Optional Kanban                               | Explicit creation with configured company, team and stage; records both links |
| Capture an ad referral or `fbads` hint                           | Base and provider adapter                     | Attribution evidence, with its source and confidence level                    |
| Resolve Meta catalog evidence and associate touchpoints with CRM | Separately installed Marketing Center bridges | Keeps acquisition evidence and CRM links under their own contracts            |

A default service case is not a CRM lead. A customer panel row is not proof of a
conversation association. An origin card, click identifier or `fbads` hint does not
invoke CRM creation. These distinctions also apply when the same customer has several
conversations or campaigns.

Meta catalog resolution exists in Marketing Center. Automatic mapping from external
campaigns to native CRM UTM fields and a GCLID-to-campaign lookup are not implemented
product workflows; a resolved touchpoint must not be presented as either capability.

`contact_center_meta` serves Messenger and Page-linked Instagram messaging.
`marketing_center_meta` serves marketing catalog/reporting and native Meta lead forms;
`marketing_center_meta_crm` adds configurable CRM intake for those submissions. Shared
Meta foundations and a shared callback do not make these workflows interchangeable.

## Accounts, identities and access

An account is one logical inbox. It owns conversations, user/team scope, assignments and
operational policy. A provider connection is one transport attached to that account.
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

Agent and Supervisor roles grant operations. Each inbox can authorize several users and
several teams, cumulatively: a user can attend through a direct grant or through any
selected team's roster. Direct grants and teams can be combined. Roster changes
reconcile native channel memberships and remove historical access when the user has no
remaining grant. A conversation's responsible agent and optional automatic assignee
remain singular. Administrators have privileged, company-scoped configuration and
technical-ledger access.

## Operator capabilities

- Inbox, state, responsibility and search filters; paged history, unread pointers,
  realtime updates, compact desktop density and responsive mobile layout.
- Text and media composition, provider-supported replies and mutations, dispatch and
  delivery status, tags, assignments, identity naming and explicit contact linking.
- Scoped quick replies, immutable internal notes, conversation follow-ups and scheduled
  provider messages. Personal quick replies are filtered by their owner in both the
  Contact Center and the native Discuss bootstrap. Agents retain native management
  rights on ordinary shortcodes that are not bound to Contact Center scopes. The
  customer panel shows opportunities, quotations, orders and invoices by contact.
  Historical CRM associations remain independent. Follow-ups belong to conversations,
  work without Kanban, and open the inbox from the activity menu. Optional Kanban adds
  service cases and CRM stage synchronization.
- Conversation states `open`, `resolved` and `archived`. New inbound messages reopen
  resolved conversations; archived conversations stay archived until explicitly
  restored.
- Start a conversation by phone in an authorized, connected inbox; validate the number
  with the provider and reuse the existing conversation without sending a message.
  Resolution reasons and justifications create immutable internal notes.
- Agents may create/link contacts and companies inside conversations they can access,
  including central service numbers. This does not grant general Contacts administration
  or access to another inbox.
- A contact keeps its native primary company and may have secondary company
  relationships. Quotations continue to default to the primary commercial partner.
  Relationship removal and correcting the linked person are separate operations;
  [the relationship contract](research/partner-company-relationships.md) describes their
  effects. Administrators configure secondary links in **Configuration → Contact
  relationships**.
- Sticky day markers, bounded message menus, compact voice messages and a floating video
  player with optional browser Picture-in-Picture. Native Odoo link-preview records
  appear in the timeline; extraction runs in the existing background queue using bounded
  public HTTP requests, never during webhook ingress.
- Configurable audio transcription within Base and UI, disabled by default per inbox,
  with independent modes for direct and group conversations. Manual or automatic
  processing uses an independent speech-adapter registry, with OpenAI and compatible
  HTTP services. Transcript text appears below the audio and follows conversation access
  and content deletion/retention. See
  [configuration and provider extensions](operations/audio-transcription.md).
- Ad origin cards show available title, copy, public link and a private thumbnail from
  WuzAPI or Meta referrals. Optional Marketing enrichment fills missing details only
  after an authorized catalog match. Cards follow attribution visibility, conversation
  access and content expiry. See
  [ad preview configuration and lifecycle](operations/ad-origin-preview.md).
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
| Inbound    | Text, supported media, typed cards and selections                           | Direct text, replies, echoes and supported media                                             |
| Outbound   | Text, media, buttons, lists, contacts and static locations                  | Text/replies and private media, one file/no caption, 24h; outbound cards unavailable         |
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
labels. Optional Kanban CRM mappings require explicit administration, and managed role
grants retain provenance so removal does not revoke pre-existing manual permissions.

## Installation and operation

1. Use Odoo 16/OCB and the OCA `queue` repository on branch `16.0`. Pin dependency
   commits with the selected release, and make each repository root available on
   `addons_path`.
2. Install the Python requirements declared by the selected addons and their
   prerequisites. For this repository, use `python -m pip install -r requirements.txt`
   in the Odoo environment. Before upgrading Base, verify `import phonenumbers` with the
   exact Python executable used by each Odoo service; installing it into another shell
   or virtual environment does not satisfy the module dependency.
3. Install `queue_job`, `contact_center_base`, a provider addon and `contact_center_ui`.
   Add `contact_center_crm` for the customer panel; Sales and Accounting tabs use those
   native modules when installed and permitted. Add `contact_center_kanban` when service
   pipelines and mapped CRM stages are needed. Meta also requires the matching
   `meta_api_base` and `meta_webhook_base` sources.
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
covers mounted components, history/reading, menus, media, relationships and the
technical JSON viewer. Run the Contact Center QUnit suites in both normal and
`debug=assets` modes against the exact candidate before deployment; the Python CI result
alone is not evidence that those browser suites passed. The
[CI workflow](.github/workflows/test.yml) documents the OCB/PostgreSQL test environment
and private cross-repository dependency checkout. Use disposable test databases and
synthetic provider responses; provider integration checks require their own evidence.

See the [roadmap](docs/roadmap.md) for open work and the
[history index](docs/history/index.md) for version-specific reviews and validation.
Follow [AGENTS.md](AGENTS.md) before an official release from `16.0`.

### Maintaining addon documentation

Addon `README.rst` files are generated from each addon's `readme/*.rst` fragments. Edit
the fragments, then run the configured `oca-gen-addon-readme` hook. To regenerate only
the README targets without changing the app-store HTML descriptions:

```sh
oca-gen-addon-readme --addons-dir=. --branch=16.0 --org-name=soloztech \
  --repo-name=contact-center --keep-source-digest --no-gen-html --no-commit
```

The repository README and [Portuguese guide](docs/crm-and-attribution.md) are maintained
directly. Keep module responsibilities and dependency names aligned with manifests and
code; release and production status require their own deployment evidence.

## License

[AGPL-3.0](LICENSE). Each addon's `__manifest__.py` declares its license.
