# Contact Center 1.0 — release candidate status

Date: 2026-09-04 (America/Sao_Paulo).

## Functional candidate

All six Contact Center addons use `16.0.1.0.0`. The candidate includes the optional
Kanban extraction, automatic reopening of resolved conversations, persistent archival,
per-user pin/mute preferences and opening at the first unread message.

Validated backend suites: Base 462/462, WuzAPI 199/199, Kanban 49/49 and integrated
901/901. These are suite executions, not a claim of 1,611 distinct tests. QUnit passed
Base 4/4 and UI 151/151 in both minified and debug asset modes, totaling 2,630
assertions. Authenticated desktop (1920×1080) and mobile (390×844) smoke checks passed
without console errors.

The functional validation tree was
`02e857d8d2de6b4eafa72e4499b82a09cc5f43784f6f3960be124d7ab5a4d724`. Subsequent source
differences consist of README updates and whitespace in the external one-shot release
script; they do not change addon runtime behavior.

## Canonical local evidence

All paths below are relative to the private infrastructure repository; raw artifacts are
intentionally not copied into this addon repository.

- Backend:
  `scans/raw/20260903-odoo16-contact-center-base-crm-greenfield-closeout/release/20260904T175334838664Z/pre-upgrade-test-result.json`.
- QUnit:
  `scans/raw/20260903-odoo16-contact-center-base-crm-greenfield-closeout/release-qunit-resume/20260904T214053960666Z/browser-acceptance/02e857d8d2de6b4eafa72e4499b82a09cc5f43784f6f3960be124d7ab5a4d724/result.json`.
- Public release receipt:
  `scans/raw/20260903-odoo16-contact-center-base-crm-greenfield-closeout/release-finalize/20260904T191827011052Z/qunit-remediation-02e857d8d2de6b4eafa72e4499b82a09cc5f43784f6f3960be124d7ab5a4d724.success.json`.
- Browser smoke: `output/playwright/contact-center-release-20260904/.playwright-cli/`.

The durable `public_release_proven` receipt is authoritative. The subsequent CLI
finalization failure was in lock release, not in the committed addon upgrade. The lock
release failure has since been fixed and regression-tested.

## Remaining release gates

The controlled capacity/resilience drill is **not yet accepted**. Earlier attempts
failed during harness bootstrap, before load: tmpfs fixture placement and the Odoo
Python interpreter were corrected. Its SQL/memory monitor was also corrected before the
next workload. Local tests do not substitute for a successful operational report.

The repository-wide pre-commit checks passed. The release infrastructure's selected
local suites passed 167/167, including 24 drill contract tests. This is separate from
the backend/UI results above.

The existing GitHub prerelease `16.0.20260904.3-rc1` predates this candidate. No new
production-ready tag is implied by this document. Controlled load evidence, final source
identities and coordinated CI remain gates before publication of the new release. The
edge rate-limit gate was deferred explicitly by the operator.

The combined source graph contains 24 addons and 38 internal dependency edges, without
cycles: six Contact Center and eighteen Marketing Center addons. Marketing Center
remains at its previous prerelease; seventeen of its manifests still need a coordinated
greenfield version reset before both repositories can share the new 1.0 baseline. Its
private CI must install `contact_center_kanban` before `contact_center_crm`; both
private dependency pins must then move together to the new immutable candidate. No
Marketing version reset or deployment is implied by the Contact Center results above.

No production change was made by this Contact Center validation. The deployment window
and provider-side-effect-aware rollback procedure are defined in
`operations/production-cutover-rollback.md`.
