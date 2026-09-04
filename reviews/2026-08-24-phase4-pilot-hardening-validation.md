# Phase 4 pilot webhook hardening — validation

Date: 2026-08-24 Environment: disposable SERVIDOR05 laboratory,
`odoo16-teste.soloz.com.br` Result: **implemented, deployed and validated**

## Conversation investigation

The investigation covered all **114** persisted inbox events associated with the
sanitized fixture `700000000000678@lid`, without copying message bodies into this
report:

- 46 `Message` events: 12 inbound and 34 `from_me`;
- 68 `ReadReceipt` events;
- initial state: 102 `done`, three early direct `from_me` events `unsupported`, and nine
  dependent/self-side receipts `dead`;
- all 45 resulting message bindings have a nonempty external ID and there are zero
  duplicate external IDs.

Inbox event 5681 was an `UnavailableRequestID` recovery copy. It was processed first,
carried the canonical conversation addresses but no usable `PushName`, and therefore
created the identity, guest and channel with the LID fallback. Event 5682 arrived next
with `PushName=Contato de laboratório` and the corresponding PN alias. Ten later inbound
events repeated that valid provider name. The previous implementation enriched aliases
and deduplicated the message but never converged a later provider name into the existing
managed records.

The WuzAPI contact lookup independently agreed that the LID and PN represent the same
laboratory contact. It was used as diagnostic evidence only; the fix and migration do
not depend on provider lookup.

## Delivered fixes

- Identity name provenance distinguishes identifier fallback, provider observation and
  manual naming.
- Valid observations are ordered by provider occurrence time plus inbox event ID. An
  older or duplicate event cannot revert a newer name.
- Empty, punctuation-only, emoji-only and address-equal observations are ignored.
- A provider-managed identity projects its new name to the durable `mail.guest` and
  direct `mail.channel` only while they still have the previous managed value.
- An operator rename, a separately renamed channel and a promoted partner remain
  authoritative.
- The migration backfills only canonical persisted inbound DTOs, does no provider I/O,
  creates no records and emits no notification storm.
- A direct `from_me` message sent from another device can create the missing
  conversation from its conversation addresses. It never uses the account actor name for
  the remote identity.
- Self-side direct read evidence is terminal `unsupported`, not outbound delivery.
- WuzAPI receipt normalization includes `IsFromMe` in direction and stable identity. An
  explicitly empty/non-string `MessageIDs` collection is terminal unsupported.
- `kind=document` accepts nonempty vendor MIME types such as `image/vnd.dwg`; active
  content remains download-only and the typed image/audio/video checks are unchanged.
- The administrative requeue action is shown only for terminal inbox states.

## Directed historical convergence

Only the exact affected records were reprocessed through the Odoo ORM:

- events 5520, 5523 and 5527: early external-device messages, now `done`;
- events 5521, 5522, 5524, 5525, 5528 and 5529: dependent delivery receipts, now `done`;
- events 5757, 5770 and 6580: own-device read evidence, now terminal `unsupported` with
  `UnsupportedEventError`;
- two historical DWG media bindings: now `ready` with private attachments and validated
  exact sizes;
- exactly 104 dead WuzAPI receipt batches with no nonempty message ID: now terminal
  `unsupported`.

No broad inbox replay was performed. For the target conversation, the final projection
has one direct channel, one identity/guest, 45 bound messages, 11 provider-originated
inbound messages and 34 external-device messages. The UI exposes both the LID and the
masked PN alias on that guest.

The migration also recovered three other legacy fallback projections from their own
persisted canonical DTOs. Two fallback identities had no trustworthy persisted name and
were intentionally left unchanged; a future bounded contact-directory sync may enrich
them without making one provider call per conversation.

## Validation

- Pre-commit repository gates and `git diff --check`: passed.
- Isolated base suite: **193/193**, no failures or errors.
- Integrated base + WuzAPI + UI suite: **290/290**, no failures or errors.
- Five WuzAPI connections: connected, healthy and outbound enabled.
- Outbox: 17 `done`; zero pending, retry, dead or uncertain commands.
- Target conversation final state: 111 inbox events `done` and the three self-side read
  receipts `unsupported`; no pending, retry, blocked or dead event.
- The fleet-wide post-release snapshot still had 16 unrelated inbox events in backoff,
  all with an active OCA job and none orphaned. Ten are old self-side receipts expected
  to converge to the new terminal classification at their scheduled ETA.
- Three unrelated group receipts created before the release reached `dead` after 12
  attempts with an explicit uncorrelated-target reason. Their four target IDs have no
  local message binding or persisted `Message` webhook, so they are expected historical
  orphan evidence rather than a new regression.
- Authenticated Chromium: the search returned one matching laboratory conversation;
  list, heading, guest, LID/PN aliases and multimedia timeline were coherent; five of
  five connections were shown as connected; zero browser console errors.
- The two remaining failed media records are approximately 95 MiB videos rejected by the
  configured 50 MiB limit. This is expected policy enforcement, not a download defect.

## Release and recovery evidence

- OCA `queue_job`: `16.0.3.0.2`
- `contact_center_base`: `16.0.1.17.0`
- `contact_center_wuzapi`: `16.0.1.14.0`
- `contact_center_ui`: `16.0.1.14.0`
- deployed runtime tree hash:
  `ea16430a070ca609cc8f1bbf6c56e8e8ddd752e11a7f699fd4b28dbe2b6235fc`

The first atomic upgrade failed closed because a provider timestamp was timezone-aware
while the Odoo helper expected a naive UTC value. The upgrade transaction rolled back,
the preserved prior source was restored, and public service returned HTTP 200. The
normalizer was corrected and the second atomic release completed successfully. The exact
Traefik route was restored byte-identically after each attempt.

Final evidence:

- atomic release:
  `scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260825T002117235160Z`;
- final source deploy:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/deploy/20260825T002346998319Z`;
- final isolated tests:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260825T002438150029Z`.

Docker's disposable build cache was pruned after the lab filesystem reached 98%,
reclaiming 1.362 GiB and leaving about 3 GiB free. No database, attachment or backup was
deleted. The disposable lab backup was explicitly waived, and production was not
accessed or changed.

## Remaining operational acceptance

The code hardening is complete for this increment. General pilot acceptance still
requires the coordinated disconnect/reconnect drill, an ambiguous `uncertain` canary and
JobRunner capacity measurement for the intended topology. Stickers, contact cards,
location and polls remain feature backlog rather than defects in this release.
