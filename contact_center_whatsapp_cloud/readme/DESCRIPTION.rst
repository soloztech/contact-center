This addon connects ``contact_center_base`` to the WhatsApp Business Platform
(Cloud API).

The shared Meta core owns the App, credential references, callback, the WhatsApp
Business Account owner and asset, and the immutable webhook delivery ledger. This
addon owns only the Contact Center adapter: atomic claims of messages, statuses and
errors, routing by business account and phone number ID, normalized DTOs, outbound
messaging, media and health.

A provider connection binds to one ``meta.webhook.asset`` of a WhatsApp Business
Account owner and one phone number ID (unique among active connections). The inbox
platform must be ``whatsapp``. The system-user token is resolved in memory from the
owner's reference; nothing secret is stored.

Inbound text, media (image, audio/voice, video, document, sticker), location,
contacts, button and list replies, reactions, replies, system notices and
unsupported types enter the canonical inbox pipeline. Each atomic item becomes one
inbox event only at dispatch, after the shared subscription policy; items of an
ignored conversation are never claimed, on any number that can claim (primary,
standby or migration connection). Only phone numbers with a connection on the
receiving endpoint are claimed: another number of the same business account (used by
another tool or not configured here) stays a content-free placeholder. Each claim
records the inbox whose number received it: it is projected only into that inbox and
erased only by that inbox's conversation deletion, also after the number moves to
another inbox. A message keeps the inbox of its first claim: a copy Meta repeats after
the number moved stays unclaimed. A phone number converges on the same
identity as WuzAPI (``whatsapp.pn``, company scope); a BSUID is qualified by the
business account and stays inside the inbox. Redeliveries are deduplicated across
every connection of the same inbox, business account and phone number. Once a
conversation is deleted, its erased occurrences (dispatched or not) are never claimed
or projected again, whatever delivery body repeats them. Deletion erases this addon's
items even after their subscription was disabled or archived with the last route; if
another webhook consumer shares an item (including one whose subscription is archived
while the item can still be fanned out), the deletion is refused instead of leaving
content behind. It also drops the private thumbnail URLs of the conversation's ads,
before dispatch or before processing as well.

Click-to-WhatsApp ads create a paid touchpoint only with a numeric ad source ID or a
click ID. The public permalink is kept in a private, short-lived link store until
dispatch writes it into the private inbox envelope; the ad thumbnail uses the core
CDN locator. Both survive a replacement of the receiving connection by another
connection of the same inbox, business account and number before dispatch.

Sends, replies and reactions require a customer message in the last 24 hours on the
same inbox, business account and phone number (including replaced connections of
that route). Every ambiguous send result is uncertain and never repeated
automatically. Read receipts follow their own 30-day contract. Statuses of a
reaction sent through the phone number are acknowledged without an inbox event.
A status is applied only when its recipient belongs to the message's conversation,
and it still reaches a message sent before its connection was replaced by another
connection of the same inbox, business account and number. Reactions reach such a
message too, and only its own contact can react to it.
``failed`` statuses and business account ``errors[]`` are recorded in an
administrator-only diagnostic list; a ``failed`` status still records the provider
message ID and completes a send left uncertain as sent, and Meta repeating it after a
connection replacement records it only once. A controlled primary switch
keeps the consumer subscription; the subscription is archived only when the last live
route of its business account owner retires. Lifecycle decisions for one business
account are serialized with every change that can make a route live (creation,
promotion, inbox unarchive, consumer registration), so concurrent changes always
decide on the final topology. That archive never waits for a webhook being ingested
or dispatched: the route change is retried instead.
