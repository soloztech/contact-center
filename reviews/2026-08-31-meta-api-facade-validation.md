# Meta API shared-runtime facade validation — 2026-08-31

## Outcome

`contact_center_meta` `16.0.1.10.0` now consumes the provider-neutral `meta_api_base`
`16.0.1.0.0` transport without creating a dependency on `marketing_center`.

The local facade preserves the established public functions and translates the neutral
Meta error hierarchy at the domain boundary. Only bounded diagnostic attributes are
copied; provider payloads and credentials are not retained in the translated errors. The
Contact Center webhook implementation remains local until its version-validation
contract and the future shared delivery registry are migrated separately.

## Validation

- Black, isort, flake8, compileall and `git diff --check`: passed.
- Isolated Contact Center base suite: 355 tests, 0 failures, 0 errors.
- Isolated integrated suite: 701 tests, 0 failures, 0 errors.
- Temporary test databases: removed.
- Installed module: `contact_center_meta` `16.0.1.10.0`.
- Private and public laboratory login probes: HTTP 200.
- Exact SERVIDOR04 test route restored with SHA-256
  `2b06d3c38cbac9df1d320a5ec8e1c3dda24c80883eafe059307020c572b34383`.
- Production touched: no.

## Evidence

- Shared runtime release:
  `scans/raw/20260830-odoo16-integration-core-meta-api-base/release/20260831T031224140828Z`
- Contact Center source deploy:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/deploy/20260831T032223540631Z`
- Isolated suites:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260831T032255374569Z`
- Atomic module release:
  `scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260831T032554278909Z`

The SERVIDOR05 preflight initially found less than 1.5 GB free. Only unused Docker
builder cache was pruned; no image, volume, database or application record was removed.
