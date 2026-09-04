# Cross-inbox identity and attribution validation

- Date: 2026-08-25
- Target: disposable SERVIDOR05 Odoo 16 laboratory
- Production touched: no
- Database backup: waived by explicit instruction for this development environment

## Deployed versions

- OCA `queue_job`: `16.0.3.0.2`
- `contact_center_base`: `16.0.1.19.1`
- `contact_center_wuzapi`: `16.0.1.16.0`
- `contact_center_ui`: `16.0.1.16.0`

The final atomic source tree hash is
`b9335857750ab166c3076017dfd7c142a74e4f11982b9f98bd4c5dec18378920`. The upgrade,
source-integrity checks, container start, exact Traefik route restoration and public
HTTP 200 check all passed.

## Cross-inbox identity result

- 55 trusted WhatsApp PNs occur in multiple accounts and each resolves to one active
  company-scoped identity;
- 19 identities currently serve conversations in multiple inboxes, covering 42 direct
  bindings and up to three inboxes for one identity;
- 67 legacy identities were retired, with no redirect chain and no active alias, binding
  or attribution row left on a retired identity;
- all 115 active direct conversations contain exactly the canonical guest;
- 286 historical messages and five mutation records retain retired guest authorship,
  proving that the migration did not rewrite history;
- there is no duplicate active company/PN key or cross-company resolution.

## Attribution replay result

Three historical sanitized commercial inbox events were normalized by the deployed
WuzAPI adapter and passed only through attribution capture/link. Message processing was
not replayed.

- two canonical touchpoints: one `paid_ad_click` and one `entry_point`;
- three evidence links distributed `1 + 2` because two inbox rows represented the same
  provider message;
- two restricted external identifiers;
- both touchpoints linked to an existing message, channel and identity;
- exact recapture preserved the same two touchpoints, two identifiers and three evidence
  links;
- zero attribution conflicts.

Account 2 was explicitly opted into the optional operator projection for the laboratory
validation. An authorized member received only the allow-listed UiDTO. A clean Chromium
session displayed the paid click and the non-paid entry point in the right panel, with
no source URL, external identifier, identifier collection or provider extension in the
projection.

## Automated validation

- base Odoo suite: **225/225**, zero failures and errors;
- integrated base + WuzAPI + UI suite: **345/345**, zero failures and errors;
- Contact Center UI QUnit, minified: **68/68**, 586/586 assertions;
- Contact Center UI QUnit, `debug=assets`: **68/68**, 586/586 assertions;
- authenticated application and both QUnit modes: zero console errors or warnings.

Temporary test databases and the transient browser authentication state were removed.

## Evidence

- atomic release:
  `scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260825T060921102420Z/`;
- final formatted-source synchronization:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/deploy/20260825T063138686686Z/`;
- isolated Odoo suites:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/test/20260825T061020481109Z/`;
- attribution replay:
  `scans/raw/20260824-odoo16-contact-center-phase4-pilot-hardening/attribution-replay/20260825T062308904781Z/`.
