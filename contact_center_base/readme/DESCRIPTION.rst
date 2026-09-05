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
lifecycle, and the existing responsible agent is preserved.

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
