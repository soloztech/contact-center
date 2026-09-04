# WuzAPI event coverage

This document pins the functional coverage of `contact_center_wuzapi` to WuzAPI
`v1.0.8`, commit `9487eca`. A provider upgrade must update fixtures, this matrix and the
adapter before enabling new event names.

## Projection contract

The selected webhook list belongs to each provider connection. The controller accepts
traffic only from that account's active `primary` ingress. Every authenticated event is
persisted as a sanitized Inbox Event before asynchronous normalization; an event that
the adapter does not implement converges to `unsupported` and remains observable. The
fleet panel shows the last 24 hours of unsupported events, while the administration
ledger retains the lifetime count.

The account is the logical inbox; a WuzAPI connection is only one transport/session for
it. `standby` and `migration` connections may be monitored but never accept webhook
work, and a `historical` connection is archived for references/audit. The topology is
not a load balancer. During a controlled primary cutover, new uncorrelated sends move to
the replacement, while replies and mutations remain bound to the provider connection
that issued the target external message ID. The core fails those actions closed rather
than passing a WuzAPI ID to another adapter or session.

| Provider event                                                                                                                                                                      | Current projection                                                                                                                                                                                                                                                                                                                                                                        |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Message`                                                                                                                                                                           | Text, image, audio, video, document, WebP sticker, location/live location, contact summaries, templates, polls and interactive messages. Supported ephemeral/view-once/associated-child wrappers preserve only the exact download key; association metadata is bounded and never becomes a false reply. Reply and mutation envelopes remain correlated through the common message ledger. |
| `ReadReceipt`                                                                                                                                                                       | Provider-neutral delivery/read observations.                                                                                                                                                                                                                                                                                                                                              |
| `GroupInfo`, `JoinedGroup`, `Picture`                                                                                                                                               | Bounded metadata/avatar invalidation followed by an OCA `queue_job` refresh.                                                                                                                                                                                                                                                                                                              |
| `CallOffer`, `CallAccept`, `CallTerminate`                                                                                                                                          | Immutable provider-neutral call cards in the direct conversation. The provider call ID is represented only by a scoped SHA-256 reference.                                                                                                                                                                                                                                                 |
| `IdentityChange`                                                                                                                                                                    | Immutable security card on an existing direct conversation; it never changes aliases, guest names, PN/LID evidence or partner links.                                                                                                                                                                                                                                                      |
| `Connected`, `Disconnected`, `ConnectFailure`, `KeepAliveRestored`, `KeepAliveTimeout`, `StreamError`, `StreamReplaced`, `LoggedOut`, `QRTimeout`, `ClientOutdated`, `TemporaryBan` | Connection-health hints. Polling `/session/status` remains the authoritative repair path.                                                                                                                                                                                                                                                                                                 |

Unknown human message wrappers become one safe `[Conteúdo do WhatsApp não suportado]`
projection so an operator can see that content arrived. Protocol-only, cryptographic and
coordination envelopes do not create a misleading chat bubble and converge to the
technical `unsupported` ledger.

## Deliberately not projected

These provider events exist in the pinned catalog but are not part of the default
subscription and need a separate product contract before implementation:

- `CallOfferNotice` and group calls: duplicate/provider-side call coordination;
- `CallRelayLatency`: diagnostic telemetry rather than conversation content;
- `Presence` and `ChatPresence`: high-volume ephemeral state;
- `Blocklist` and `BlocklistChange`: account policy with destructive consequences;
- `HistorySync`, `OfflineSyncPreview` and `OfflineSyncCompleted`: require a bounded
  importer and a clear replay/deduplication policy;
- `MediaRetry` and `UndecryptableMessage`: cryptographic recovery lane;
- `Receipt`: the pinned provider accepts this subscription name but maps the underlying
  `events.Receipt` payload to webhook type `ReadReceipt` before checking subscriptions.
  Therefore `ReadReceipt` is the effective subscription and canonical projection;
  selecting only `Receipt` is an upstream no-op, not a second delivery contract;
- `AppState`, `AppStateSyncComplete`, `CATRefreshError`, `FBMessage`, `PairError`,
  `PairSuccess`, `QR`, `QRScannedWithoutMultidevice`, `PrivacySettings`,
  `PushNameSetting` and `UserAbout`: session/configuration state without a current
  Contact Center projection;
- newsletter join/leave/live/mute events: newsletters are not a supported conversation
  type.

Selecting one of these events manually is safe: it is authenticated, sanitized and
recorded, but it does not mutate guests, contacts, channels or messages. The unsupported
counter makes the decision visible for a later prioritized implementation.

## Webhook authentication and HMAC rotation

Each WuzAPI connection has an opaque callback route and its own HMAC secret. The route
key selects a candidate connection but never replaces signature authentication. An
established key cannot be replaced by editing the field directly.

The **Rotate HMAC** action creates a strong pending secret inside Odoo and queues a
revision-fenced job. That job calls `/session/hmac/config` and accepts only an explicit
successful response (`code = 200`, `success = true` and non-empty `data.Details`) before
promoting the pending key. Neither the generated secret nor another credential is copied
into a job argument, job description, DTO, bus payload or log.

Webhook verification accepts current, pending and—only until its deadline—previous keys.
This overlap covers a callback already in flight while the provider and Odoo commit the
cutover. After a successful promotion, the old key has a fixed five-minute,
non-renewable drain window; a separately fenced cleanup job erases it. A stale rotation
or cleanup job cannot promote or remove a key belonging to a newer revision.

## Outbound controls

`mark_read` is provider-neutral in the core and maps to WuzAPI `/chat/markread` only for
direct conversations when the inbox policy and adapter capability are both enabled. It
is asynchronous, batched to 100 provider message IDs and optional: a local seen pointer
never depends on provider I/O.

Call answer/reject, presence publishing, blocklist changes and history requests are not
outbound Contact Center commands in this release.
