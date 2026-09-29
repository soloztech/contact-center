# WuzAPI group ingress: acquire the account lock before the connection

Two concurrent group callbacks used to hold account and connection KEY SHARE locks
before the retention check requested account FOR UPDATE. Each callback could then wait
for the other's shared account lock. A related cycle involved an operational reader
(send/mark-read admission): it holds account SHARE and waits for connection UPDATE,
while group ingress holds connection KEY SHARE and waits for account UPDATE.

Group ingress now requests account UPDATE in the existing admission helper, before
connection KEY SHARE. Subsequent retention calls reuse the account lock. The helper does
not rewrite the account or advance a revision. Other callers and direct/lifecycle WuzAPI
callbacks retain the shared default. Classification lives in
`_conversation_ingress_response`, after the outer controller authenticates and
bounds/parses the body. Authentication and connection eligibility are still checked
after the locks.

The regression tests compare the raw and sanitized retention route for all message group
fixtures, GroupInfo, JoinedGroup, Picture, a synthesized group ReadReceipt and an
unsupported message body. The same decision therefore covers the later create-time
retention check for these event shapes.

## Validation

Baseline: `fb512b9fd69abc06bf50893c4eebc8025eb9b965`. Production source hashes of the
four implicated files matched this baseline on 2026-09-28 at21:46BRT. All503 captured
PostgreSQL deadlock DETAILs for that local day were account UPDATE/account UPDATE pairs;
there were no incomplete pairs or account/connection pairs. The operational-reader cycle
was reproduced in the lab and is not attributed to those503 records.

The account/account signature is shared with inbox-job, policy-writer and
start-conversation callers of `_lock_inbound_account_scope`. These503 records are
consistent with the reproduced webhook cycle, but were not individually correlated with
HTTP versus queue-job origin. Compare post-deployment rates over equivalent traffic
windows instead of assuming all deadlocks disappear.

Seven new tests use independent PostgreSQL transactions, bounded lock/statement timeouts
and event/pg_blocking_pids coordination. They cover:

- Two group callbacks: both commit exactly one pending inbox, without retries.
- Two direct callbacks: remain concurrent readers.
- Expiry and connection demotion: stale snapshots fail serialization; fresh callbacks
  acknowledge and discard the disallowed content.
- Operational admission versus a group callback in both orders: the bare operational
  helper locks account SHARE and then connection UPDATE; both transactions commit
  without deadlock. No real provider call or send occurs.
- Raw/sanitized group routing equivalence, with non-empty-route assertions.

On the original code the three concurrent group/operational scenarios fail by
DeadlockDetected. On the fixed code all7 pass. A deliberately incorrect mutation that
acquires the connection before the account fails the operation-first test by
DeadlockDetected, proving the test pins the order as well as the lock mode.

Python3.10, PostgreSQL14.24 and PostgreSQL16 were exercised in isolated local clusters
with synthetic data. Each wider fixed run executed319 tests, with only the existing
`test_unique_collision_retries_http_and_acknowledges_existing_inbox` failure already
observed in the312-test baseline. No new failure or error. Black22.8, isort5.12,
flake8+bugbear, mandatory pylint-odoo8.0.19 and diff whitespace checks passed on the
changed Python files.

Local evidence: `~/.cache/cc-deadlock-20260928/logs/` (`pg14-baseline-expanded.log`,
`pg14-fixed-regression.log`, `pg14-fixed-suites.log`, `pg16-fixed-suites.log`,
`pg14-wrong-order-mutation.log`). Test class: `TestWuzapiIngressConcurrency`. The wider
tags additionally select all WuzAPI tests and base classes TestHistoryRetention,
TestConversationPrivacy, TestPhase1Concurrency and TestAccessTopologyConcurrency. The
queues/crons and external transports were not running. ARM64/Noble and synthetic load
differ from production AMD64/Jammy.

## Deployment and limits

This change is local only. Publishing still requires the official16.0 commit and GitHub
equality, operator deployment authorization/backup decision and the normal controlled
release procedure. Patch versions are base16.0.1.11.1 and WuzAPI16.0.1.3.2; no schema
change or data migration was added. Do not patch the live immutable release, change
database isolation or replay messages blindly.

The exclusive window starts earlier, including signed group callbacks that subsequently
return as ignored, inactive or retired. A group callback can hold the account while
waiting for a health task's connection UPDATE lock, delaying other operations on that
account. The code preserves correctness and does not promise zero queueing or
elimination of every possible deadlock.

After an authorized deployment, compare equivalent traffic windows: PostgreSQL deadlock
signatures and account/connection lock waits, HTTP worker occupancy, webhook
duration/errors/retries, inbox processing and outbox uncertain states. Delivery has not
been verified by the inbox-persistence tests. Historical503 errors do not imply503 lost
messages. Financial-screen latency remains a separate investigation.
