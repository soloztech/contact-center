# Odoo 16 Mail Guest Identity

- Status: implemented reference; CRM promotion remains optional
- Reviewed: 2026-08-25
- Odoo source: commit
  [`ffb1d0e6`](https://github.com/odoo/odoo/tree/ffb1d0e6e9f8c0c51d51840d79a29e0d3b57a73f)

## Decision

External participants are **guest-first**:

- `mail.guest` is the persistent Discuss persona used for channel membership, message
  authorship, avatar/name rendering, reactions, and native bus payloads.
- `contact.center.identity` is the canonical identity-resolution record.
- `contact.center.identity.alias` stores all namespaced external identifiers.
- `res.partner` is optional and is linked only after an explicit promotion or match.

`mail.guest` is not the canonical identity store. It must not receive provider columns
such as `whatsapp_id`, `telegram_id`, or `instagram_id`.

```text
contact.center.identity
|-- guest_id --------------------> mail.guest
|-- partner_id (optional) -------> res.partner
|-- merged_into_id (optional) ---> contact.center.identity
+-- alias_ids -------------------> contact.center.identity.alias

mail.channel --------------------> canonical conversation
mail.message.author_guest_id ----> inbound external author
mail.message.author_id ----------> Odoo agent or system author
```

This sidecar does not duplicate conversations or messages. It resolves external aliases
and bridges the persistent guest persona to an optional business contact.

One persistent guest may participate in multiple Contact Center channels. In particular,
a person can talk to two WhatsApp inboxes operated by different teams while keeping one
company-local identity/guest and two distinct conversations. Operational ownership
belongs to each account/channel, not to the guest row.

## Relevant native behavior

### `mail.guest`

Odoo 16 defines a persistent model with:

- required `name`;
- generated `access_token`;
- country, language, and timezone;
- channel membership through `mail_channel_member`;
- computed instant-messaging presence.

Source:
[`mail_guest.py`](https://github.com/odoo/odoo/blob/ffb1d0e6e9f8c0c51d51840d79a29e0d3b57a73f/addons/mail/models/mail_guest.py#L14-L47).

The token and `dgid` cookie support browser guests in public Discuss. They are not
provider credentials and must never be sent to WuzAPI, WAHA, Evolution, Meta, or another
external platform.

`mail.guest` has no native company scope, provider account, partner promotion, or
external alias graph. Those responsibilities belong to `contact.center.identity`.

### Channel membership

`mail.channel.member` accepts exactly one of:

- `partner_id`; or
- `guest_id`.

It also stores native per-member state such as last fetched/seen message, unread
counter, fold state, pin state, and last interest date. Odoo enforces unique partner and
guest membership per channel.

Source:
[`mail_channel_member.py`](https://github.com/odoo/odoo/blob/ffb1d0e6e9f8c0c51d51840d79a29e0d3b57a73f/addons/mail/models/mail_channel_member.py#L17-L72).

### Message authors and reactions

`mail.message` supports both `author_id` and `author_guest_id`. Its formatter exports a
separate guest author to the Discuss frontend, and reactions can also belong to a guest.

Sources:

- [message author fields](https://github.com/odoo/odoo/blob/ffb1d0e6e9f8c0c51d51840d79a29e0d3b57a73f/addons/mail/models/mail_message.py#L125-L134)
- [guest formatting and reactions](https://github.com/odoo/odoo/blob/ffb1d0e6e9f8c0c51d51840d79a29e0d3b57a73f/addons/mail/models/mail_message.py#L847-L896)

Human conversation messages remain `message_type='comment'`. Platform, provider,
direction, and external content type are transport metadata and must not be encoded in
`message_type`.

## Public Discuss is the guest reference

The generic public Discuss flow is the best Odoo 16 reference for guest behavior:

1. create or recover a `mail.guest`;
2. authenticate it with its access-token cookie;
3. add it as a `mail.channel.member`;
4. put the guest record in the Odoo environment context;
5. call the standard `message_post()` API.

Source:
[public Discuss invitation flow](https://github.com/odoo/odoo/blob/ffb1d0e6e9f8c0c51d51840d79a29e0d3b57a73f/addons/mail/controllers/discuss.py#L72-L117).

## Odoo 16 Live Chat must not be copied for identity

The legacy `im_livechat` visitor flow does **not** create a dedicated `mail.guest`. For
an anonymous visitor it uses a shared public partner/channel context,
`mail.channel.anonymous_name`, and a message with:

- `author_id=False`;
- the visitor name in `email_from`;
- the channel UUID as the public transport key.

Sources:

- [public live-chat posting route](https://github.com/odoo/odoo/blob/ffb1d0e6e9f8c0c51d51840d79a29e0d3b57a73f/addons/mail/controllers/bus.py#L21-L52)
- [official anonymous-flow test](https://github.com/odoo/odoo/blob/ffb1d0e6e9f8c0c51d51840d79a29e0d3b57a73f/addons/website_livechat/tests/test_livechat_basic_flow.py#L114-L141)

That design cannot distinguish persistent WhatsApp, Telegram, Instagram, or Messenger
senders. Reuse the Live Chat routing and Discuss UI ideas, but use the generic
`mail.guest` mechanism for external identities.

## Implemented records

### `contact.center.identity`

Relevant fields:

```text
company_id          required company boundary
mail_guest_id       required persistent persona; unique
partner_id          optional business contact
name                current external display name
name_source         fallback | provider | manual
observed_name       latest monotonic provider observation
state               active | merged
merged_into_id      redirect after identity consolidation
```

The identity may have aliases from multiple providers or platforms only after reliable
evidence or explicit linking. It must not merge people globally by display name or phone
alone.

### `contact.center.identity.alias`

Relevant fields:

```text
identity_id
account_id
namespace
value_raw
value_normalized
role
source_field
confidence
resolution_scope    account | company
first_seen_at
last_seen_at
```

The provider-specific details and WhatsApp namespaces are documented in
[`whatsapp-jid-lid-identifiers.md`](whatsapp-jid-lid-identifiers.md).

## Cross-inbox resolution

Identity scope is explicit on each observed address:

- a protocol-validated `whatsapp.pn` uses `resolution_scope='company'`; all accounts of
  that Odoo company may resolve the same active `contact.center.identity` and therefore
  the same `mail.guest`;
- `whatsapp.lid` and opaque `whatsapp.jid` remain `account` scoped;
- future `messenger.psid` and `instagram.igsid` also remain account/asset scoped unless
  their provider contract later supplies a genuinely portable identifier;
- company boundaries are absolute: the same PN in two Odoo companies does not share an
  identity or guest.

The shared identity does not merge inboxes. The direct-conversation key remains
`(account_id, identity_id, direct)`, so each inbox keeps its own `mail.channel`, team,
assignee, status, unread state and channel aliases. The same guest is simply a native
member of each channel. Alias record rules require both the alias's own account/team and
membership in a direct conversation of that identity; sharing the identity does not
grant an operator access to aliases from another inbox or to group-only participant
identities.

Concurrent first observations of a portable PN are serialized with a transaction
advisory lock. When legacy account-local identities are later bridged by the same
trusted PN, automatic reconciliation is allowed only when all of these conservative
checks pass:

- every identity is active and belongs to the same company;
- there are not two different linked contacts;
- there are not conflicting manual display names;
- no account already has more than one active direct conversation in the component.

If a check fails, processing remains an explicit identity conflict. When it passes, the
survivor is chosen deterministically, aliases and direct bindings point to it, and the
retired identities keep a redirect to the survivor. Odoo 16 does not permit changing
`mail.channel.member.guest_id`; migration therefore creates/merges the survivor member
before unlinking only the retired active membership.

Historical evidence is deliberately not rewritten. Existing
`mail.message.author_guest_id`, guest reactions, mutation actors and tombstones continue
to reference the guest that authored them. Future messages and active memberships use
the survivor guest.

The SERVIDOR05 migration reconciled 67 legacy identities. All redirects end at an active
survivor in the same company, while 286 historical messages and five mutation records
continue to reference the retired author guest. Fifty-five portable PNs observed in
multiple accounts converged without an active duplicate; 19 identities already back 42
direct bindings across multiple inboxes, with no retired guest left in an active Contact
Center membership.

## Posting an inbound message as a guest

There is an Odoo 16 implementation constraint: `message_post()` sets `author_guest_id`
only when both conditions hold:

1. the environment user is the public user; and
2. a valid `mail.guest` is present in the environment context.

Supplying `author_guest_id` as an arbitrary keyword is not a supported substitute; the
author computation overwrites it.

Source:
[`mail.thread.message_post()` author resolution](https://github.com/odoo/odoo/blob/ffb1d0e6e9f8c0c51d51840d79a29e0d3b57a73f/addons/mail/models/mail_thread.py#L2073-L2103).

The core therefore needs one tested helper with this conceptual sequence:

```text
1. resolve or create contact.center.identity
2. resolve or create its mail.guest
3. ensure the guest is a member of the target mail.channel
4. enter the controlled public-user + guest context
5. call mail.channel.message_post(message_type='comment', ...)
6. create the message binding in the same transaction
7. mark the execution as inbound so no outbound command is generated
```

The exact `with_user`/`sudo` ordering must be covered by an Odoo test. The helper must
use `message_post()` rather than creating `mail.message` directly so that sanitization,
replies, attachments, formatting, notification hooks, and bus updates remain native.

## Promotion to `res.partner`

Promotion is a link, not a database-row conversion:

1. create or select a `res.partner`;
2. set `contact.center.identity.partner_id`;
3. keep the existing `mail.guest`;
4. keep historical `mail.message.author_guest_id` values unchanged;
5. show the linked contact and CRM actions in the Contact Center banner.

Inbound platform messages should continue to use the stable guest persona after
promotion. This avoids splitting one transport identity between old guest-authored
messages and new partner-authored messages. The linked partner supplies business
context, not transport authorship.

Never bulk-rewrite historical authors during promotion. If two external identities are
consolidated, keep a merge redirect/audit trail and never auto-merge two different
linked partners.

## Native state versus platform state

These concepts must remain separate:

| Native Odoo state                     | External platform state                    |
| ------------------------------------- | ------------------------------------------ |
| `mail.channel.member.seen_message_id` | Provider receipt: delivered/read           |
| `message_unread_counter`              | External participant unread state          |
| Discuss typing indicator              | WhatsApp/Telegram presence or typing event |
| Discuss pin/fold state                | Contact-center open/pending/resolved state |

External receipts belong to `contact.center.delivery.event`. They must not update a
guest or agent member's native seen pointer.

## Required tests

- First inbound event creates one identity, one guest, one membership, and one
  guest-authored message.
- A duplicated webhook creates no duplicate message or guest.
- The same identity can participate in multiple channels using the same guest.
- The same trusted PN in two accounts of one company reuses one identity/guest while
  preserving one direct channel per account.
- The same LID, opaque JID, PSID or IGSID in two accounts does not cross-resolve.
- Equal PNs in different companies remain isolated.
- A safe legacy merge replaces active memberships but leaves historical authors,
  reactions and tombstones unchanged; every blocker fails closed.
- A guest-authored message is formatted correctly and reaches agents through the native
  bus.
- Incoming processing never creates an outbound command.
- Promotion links a partner without rewriting messages or removing the guest.
- A later alias enriches the existing identity instead of creating another guest.
- Conflicting aliases do not auto-merge two linked partners.
- Native agent unread/seen state does not change external delivery receipts.
- Removing or archiving a conversation does not delete the canonical guest or its alias
  history.

## Implementation invariants

- Do not create `res.partner` automatically for every external sender.
- Do not store provider identifiers directly on `mail.guest`.
- Do not use `mail.guest.access_token` outside Odoo's guest authentication.
- Do not use `email_from` as an omnichannel identity key.
- Do not rewrite historical authors on promotion.
- Do not infer company-wide portability from a display name, a typed phone number or a
  numeric-looking opaque identifier.
- Do not expose one account's aliases merely because its identity is shared with a
  channel in another account.
- Do not create `mail.message` directly for normal inbound messages.
- Keep identity resolution separate from message deduplication.
