# Realtime inbox refresh validation

Date: 2026-09-01

Status: fixed, deployed and validated on the disposable SERVIDOR05 laboratory.
Production/SERVIDOR02 was not accessed.

## Diagnosis

The inbound path was healthy: the application persisted each message, advanced the
canonical channel cursor and published `message_created` through `bus.bus` after the
database commit. The test operator was a member of every visible active channel,
including the reported direct conversation. The public and direct WebSocket probes
returned `101`, and Traefik routed `/websocket` and `/longpolling` to the evented Odoo
port `8172`.

The defect was in the client lifecycle. Odoo 16 resolves `busService.start()` after the
Worker is initialized, before the WebSocket emits `connect`. The Contact Center treated
that resolution as proof of connectivity, did not listen to `connect` or `reconnecting`,
and enabled its RPC fallback only after a received `disconnect`. A tab opened during an
initial failure or an existing SharedWorker reconnection could therefore display **Tempo
real ativo** and remain stale indefinitely.

## Correction

`contact_center_ui 16.0.1.19.1` now:

- considers only `connect`, `reconnect`, or an actual notification proof of an online
  transport;
- maps `reconnecting` to `connecting` and `disconnect` to `offline`;
- treats any received bus notification as connectivity proof, covering a tab that joins
  an already-connected Odoo 16 SharedWorker;
- keeps a bounded 30-second RPC reconciliation armed in every transport state, so a lost
  invalidation cannot leave the projection stale indefinitely;
- reissues the idempotent bus start command while offline or connecting;
- removes all five bus listeners and the consistency timer when the store is destroyed.

Two QUnit regressions cover the complete lifecycle/fallback and two stores consuming the
same SharedWorker notification independently.

## Validation

- Core: **396/396**, zero failures and errors.
- WuzAPI adapter: **203/203**, zero failures and errors.
- Integrated Base/WuzAPI/Meta/CRM/UI: **729/729**, zero failures and errors.
- Minified authenticated QUnit: **95/95** tests and **856/856** assertions, zero
  failures, skips or console errors.
- Two clean authenticated browser sessions both reported realtime active and the same
  conversation total. A controlled no-op `conversation_updated` event caused each
  session to issue its own list/conversation/timeline refresh within two seconds; both
  clean consoles remained at zero errors and warnings.
- A synthetic browser network cut changed the indicator away from online and the
  30-second RPC reconciliation continued to return `200`, proving the stale-state safety
  net independently of the WebSocket.

Canonical release evidence:

`scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260901T062310824116Z/summary.json`

The released tree hash is
`6d931e41d41b418c990a2f8d0326a3f8757b876013fffb47f19936f8a4bbc056`. The byte-identical
test Traefik route was restored and public HTTP returned 200. The operator waived a
backup for this disposable development database.
