Implemented baseline
====================

* Strict HMAC webhook authentication and bounded envelope persistence.
* WuzAPI payload normalization into provider-neutral DTOs and pinned command transport.
* Inbound/read-only group messages plus authoritative group metadata pulls from
  ``GET /group/info?groupJID=...``.
* ``GroupInfo``, ``JoinedGroup`` and group ``Picture`` hints, complete/partial technical
  PN/LID rosters and bounded group-avatar retrieval through ``POST /user/avatar``.
* Opt-in group text/media, replies, reactions, edits and deletes with participant-aware
  addressing, capability gates and fail-closed roster validation.
* Per-participant group delivered/read receipts in a dedicated Odoo ledger; aggregate
  message delivery is not promoted and the UI receives counts only.
* Bounded reply/URL/phone buttons, lists, contact cards and static locations in direct
  and group conversations, using the pinned provider endpoints and the same queue.
* Received selections retain their stable identifiers; contact and location payloads
  become typed cards. Circular video is downloaded and rendered as ordinary video.
* Album children remain individual media messages. The pinned revision has no native
  album-send endpoint; coordination envelopes do not create empty human messages.

Next increments
===============

* Keep group participant management outside the pilot until it has a provider-neutral
  contract, explicit authorization rules and real fixtures.
* Evaluate bounded historical mutation/receipt reconciliation only when message
  correlation and the participant roster are unambiguous; no automatic replay is part
  of the current release.
* Validate production capacity and operational acceptance separately from the completed
  SERVIDOR05 technical laboratory phase.
