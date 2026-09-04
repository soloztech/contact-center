# Inbox automatic assignment validation

Date: 2026-09-01

Status: implemented, deployed and validated on the disposable SERVIDOR05 laboratory.
Production/SERVIDOR02 was not accessed.

## Result

`contact_center_base 16.0.1.31.0` adds an optional fixed automatic assignee to each
`contact.center.account`. An empty value preserves the existing manual **Assumir** flow.
The configured user must be an active internal user in the account's effective owner and
access-team union.

The provider-neutral application service applies the policy to a new conversation before
the default case is created. For an existing unassigned conversation, it applies only
after accepting a new inbound provider message. Exact replays return before assignment,
and an existing responsible is never replaced. Scope revocation clears an invalid policy
and relies on the existing exact channel-membership reconciliation to remove
unauthorized historical responsibility.

## Automated validation

- Core: **396/396**, zero failures and errors.
- WuzAPI adapter: **203/203**, zero failures and errors.
- Integrated base/WuzAPI/Meta/CRM: **729/729**, zero failures and errors.
- Local release contracts: **35/35**; the complete script suite reported **47/47**.
- Black, Flake8 on touched addon files, Python compilation, XML parsing and
  `git diff --check`: passed.
- Independent adversarial review found no release blocker. Two explicit concurrency test
  scenarios remain useful future coverage, while the inspected lock order is consistent.

Canonical release evidence:

`scans/raw/20260824-odoo16-contact-center-phase4-hardening-atomic-release/release/20260901T054616220926Z/summary.json`

The first apply attempt stopped before database upgrade because the release runner had
not yet allowed the newly added isolated `wuzapi` suite name. Recovery restored the
previous source, both containers and the byte-identical Traefik route. The runner now
reserves an independent WuzAPI test port, accepts that suite explicitly and has a unit
contract preventing the regression. The subsequent atomic release completed normally.

## Runtime/UI validation

- HTTP returned 200 after the upgrade and the public test route was restored with the
  original SHA-256.
- ORM exposed `auto_assignment_user_id` as an optional many2one; all eight existing
  accounts remained disabled (`0/8` configured).
- The account list and form show **Atribuir automaticamente a**.
- The guided **Adicionar caixa** form includes the same selector.
- On the Lucas inbox, the dropdown exposed only `Lucas Zotelli`, matching its effective
  access team.
- Browser validation produced zero JavaScript console errors and made no persistent
  configuration change.
