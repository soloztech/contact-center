# Provider coverage, reply scope and timeline validation

Date: 2026-08-30

Target: disposable neutralized Odoo 16 laboratory on SERVIDOR05. Production and its
routes were not accessed or changed. The operator waived a database backup for this
development environment.

## Implemented

- Added exclusive forward timeline pagination, contiguous high-water tracking, aggregate
  bounded recovery, fresh-head reanchoring and bounded silent retry.
- Added WuzAPI WebP sticker and bounded human-wrapper support, including
  `associatedChildMessage` evidence without treating it as a reply.
- Restricted WuzAPI webhook persistence to the one media key selected by the adapter and
  removed unsupported sender-key, app-state and encrypted protocol subtrees.
- Added Page-linked Meta direct-text replies through the authorized Page route and
  top-level `reply_to.mid`.
- Anchored every direct reply command at dispatch to the persisted message-binding
  relation, rather than trusting two mutually consistent DTO copies.

Events that intentionally have no product contract remain authenticated, sanitized and
durably classified as `unsupported`. They do not create a guest, conversation or fake
message.

## Validation

- Static gates: Black, isort, Flake8, mandatory pylint-odoo, OCA module checks,
  Prettier, ESLint, `node --check`, Python compilation and `git diff --check` passed.
- Isolated Odoo core: 353 tests, 0 failures, 0 errors.
- Integrated Odoo addons: 674 tests, 0 failures, 0 errors.
- Contact Center UI QUnit: 80 tests and 700 assertions passed in both minified and
  `debug=assets` modes; no console errors or warnings.
- Base JSON viewer QUnit: 4 tests and 15 assertions passed in both modes; no console
  errors or warnings.
- Authenticated application smoke: 487 conversations loaded, realtime active, five
  WuzAPI connections healthy and the two intentionally gated Meta connections visible;
  no console errors or warnings.

The first browser navigation omitted the QUnit `filter` parameter and therefore also ran
unrelated Odoo web tests. One core MockServer test rejected the database neutralization
banner. That run is not a Contact Center result. Both canonical runs used the exact
`contact_center_ui` filter and passed completely.

## Release evidence

- Module tree: `3236382e2d5b333b7f95f0c086b0c02aeaab4aea0be43ecf4b88565881a9bb98`
- Atomic release:
  `scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260830T061716589446Z`
- Isolated suites:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260830T061817057015Z`
- Installed-state validation:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/validate/20260830T062110843001Z`
- Browser artifacts: `output/playwright/contact-center-20260830-final/.playwright-cli`

## Residual boundary

The forward cursor uses `mail.message.id`, which is allocation-ordered rather than
commit-ordered. The final fresh-head read heals normal short Odoo transactions inside
the newest-page window. Absolute protection against an unusually long transaction that
commits an older ID after the cursor advanced would require a dedicated commit-order
projection cursor. This is a post-pilot architecture hardening item, not an observed
release failure.

Operational WuzAPI outage/capacity drills and external Meta permission/App Review gates
remain pilot acceptance work; they are not missing code in this release.
