Provides the provider-neutral foundation for external customer conversations.

The addon keeps ``mail.channel`` and ``mail.message`` canonical and adds versioned
DTOs, an adapter registry, persistent guest identities and aliases, transport
bindings, durable inbox/outbox ledgers and a stable local API for a dedicated frontend.
The ledgers preserve domain state and audit evidence; OCA ``queue_job`` provides the
actual asynchronous executor, concurrency control and retry schedule.

Canonical conversations use only ``open``, ``resolved`` and ``archived`` operational
states. A genuinely new inbound message accepted after provider deduplication always
reopens a resolved conversation, while an archived conversation deliberately remains
archived. Webhook replays, receipts, mutations and self-side echoes do not change its
lifecycle, and the existing responsible agent is preserved. A resolved conversation
always has a responsible agent: resolving an unassigned conversation claims it for the
actor in the same transaction, and an assignment cannot leave it without one.

Every change of responsible or service state is also appended to the immutable
``contact.center.conversation.event`` ledger (created, assigned, unassigned, resolved,
reopened, archived, unarchived; transfers are assignments with a previous
responsible), with the actor, the source and a processing time that never goes
backwards. ``mail.channel`` create/write is the single recording point: one event per
changed dimension, none for a write that changes nothing. Each event advances the
conversation's ``contact_center_lifecycle_seq`` in the same channel write, and every
message binding copies that counter when it is created, so messages and transitions
of a conversation are ordered exactly as their transactions committed. Supervisors
read the ledger of their conversations; administrators read it for their companies.
Upgrading records one ``baseline`` event per existing conversation (its present state
and responsible, not a reconstruction of the past); history starts there. The first
baseline is the publication of the history; an installation that never had
conversations before the ledger has none, and its history is complete. A report period
is partial only when a conversation in its scope received its baseline after the
period start, so an inbox created after the publication is complete.

The ledger's only foreign key is the conversation: the inbox and company are derived
from the channel and its unique binding, and users are kept as plain ids. A
transition therefore never locks an inbox, company or user row after the channel,
the reverse of the order used by inbound processing and access changes. A deleted
user keeps its id in the ledger, the episodes and the report groups; every name is
resolved only for existing users and a deleted one reads as "Removed user (#id)". The
episode list and pivot group "Responsible at Start" by that historic key, so each
deleted user, "No responsible" and "Unknown responsible" stay separate groups; its
choices, like the report's responsible filter, come only from the ledger the viewer
may read, so no one learns who handled a conversation outside their scope. The
ledger's derived inbox, channel, type and user fields search like stored fields:
negative operators are exact complements, like operators keep their case and
wildcards, and unsupported operators are refused.

The read-only ``contact.center.attendance.episode`` projection turns that ledger into
waiting episodes of direct conversations: an episode starts with a customer message
and ends with the first human response (an agent message with positive delivery
evidence, or a reply written on the phone); automation never answers, and the end of
a cycle closes a pending episode as *closed without response*. The responsible at the
start is the one in force at the customer's provider time: the event the message's
own processing recorded (its automatic assignment, or the creation of the
conversation), else the last assignment not later than that time, else unknown. A
conversation created by a message written on the phone keeps that creation as the
evidence of its first message. A phone reply whose webhook arrives late is placed by
provider time; a cycle created by a customer or phone message, or reopened by inbound
processing, starts, for that purpose, at the provider time of the message that opened
it, so a customer webhook processed after the phone reply that created the
conversation is still answered by it. Waiting times are elapsed time: no business
calendar is configured.

Pinning and muting are sparse preferences scoped to one user and conversation. Pinning
changes only that user's list order. Muting suppresses only that user's browser
attention signal; the message, unread state and realtime invalidation are still
persisted and delivered normally.

Remote participants remain ``mail.guest`` records until an agent explicitly links
their identity to an existing or newly created contact.

Identity linking is person-first.  The normal flow accepts a personal
``res.partner`` and may then relate that person to a principal company through Odoo's
native ``parent_id``.  Linking the identity directly to a company is a separate,
supervisor-only action for a genuinely centralized number shared by several people.
The protected ``identity.partner_link_kind`` records that operator decision instead
of inferring it later from the mutable ``res.partner.is_company`` field.  A drifted
partner is therefore flagged without weakening the original authorization boundary.
Both flows preserve the original guest, channel memberships, aliases and historical
message authorship.  Future business integrations should derive the commercial entity
from ``partner_id.commercial_partner_id`` instead of replacing the personal identity.

Odoo's native contact merge remains available when every linked identity is compatible
with the chosen survivor. The merge contract is checked before and again under the
canonical partner/identity locks; identities are rebound to the survivor and Contact
Center memberships are reconciled from their inbox authority. A person/company,
cross-company or internal-user conflict rejects the complete merge atomically. Direct
deletion of an identity-linked contact remains protected.

Inbound group conversations may also have one provider-neutral group profile. It keeps
the safe display name, private avatar, synchronization state and a technical
participant roster with all observed PN/LID aliases. Roster synchronization never
creates ``mail.guest``, ``contact.center.identity``, ``res.partner`` or
``mail.channel.member`` records; only authors actually observed in messages enter the
normal guest/identity flow. Complete snapshots may reconcile removals, while partial
snapshots only upsert observations and remain explicitly stale.

The operational API exposes only aggregate group metadata and a local authenticated
avatar URL. Technical participants, aliases, raw group identifiers and provider
revisions remain outside UiDTO and bus payloads. Group outbound and mutation
capabilities stay disabled unless both the inbox policy and provider adapter opt in.

The generic *Provider Connections* form exposes a stable extension notebook. Provider
addons register their selector entry and add conditional settings there without adding
provider fields to the core.

CRM and attribution boundary
============================

This module depends on native ``mail``, ``rating``, ``web`` and OCA ``queue_job``;
it does not depend on CRM, Sales, Accounting or Marketing Center. Attribution
records retain provider observations, their evidence level and external identifiers.
They do not qualify a customer, create a ``crm.lead`` or choose a campaign.

A conversation, a linked contact, a commercial association and an attribution
observation are separate records with separate lifecycles. Install
``contact_center_crm`` to browse a customer's native business records and
``contact_center_kanban`` only when service cases and explicit CRM actions are needed.
Optional cross-project attribution bridges are distributed by
`Marketing Center <https://github.com/soloztech/marketing-center/tree/16.0>`_.
