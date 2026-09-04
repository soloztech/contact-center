# Inbox team boundary — implementation review

Date: 2026-08-31 Scope: local Contact Center Odoo 16 source only Production touched: no
SERVIDOR05 touched: no

## Outcome

The partner card no longer presents a conversation-level team transfer. It displays the
inbox team as a read-only value and keeps only the responsible-agent assignment
interactive for supervisors.

`contact.center.ui.api.update_conversation` now rejects every payload containing
`team_id`, including the current team. The supported operational patch remains limited
to state, responsible agent and tags. Responsible agents are still validated against the
access team defined by the bound inbox.

## Fail-closed behavior

- The browser no longer exposes `setTeam` or an `onTeamChange` handler.
- The store rechecks `manage_assignment` before sending a responsible-agent patch.
- An absent or unknown conversation team yields an empty responsible roster instead of
  falling back to every visible agent.
- Non-supervisors remain protected by the server-side group check even if they bypass
  the disabled browser control.

## Compatibility boundary

The stored `mail.channel.contact_center_team_id` projection remains in place for
membership, serialization and existing records. This cut does not change schemas or data
and does not perform an administrative inbox-team migration. Changing
`contact.center.account.default_team_id` and reconciling historical channels is a
separate operation that requires its own transactional design and evidence.

## Verification

Passed locally:

- Prettier 2.7.1 for JavaScript, XML, SCSS and QUnit sources;
- ESLint 8.24.0 for the affected JavaScript and QUnit sources;
- Black 22.8.0, Flake8 5.0.0 and mandatory pylint-odoo checks;
- XML parsing with lxml;
- Dart Sass compilation of the complete Contact Center stylesheet;
- Python byte-compilation, manifest/version assertions and release-script compilation;
- staged and unstaged `git diff --check`.

The focused Odoo test and integrated QUnit/browser acceptance remain pending because no
candidate was deployed to SERVIDOR05 in this implementation step.

## Follow-up

This historical pending state was closed by the owner/team and CRM rollout documented in
[`2026-09-01-owner-team-pipeline-crm-release-validation.md`](2026-09-01-owner-team-pipeline-crm-release-validation.md).
