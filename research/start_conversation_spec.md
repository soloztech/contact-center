# Spec: `contact.center.ui.api.start_conversation` (contact-center, Odoo 16)

Research notes (2026-09-10) for whoever implements the method in
https://github.com/soloztech/contact-center (branch `16.0`). This is based on reading
the code only: nothing was run against a lab. Line numbers refer to the `16.0` tree at the time.

## Why it's needed
- An Agent can only send to an **existing** conversation. `send_message` needs `channel_id`.
- Direct channels are only created in `application._resolve_channel`
  (`contact_center_base/models/application.py:2415-2447`). Callers: inbound
  (`:2599`), control events (`control_events.py:243`), from-me echo (`:1279`).
- Creating a channel any other way is blocked (`channel.py:221-241`).
- Business need: send an image and caption to ~440 trade-show leads that never talked
  to the salesperson's WuzAPI inbox, about 10 per inbox per business day.

## Contract expected by the dispatch script (`disparo-whatsapp/enviar.py`)
```python
# JSON-RPC /web/dataset/call_kw, internal user = Agent with access to the inbox
res = env["contact.center.ui.api"].start_conversation(account_id, "5511912345678")
# -> {"schema_version": ..., "channel_id": int, "created": bool, "item": {...serialized conversation...}}
```
- Idempotent: if a direct conversation for that number already exists in the inbox,
  return it (`created=False`), including when the typed number differs only by
  Brazil's 9th digit.
- Afterwards the new channel must show up in
  `list_conversations(filters={"account_id": X, "query": digits})` with `can_send=True`,
  and accept the existing flow: `/contact_center/media/upload` then `send_message(channel_id, caption, False, uuid, [media_ref])`.
- Errors come back as `UserError`/`ValidationError` with a clear message. The cases are:
  not on WhatsApp, not authorized, provider not ready, identity conflict, ignored contact.

If the final signature or return differs from this, tell the dispatch agent so it can adjust
`Central.iniciar_conversa` in `enviar.py`.

## Suggested design
Reuse the webhook projection path so locks, members, auto-assignment, the kanban
default case and avatar sync all come for free. No schema change or migration is needed.

1. **Security first:**
   - `_check_agent` (`application.py:67-71`).
   - The account exists and is active, and `account.company_id in env.companies`.
   - `check_access_rule("read")` passes, and `account._contact_center_check_user_scope()` (`account.py:1390-1397`).
   - A primary, active, outbound connection exists, and it is not `identity_mismatch_latched`.
   - `adapter.is_provider_read_ready(...)` holds.
2. **Provider I/O before any lock:** add a provider-neutral hook in `services/adapter.py`,
   next to `supports_identity_profile` / `fetch_identity_profile` (`:358-370`):
   `supports_direct_conversation_start(connection)` and
   `resolve_direct_address(connection, value) -> DirectAddressResult(state, conversation_ref, addresses)`
   (a new frozen DTO in `services/dto.py`).
   Call it on `connection.sudo()`, because `wuzapi_api_token` is admin-only
   (`provider_connection.py:143-147`). The result carries addresses only, never the token.
3. **WuzAPI implementation** (`contact_center_wuzapi/services/group.py`, next to `/user/check` usage at `:957-1012`):
   - Call `POST /user/check {"Phone": [digits]}` through `_identity_profile_request`, which already maps 401/403, 429 and 5xx.
   - If `IsInWhatsapp` is false, return `not_registered`.
   - **Do not reuse the existing strict JID==query check** (`group.py:990-994`). Accept the canonical
     JID when it differs only by Brazil's 9th digit (13↔12 digits starting with `55`), and reject anything else.
   - If a PN JID comes back: make it the primary address (`namespace=whatsapp.pn`, `confidence=protocol`,
     `resolution_scope=company`). Best-effort `GET /user/lid/{jid}` (WuzAPI v1.0.8), and if it maps, add the LID
     as an alternate (`whatsapp.lid`, protocol, account scope).
   - If a LID JID comes back: make the LID primary, and add the typed PN as an alternate with `confidence=observed`
     (it is promoted later when seen with protocol confidence, `application.py:88-101`).
   - Reject the inbox's own number (`account.own_external_identity`).
