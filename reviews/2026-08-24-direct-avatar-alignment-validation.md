# Direct avatar and message alignment — validation

Date: 2026-08-24 Environment: disposable SERVIDOR05 laboratory,
`odoo16-teste.soloz.com.br` Result: **implemented and validated**

## Delivered

- Direct avatars are account-scoped fields on the canonical direct channel binding.
- A direct WuzAPI `Picture` event normalizes to `identity.avatar.changed` and only
  invalidates an existing binding. Unknown profile events create no identity, guest,
  channel or message.
- `queue_job` performs the provider fetch. The WuzAPI adapter accepts validated PN, LID,
  `c.us` or normalized phone targets and reuses the group avatar SSRF, redirect,
  response-size and MIME protections.
- The core stores a bounded private attachment and exposes only
  `/contact_center/conversation/<channel_id>/avatar` after membership authorization.
  Provider URLs and attachment IDs never enter UiDTO or the bus.
- List and contact panel render `identity.avatar_url`; group metadata remains
  authoritative for group conversations.
- Message-run avatars and continuation spacers share one CSS size (`30px` on desktop),
  removing the previous `7.2px` horizontal drift.

## Laboratory convergence

- Direct bindings processed: **113**.
- Avatar state: **91 ready**, **3 absent**, **19 unavailable**.
- Private avatar attachments: **91**.
- Technical avatar errors: **0**.
- Queue jobs: **113 done**, none pending or failed.

## Validation

- Pre-commit: Black, isort, Flake8, OCA manifest checks, pylint-odoo, Prettier, ESLint,
  XML checks and `git diff --check` passed.
- Isolated Odoo base suite: **181/181**, no failures or errors; log SHA-256
  `29039e49421f9a3eb3c4dd7c154d29edc90766de11ff4f0dc7e9c9be30fa5d8b`.
- Integrated base + WuzAPI + UI suite: **277/277**, no failures or errors; log SHA-256
  `0e4f385a2a65d3d82324b53a0d5f7c56638caa81f87d2c090f3be229082f2967`.
- Authenticated QUnit: **53/53 tests**, **495/495 assertions**, in minified and
  `debug=assets` bundles.
- A clean authenticated Chromium session loaded a direct avatar in the list and panel
  from the same local URL, with a nonzero natural width and no console error.
- Direct timeline: 21 inbound messages, 12 continuations, every bubble at `x=411`;
  avatar and spacer both `30px`.
- Group timeline: 36 inbound messages, 9 continuations, every bubble at `x=411`; avatar
  and spacer both `30px`.

The first isolated run exposed one integration defect: an agent serializer attempted to
read the admin-only attachment field. The final implementation keeps that field
restricted and uses a scoped technical read only after channel authorization, exposing
only the local URL. The complete final suites above passed afterward.

## Release

- OCA `queue_job`: `16.0.3.0.2`
- `contact_center_base`: `16.0.1.16.0`
- `contact_center_wuzapi`: `16.0.1.13.0`
- `contact_center_ui`: `16.0.1.14.0`
- Runtime tree hash: `6570fffb30128a3a3a3a2594f75aa1cff025767eed67d14140371be93d405438`

The atomic release isolated only the exact test route, upgraded offline, restored the
Traefik route byte-identically and returned internal and public HTTP 200. The disposable
lab backup was waived. Production was not accessed or changed.
