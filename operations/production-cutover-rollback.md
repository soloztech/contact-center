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

## Defined window

- Change window: **Saturday 2026-09-05, 07:00–10:00 BRT**.
- Deployment and canary target: complete by 08:00 BRT.
- Mandatory go/no-go decision: no later than 08:30 BRT.
- Rollback reserve: 08:30–10:00 BRT.
- Heightened observation: 24 hours after go-live.

If the immutable RC, controlled resilience drill or capacity report is not green by
06:00 BRT, the window is automatically cancelled. A cancelled window makes no production
change.

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
   that exact tree.
4. Prove the controlled provider-failure and 20-inbox capacity drills against that exact
   tree; no unexplained `dead`, `uncertain` or orphaned queue record may remain.
5. Inventory production prerequisites, disk, PostgreSQL health, filestore, queue runner,
   workers, cron, bus/websocket routing and provider credentials without exposing
   secrets in evidence.
6. Export the intended account/connection/team mapping. Every provider asset must map to
   exactly one logical inbox and one active primary connection.
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
