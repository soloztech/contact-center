=================
Contact Center UI
=================

Standalone Odoo 16 Owl client action for omnichannel Contact Center agents.

Features
========

The inbox provides:

* conversation search plus account, state, and server-side responsibility filters;
* grouped-by-inbox and flat conversation views with global totals and pagination;
* the logical inbox name as the primary route metadata on every conversation card and
  in the selected-conversation header;
* a persisted compact desktop density with collapsible filters, while mobile keeps its
  responsive full-width layout;
* localized, color-coded conversation states and compact connection-health details;
* paged conversation and message timelines with unread pointers;
* provider, platform, origin, reply, delivery, and dispatch context;
* a text and media composer with preview, removal, reply mode, and idempotent client
  request identifiers;
* consecutive-message grouping without hiding the run boundaries;
* an internal image/video/PDF viewer, audio player with playback-speed control, and
  safe direct download for other documents;
* deferred image loading with reserved layout space and a two-request concurrency cap;
* keyboard-, touch-, and screen-reader-accessible reply, reaction, edit, delete, and
  media actions, including focus restoration when the viewer closes;
* only ``open`` and ``resolved`` operational states, with one contextual *Resolve* or
  *Reopen* action;
* a read-only inbox team plus responsible-user, claim, and tag actions;
* guest/contact details and all observed external aliases;
* person-first contact search, creation, linking, and unlinking, followed by safe
  search or creation of the person's principal company;
* an explicit supervisor-only alternative for a centralized company number, visually
  distinct from the normal personal-contact flow;
* direct opening of the linked person or company in the native Contacts form;
* inbound group badges, read-only state, safe group names, private avatars, aggregate
  participant/admin counts, own role, and metadata freshness;
* Contact Center-specific bus synchronization;
* realtime refreshes that preserve the complete conversation tail already loaded by
  the operator, including lists larger than the 200-row refresh batch;
* out-of-focus attention without customer names or message bodies in browser
  notifications;
* one text draft per conversation in ``sessionStorage``, with stale asynchronous
  responses discarded when the operator changes conversation;
* scoped native quick replies, immutable internal notes, native follow-up activities,
  and provider-neutral scheduled messages;
* readable desktop typography with bottom-anchored short conversations; and
* responsive desktop and mobile navigation.

Architecture boundary
=====================

::

    Provider adapter -> DTO -> Contact Center application API -> Contact Center UI

The browser calls only the versioned local API and authenticated media routes from
``contact_center_base``. It never calls WuzAPI, WAHA, Evolution, Meta, Telegram, or
another provider directly. Sending creates the canonical ``mail.message``, message
binding, consumed media, and durable outbox in one transaction; OCA ``queue_job``
dispatches the command after commit.

The addon does not import or patch private Discuss or Live Chat JavaScript. Odoo 16
``mail.channel`` and ``mail.message`` remain the canonical records behind the local
versioned API, which keeps the UI portable to later Odoo versions.

Local API v1
============

The client consumes these authenticated operations:

* ``bootstrap`` and ``list_conversations``;
* ``get_conversation`` and ``get_timeline``;
* ``mark_fetched`` and ``mark_seen``;
* ``update_conversation`` and ``claim_conversation``;
* ``search_quick_replies`` for the selected conversation only;
* ``post_internal_note``;
* ``schedule_followup`` and ``complete_followup``;
* ``schedule_message`` and ``cancel_scheduled_message``;
* ``search_partners``, ``link_partner``, ``create_and_link_partner``, and
  ``unlink_partner``;
* ``search_partner_companies``, ``link_partner_company``, and
  ``create_and_link_partner_company``;
* ``search_central_companies``, ``link_central_company``, and
  ``create_and_link_central_company``; and
* ``send_message``, ``react_message``, ``edit_message``, and ``delete_message``.

``send_message`` accepts text and/or an opaque media reference. Files are uploaded to
the authenticated ``/contact_center/media/upload`` route, and ready attachments are
served by ``/contact_center/media/<id>/content`` only after ACL and record-rule checks;
operational users are scoped by native membership.

Company search, creation, and linking are supervisor-only.  In the normal path the
identity remains linked to a personal contact and the company uses Odoo's native
``res.partner.parent_id`` commercial relation, which also synchronizes the person's
commercial address and fiscal fields.  Reparenting and unlinking a company remain in
the Contacts application.

The direct-company path is intentionally separate and must be chosen explicitly for a
central service number that represents the company rather than one fixed person.  It
links ``identity.partner_id`` to an ``is_company`` partner without rewriting the
original guest or historical authorship.  The protected ``partner_link_kind`` keeps
that classification stable if someone later changes the partner type in Contacts;
the panel visibly flags such drift for review.  Downstream CRM, sales, invoicing and
financial projections should use ``commercial_partner_id``.  Clicking a linked name in
the profile opens that native partner record.

All responses contain ``schema_version = 1``. Unsupported versions fail closed. UI
request UUIDs make a repeated send return the original message/outbox instead of
creating a duplicate.

For ``conversation_type = group``, conversation responses include only aggregate group
metadata: safe display name, local authenticated avatar URL, complete
participant/admin counts, own role, metadata state and last synchronization time. The
browser never receives the technical roster, PN/LID aliases, raw group JID, provider
revision, remote avatar URL or provider credentials. Metadata does not change the
read-only capabilities established for inbound groups.

Installation
============

Install OCA ``queue_job``, ``contact_center_base``, at least one provider addon, and
then **Contact Center UI**. The addon depends directly on Odoo ``web`` and registers its
client action in ``web.assets_backend``.

Current limits
==============

The current MVP supports individual text, image, audio, video, and document messages,
with one attachment per outbound message, plus reply, reaction, edit, and delete when
the provider advertises the corresponding capability. Group metadata is displayed only
as safe aggregates; group sends and mutations additionally require explicit inbox and
adapter capability opt-in. Group membership management, campaign broadcasts,
automated journeys, and history import remain outside the pilot.
