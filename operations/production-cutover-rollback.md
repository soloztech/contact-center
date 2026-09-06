# Contact Center 16.0 — cutover and rollback

## Scope and release unit

The production release is one immutable unit. The following addons must use the same
`16.0.1.0.0` source tag:

- `contact_center_base`
- `contact_center_wuzapi`
- `contact_center_meta`
- `contact_center_ui`
- optional service pipeline plugin `contact_center_kanban`
- `contact_center_crm`, only together with `contact_center_kanban`

`queue_job` and the technical Meta foundations distributed by the Marketing Center
repository are pinned external prerequisites. No source directory is edited on the
production host during the window.

The first production baseline is greenfield. Production must not contain an older
Contact Center schema. The existing SERVIDOR05 laboratory is different: its former
Base-owned pipeline XML IDs must be removed by the laboratory release procedure while
Base, Kanban and CRM are upgraded in the same stopped Odoo invocation.

## Window status after the 2026-09-05 audit

The former **2026-09-05, 07:00–10:00 BRT** window is cancelled. Its prerequisite
controlled drill had stopped after 14 of 4,800 webhooks returned HTTP 400, before the
recovery scenarios. The required acceptance was not available before 06:00 BRT, and the
window has elapsed. This document does not authorize a later deploy.

Before a new window, record its date, operator, canary deadline, go/no-go deadline and
rollback reserve against the exact coordinated source revisions and updated
[audit evidence](../reviews/2026-09-05-greenfield-audit.md). Require all acceptance
gates to pass at least one hour before the window; otherwise cancel it. Retain 24 hours
of heightened observation after go-live.

The explicit product decision for this first window is to defer edge rate limiting. That
waiver does not remove ingress telemetry: webhook request rate, response code, latency,
body rejection and queue age must be observed from the first request. A sustained
overload is a rollback/freeze signal, not a reason to improvise application changes
inside the window.

## Roles

- Change operator: owns deployment commands and evidence capture.
- Business validator: validates one direct and one group WhatsApp conversation.
- Incident lead: makes the go/no-go call and owns provider callback restoration.

One person may hold more than one role, but the change operator must state each role in
the release evidence before T0.

## T-24 h to T-1 h

1. Freeze the release commit and create signed/private tag `16.0.1.0.0-rc1`.
2. Record the exact commit and SHA-256 tree of both private repositories.
3. Prove CI, isolated Odoo suites, QUnit and authenticated desktop/mobile smoke against
   that exact tree. Dispatch each repository's test workflow on its release revision
   with `peer_ref` set to the other repository's full commit SHA. Record both workflow
   run IDs; the default PR baseline alone does not validate the final coordinated pair.
4. Prove the controlled provider-failure and 20-inbox capacity drills against that exact
   tree; no unexplained `dead`, `uncertain` or orphaned queue record may remain.
5. Inventory production prerequisites, disk, PostgreSQL health, filestore, queue runner,
   workers, cron, bus/websocket routing and provider credentials without exposing
   secrets in evidence.
6. Export the intended account/connection/team mapping. Every provider asset must map to
   exactly one logical inbox and one active primary connection. For inboxes that import
   messages sent from another device, configure the account's **Technical Author**
   explicitly and validate one such message plus its receipt. Provider readiness alone
   does not configure that optional author. Without it, external-device echoes remain
   unsupported and receipts may await their message; never attribute them automatically
   to an operator who did not send them.
7. Disable automatic module updates and unrelated deployments for the entire window.
8. Announce a short agent maintenance period. Browser sessions may remain open but are
   not considered valid until reloaded after the release.

## Pre-boundary backup

Immediately before stopping Odoo:

1. Quiesce scheduled jobs and stop the Odoo HTTP/cron/job-runner processes.
2. Verify there is no active Contact Center dispatch. On a greenfield install this count
   must be zero.
3. Take a transactionally consistent PostgreSQL backup and a matching filestore
   snapshot. Record size, SHA-256, start/end time and restore target.
4. Export current provider callback/subscription configuration and the exact previous
   application image/source identity.
5. Perform a read-only backup verification. An unreadable or unpaired DB/filestore
   snapshot cancels the window.

The DB and filestore form one restore unit. Restoring only one is forbidden.

## Deployment sequence

1. Keep provider ingress pointed away from the new Odoo routes and keep outbound
   disabled.
2. Mount the immutable Contact Center and Marketing Center release trees.
3. Start a no-HTTP Odoo process and install/update the complete graph in one invocation.
   If CRM is installed, the invocation includes Base, Kanban and CRM together.
4. Validate the installed schema, module versions, ACL/rules, menu graph, queue channels
   and absence of Base-owned pipeline/case/follow-up models or XML IDs.
