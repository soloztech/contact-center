# Meta Phase 6.1 foundation validation

- Date: 2026-08-25
- Target: disposable SERVIDOR05 Odoo 16 laboratory
- Addon: `contact_center_meta` `16.0.1.0.0`
- Result: installed and validated
- Production touched: no
- External Meta state changed: no

## Delivered boundary

- Adapter key `meta`, kept fail-closed for normalization and outbound.
- Company-scoped Meta App and Page-linked authorization registry.
- One operational connection per Messenger Page or linked Instagram asset.
- GET challenge with constant-time verify-token comparison.
- POST HMAC-SHA256 over exact request bytes before JSON decoding.
- Required bounded `Content-Length` and 2 MiB body ceiling.
- Explicit allow-list sanitizer with provider URLs and unknown fields discarded.
- Immutable delivery and atomic-item ledgers with receipt-time Graph-version snapshot.
- OCA `queue_job` fan-out and strict object/mode/asset routing.
- Unknown, inactive, ambiguous and incomplete routes remain auditable without a default
  inbox.
- Delivery and semantic event replay are idempotent.
- App, authorization and routing identities are immutable; credentials may rotate.
- App secrets, verify tokens and Page tokens render as password inputs and remain
  administrator/company scoped.

## Verification

- Repository hooks passed for the complete addon: OCA checks, Black, Prettier, Flake8,
  optional Pylint and mandatory Pylint-Odoo.
- `contact_center_meta`: 24 focused tests.
- Core-only isolated suite: 225 tests, 0 failures, 0 errors.
- Integrated isolated suite: 369 tests, 0 failures, 0 errors.
- Both temporary PostgreSQL databases and temporary data directories were removed.
- Installed-state validator confirmed `queue_job 16.0.3.0.2`,
  `contact_center_base 16.0.1.19.1`, `contact_center_wuzapi 16.0.1.16.0`,
  `contact_center_meta 16.0.1.0.0` and `contact_center_ui 16.0.1.16.0`.
- Runtime read-back found all four Meta models, the Meta Apps menu and exactly one queue
  function on `root.contact_center.meta_webhook`.
- HTTPS login returned 200; an unknown opaque Meta callback returned 404.

Evidence directories:

- source deployment:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/deploy/20260825T071104533174Z`
- isolated tests:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260825T071126359991Z`
- installation:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/upgrade/20260825T071357106423Z`
- installed-state validation:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/validate/20260825T071432528341Z`
- final source-integrity validation:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/deploy/20260825T071846741248Z`

## Deferred gates

- Phase 6.0: create a dedicated Meta Business App and laboratory assets without changing
  the legacy Lead Ads integration; capture anonymized fixtures from a sender without an
  app role.
- Phase 6.2: normalize direct text and echoes into the canonical DTO/core pipeline.
- Phase 6.3: add a private short-lived media locator before retaining CDN URLs; define
  edit event revision semantics before enabling edit replay.
- Phase 6.4+: outbound, policy window, rate limits, subscription management and health.
