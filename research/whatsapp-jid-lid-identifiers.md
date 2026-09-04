# WhatsApp JID and LID Identifiers

- Status: development reference
- Reviewed: 2026-08-21

Reviewed source snapshots:

- whatsmeow
  [`fb386f15`](https://github.com/tulir/whatsmeow/tree/fb386f15283797989565965f5a55cf37e2cf9cff)
- Baileys
  [`0af23862`](https://github.com/WhiskeySockets/Baileys/tree/0af2386292907f7d9742d8d41f830d8c48208fa1)
- WuzAPI
  [`919c72c9`](https://github.com/asternic/wuzapi/tree/919c72c9750b2a1eedf0fcf9c9592f05fe46f61c)

Implementation baseline: the deployed adapter target is WuzAPI `v1.0.8`, commit
`9487eca`. The newer snapshot above remains research evidence only. Fixtures and
contract tests must run against `9487eca`; behavior found only on `main` must not be
assumed until the pinned deployment is upgraded and revalidated.

This document describes observed library/provider behavior, not a stable official
WhatsApp API contract. Revalidate it before updating any adapter dependency.

## Core decision

The Contact Center must not model a WhatsApp participant as a single phone number. It
must persist every observed identifier, its namespace, its role in the event, and the
evidence that links it to another alias.

An event with only a LID is valid and must be processed without a phone number.

## Terminology

`JID` is the general WhatsApp address structure, usually `user@server`. PN and LID are
not alternatives to “JID”; they are different JID namespaces.

| Kind            | Example                                                 | Meaning                                                     |
| --------------- | ------------------------------------------------------- | ----------------------------------------------------------- |
| PN JID          | `5511999999999@s.whatsapp.net`                          | Phone-number-backed user address                            |
| LID JID         | `123456789012345@lid`                                   | Opaque user address; its numeric part is not a phone number |
| Device JID      | `5511999999999:12@s.whatsapp.net` or `123456789:12@lid` | Address with a linked-device component                      |
| Group JID       | `120363000000000000@g.us`                               | Conversation/group, not a person                            |
| Broadcast JID   | `status@broadcast`                                      | Broadcast/status address, not a person                      |
| Newsletter JID  | `123456789@newsletter`                                  | Channel/newsletter address                                  |
| Legacy user JID | `5511999999999@c.us`                                    | Older user namespace; preserve raw and normalize carefully  |

whatsmeow declares PN, LID, group, broadcast, hosted, newsletter, and other server
namespaces in its JID type. Its `ToNonAD()` operation removes agent/device parts.

Source:
[whatsmeow JID types](https://github.com/tulir/whatsmeow/blob/fb386f15283797989565965f5a55cf37e2cf9cff/types/jid.go#L20-L82).

Baileys likewise parses the server, user, agent, and device and explicitly distinguishes
PN from LID by suffix.

Source:
[Baileys JID utilities](https://github.com/WhiskeySockets/Baileys/blob/0af2386292907f7d9742d8d41f830d8c48208fa1/src/WABinary/jid-utils.ts#L1-L127).

## Normalization rules

1. Persist the raw value exactly as received.
2. Classify by the server suffix; never classify only because the user part is numeric.
3. Resolve a person with a non-device normalized JID, but preserve the raw device JID
   and device number as evidence.
4. Treat `@lid` values as opaque strings. Never derive E.164 from a LID.
5. Derive a phone/E.164 alias only from a valid PN JID or an independently validated
   phone field.
6. Preserve `@c.us` as raw evidence even if the adapter normalizes it to
   `@s.whatsapp.net`.
7. Store identifiers as strings, never database integers.
8. A display name or WhatsApp `pushName` is profile metadata, not an identity key.

## Fields exposed by whatsmeow

Current whatsmeow `MessageSource` exposes:

- `Chat`;
- `Sender`;
- `IsFromMe`;
- `IsGroup`;
- `AddressingMode`: `pn` or `lid`;
- `SenderAlt`;
- `RecipientAlt`.

Source:
[`MessageSource`](https://github.com/tulir/whatsmeow/blob/fb386f15283797989565965f5a55cf37e2cf9cff/types/message.go#L14-L40).

`SenderAlt` and `RecipientAlt` mean “alternative address for this role”. They are not
fixed PN or LID fields. The JID suffix determines the namespace.

### Direct inbound message

```text
Chat   = primary address of the remote conversation
Sender = primary address of the sender

If primary addressing is LID:
    Sender    may be user@lid
    SenderAlt may be phone@s.whatsapp.net

If primary addressing is PN:
    Sender    may be phone@s.whatsapp.net
    SenderAlt may be user@lid
```

The alternate is optional. Some events legitimately contain only the LID or only the PN
JID.

### Group message

```text
Chat      = group@g.us
Sender    = participant person JID
SenderAlt = alternative PN/LID for that same participant, when supplied
```

The group JID identifies the conversation. It must never be attached as a person alias.

### Message synchronized from another own device

For messages sent by the connected account from another linked device:

- `Sender` represents the connected account;
- `Chat` represents the remote destination;
- `RecipientAlt` is the alternative address of that destination;
- `DeviceSentMeta.DestinationJID` is a destination, not a sender alias.

Never attach `RecipientAlt` or `DestinationJID` to the message author without first
evaluating direction and role.

Parser source:
[whatsmeow message parsing](https://github.com/tulir/whatsmeow/blob/fb386f15283797989565965f5a55cf37e2cf9cff/message.go#L87-L190).

## Fields exposed by Baileys

Current Baileys extends the message key with:

- `remoteJid` and `remoteJidAlt`;
- `participant` and `participantAlt`;
- `addressingMode`;
- `fromMe` and message `id`.

Sources:

- [`WAMessageKey`](https://github.com/WhiskeySockets/Baileys/blob/0af2386292907f7d9742d8d41f830d8c48208fa1/src/Types/Message.ts#L20-L28)
- [addressing-context extraction](https://github.com/WhiskeySockets/Baileys/blob/0af2386292907f7d9742d8d41f830d8c48208fa1/src/Utils/decode-wa-message.ts#L106-L135)
- [message-key construction](https://github.com/WhiskeySockets/Baileys/blob/0af2386292907f7d9742d8d41f830d8c48208fa1/src/Utils/decode-wa-message.ts#L228-L243)

For direct chats, `remoteJidAlt` holds the available alternative address. For groups,
`participantAlt` holds the alternative participant address. These fields can be absent
and their semantic role still depends on `fromMe` and chat type.

Baileys persists PN-to-LID and LID-to-PN mappings in its authentication key store and
can fetch missing mappings. This provider cache is operational state, not the canonical
Odoo identity store.

Source:
[Baileys `LIDMappingStore`](https://github.com/WhiskeySockets/Baileys/blob/0af2386292907f7d9742d8d41f830d8c48208fa1/src/Signal/lid-mapping.ts#L30-L106).

## The LID migration is active

whatsmeow changed direct-message sending in July 2026 to prefer LID addressing. It can
receive a PN, resolve/fetch the corresponding LID, send to the LID, and retain the PN as
an alternative recipient address.

Source:
[whatsmeow change “always use LID for DMs”](https://github.com/tulir/whatsmeow/commit/4f8f64e).

Therefore the same remote participant may appear as PN or LID depending on:

- message direction;
- direct versus group conversation;
- linked-device/history synchronization;
- library and provider version;
- local mapping-cache state;
- which alternative fields WhatsApp supplied.

The Contact Center must tolerate all of these shapes.

## Mapping stores are not canonical identity

whatsmeow currently persists a one-to-one operational map:

```sql
CREATE TABLE whatsmeow_lid_map (
    lid TEXT PRIMARY KEY,
    pn TEXT UNIQUE NOT NULL
);
```

Source:
[whatsmeow SQL schema](https://github.com/tulir/whatsmeow/blob/fb386f15283797989565965f5a55cf37e2cf9cff/store/sqlstore/upgrades/00-latest-schema.sql#L155-L158).

Its implementation can replace an older mapping when a PN or LID changes. Odoo must
preserve observation history instead of silently overwriting evidence.

Source:
[whatsmeow mapping replacement](https://github.com/tulir/whatsmeow/blob/fb386f15283797989565965f5a55cf37e2cf9cff/store/sqlstore/lidmap.go#L244-L266).

Provider mapping databases may be rebuilt, upgraded, lost, or replaced when the adapter
changes. Every useful observation must therefore be copied into the Odoo alias graph
during ingestion.

## Canonical persistence in Odoo

### Identity aliases

Recommended `contact.center.identity.alias` fields:

```text
identity_id
provider_account_id
platform                 whatsapp
namespace                whatsapp.lid | whatsapp.pn_jid | phone.e164 | whatsapp.raw_jid
value_raw
value_normalized
server
device
source                    sender | sender_alt | chat | participant | mapping | history
confidence                observed | provider_mapped | inferred | user_confirmed
first_seen_at
last_seen_at
active
valid_from                optional
valid_to                  optional
```

Minimum uniqueness:

```text
UNIQUE(provider_account_id, namespace, value_normalized)
```

Scope by provider account first. Do not globally merge platform-scoped identifiers or
recycled phone numbers. Cross-account and cross-platform consolidation requires reliable
evidence or an explicit user decision.

### Message protocol snapshot

`contact.center.message.binding` must preserve enough information to address the
original message later:

```text
provider_account_id
external_message_id
chat_jid_raw
sender_jid_raw
sender_alt_jid_raw
participant_jid_raw
participant_alt_jid_raw
recipient_alt_jid_raw
addressing_mode
is_from_me
device_sent_destination_jid_raw
sanitized_raw_payload or durable inbox reference
```

An external message ID alone is not always sufficient for reply, reaction, edit, or
delete. whatsmeow builds a message key from ID, remote chat JID, `fromMe`, and, for
groups, participant.

Source:
[whatsmeow message-key construction](https://github.com/tulir/whatsmeow/blob/fb386f15283797989565965f5a55cf37e2cf9cff/send.go#L492-L512).

Message deduplication should use an account-scoped composite key, for example:

```text
(provider_account_id, external_chat_namespace, external_chat_id, external_message_id)
```

Exact provider guarantees must be verified before reducing that key.

## DTO requirements

The provider adapter must emit all observed addresses with explicit roles. It must not
emit only a selected phone number.

Example:

```json
{
  "actor": {
    "display_name": "Example sender",
    "addresses": [
      {
        "namespace": "whatsapp.lid",
        "value_raw": "123456789012345@lid",
        "value_normalized": "123456789012345@lid",
        "role": "sender",
        "source_field": "Info.Sender",
        "confidence": "observed"
      },
      {
        "namespace": "whatsapp.pn_jid",
        "value_raw": "5511999999999@s.whatsapp.net",
        "value_normalized": "5511999999999@s.whatsapp.net",
        "role": "sender_alt",
        "source_field": "Info.SenderAlt",
        "confidence": "observed"
      }
    ]
  },
  "conversation": {
    "addresses": [
      {
        "namespace": "whatsapp.lid",
        "value_raw": "123456789012345@lid",
        "role": "chat"
      }
    ]
  }
}
```

Conversation addresses and actor addresses must be separate. This is mandatory for
groups and for messages synchronized from another own device.

The inbox must preserve a sanitized provider envelope and a DTO schema version so that a
future parser can reprocess old events. Sanitization must retain all protocol and
identity evidence while excluding credentials, inline binary, thumbnails, waveform and
redundant raw-message copies.

## Identity-resolution algorithm

1. Accept, sanitize and persist the provider event in the durable inbox.
2. Parse every observed address and preserve its source field and role.
3. Classify namespaces by server suffix, not numeric shape.
4. Normalize person-level and device-level representations without discarding raw
   evidence.
5. Look up all aliases within the provider-account scope.
6. If no alias exists, create an identity, a `mail.guest`, and all observed aliases
   atomically.
7. If aliases resolve to one identity, attach any newly observed aliases and update
   first/last-seen metadata.
8. If aliases resolve to different identities, record a resolution conflict. Do not
   silently auto-merge.
9. Process an event containing only LID normally. Attach PN later if reliable evidence
   arrives.
10. Keep alias enrichment independent from message deduplication: a duplicate webhook
    may contain a richer alternative identifier.

## Evidence for linking aliases

Strong evidence:

- a primary and alternative address supplied for the same role in one provider event;
- a validated PN/LID pair returned by the provider library's mapping API;
- a pair learned through WhatsApp history/protocol synchronization;
- a provider response such as whatsmeow `GetUserInfo` containing the mapped LID;
- explicit user confirmation.

Insufficient evidence on its own:

- both values contain digits;
- identical display names or `pushName`;
- treating the numeric part of a LID as a phone;
- attaching `RecipientAlt` or `DestinationJID` to the sender;
- matching a phone across different provider accounts without validation;
- a provider's current “primary” flag.

Even strong protocol evidence must not silently merge two different `res.partner`
records. That requires a controlled conflict-resolution path.

## Outbound addressing

The core selects the canonical identity and conversation binding. The provider adapter
selects the currently valid PN/LID address required by its library.

The core must not contain a global rule such as “always send to phone” or “always send
to LID”. That behavior changes with the provider and dependency version.

For message-level actions, pass the stored protocol snapshot to the adapter:

```text
external_message_id
chat address
from_me
participant for groups
provider-specific key extensions when required
```

## WuzAPI-specific observations

At the deployed `v1.0.8` commit `9487eca`, WuzAPI exposes `GET /user/lid/{jid}`. The
handler parses a PN/JID, queries whatsmeow's persisted LID store with `GetLIDForPN`, and
returns both the normalized `jid` and `lid` when the mapping exists. The Contact Center
Phase 1 acceptance validated this endpoint against the connected laboratory session
without recording either identifier.

This endpoint is reliable evidence for enriching an identity with a PN/LID pair; it is
not a reason to discard aliases observed in message events, and a `404` must not block
processing of a valid LID-only event.

Sources:

- [WuzAPI v1.0.8 LID route](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/routes.go#L141)
- [WuzAPI v1.0.8 `GetUserLID` handler](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/handlers.go#L7186-L7237)

At reviewed commit `919c72c9`:

- WuzAPI pins a whatsmeow revision from before the July 2026 “always LID for DMs”
  change;
- live webhooks include a raw whatsmeow message event;
- materialized message history stores only chat JID, sender JID, and message ID;
- synthesized history events can omit alternate identifiers;
- numeric input without an explicit server can be ambiguous, and current code asks
  callers to specify `@lid` or `@s.whatsapp.net` in ambiguous cases.

Sources:

- [pinned whatsmeow dependency](https://github.com/asternic/wuzapi/blob/919c72c9750b2a1eedf0fcf9c9592f05fe46f61c/go.mod#L15)
- [live event payload](https://github.com/asternic/wuzapi/blob/919c72c9750b2a1eedf0fcf9c9592f05fe46f61c/wmiau.go#L936-L941)
- [materialized history columns](https://github.com/asternic/wuzapi/blob/919c72c9750b2a1eedf0fcf9c9592f05fe46f61c/db.go#L124-L149)
- [synthesized history source](https://github.com/asternic/wuzapi/blob/919c72c9750b2a1eedf0fcf9c9592f05fe46f61c/wmiau.go#L1616-L1649)
- [ambiguous numeric identifier handling](https://github.com/asternic/wuzapi/blob/919c72c9750b2a1eedf0fcf9c9592f05fe46f61c/handlers.go#L6387-L6438)

Consequences:

- do not treat WuzAPI history as the canonical identity database;
- ingest all useful live-event observations immediately;
- preserve sanitized payload evidence and identify its provider/schema version;
- maintain fixtures for both current WuzAPI payloads and newer whatsmeow shapes.

## Required contract tests

- Direct inbound: primary LID with alternate PN.
- Direct inbound: primary PN with alternate LID.
- Direct inbound: LID only.
- Direct inbound: PN only.
- Group message: group chat plus participant LID/PN pair.
- Group message: participant only, with no alternate.
- Message synchronized from another own device with `RecipientAlt`.
- `DeviceSentMeta.DestinationJID` is not attached to the sender.
- Device JID is normalized for person resolution while raw/device data is kept.
- Legacy `@c.us` input preserves raw and normalizes consistently.
- A later PN/LID mapping enriches an existing guest.
- Conflicting aliases produce a resolution conflict instead of an automatic merge.
- A duplicate message with richer aliases enriches identity but does not duplicate
  `mail.message`.
- WuzAPI history event without alternate identifiers remains processable.
- Baileys `remoteJidAlt` and `participantAlt` map to the correct roles.
- Bare numeric input is not silently classified as PN or LID.
- Provider replacement preserves Odoo identity aliases and conversation bindings.

## Implementation invariants

- JID is the envelope; PN JID and LID JID are namespaces within it.
- A LID is opaque and is never a phone number.
- Phone is an alias, not the canonical database key.
- Alternate identifiers are optional.
- Roles matter: chat, sender, participant, and recipient are not interchangeable.
- Preserve raw and normalized identifiers.
- Preserve mapping history and evidence.
- The provider's mapping cache is not Odoo's source of truth.
- Deduplication and identity enrichment are separate operations.
- The adapter chooses outbound PN/LID addressing; the core does not.
