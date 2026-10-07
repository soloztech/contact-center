# Upgrade do CRM com escopo comercial

**O backfill de extração abaixo é histórico e está bloqueado antes de qualquer escrita
quando o ledger contém `scope_state`.** Revogar case assertions e recriar somente
contextos apagaria crédito antigo. Bases pendentes de extração exigem plano específico e
backup verificado; não executar o procedimento histórico nem bypassar o guard.
`contact_center.case` permanece pendente de revisão no classificador.

Para bases já extraídas, atualizar os módulos coordenados. Vínculos existentes recebem
`legacy`/`unknown`, sem vigência inventada. As novas ligações são contexto; a Jornada
permite confirmação explícita. Ver [contrato e diagrama](../docs/crm-journey.md).

---

# Independent conversation CRM: prerelease upgrade

`contact_center_crm` depends on `contact_center_ui` and native `crm`. It links
conversations directly to opportunities. `contact_center_kanban` depends on
`contact_center_crm` and adds service cases, pipeline mappings, synchronized stages and
roster authority. Marketing consumes the conversation association and installs without
Kanban. Removing a case link preserves the conversation link; removing the conversation
link also removes the corresponding optional case links.

The two repository revisions form one release candidate. The historical consumer
`marketing_center_contact_center_crm` imports models that moved to Kanban. On a
pre-extraction base the script marks that old consumer for upgrade. The current consumer
is `marketing_center_contact_center`; the operator must explicitly include it in the
coordinated module upgrade when installed. Stage both repositories together and use full
peer commit SHAs in coordinated CI.

For an existing prerelease database, keep Odoo, its queue workers and the database
manager stopped throughout the following steps. Take and verify a paired database,
filestore and source backup first. Fresh installs need no migration.

1. Stage the reviewed source from both repositories. Run the module upgrade with
   `--pre-upgrade-scripts=/path/contact-center/release_migrations/crm_conversation_extraction.py`.
   If the older Base-to-Kanban extraction is also necessary, prepend
   `kanban_extraction.py` to the comma-separated script list. The CRM script hands off
   47 declared XML IDs, nine models, their fields, selections, constraints and relation
   ownership. Existing business record IDs and rows are preserved. An incomplete or
   conflicting inventory aborts the upgrade. The script marks the historical
   `marketing_center_contact_center_crm` consumer for upgrade in the same registry load;
   it does not mark the current `marketing_center_contact_center` consumer. The same
   transaction sets the technical parameter
   `contact_center_crm.conversation_extraction_state` to `pending`.
2. Load the complete new registry in an offline Odoo shell, with
   `ODOO_QUEUE_JOB_CHANNELS=root:0`, and execute:

   ```python
   import runpy
   result = runpy.run_path(
       "/path/contact-center/release_migrations/crm_conversation_backfill.py"
   )["migrate"](env)
   env.cr.commit()
   print(result)  # Counts only; no customer or message contents.
   ```

   The ORM operation backfills active case/lead pairs into unique conversation/lead
   pairs, retaining the first source link's origin and audit timestamps. It retires old
   standalone Marketing convergence jobs, revokes the old case authority and rebuilds
   attribution through the new conversation authority in bounded pages. It does not
   change customers, teams, opportunities, stages or pipeline defaults. Running it again
   creates no duplicate links or revocations. Started jobs or job dependency graphs
   require investigation and cause the operation to abort.

3. Verify the new link for every active former case link, preserved cases/mappings,
   absence of live `contact_center.case` Marketing assertions, and the independent CRM
   and integrated test results. Restart workers only after all checks pass.

The post-migration changes the technical state to `done` through ORM only after
backfill, reconciliation and constraint checks succeed. An interruption after the module
upgrade, or a rollback of the post-migration, leaves `pending` and continues to block
release even though the old XML IDs have already moved.

The metadata handoff shares the Odoo upgrade transaction. The ORM backfill is a separate
transaction after the new registry is available. A failure leaves shared services
stopped; restore the paired backup and both previous source revisions. Do not attempt to
downgrade the schema by uninstalling/reinstalling addons.

`odoo16_contact_center_lab_release.py` contains both migration phases and a separate CRM
test database. Before applying, it reads the fixed old CRM ownership inventory and the
technical completion state. **Only releases that require or have not completed this
extraction are blocked**: the historical runner waives database backups and cannot
verify a paired, restorable backup. Fresh installations and databases already migrated
retain their normal release paths. There is no flag to bypass the extraction guard.
Dry-run and isolated QA remain available; the actual laboratory extraction is pending
that operational requirement. Historical Base-extraction resume evidence cannot approve
it.

OCA CI uses the documented `INCLUDE` and `PGDATABASE` variables to run this same
separate installation:
[OCA CI command contract](https://github.com/OCA/oca-ci/blob/master/README.md). No
production deployment is implied by local tests or this procedure.
