# Meta external sandbox connection validation

- Date: 2026-08-25
- Environment: SERVIDOR05 / `odoo16-teste.soloz.com.br`
- Scope: dedicated Meta laboratory app, Messenger Page, Page-linked Instagram and
  inbound Phase 6.2 pipeline
- Result: passed with partial Phase 6.0 acceptance

## External configuration

- Created the dedicated Meta Business App `Soloz Contact Center`, App ID
  `1588399876398758`, pinned to Graph `v26.0`.
- Preserved the legacy Lead Ads app and its `leadgen` subscription.
- Connected the Page `Soloz Industrial - Estrutura Solar`, ID `117145694671129`.
- Connected the linked professional Instagram account `@solozindustrial`, ID
  `17841459160634663`.
- Verified the public Messenger and Instagram callbacks.
- Confirmed Page fields `messages` and `message_echoes` and Instagram field `messages`.
- Restricted the authorization to the selected Page and Instagram assets.

No App Secret, access token, verify token, routing key, cookie or raw conversation is
recorded in this file.

## Odoo configuration

- One restricted Meta App record and one shared Page-linked authorization.
- Separate logical accounts and provider connections for Messenger and Instagram.
- Both accounts use the laboratory team and enable the provider-neutral attribution
  projection.
- Outbound remains disabled.
- Authorization health remains `unverified` and both connections remain `degraded` until
  Phase 6.5 implements Graph health, lifecycle and subscription reconciliation.

## End-to-end smoke

The smoke used unique synthetic sender and message IDs and the real public callback. The
request body was signed with `X-Hub-Signature-256` over the exact posted bytes.

| Check                   | Result                                                            |
| ----------------------- | ----------------------------------------------------------------- |
| GET challenge           | accepted                                                          |
| Messenger signed POST   | HTTP 200, new delivery                                            |
| Exact Messenger replay  | HTTP 200, duplicate, same delivery                                |
| Instagram signed POST   | HTTP 200, new delivery                                            |
| Delivery fan-out        | 2/2 `done`, one attempt each                                      |
| Atomic routing          | 2/2 `routed`, zero unrouted/ignored                               |
| Canonical inbox         | 2/2 `done`, schema `meta.webhook.v1`                              |
| Message projection      | 2/2 inbound/provider/text/delivered                               |
| Conversation projection | one direct conversation in each correct account                   |
| Attribution             | Messenger referral produced one provider-asserted `paid_ad_click` |

The test proves callback authentication, durable delivery ledger, OCA queue fan-out,
strict asset routing, inbox normalization, identity/guest creation, direct conversation,
`mail.message` projection, replay idempotency and immutable attribution capture.

## Remaining acceptance gates

- Issue `pages_read_engagement` in the operational credential if metadata discovery and
  full Graph health require it.
- Replace the current user/Page authorization with a managed system-user or equivalent
  long-lived operational credential and implement renewal/expiry monitoring.
- Implement Phase 6.5 Graph health and subscription reconciliation before treating the
  connections as `connected`.
- Publish/complete App Review as required and test a representative sender without an
  app role.
- Phase 6.3 media/state events and Phase 6.4 outbound remain intentionally disabled.
