Provider addons register a ``ProviderAdapter`` and exchange only ``EventDTO`` and
``CommandDTO`` payloads with this module. They must not create channels, identities or
messages directly.

Group metadata
==============

For an inbound group binding, the base addon maintains a provider-neutral group profile
and schedules its metadata reads through OCA ``queue_job``. The provider webhook may
only request a refresh; the adapter's subsequent snapshot is authoritative. A complete
snapshot expires after six hours. A partial snapshot remains ``stale``, is retried
after 15 minutes and cannot remove unseen participants or publish old aggregate counts
as current.

The synchronized roster is technical data. PN and LID values are preserved as separate
participant aliases, without creating guests, identities, partners or channel
memberships. The agent UI receives only the safe group name, authenticated local avatar
URL, complete aggregate counts, own role, metadata state and last synchronization time.
This metadata does not enable outbound group actions.

Agents use the local ``contact.center.ui.api`` service. Outbound provider calls are
performed only by OCA ``queue_job`` after the UI transaction commits. Inbox events and
outbox commands remain domain ledgers; scheduling, concurrency and retry timing belong
to ``queue_job``.

Provider configuration
======================

Administrators create every provider instance under *Contact Center > Configuration >
Provider Connections*. The ``Provider`` selector is populated by installed adapter
addons. The base form provides a named notebook extension point; each provider addon
must inject a conditional tab for its own endpoint and credentials. It must not add a
parallel provider menu or configuration model.

Connections are fail-closed by construction. An ORM create without ``role`` always
creates an active ``standby`` with ingress and egress disabled; traffic flags never
promote it implicitly. Programmatic creation of a ``primary`` must explicitly provide
``active=True``, ``role='primary'``, ``inbound_active=True`` and the intended boolean
``outbound_active``. Existing standby or migration connections become primary only
through ``action_use_as_primary()``; enabling outbound directly on either role is
rejected. The normal archive toggle remains intentional: archive maps a connection to
``historical`` and unarchive restores it as traffic-disabled ``standby``.

Queue runtime
=============

The OCA ``queue`` repository for branch ``16.0`` must be available in the Odoo
``addons_path`` before installing this module. Load ``queue_job`` server-wide and keep
an active JobRunner. Production should use Odoo workers sized together with the queue
capacity. A minimal configuration is conceptually::

    server_wide_modules = web,queue_job

    [queue_job]
    channels = root:4,root.contact_center.health:2,root.contact_center.media:1

Channel capacities are a JobRunner runtime setting, not a field on
``queue.job.channel``.  The example caps slow media downloads at one slot, admits two
health probes for a fleet of roughly twenty connections, and keeps the global root cap
at four. Choose the production capacity according to the database connection pool,
provider limits and measured workload. Do not enable ``QUEUE_JOB__NO_DELAY`` in a
running environment; direct mode is only suitable for focused tests.

Inbox and outbox records remain visible under *Contact Center > Message Flow*. Their
states and error fields are domain audit data, while the referenced OCA job owns ETA
and retry scheduling. A Contact Center administrator may use *Requeue* on an eligible
ledger whose referenced job is no longer active. The action does not duplicate an
active job. An outbox record that has crossed the durable dispatch boundary is never
blindly requeued: a resumed execution is classified as ``uncertain`` for manual
reconciliation.

Operational productivity
========================

The provider-neutral base exposes productivity features which do not require a
business workflow addon:

* internal notes are immutable ``mail.message`` comments with ``mail.mt_note`` and
  never create an outbox;
* scheduled external messages are provider-neutral, cancelable intents dispatched
  by OCA ``queue_job`` only after their due time and a fresh authorization check; the
  immutable ``outbound_request_id`` preserves the exact request that was admitted; and
* quick-reply text remains native ``mail.shortcode``, but is invisible to the agent
  until an active ``contact.center.quick.reply.binding`` explicitly exposes it at
  company, team, inbox or conversation scope. The most specific applicable binding
  wins.

There is intentionally no automatic backfill for historical ``mail.shortcode`` rows.
Administrators opt each reusable text into an audience explicitly, which keeps a new
or upgraded installation fail-closed.

Install ``contact_center_kanban`` when service cases, configurable pipelines, Kanban
stages or transition history are required. Conversation follow-ups belong to the
base addon and remain available without Kanban. The base addon has
no models, fields, hooks or database assumptions from that optional workflow.
