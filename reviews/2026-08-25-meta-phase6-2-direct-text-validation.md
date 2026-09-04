# Meta Phase 6.2 direct-text validation

- Date: 2026-08-25
- Target: disposable SERVIDOR05 Odoo 16 laboratory
- Addons: `contact_center_base 16.0.1.19.2`, `contact_center_wuzapi 16.0.1.16.1`,
  `contact_center_meta 16.0.1.1.0`
- Result: installed and validated
- Backup: not created, by laboratory policy
- Production touched: no
- External Meta state changed: no

## Delivered boundary

- Direct Messenger and Page-linked Instagram text normalization.
- Strict sender/recipient/asset/`is_echo` orientation before identity resolution.
- Account-scoped `meta.messenger.psid` and `meta.instagram.igsid` identities.
- Canonical guest, identity, channel, message and binding reuse.
- Provider echoes through the existing `external_device` path; no direct provider call.
- Reply correlation by `mid`; an out-of-order reply retries until its target exists.
- Bounded Meta referral conversion to `AttributionDTO` and immutable touchpoint ledger.
- Attribution-only capture when message media is still unsupported, without partial
  message projection.
- Explicit adapter opt-in before direct-avatar jobs are scheduled; Meta remains off.
- Fail-closed media, quick reply, story reply, self-message, delete, reaction, receipt,
  outbound and Graph-health boundaries.

## Verification

- Complete repository pre-commit selection passed: OCA checks, generated README, Black,
  Prettier, Flake8, Pylint and Pylint-Odoo.
- Core-only isolated suite: 225 tests, 0 failures, 0 errors.
- Integrated isolated suite: 382 tests, 0 failures, 0 errors.
- Temporary PostgreSQL databases and data directories were removed.
- Installed-state validation confirmed OCA `queue_job 16.0.3.0.2`, the three versions
  above and `contact_center_ui 16.0.1.16.0`.
- HTTPS login returned 200 after upgrade.

Evidence directories:

- source deployment:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/deploy/20260825T105856779190Z`
- isolated tests:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260825T105923669045Z`
- upgrade:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/upgrade/20260825T110058585086Z`
- installed-state validation:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/validate/20260825T110653539269Z`
- final source-integrity validation:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/deploy/20260825T110352394275Z`

The final documentation/source restart needed 73 seconds under public laboratory
traffic. HTTPS was already returning 200 while two immediate read-only RPC snapshots
timed out. Containers remained running with zero restarts, low CPU, three database
sessions and no database mutation; the next canonical installed-state validation passed
in 5.6 seconds without another restart.

## Remaining gates

- Complete Phase 6.0 with a dedicated laboratory Meta App, Page and professional
  Instagram account; collect anonymized events from a sender without an App role.
- Requeue any Phase 6.1 Meta text Inbox deliberately if one exists; already-routed
  delivery items are immutable and are not replayed automatically on upgrade.
- Phase 6.3 adds private media locators, profile enrichment and inbound state events.
- Phase 6.4 adds policy-windowed outbound. Until then all sending capabilities remain
  false and no Meta transport can be invoked from the composer.