5. Start Odoo with ingress and outbound still disabled. Require healthy internal HTTP,
   bus, cron and JobRunner.
6. Configure all logical inboxes, connections, teams and owners. Verify each provider
   identity individually; a fleet aggregate alone is insufficient.
7. Enable one low-risk direct WhatsApp inbox as canary. Switch only its authenticated
   webhook and then its outbound gate.
8. Prove inbound text/media, outbound text, delivery status, realtime update, first
   unread anchor, resolve/reopen, archive persistence, pin/mute and access isolation.
9. Enable the remaining inboxes in batches of five, waiting five minutes and checking
   the gates below after each batch.
10. Enable Meta assets only after their Page/Instagram subscription and managed health
    checks are independently green.

## Go/no-go gates

All gates are fail-closed:

- zero cross-team or cross-company access violations;
- zero duplicate provider dispatches;
- zero unowned or ambiguously routed provider assets;
- zero new `uncertain` outbound commands;
- zero permanently failed text messages;
- oldest eligible inbox/outbox job below 120 seconds after each batch;
- webhook 5xx below 0.5% over five minutes, excluding deliberate invalid-signature
  probes;
- accepted webhook p95 no worse than the validated capacity baseline by more than 50%;
- realtime delivery visible to an idle second browser within 10 seconds at p95;
- process memory, PostgreSQL connections and queue depth remain below the capacity
  drill's recorded safe ceiling;
- canary media is downloadable from the authenticated Odoo route and no private media
  URL or secret appears in bus/log/browser console.

Any security failure, duplicate outbound or irreconcilable provider boundary triggers
immediate rollback. Two consecutive five-minute breaches of a latency/queue resource
gate also trigger rollback.

## Rollback modes

### A. Before any provider side effect

Stop the new Odoo processes, restore the previous code/image plus the paired database
and filestore snapshot, restore prior routes and start the old service. Validate HTTP,
cron and queue health. This is a conventional full rollback.

### B. Ingress switched, outbound still disabled

Disable new ingress, restore provider callbacks/subscriptions, wait for the new ingress
high-water mark to remain stable, archive the bounded transition evidence, then perform
mode A. Replayed provider deliveries are safe only through the normal dedupe contract.

### C. Any outbound may have crossed the provider boundary

Do **not** blindly restore the pre-cutover database: that would erase knowledge of
messages that the provider may already have delivered and could duplicate them later.

1. Disable outbound for every account and stop queue workers.
2. Preserve the new database and filestore as incident evidence.
3. Classify each command as pre-boundary, confirmed, failed or uncertain using its
   immutable request ID and provider evidence.
4. Restore ingress to the previous system only after it cannot dispatch the same
   requests.
5. Reconcile confirmed provider side effects into the chosen surviving database.
6. Resume outbound only when the uncertain set is empty or explicitly accepted by the
   incident lead.

Mode C is a forward recovery with provider reconciliation, not a database rewind.

## Post-cutover

- Keep the release tag, complete evidence, backups and previous image immutable for at
  least seven days.
- Review webhook rate/latency and queue age after 1 h, 4 h and 24 h.
- Decide the edge rate-limit policy from measured production percentiles; the waiver is
  valid only for this first observation period.
- Close the change only after the 24-hour review records zero unexplained duplicate,
  access, routing or queue-recovery incident.

## Rich messaging candidate — 2026-09-06

The local candidate additionally requires the new
`contact.center.message.binding.structured_content_json` field and refreshed UI assets.
Update the base and provider/UI addons together. Meta attachment upload uses the shared
`meta_api_base` multipart client from the same approved Marketing tree.

Local evidence is recorded in `reviews/2026-09-06-rich-messaging.md`: 1,719 tests on a
clean install of all 24 addons and a successful registry update replay. The UI gate now
requires all 166 Contact Center tests; filtering must include the new structured message
suite. This is local validation and does not replace the pilot/provider checks.

For the pilot, verify one image and one supported document on each enabled Meta
transport, and reply/list/contact/location on WhatsApp. Confirm provider permissions and
the response window. A Meta upload retry can leave an unattached media object; an
uncertain final send must remain fenced under rollback mode C. Multiple selected files
are independent messages, not a native album.

The subsequent
[channel capability revision](../reviews/2026-09-06-channel-capabilities.md) replaces
the outgoing card type list with `outbound_structured_content` specifications. Its
evidence supersedes the above test counts for release acceptance. On an existing
laboratory database, run the normal connection capability refresh after updating the
complete source pair; the old capability key intentionally does not enable cards. Verify
the actual advertised action types and limits for each pilot connection before sending.
A rendered card received from a provider is not evidence that outbound cards are
supported by that adapter.
