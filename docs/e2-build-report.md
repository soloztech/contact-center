# E2 builder handoff — advisory, uncommitted

This report records source implementation and local evidence. It does not approve
publication or replace the coordinator's independent native, browser and Claude
inspection. No commit, push, publication, production operation or sibling checkout edit
was performed.

## Source binding

- Checkout:
  `/home/lucaszotelli/.local/state/claudex-loop/marketing-platform-delivery-20261008/contact-center-e2`.
- Actual prebuild HEAD and unchanged final HEAD:
  `2af6fe109dea78fe3ecddb7c518c44d2ba7b5591`.
- Frozen plan SHA256:
  `8fee817c42cec2c5469805d877c3c305dd70604227f762373538c622a8fcb1ef`.
- Read `E2-PLAN.md`, `E2-BUILD-NOTES.md`, `E2-BUILD-BASE.json` from the task's parent;
  source paths were resolved inside this checkout.
- Recorded Marketing peer: `dad9e54b423a8953692c24ac327e8266c991dd45`.
- Recorded Soloz peer: `26478a135433b4762d42620733bb1b6d3cc40d2e`.
- Versions: Base `16.0.1.11.4`, UI `16.0.1.11.3`, CRM `16.0.1.5.1`.
- Ownership: Base, UI, CRM and docs only. E1 provider changes remain untouched. A
  changed E1 or peer requires the coordinator's rebase and repeated relevant proofs.

## Implemented behavior

The backend publishes exactly scoped name/aliases metadata and aggregates resolver
convergence and identity merge into one legacy event per affected channel. Merge uses
private process tokens and the existing name-notification suppression. The four strict
metadata scopes bypass systray recount; mixed, missing-version, unknown and legacy
events use conservative refresh.

`_conversation_list_window` centralizes authorized native search, preference ordering,
cursor and total. `reconcile_conversations` validates exact positive integer lists,
recomputes the authorized window, compares ordered IDs, and serializes only the affected
intersection. A mismatch returns no IDs or conversation payload. Native tests check
ACL/company/filter/pin/order/cursor/total parity and guard unread work outside that
intersection. Search and unread-only clients use ordinary refresh.

`list_v1` is an explicit opt-in with a dedicated serializer and prefetch. Default full
DTO callers, including bulk read, retain their contract. Compact identity/group names
use the same native resolution as full detail, including linked partners and group
profile names. Rows contain only light fields, native message preview, seven group
fields and two management booleans calculated per account. No phone, company links,
provider diagnostics or send authorization enters compact rows.

The store keeps a bounded metadata lane, validates delta rows/envelope, checks request,
filter, context and conversation generations, and performs one ordinary fallback in the
same cycle. Strict metadata refreshes selected detail without a timeline read. Messages,
identities, reactions, media, unknown and mixed events retain the existing full path;
the avatar lane and 30-second/5-minute repair remain active.

Selected full detail has a separate lifetime and loading/error/ready/denied state.
Compact clicks read detail and timeline concurrently. Directed, remembered, restored,
chat and start paths seed the full item from that opening operation. Reopening a row
gets fresh authorization. A first transient failure has visible retry and one bounded
automatic retry without a second timeline request. Current-selection revalidation
retains the last authorized snapshot, capabilities and mounted panels under R3; current
AccessError revokes them. Selection/context changes and nested account or identity ID
changes invalidate detail. Mutation answers enforce selection/context and
newer-applied-answer guards while accepting the own-event-before-answer case. Light row
updates do not reset attribution, company linker, drafts or CRM panel keys.

The registered optional `contact_center_ui.shared_reads` service shares only identical
inflight `get_connection_health` transports within a context and causal generation. Each
consumer has independent cancellation, owner deadline and cloned JSON results. The last
consumer aborts the transport. Unsupported RPC methods are rejected before dispatch;
nonplain inputs to allowed reads bypass sharing. Context is checked on admission and
delivery. Central health events/reconnect and settlement of the real
`check_connection_health` mutation establish barriers before subsequent reads. Stores
work with direct ORM when the optional service is absent. There is no settled cache,
cross-tab protocol, IP identity or module-global context pool.

## Local evidence

Run from this checkout:

```bash
node contact_center_ui/static/tests/e2_standalone.mjs
python3 contact_center_base/tests/test_ad_preview_transport_standalone.py
python3 contact_center_base/tests/test_transcription_adapters_standalone.py
git diff --check
```

