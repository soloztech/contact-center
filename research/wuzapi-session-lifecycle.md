# WuzAPI session lifecycle and health

- Status: M4 deployed and validated only in the disposable SERVIDOR05 laboratory;
  general pilot pending
- Reviewed: 2026-08-23
- Baseline: WuzAPI `v1.0.8`, commit `9487eca`

## Reviewed provider contract

The pinned WuzAPI source exposes session status through `GET /session/status` and can
publish these exact lifecycle events through its regular JSON webhook:

- `Connected` and `KeepAliveRestored`;
- `Disconnected`, `ConnectFailure`, and `StreamReplaced`;
- `KeepAliveTimeout` and `StreamError`;
- `LoggedOut` and `QRTimeout`;
- `ClientOutdated` and `TemporaryBan`.

The complete M4 subscription keeps the existing `Message` and `ReadReceipt` events and
adds all eleven lifecycle events above. In JSON webhook mode, WuzAPI wraps the event as
`event`, adds `type`, `instanceName`, and `userID`, and signs the final body when HMAC
is enabled. Some lifecycle payloads, including `Connected` and `Disconnected`, are empty
structs. The raw-body hash therefore cannot identify a transition across multiple
disconnect/reconnect cycles.

Primary source references:

- [event names at the pinned revision](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/constants.go);
- [webhook wrapping, filtering and delivery at the pinned revision](https://github.com/asternic/wuzapi/blob/9487eca9a40f292d19953a44983979c85d91ccce/wmiau.go);
- [WuzAPI API reference](https://github.com/asternic/wuzapi/blob/main/API.md).

## Canonical state mapping

The adapter emits a provider-neutral `connection.updated` EventDTO. The core then
normalizes legacy transient inputs so the public health state has four persisted values:

| WuzAPI event                                       | Adapter EventDTO state                       | Core health state                            |
| -------------------------------------------------- | -------------------------------------------- | -------------------------------------------- |
| `Connected`, `KeepAliveRestored`                   | `connected` + `health_confirmation_required` | `degraded/identity_unverified` until polling |
| `Disconnected`, `ConnectFailure`, `StreamReplaced` | `disconnected`                               | `disconnected`                               |
| `KeepAliveTimeout`, `StreamError`                  | `degraded`                                   | `degraded`                                   |
| `LoggedOut`, `QRTimeout`                           | `authentication_required`                    | `authentication_required`                    |
| `ClientOutdated`                                   | `error`                                      | `degraded`                                   |
| `TemporaryBan`                                     | `paused`                                     | `degraded`                                   |

`unknown` is derived when there is no observation in the last 180 seconds; it is not
persisted as a provider state. UI and dispatch use the exact same inclusive freshness
boundary, so an observation at the cutoff is still fresh and one second older is
`unknown` and cannot dispatch. `checking` is a transverse boolean. It does not replace a
last-known state, so canonical states plus `unknown` sum the fleet total while
`checking` may overlap them. A durable identity latch does not mask a fresh
`disconnected` or `authentication_required` observation. It publishes `degraded` for a
connected or stale observation and still blocks outbound in every state until verified.

WuzAPI lifecycle payloads do not provide a reliable event timestamp. The adapter uses
the Odoo receipt time, records `occurred_at_source=odoo_received`, and does not invent a
provider ID or timestamp. Domain ordering uses the durable inbox `create_date` and,
within the same Odoo second, the inbox ID. A polling/lifecycle tie keeps the less
permissive state, delaying recovery rather than opening outbound dispatch ambiguously.

## Periodic polling and own identity

The periodic probe calls `GET /session/status` with timeout `(3, 10)` and maps:

- `connected=true` and `loggedIn=true` to `connected`;
- `loggedIn=false` to `authentication_required`;
- `loggedIn=true` and `connected=false` to `disconnected`;
- unreachable, unauthorized, rate-limited, provider-error, or malformed responses to
  bounded degraded/disconnected/authentication diagnostics in the core.

When `account.own_external_identity` is configured and the response is otherwise
healthy, the adapter normalizes both it and `data.jid`, removing a WhatsApp device
suffix before comparison:

- a match returns only `identity_matches=true`;
- a mismatch returns `degraded` with `identity_mismatch` and `identity_matches=false`;
- a connected/logged-in session without JID returns `degraded` with
  `identity_unverified` and `identity_matches=false`;
- without a configured own identity, a connected/logged-in session also returns
  `degraded/identity_unverified` with `identity_matches=false`.

WuzAPI may report health `connected` only when the expected own identity is configured
and matches the normalized observed JID. A missing expected identity is therefore a
fail-closed configuration error, not permission to trust whichever session answers.

The adapter never returns or logs the raw JID, token, provider URL, or response body in
the public health result. The UiDTO displays only a bounded address derived from the
account's own configured identity, never a customer/sender identifier.

An observed mismatch, missing expected identity, or otherwise unverified identity arms
the provider-neutral, durable `identity_mismatch_latched` safety lock. The historical
field name covers all three negative outcomes. This is intentionally stronger than a
transient health state:

- lifecycle events cannot clear the lock because they prove only that _a_ session is
  connected, not which WhatsApp identity owns it;
- an intermediate disconnect, authentication loss, or other health transition does not
  clear it;
- a later health response saying only `connected`/`healthy`, without
  `identity_matches=true`, does not clear it;
- only a fresh `health_job` observation with both `state=connected` and
  `identity_matches=true` releases it.

While latched, a fresh `disconnected` or `authentication_required` state remains visible
in the fleet instead of being hidden by the identity diagnostic. A connected or stale
observation is fail-closed as `degraded` with the safe detail `identity_mismatch` or
`identity_unverified`. Outbound dispatch remains blocked in every case. The raw observed
or expected identifiers are never needed in UiDTO or bus to enforce this rule.

Polling repairs state after a lost webhook; lifecycle events accelerate visibility.
`Connected` and `KeepAliveRestored` do not prove session identity or current status, so
the adapter marks them `health_confirmation_required`. The core records
`degraded/identity_unverified` and only schedules a unique priority-30 health job; it
does not reopen outbound. Only a successful fresh `/session/status` result may do so.
Neither path calls `/session/connect`, login, QR generation, or any other session
mutation automatically. `authentication_required` always requires a human action.

If a provider health response includes `Retry-After`, the core persists the earliest
allowed next request in `health_retry_not_before`. Both the minute cron and a manual
Supervisor/Administrator refresh must respect this timestamp and enqueue no probe while
the cooldown is active. Manual refresh is not an escape hatch around provider rate
limits and still performs no provider I/O in the request.

## Configuration generation boundary

The service URL, API token, and expected own identity define which remote session a
connection addresses. They cannot change while outbound is active or while any affected
command is `processing`. The operator must disable outbound and let in-flight dispatch
drain first.

An allowed change still fails closed under the same connection locks: it sets
`degraded/identity_unverified`, arms `identity_mismatch_latched`, increments the
monotonic `health_configuration_revision`, clears any `health_retry_not_before` cooldown
owned by the previous configuration, publishes a safe fleet update, and schedules a
priority-30 health check. Immediately before health provider I/O, the adapter
invalidates the ORM cache for service URL, token, and expected identity and rereads
them. A response from a probe started under an older revision is discarded. It cannot
clear the latch, restore `connected`, or recover outbox work after the configuration
changed. URL, token, expected identity, and revision internals do not enter the public
UiDTO or bus payload.

## Lifecycle deduplication

Message and receipt events keep their existing provider identifiers or sanitized-body
hashes. Lifecycle events use a transition-cycle key:

1. a lifecycle record still in `pending`, `processing`, or `retry` is reused by a retry
   of the same event type;
2. a terminal record of the same type is normally reused;
3. if a later health job observed a different canonical state, the same lifecycle type
   opens a new cycle keyed by a truncated SHA-256 revision derived only from the last
   inbox ID and safe state/source/timestamp values;
4. retries inside that new cycle converge on the same inbox unique key.

This covers both `Connected -> Disconnected -> Connected` and
`Connected -> polling says disconnected -> Connected`. It avoids suppressing a real
reconnection while still deduplicating provider retries of an empty lifecycle payload.

A webhook targeting an archived account or provider connection still crosses the
authentication and basic request-validation boundary. A correctly signed, valid JSON
request receives `200` with `ignored=inactive`, but creates no Inbox event, domain
ledger, or queue job. An invalid signature remains rejected; archive is not an
authentication bypass.

## Recovery boundary

Outbound dispatch is allowed only when both the logical account and provider connection
are active, the connection is outbound-enabled and `connected`, its observation is
within the same 180-second UI freshness window, and it is not protected by
`identity_mismatch_latched`. Account archive is a final guard at the provider boundary.
After a mismatch/unverified identity, only the verified health match described above may
both clear the lock and make recovery eligible. A lifecycle `Connected` hint or an
unverified healthy result cannot open dispatch. On that valid transition, the core may
resume only outbox commands in `pending` or `retry` with no
`dispatch_job_uuid`/`dispatch_started_at` boundary evidence. It must never automatically
resend `processing`, `uncertain`, `dead`, `done`, or `cancelled` commands.

An existing pending OCA job is requeued without duplication; its executor ETA and retry
counter are cleared, while domain attempts in the outbox ledger are preserved. A
historical or standby connection with `outbound_active=false` may be monitored but must
not recover old commands.

Connection state and outbox rows use a consistent lock order. Repeated health/lifecycle
observations are idempotent. Recovery follows a transition of the complete availability
predicate from false to true, not only a raw `state` change. Consequently, a fresh
health observation may recover eligible work when the stored state was already
`connected` but its previous observation had become stale.

## Fleet operation

The baseline is approximately 20 WhatsApp connections:

- an Odoo cron runs every minute but only schedules idempotent OCA jobs;
- jobs use `root.contact_center.health`, priority 30, and deterministic 0–29 second
  jitter; provider HTTP never runs inside the cron transaction or a UI request;
- Agent sees only roster-visible health; Supervisor/Administrator may schedule a
  refresh, subject to the same persisted `health_retry_not_before` cooldown as cron;
- UiDTO returns a compact summary and items; incremental `connection_health_updated`
  events update one item without forcing 20 bootstraps;
- a client-side revision fence prevents a stale refresh RPC from overwriting a newer bus
  result; lost bus events converge through 11 bounded polling attempts over 180.5
  seconds, with `connectionHealthRevision` providing Owl 16 reactivity.

Account/connection archive, default-team changes, and roster changes invalidate the
affected fleet scope for both current and previous recipients. An incremental bus event
whose connection ID is absent from the current authorized snapshot is never inserted
optimistically; it triggers a scoped, server-filtered bootstrap refresh instead. This
keeps archive, team, roster, company, and ACL boundaries authoritative in the server.

The current laboratory runner configuration is
`root:4,root.contact_center.health:2,root.contact_center.media:1`. Runtime logs
previously confirmed global capacity four and health capacity two. The media leaf cap
was added after the independent audit so one slow download cannot exhaust the runner; it
must be revalidated together with the pilot workload of approximately 20 connections.
This is a SERVIDOR05 setting, not approval of a production topology.

## Evidence status

On 2026-08-23 the M4 source was deployed and upgraded only on SERVIDOR05 with tree hash
`40c486d6de2c753cd6efa940763266b4591e4c4bcc3b9c27a390ee4c2d856c8c`. Deployed versions
are `queue_job` `16.0.3.0.2`, base `16.0.1.7.0`, WuzAPI `16.0.1.5.0`, and UI
`16.0.1.5.2`. Base tests passed 92/92 and integrated tests 141/141 under
`scans/raw/20260823-odoo16-contact-center-m4-health-recovery/test/20260823T215648678610Z`.
QUnit passed 28/28 tests with 168 assertions. All five WuzAPI connections were connected
with valid identity; **Verificar todas** converged without reload with zero browser
console errors. No message was sent, no database backup was made under the explicit
disposable-lab instruction, and production was not touched.

The laboratory evidence does not complete the general pilot. Failure drills under the
expected workload, including disconnect/reconnect and an `uncertain` non-requeue canary,
remain pilot gates.

The acceptance placeholders and execution order are in the
[M4 incident/runbook](../../../incidents/2026-08-23-odoo16-contact-center-m4-health-recovery.md).

## Revalidation gate

Before changing the WuzAPI image, commit, webhook format, or JobRunner topology:

1. compare supported event constants and wrapper fields with this mapping;
2. replay repeated empty lifecycle fixtures, a full reconnect cycle, and a lifecycle
   event repeated after an intervening polling state change;
3. verify HMAC over the exact delivered JSON body;
4. test all status combinations, own-identity match/mismatch, missing JID,
   `identity_unverified`, durable latch persistence across lifecycle/outage/unverified
   health, explicit verified release, and bounded timeout behavior without exposing raw
   identifiers;
5. prove that `Connected`/`KeepAliveRestored` remain degraded until polling confirms
   health and that a stale observation blocks dispatch at the same 180-second UI cutoff;
6. prove that fresh polling can recover a stale-but-persisted `connected` connection and
   does not enqueue uncertain, post-boundary, terminal, standby, or already active work;
7. verify that provider Retry-After blocks cron and manual refresh until the durable
   cooldown expires;
8. prove that an archived account blocks dispatch at the final boundary and that a
   correctly signed inactive webhook is acknowledged without Inbox, ledger, or job;
9. prove that URL, token, and own identity cannot change during active outbound or
   `processing`, and that a permitted change increments `health_configuration_revision`
   so an older in-flight health result cannot release the latch, clears the old
   `Retry-After` cooldown, and rereads configuration after ORM cache invalidation;
10. verify that archive/team/roster changes invalidate the authorized fleet and that an
    unknown bus connection ID causes a filtered refresh rather than direct insertion;
11. verify that a latched fresh disconnect/authentication requirement remains visible
    while connected/stale stays degraded and outbound is blocked in all cases;
12. validate queue channel syntax and measure inbox/outbox latency with approximately 20
    simultaneous health probes.
