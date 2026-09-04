# Phase 5.3 group events — final validation

Date: 2026-08-24 Environment: disposable SERVIDOR05 laboratory,
`odoo16-teste.soloz.com.br`, database `odoo16` Result: **Phase 5.3 completed and
validated**

## Delivered scope

Phase 5.3c adds provider-neutral group reaction, edit and delete. Reactions may target
remote or own messages; edit and delete are limited to correlated outbound messages. The
DTO contract carries `target_from_me`, the account's own protocol participant and the
target participant. The core revalidates account opt-in, capability, connection, health
revision, profile, direction, roster and PN/LID equivalence before dispatch.

Phase 5.3d adds `contact.center.group.delivery.event`, a convergent receipt ledger keyed
by outbound message, technical participant and `delivered/read` milestone. Replays,
out-of-order events and PN/LID aliases converge without creating a guest, partner,
identity or channel membership. A mixed receipt batch keeps valid correlations and
records only the count of unknown message IDs as technical Inbox metadata.

Group receipts do not promote the aggregate delivery state. UiDTO exposes only
`delivered_count` and `read_count`; technical participants and protocol identifiers stay
out of the bus and frontend contract. The ledger is read-only for Contact Center
administrators and restricted by company.

## WuzAPI adapter and migration

The adapter normalizes and dispatches group reaction, edit and delete through the pinned
`/chat/react`, `/chat/send/edit` and `/chat/delete` contracts. A self reaction uses
`me:<message-id>`; a remote-target reaction carries `Participant`.

Migration `16.0.1.12.0` refreshes provider capabilities and schedules authoritative
group metadata. Historical raw mutations and receipts are deliberately not replayed:
safe replay requires exact message correlation and an unambiguous roster at execution
time.

## Installed versions

- OCA `queue_job`: `16.0.3.0.2`
- `contact_center_base`: `16.0.1.15.0`
- `contact_center_wuzapi`: `16.0.1.12.0`
- `contact_center_ui`: `16.0.1.13.0`
- Runtime tree hash: `6cb0020a07c9189880076d4a2144cdf3d900dd601e113bf28137dd856c562f08`

## Automated validation

The final isolated Odoo suites passed without failures or errors:

- base: **176/176**; log SHA-256
  `bc03bd88038647d2ecc9dab759c81940ae82116c695af356ca42244b40319e21`;
- integrated base + WuzAPI + UI: **267/267**; log SHA-256
  `dc82b1a20c20ba0a9d1e346af1d801810dc3148845696746a799907a40f448a5`;
- both temporary databases were removed.

Authenticated Chromium 152 validation passed in both minified and `debug=assets`
bundles:

- Contact Center UI: **51/51 tests**, **478/478 assertions**;
- JSON viewer: **4/4 tests**, **15/15 assertions**;
- zero console errors in the scoped runs.

Repository checks cover Black, isort, Flake8, mandatory Pylint/OCA checks, Prettier,
ESLint, Python compilation, JavaScript syntax, XML validation and `git diff --check`.

## Laboratory state and browser smoke

The authenticated application loaded the standalone Contact Center inbox with direct and
group conversations. A real group timeline displayed participant authors, replies,
reactions, media and per-message action controls. Its side panel displayed an
authoritative synchronized roster. The compact fleet indicator settled at **5/5
connected**. The browser console had zero errors. The smoke was read-only: it did not
send a group message or execute a mutation.

The release procedure isolated only the exact test Traefik route, stopped Odoo, switched
the three addon trees atomically, upgraded offline, started and validated Odoo, and
restored the route byte-identically. Internal and public HTTP checks returned 200. The
disposable lab database backup was explicitly waived. Production routes, host and
database were not addressed.

## Evidence

- Odoo suites:
  `scans/raw/20260824-odoo16-contact-center-phase5-3-group-events/test/final3/summary.json`
- Atomic release:
  `scans/raw/20260824-odoo16-contact-center-phase5-3-atomic-release/release/final-apply/summary.json`
- Installed-state validation:
  `scans/raw/20260824-odoo16-contact-center-phase5-3-group-events/validate/final-retry/summary.json`

## Preserved limits

- Group outbound opt-in remains disabled by default.
- No automatic replay of historical mutations or receipts.
- Receipt counts describe observed participants, not total roster membership.
- Group participant management, campaigns, templates and history import remain outside
  the pilot.
- Technical completion in SERVIDOR05 does not authorize a production rollout.