- Repository logic runner: **41 tests, 329 assertions, zero failures**. It executes
  actual model/store/refresh/pool/systray source and repository QUnit logic cases in
  Node. It does not execute OWL rendering or native QUnit.
- Existing Base standalone suites: **10 + 16 tests, OK**.
- Supplementary temporary Node harness exercised **97 existing inbox cases, 401
  assertions, zero failures**, with one DOM case excluded. This is secondary logic
  evidence only; the durable command above is the repository reproduction.
- Prettier completed on changed JS/XML/docs; ESLint exited 0 with **0 errors, 54
  warnings** (complexity/import ordering/JSDoc and existing diagnostic fixtures).
- Black check, isort check and flake8 passed for all **13 changed Python files**.
  Black's multifile process-pool invocation stalled, so it was canceled; a bounded
  invocation also timed out. All single-file checks subsequently passed without changing
  permissions. `BLACK_CACHE_DIR=/tmp/e2-black-cache` kept cache writes local.
- `git diff --check` passed.
- The full pre-commit orchestration could not run because `python3 -m pre_commit`
  reports no installed module. Relevant cached format/lint executables were used; the
  full hook chain is not claimed to have passed.

Cached hook executables used, with changed files passed as explicit arguments:

```text
node /home/lucaszotelli/.cache/pre-commit/repo67jr1xi0/node_env-16.17.0/lib/node_modules/prettier/bin-prettier.js --write <changed JS/XML/docs>
node /home/lucaszotelli/.cache/pre-commit/repo6k4wk9lf/node_env-16.17.0/lib/node_modules/eslint/bin/eslint.js <changed JS>
/home/lucaszotelli/.cache/pre-commit/repoegduqs2a/py_env-python3.10/bin/black --check <one changed Python file>
/home/lucaszotelli/.cache/pre-commit/repod2sp5awy/py_env-python3.10/bin/isort --check-only --settings=. <changed Python except __init__.py>
/home/lucaszotelli/.cache/pre-commit/repodqig9u76/py_env-python3.10/bin/flake8 <changed Python>
```

Agreed static proof command and output:

```bash
python3 /home/lucaszotelli/.local/state/claudex-loop/marketing-platform-delivery-20261008/e2_static_proof.py /home/lucaszotelli/.local/state/claudex-loop/marketing-platform-delivery-20261008/contact-center-e2
```

```text
STATIC_E2_OK 43 Native and real-browser verification is still mandatory and coordinator-owned.
```

## Coordinator-owned verification still required

These are exact source-bound commands for the coordinator's prepared synthetic runtime;
none was executed by the builder. They write outside this checkout and use the
coordinator's PostgreSQL/runtime. They require the recorded peer SHAs and existing
isolated databases; database preparation remains the coordinator's responsibility.

```bash
E2_TASK=/home/lucaszotelli/.local/state/claudex-loop/marketing-platform-delivery-20261008
python3 "$E2_TASK/qa.py" tests --modules contact_center_base,contact_center_ui --contact-suffix=-e2 --marketing-suffix=-e2 --db-suffix=e2_base_ui --name E2-base-ui
python3 "$E2_TASK/qa.py" tests --modules contact_center_base --tags '/contact_center_base:TestConversationDelta,/contact_center_base:TestConversationListProjection,/contact_center_base:TestIdentityNameConvergence,/contact_center_base:TestContactCenterIdentityAvatar' --contact-suffix=-e2 --marketing-suffix=-e2 --db-suffix=e2_base_ui --name E2-contracts
python3 "$E2_TASK/qa.py" tests --kanban --contact-suffix=-e2 --marketing-suffix=-e2 --db-suffix=e2_composed --name E2-composition
python3 "$E2_TASK/qa.py" upgrade --kanban --contact-suffix=-e2 --marketing-suffix=-e2 --db-suffix=e2_upgrade --name E2-native-upgrade
```

The upgrade database must first contain the exact E1 baseline and the installed
dependent closure. The composed upgrade command uses the runner's full module list; its
installation state, dry-run rollback and postflight are separate required proofs. The
50-row SQL/byte benchmark is in
`TestConversationListProjection.test_synthetic_batch_50_benchmark_and_parity`; run that
native class and record its observed `E2_SYNTHETIC_LIST_BENCHMARK` output. No production
byte/SQL percentage has been measured or claimed.

For existing browser_qa.py, the coordinator must serve the final source on its hardcoded
loopback port and default synthetic database, after upgrading that database:

