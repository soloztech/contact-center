# Meta standalone referral validation

- Date: 2026-08-26
- Environment: SERVIDOR05 / `odoo16-teste.soloz.com.br`
- Scope: standalone Messenger and Instagram referral events
- Result: passed

## Implemented contract

- Messenger `messaging[].referral` is received through the `messaging_referrals` webhook
  field.
- Instagram `messaging[].referral` is received through the `messaging_referral` webhook
  field.
- A standalone referral is normalized as provider-neutral
  `EventDTO(event_type="attribution.observed")` with `AttributionDTO` evidence.
- It creates an immutable `contact.center.attribution.touchpoint` but no artificial
  guest, identity alias, channel, message or message binding.
- If a later message resolves the same exact account and address, the touchpoint may be
  linked to that identity/channel. The later message never becomes the source message of
  the older standalone touchpoint.
- Dedupe includes platform, destination asset, sender, recipient, timestamp and bounded
  referral evidence; equal referral values from different people cannot collide.
- `SHORTLINK` and `SHORTLINKS` both normalize to canonical `source_type=shortlink`.

## External configuration

The dedicated Contact Center Meta app was updated through Graph API. Existing fields
were preserved and the resulting configuration was read back:

| Object                          | Active fields                                       |
| ------------------------------- | --------------------------------------------------- |
| Page/Messenger app subscription | `messages`, `message_echoes`, `messaging_referrals` |
| Page installed-app subscription | `messages`, `message_echoes`, `messaging_referrals` |
| Instagram app subscription      | `messages`, `messaging_referral`                    |

The legacy Lead Ads app was not changed. No App Secret, access token, verify token,
routing key, cookie, signature or raw conversation is stored in this report.

## Automated validation

| Check                                     | Result                                                                  |
| ----------------------------------------- | ----------------------------------------------------------------------- |
| Python compilation/format/import ordering | passed                                                                  |
| Isolated base suite                       | **259/259**, zero failures/errors                                       |
| Isolated integrated suite                 | **433/433**, zero failures/errors                                       |
| GET webhook challenge                     | accepted                                                                |
| Signed Messenger standalone referral      | HTTP 200, delivery `done`, atomic item `routed`                         |
| Signed Instagram standalone referral      | HTTP 200, delivery `done`, atomic item `routed`                         |
| Exact Messenger replay                    | duplicate, same durable delivery                                        |
| Touchpoint projection                     | one per platform                                                        |
| Chat projection                           | zero identity aliases, channels and messages for both synthetic senders |
| Installed-state validation                | passed; production not touched                                          |
| Source-tree validation                    | passed; production not touched                                          |

The Messenger smoke normalized `source_type=ad`; the Instagram smoke normalized
`source_type=shortlink`. Each delivery contained one item, with one routed and zero
unrouted/ignored items.

## Installed release

- `contact_center_base 16.0.1.20.3`
- `contact_center_wuzapi 16.0.1.17.0`
- `contact_center_meta 16.0.1.1.2`
- `contact_center_ui 16.0.1.17.1`
- `queue_job 16.0.3.0.2`
- Source tree: `7fd2d1894017b4dfb4e661826b892c557362a70b0ee2a4413a988cfa2960271e`

Canonical evidence directories:

- `scans/raw/20260826-odoo16-contact-center-meta-referral/release-apply`
- `scans/raw/20260826-odoo16-contact-center-meta-referral/testfix-source-deploy`
- `scans/raw/20260826-odoo16-contact-center-meta-referral/isolated-tests-rerun`
- `scans/raw/20260826-odoo16-contact-center-meta-referral/final-installed-validation`
- `scans/raw/20260826-odoo16-contact-center-meta-referral/final-source-validation-rerun`

## Remaining gates

- Phase 6.5 still needs automatic subscription reconciliation, token/scope/asset health,
  revision fencing and credential lifecycle monitoring.
- The app remains in Development mode; publication/App Review and a representative
  non-role sender are required before the operational pilot.
- Phase 6.3 media/state events and Phase 6.4 outbound remain intentionally disabled.
