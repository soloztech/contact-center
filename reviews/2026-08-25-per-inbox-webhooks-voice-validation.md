# Per-inbox controls, WuzAPI webhooks and voice recording validation

- Date: 2026-08-25
- Target: disposable SERVIDOR05 Odoo 16 laboratory
- Production touched: no
- Database backup: waived by explicit instruction for this development environment

## Delivered scope

- optional agent signature per account/inbox, rendered only at the provider boundary;
- independent account switches for receiving and sending group messages;
- versioned WuzAPI webhook-event selection with asynchronous apply/read-back;
- scoped inline rename for a persistent `mail.guest`;
- read-only analysis of Meta Click-to-WhatsApp attribution payloads;
- capability-driven browser audio recording and WuzAPI audio dispatch.

## Automated validation

- `contact_center_base`: **208/208**, zero failures and zero errors;
- integrated base + WuzAPI + UI install: **320/320**, zero failures and zero errors;
- UI QUnit, minified assets: **61/61**, 545/545 assertions;
- UI QUnit, `debug=assets`: **61/61**, 545/545 assertions;
- Black: 68 Python files unchanged;
- isort and Flake8: clean.

The isolated Odoo test databases were removed after each run. Test evidence:

- `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260825T024130267481Z/`
- `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/validate/20260825T024314395469Z/`

## Real-browser and provider checks

Authenticated Chromium 152 loaded the standalone Contact Center with five of five
connections healthy and zero application console errors. It confirmed the per-account
signature/group controls, the guest-name pencil and edit mode, the recorder control and
the WuzAPI event selector. A real `Check WuzAPI` job completed with `In Sync` and a
matching callback URL on the Comercial01 laboratory connection.

The browser recorded a synthetic 440 Hz stream using its native `MediaRecorder`. The
result was a real fragmented MP4/M4A file, uploaded through the production UI route,
validated by the server and displayed as a 21-second audio preview. The attachment was
then removed before invoking Send; therefore no external message was requested. No
screenshots with real conversation data were retained in the repository.

The real-browser pass caught one Odoo 16 Sass incompatibility in `min(24rem, 100%)`.
Replacing it with `width: 100%` plus `max-width: 24rem` restored backend asset
compilation. The final source-only deployment completed without rollback with tree hash
`8f10cddce7bf3137362704478c82ad2e2c192d37726cce60c90f8e6c2778a93d`:

- `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/deploy/20260825T024707863919Z/`

Installed versions after deployment:

- OCA `queue_job` `16.0.3.0.2`;
- `contact_center_base` `16.0.1.18.0`;
- `contact_center_wuzapi` `16.0.1.15.0`;
- `contact_center_ui` `16.0.1.15.0`.

## Meta attribution boundary

The commercial-account payload review is evidence and architecture research, not a CRM
feature claim. It is recorded in `research/meta-click-to-whatsapp-attribution.md`. The
future implementation must add a provider-neutral DTO projection and an immutable
conversation-scoped touchpoint ledger before any CRM or tag projection.