```bash
python3 "$E2_TASK/qa.py" serve --contact-suffix=-e2 --marketing-suffix=-e2 --name E2-browser-server
python3 "$E2_TASK/browser_qa.py" --name E2-qunit-contact-center --filter contact_center
python3 "$E2_TASK/browser_qa.py" --name E2-qunit-marketing-center --filter marketing_center
```

Each browser command runs normal and debug assets. Require nonzero counts and zero
failures for all Contact UI/CRM, unchanged Sale conversations, CRM Journey and Marketing
Visitor Journey suites. New real component tests cover compact row menus,
pending/error/retry/ready rendering, chat/composer gating, CRM opening with one customer
records read, stable mounting through row refresh/revalidation and actual denial.
Desktop/mobile open/filter/pin/read/lead/Journey scenarios remain mandatory in the
coordinator's single owned Playwright session. No browser was opened by the builder.

## Dispositions, blocked and denied actions

No action was denied by automatic approval review or sandbox during source work.
Native/Docker/upgrade/browser/production operations and writes to parent proof paths
were not attempted, because the host assigned them to the coordinator. No permission
escalation or sandbox change was requested. The unavailable full pre-commit runner and
the resolved Black process-pool stalls are recorded above.

One contract ambiguity was reported during implementation: the early acceptance text
says a change of `has_more` itself must force fallback, but reconcile receives no prior
`has_more`, total or cursor to compare. Later binding F05 instead requires authoritative
total/cursor/has-more on a successful unchanged window, including changes outside the
window. The implementation follows F05: equal ordered authorized IDs can return new
metadata; changed IDs fall back. A literal prior-has-more comparison would require an
additional RPC contract and remains a proposed future deviation rather than an
unannounced signature redesign. No other functional deviation is proposed.

## Complete changed-file inventory

All paths below are relative to this checkout; `M` is tracked modified, `A` is new and
untracked. New services and every new native/QUnit test must be included in the fresh
inspection, not just the tracked diff stat.

```text
M contact_center_base/__manifest__.py
M contact_center_base/models/application.py
M contact_center_base/models/identity.py
M contact_center_base/models/ui_api.py
M contact_center_base/services/tokens.py
M contact_center_base/tests/__init__.py
A contact_center_base/tests/conversation_list_common.py
A contact_center_base/tests/test_conversation_delta.py
M contact_center_base/tests/test_identity_avatar.py
M contact_center_base/tests/test_identity_name.py
A contact_center_base/tests/test_list_projection.py
M contact_center_crm/__manifest__.py
M contact_center_crm/static/src/js/contact_center_app_crm.esm.js
M contact_center_crm/static/src/js/crm_panel.esm.js
M contact_center_crm/static/src/xml/contact_center_app_crm.xml
A contact_center_crm/static/tests/readiness_tests.esm.js
M contact_center_ui/__manifest__.py
M contact_center_ui/static/src/js/contact_center_app.esm.js
M contact_center_ui/static/src/js/contact_center_messaging.esm.js
M contact_center_ui/static/src/js/contact_center_model.esm.js
A contact_center_ui/static/src/js/contact_center_shared_reads.esm.js
M contact_center_ui/static/src/js/contact_center_store.esm.js
M contact_center_ui/static/src/js/contact_center_systray.esm.js
M contact_center_ui/static/src/js/contact_panel.esm.js
M contact_center_ui/static/src/js/conversation_timeline.esm.js
M contact_center_ui/static/src/js/message_composer.esm.js
M contact_center_ui/static/src/xml/contact_center_app.xml
M contact_center_ui/static/src/xml/contact_center_messaging.xml
M contact_center_ui/static/tests/contact_center_model_tests.esm.js
A contact_center_ui/static/tests/conversation_delta_tests.esm.js
A contact_center_ui/static/tests/detail_component_tests.esm.js
A contact_center_ui/static/tests/e2_standalone.mjs
A contact_center_ui/static/tests/e2_test_helpers.esm.js
M contact_center_ui/static/tests/history_retention_tests.esm.js
M contact_center_ui/static/tests/inbox_state_tests.esm.js
M contact_center_ui/static/tests/refresh_tests.esm.js
A contact_center_ui/static/tests/selected_detail_tests.esm.js
A contact_center_ui/static/tests/shared_reads_tests.esm.js
M contact_center_ui/static/tests/systray_tests.esm.js
A docs/api-v1.md
M docs/architecture.md
A docs/e2-build-report.md
A docs/event-taxonomy.md
```