4. **Projection, same order as `_post_inbound` (`application.py:2587-2602`):**
   - `_lock_inbound_account_scope(account)`, then re-check active and scope.
   - `_resolve_identity(account, ActorDTO(addresses=...))`.
   - `_resolve_channel(account, identity, conversation_ref=...)`. Needs a small signature change: today it reads `event.conversation_ref` (`:2444`).
   - `_lock_inbound_projection_binding(binding, account)`, then `_enrich_channel_aliases(binding, addresses)`.
   - `IdentityConflictError` becomes a `UserError`.
   - Revision: `inbound_projection_revision` is advanced by the helpers. Do **not** touch `access_topology_revision`.
   - A partial unique index gives one active direct binding per (account, identity) (`channel.py:1202-1210`),
     which provides natural idempotency.
5. **On creation:** call `binding._request_identity_avatar_sync(connection)` and
   `_notify_ui(channel, "conversation_updated", ...)`, since channel creation sends no bus event.
   Refuse the request if a `contact.center.conversation.ignore` rule matches (`conversation_actions.py:182-237`); otherwise
   replies would be dropped at the webhook (`webhook.py:499-508`).
6. **Return value:** re-open the channel through `_authorized_channel` (`ui_api.py:77-94`). The creator is a member
   because every effective user of the inbox is added (`channel.py:516-517`).

## ⚠️ Critical risk: duplicate or blocked conversation when the reply only carries the LID
- **Reply with `Chat`=LID plus `SenderAlt`=PN:** it resolves to the same identity and binding. Fine.
- **Reply that carries only the LID** (no `SenderAlt`) when no LID alias exists: this creates a **second conversation**.
  A later event with PN+LID is then blocked as an identity conflict (`identity.py:668-670`).
- **Mitigations:**
  - (a) store the LID from `/user/lid` at creation;
  - (b) **H1:** in `_reconcile_existing_from_me_message` (before `application.py:1208`), also run
    `_enrich_identity_aliases` for direct echoes (`Chat` plus `RecipientAlt` both address the remote person), inside
    a savepoint that never fails the echo.
- The echo is the only way an `uncertain` send becomes `done`. Review the lock order against the outbox and message-binding lock
  (`application.py:1077-1123`).

## Tests to add
- **Base**, new `tests/test_start_conversation.py`. Use `FakeAdapter` (`test_contact_center.py:75-110`) and
  events from `test_cross_account_identity.py:84-199`. Cases:
  - creates channel, members, auto-assignment and bus event;
  - idempotent, including the 9th-digit variant;
  - an existing PN or LID conversation is reused;
  - a PN/LID identity conflict raises `UserError` with no writes;
  - `not_registered`;
  - authorization matrix: non-agent, out of scope, other company, inactive, no connection, not ready. The adapter must not be called;
  - ignore rule matches;
  - provider paused or transient;
  - end-to-end: start, then `send_message` with the PN target, then a LID+PN reply lands on the same binding (and with H1, a LID-only reply does too);
  - the kanban default case exists.
- **WuzAPI**, new `tests/test_direct_start.py`, patching `services.group.requests.request`
  (`test_group_metadata.py:25-31`). Cases:
  - exact `/user/check` request;
  - 9th digit handled in both directions;
  - `IsInWhatsapp=false`;
  - LID response;
  - invalid input rejected before any I/O;
  - `/user/lid` returns 404, 200 or 5xx;
  - own number;
  - 401 means paused.

## Lab checks before production (WuzAPI pinned `9487eca`, not verifiable from the repo)
1. `/user/check` for Brazilian 12- and 13-digit numbers: does it return a canonical JID, a PN or a LID?
2. `/user/lid/{jid}` response envelope, including for a number never contacted.
3. Does the first reply after a PN-addressed send include `SenderAlt`?
4. Outbound-first volume (~10 new contacts/inbox/day) is ban-sensitive on the unofficial API. Keep the pacing low.

## Rejected alternative (no deploy)
The first message would go directly through WuzAPI REST, hoping the `IsFromMe` webhook creates the conversation via
`_reconcile_from_me` (which requires `technical_author_id`). This was rejected because:
- it needs the admin-only token;
- it bypasses outbox audit, pacing and authorship;
- nothing shows that WuzAPI emits echoes for REST-originated sends.
