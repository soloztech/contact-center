Implemented (phase 1)
=====================

* Atomic, subscription-gated claims of WhatsApp messages, statuses and errors from
  the shared Meta ledger, with inbox events created only at dispatch.
* Text, media, location, contacts, interactive replies, reactions, replies, system
  notices and unsupported types.
* Paid click-to-WhatsApp attribution with a private permalink store.
* Text, media, reply and reaction sends inside the 24-hour window; read receipts.
* Status correlation by ``biz_opaque_callback_data``, including uncertain sends.
* Phone number health read by the existing health cron.

Next increments (phase 2)
=========================

* Message templates and sends outside the customer service window.
* ``subscribed_apps`` reconciliation, Embedded Signup and app coexistence.
* A core ``failed`` delivery state (today recorded only in the local diagnostics).
* Calls, groups, pricing, message edits and deletions.
