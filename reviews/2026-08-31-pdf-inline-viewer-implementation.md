# PDF inline viewer — implementation review

Date: 2026-08-31 Scope: local Contact Center Odoo 16 source only Production touched: no
SERVIDOR05 touched: no

## Outcome

`contact_center_ui` `16.0.1.18.5` adds an inline PDF preview to the existing media
dialog. A ready document is previewable only when its normalized MIME is exactly
`application/pdf` and its authenticated local content URL identifies the same media
binding. Every other document keeps the existing direct-download behavior.

The viewer builds a fixed same-origin PDF.js URL from the validated content route using
`encodeURIComponent` and `#pagemode=none`. It never consumes a provider URL, a data URL,
an attachment ID or arbitrary executable markup. The header keeps a separate download
action and the dialog keeps its existing focus restoration and responsive behavior.

## Security boundary

The backend was intentionally not changed. `/contact_center/media/<id>/content` still
checks authentication, ACLs and record rules before reading the private attachment. It
continues to return every document as `Content-Disposition: attachment`, with Range,
ETag, private cache, CSP and `X-Content-Type-Options: nosniff`. PDF.js obtains the bytes
through its internal request; direct navigation retains the safe download disposition.

This preserves the Phase 3 stored-XSS remediation for HTML and other untrusted document
formats. Non-PDF, malformed MIME, pending media, external/protocol-relative/data/script
URLs, mismatched IDs and a content URL carrying `?download=1` all fail closed.

## Changed surface

- `contact_center_ui/static/src/js/message_content.esm.js`
- `contact_center_ui/static/src/xml/message_content.xml`
- `contact_center_ui/static/src/scss/contact_center_app.scss`
- `contact_center_ui/static/tests/contact_center_model_tests.esm.js`
- `contact_center_ui/__manifest__.py`
- Contact Center laboratory version pins and this documentation

There is no database schema, migration, controller, provider adapter or data change.

## Verification completed

- Prettier 2.7.1: passed;
- ESLint 8.24.0: passed with zero errors/warnings;
- XML parse with lxml: passed;
- SCSS parse/compile with Dart Sass 1.54.9: passed;
- Python compilation for the adjusted release scripts: passed;
- manifest AST/version assertion: passed;
- nested and outer `git diff --check`, including staged diffs: passed.

QUnit cases were added for the PDF gate, encoded PDF.js URL, MIME normalization,
untrusted URLs, ID/state mismatch, download URL sanitization and unchanged image/video
items. They have not yet run inside an Odoo asset bundle.

## Release blocker and next gate

The local tree also contains an unrelated in-progress Meta `16.0.1.11.0` change and new
`meta_webhook_base` dependency, while the deploy tooling correctly pins the released
Meta `16.0.1.10.0`. The atomic Contact Center deploy includes all four addons, so a
dry-run would reject this mixed tree before touching SERVIDOR05. Updating the Meta pin
would incorrectly broaden this PDF release.

The deploy dry-run was executed and stopped during local preflight with the exact
expected/observed mismatch `16.0.1.10.0` / `16.0.1.11.0`; it performed no remote or
database operation and produced no deployment evidence directory.

Before deployment, prepare an isolated candidate that retains Meta `16.0.1.10.0`, then
run the isolated Odoo suites, QUnit in the minified bundle and `debug=assets`, and a
real desktop/mobile PDF canary. Deployment, restart and module upgrade require separate
authorization. Rollback is a UI source release restoring the document card to its prior
download link; no data restore is needed.
