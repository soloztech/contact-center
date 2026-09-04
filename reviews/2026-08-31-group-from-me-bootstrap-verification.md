# First group `from_me` bootstrap — implementation and SERVIDOR05 verification

Date: 2026-08-31 Environment: neutralized Odoo 16 laboratory on SERVIDOR05 Production
touched: no

## Outcome

`contact_center_base` `16.0.1.29.1` now creates a previously unknown group conversation
when its first human `message.created` is an authenticated, uncorrelated `from_me`
message observed on the external device. The bootstrap is gated by group receiving
opt-in, one exact protocol group address and an account technical author.

The old cross-conversation protection remains fail-closed: before any channel is
created, provider message/client IDs are searched across the same connection. If an ID
already belongs to a local outbound in another conversation, the event retains the
previous terminal error and cannot create an alias or group.

The new path reuses the existing group channel/profile primitives, stores the message as
`outbound` / `external_device` / `sent`, creates no identity, guest or outbox for the
connected account, and schedules `GroupInfo` asynchronously. Media download remains
independent from metadata synchronization.

## Changed surface

- `contact_center_base/models/application.py`: guarded bootstrap in
  `_reconcile_from_me()` and audit evidence on the delivery event.
- `contact_center_base/models/account.py`: opt-in help now documents external-device
  group initialization.
- `contact_center_base/__manifest__.py`: `16.0.1.29.1`.
- `contact_center_base/tests/test_phase5_groups.py`: success, replay, media, opt-in,
  technical author, origin, address, reply rollback and cross-group regression.
- `contact_center_wuzapi/tests`: paired protocol-only/real-image fixtures using the same
  synthetic `Info.ID`, with both arrival orders and raw webhook replay.
- `plan.md` and `research/wuzapi-group-metadata.md`: updated creation boundary.
- `odoo16/scripts/odoo16_contact_center_group_from_me_replay.py`: exact, dry-run-first
  ORM canary for inbox event `34636`.

There is no new database schema or migration directory for `16.0.1.29.1`. No WuzAPI
runtime or Contact Center UI runtime was changed for this fix.

## Automated verification

The candidate source tree hash was
`2ad816cd5a4756ca38fd8b92448b69edc3724457f40686e210221191700671a2`.

Isolated temporary databases on SERVIDOR05 completed and were removed:

- base: **365/365**, zero failures, zero errors;
- integrated base/WuzAPI/Meta/UI: **714/714**, zero failures, zero errors.

Static Python compilation, fixture JSON parsing and `git diff --check` passed. The
existing correlated cross-group test remains present and green.

## Deployment and backup

The source deployment atomically preserved the previous tree at:

`/home/administrador/odoo16/deploy-backups/contact-center/contact-center-20260831T142052601307Z`

Before the module upgrade, the installer created and validated a PostgreSQL custom
archive:

- file:
  `/home/administrador/odoo16/backups/contact-center/20260831T142449Z-before-phase5-3-group-events-odoo16.dump`;
- size: 245,493,317 bytes;
- SHA-256: `13fdbad18b1aa0bfe2c7da38db76162bd9551c39cf576e2d09f9fb1f2b9ab302`;
- `pg_restore --list` and full restore stream validation: successful.

Only `contact_center_base` (module ID 3817) required upgrade. Post-upgrade module
versions were validated as base `16.0.1.29.1`, WuzAPI `16.0.1.26.4`, Meta `16.0.1.10.0`,
UI `16.0.1.18.4` and queue_job `16.0.3.0.2`.

## Real canary

Dry-run validated immutable inbox event `34636` before replay:

- account/connection: Lucas laboratory, active and connected;
- `group_inbound_enabled=true` and technical author present;
- normalized event: group image, `from_me=true`, `origin=external_device`;
- previous state/error: `unsupported` /
  `a group from_me message requires an existing conversation`;
- no existing group, message or media projection.

The exact event was requeued through `contact.center.inbox.event.action_requeue`. The
authoritative result was:

- inbox `34636`: `done`, one attempt;
- one group channel binding and one group profile;
- profile: `ready`, complete roster, 3 participants, 1 administrator, own role `member`;
- one message binding: `outbound`, `external_device`, `sent`, immutable source inbox
  `34636`;
- one image media binding: `ready`, one download attempt, attachment present;
- zero outbox commands.

The paired protocol-only event `34635` and the unrelated protocol-only event `34754`
remain correctly `unsupported`. Historical events `30236` and `30241` were not replayed.

## Browser acceptance

An authenticated Chromium smoke opened the public laboratory route and confirmed:

- `Grupinho` is listed under `WhatsApp Lucas - Laboratorio`;
- the timeline shows `Lucas Zotelli · outro dispositivo` and the rendered image;
- delivery is shown as sent;
- group metadata is shown as synchronized;
- the composer is available after the own roster participant was confirmed;
- browser console: zero errors and zero warnings.

Screenshot: `output/playwright/group-from-me-bootstrap-34636.png`.

## Evidence and rollback

Raw, permission-restricted evidence is under:

`scans/raw/20260831-odoo16-contact-center-group-fromme-bootstrap/`

Rollback is code-first by restoring the preserved source tree. The verified database
archive is available if a database restore becomes necessary. The legitimate canary
conversation/message should not be deleted automatically by a code rollback.

Residual limitation: a pathological concurrent reuse of the same provider external ID in
two different, both-unknown groups is not globally constrained at SQL level because the
provider-neutral core permits providers whose external IDs are conversation-scoped.
Normal/replayed callbacks and local outbound echoes are covered by the correlation guard
and automated tests.
