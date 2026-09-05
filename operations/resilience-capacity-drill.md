# Contact Center — controlled resilience and capacity drill

## Objective

Validate the exact release candidate without changing production and without sending
traffic through real customer conversations. The drill runs on SERVIDOR05 using
disposable databases and synthetic provider identities. It complements, rather than
replaces, unit and integration tests.

The automated harness uses a localhost WuzAPI test server and an internal-only Docker
network. Ingress authentication, Odoo HTTP, PostgreSQL and the OCA JobRunner are real;
the provider responses and session events are simulated. A successful run does not
claim that a real WhatsApp session was disconnected or that Meta App Review gates were
validated. Live-provider canary checks remain part of cutover.

## Topology

- 20 logical WhatsApp inboxes and 20 independent primary connections;
- shared and exclusive teams represented;
- at least 100 direct conversations and 1,000 inbound messages;
- duplicate and out-of-order deliveries distributed across all connections;
- bounded text/media/reaction/receipt mix using generated fixtures only;
- two simultaneous browser clients for realtime verification.

The generated identifiers use reserved fixture namespaces and cannot match an existing
laboratory or production connection.

## Failure scenarios

1. Provider connection refused before dispatch: no provider boundary, durable retry, no
   duplicate message.
2. HTTP 429 with numeric and date `Retry-After`: shared connection cooldown and bounded
   delay.
3. HTTP 503/read timeout after crossing the dispatch boundary: classify as `uncertain`
   when acceptance cannot be disproved; do not automatically repeat the send.
4. Ambiguous timeout after the provider may have accepted the request: terminal
   `uncertain`, never automatic resend.
5. Session lifecycle
   `connected -> disconnected -> authentication_required -> connected`: outbound blocks
   during the outage and only eligible pre-boundary jobs resume after a fresh identity
   proof.
6. Worker termination after webhook ledger commit but before projection: orphan recovery
   creates one canonical job and one projection.
7. Repeated webhook delivery: one inbox ledger/projection per dedupe key.
8. Out-of-order edit/reaction/delivery: monotonic final projection.

## Measurements

Record for each run:

- admission and projection throughput;
- p50, p95 and p99 latency;
- peak process RSS;
- PostgreSQL connection peak;
- maximum ready/scheduled/started queue depth and oldest-job age;
- counts by inbox, outbox, media and queue state;
- duplicate conversation/message/outbox counts;
- recovery duration after the provider returns;
- HTTP/realtime errors and browser console errors.

## Acceptance

- all correctness assertions pass;
- no unauthorized or cross-inbox projection;
- no duplicate provider dispatch;
- no orphan remains after the recovery sweep;
- no unexplained `dead` or `uncertain` record;
- all 20 inboxes make progress (no starvation);
- memory and DB connections return near baseline after the run;
- measured p95/queue ceilings are recorded and used by the production cutover rather
  than replaced by an arbitrary absolute promise.

The evidence directory must contain the immutable source hash, commands, sanitized raw
metrics, Odoo summaries and cleanup proof. Disposable containers, databases, generated
attachments and browser sessions must be absent at the end even when a scenario fails.

The automated monitor must validate its SQL and memory counter before admitting load.
Unavailable metrics are a failed measurement, never zero load. Browser and live-provider
checks require separate evidence and are not implied by the HTTP fixture results.
