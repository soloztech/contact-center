# Inbox access upgrade to 16.0.1.1.0

This release changes each inbox's `owner_user_id` and `default_team_id` into
`access_user_ids` and `access_team_ids`. Both new fields are Many2many and grant
cumulative access. Existing singular values become singleton selections; no user, team,
conversation, responsible agent, automatic assignee or provider route is chosen or
changed by the migration. Optional Kanban case teams remain singular business
assignments and do not grant conversation access.

All six Contact Center addons share version `16.0.1.1.0`. Upgrade every installed
Contact Center addon together. Do not install Kanban, functional Marketing addons, or an
uninstalled provider merely to perform this upgrade. Keep the existing pinned Queue and
Meta foundations unchanged.

## Preflight and isolated rehearsal

Record the old and candidate commit hashes, installed module inventory, active source
trees, account grants, running services and queue state. Reject concurrent module
changes. Stage the exact candidate in an inactive directory and run the access,
team-roster, onboarding, UI and optional Kanban suites on a disposable database. Include
a real upgrade rehearsal: install the previous release, create representative
users/teams/inboxes/conversations, prepare the snapshot, switch to the candidate and run
the native upgrade. A fresh installation alone does not exercise the migration.

Prove prepare rollback in the old registry by calling `prepare` in a transaction,
rolling it back, and confirming both migration parameters remain absent and all original
grants are unchanged. After rehearsal, confirm the `done` state, preserved effective
users and conversation memberships, original assignments and provider flags, the ten
updated access-rule domains, and idempotent finalization. Exercise two direct users and
two teams, including overlapping membership and removal of one grant while another
remains.

Production upgrades change schema and access controls. Use the environment's reviewed,
restorable backup policy and record the rollback boundary. An old waiver for the first
installation does not establish a waiver for this upgrade. The disposable laboratory may
use its explicit operator waiver. No code in this migration takes a backup or silently
changes provider callbacks.

## Execution

1. Prepare source, tests, evidence and recovery commands before the maintenance
   interval. Stop every Odoo process that shares this database, including the secondary
   report/HTTP instance, cron and the queue runner. A rolling restart across different
   ORM schemas is not supported. Keep worker processes stopped until the end migration
   and postflight have passed.
2. With the **old source and old registry**, load the candidate's standalone migration
   entry point. Use an explicitly reviewed snapshot:

   ```python
   import runpy

   migration = runpy.run_path(
       "/staged/contact-center/release_migrations/account_access_many2many.py"
   )
   before = migration["snapshot"](env)
   migration["prepare"](env, before)
   env.cr.commit()  # the operator owns this explicit handoff boundary
   ```

   `prepare` stores IDs and business-state digests through ORM in `ir.config_parameter`.
   It does not copy credentials or message content. Retain the same snapshot and digest
   in private operator evidence. Run it only after workers have stopped so no
   conversation or assignment can change afterward.

3. Switch the source to the exact staged candidate. Run native Odoo with
   `--stop-after-init --no-http --workers=0 --max-cron-threads=0 --load=base,web` and
   `-u` for the installed Contact Center addons. The versioned pre migration rejects a
   missing or damaged handoff. The versioned **end migration** executes after the
   complete addon graph is loaded, fills the new relations through `account.write`,
   reconciles conversation access through the native services, and validates all
   preservation invariants before marking the handoff `done`.
4. The end migration explicitly updates only `domain_force` on ten canonical `noupdate`
   access rules, reading their target definitions from the new XML. Customized domains
   or changed rule identities/permissions stop the upgrade for review. Group membership
   and ACL grants are never broadened automatically.
5. In a new no-HTTP shell, run `prepared_snapshot` and `validate_transition` and confirm
   `contact_center_base.account_access_many2many_state` is `done`. Confirm expected
   addon versions, no pending module actions, unchanged provider identities and routes,
   and no new outbound commands or jobs. Start Odoo, verify both internal HTTP
   endpoints, then reload the browser and validate the account form, conversation scope
   and agent/team selectors.

Native Odoo module upgrades commit intermediate checkpoints. An outer cursor rollback
cannot undo the whole schema upgrade. Before the source switch, recovery can retain the
old source and old schema. Once the native upgrade has started, use a reviewed forward
repair or the paired restore procedure; never restart the old source against a partially
upgraded database. Keep the prepared snapshot for recovery, and never fabricate a `done`
marker or reconstruct grants from an arbitrary first user/team.

The native hook files and their shared implementation are packaged under
`contact_center_base/migrations/16.0.1.1.0/`. The standalone entry point uses that same
implementation; there is no runtime alias for the retired Many2one fields. Fresh
installations do not execute these versioned upgrade hooks.
