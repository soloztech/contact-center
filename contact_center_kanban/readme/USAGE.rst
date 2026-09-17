Create one or more service pipelines, enable them for the appropriate Contact
Center teams and choose the default pipeline for each inbox. Every conversation
then receives one canonical default case, while additional cases may move through
the same pipeline independently.

The Kanban is available from *Contact Center > Atendimentos*. Pipeline
configuration is available to Contact Center administrators.

Explicit CRM actions
====================

This addon depends on ``contact_center_crm`` and is optional for the inbox and
customer panel. Before using a case's CRM actions, configure an active pipeline
binding to a CRM sales team and map the case's stages to effective CRM stages.
If a roster binding exists, it must reference the same sales team. Mapping-inventory
suggestions require an administrator's explicit acceptance.

* **Create CRM lead** calls ``action_create_crm_lead`` with type ``lead``.
* **Create CRM opportunity** calls ``action_create_crm_opportunity`` with type
  ``opportunity``. These explicit choices do not use Marketing Center's ``native``
  route policy.
* Both creation actions require an active, unlinked case and CRM permissions. They
  set the inbox company, mapped sales team/stage, no salesperson, and an already
  linked direct-contact identity when available. They do not create a contact or
  infer one from an ad referral.
* **Link CRM lead** associates an existing permitted record after checking the
  company, sales team and mapped stage. It creates both the conversation association
  and the case projection without creating another lead.
* Removing a case's link preserves the conversation association and keeps unlink
  evidence. Removing the conversation association retires its matching active case
  projections. Neither operation deletes the CRM record.

Creating a default service case is not CRM lead creation. A customer message,
``fbads`` hint or origin card does not invoke the case's commercial actions.

Stage and access diagnosis
==========================

For a blocked create/link, check case activity, an existing active link, native CRM
rights, company, pipeline binding and stage mapping. For a blocked transition, check
that the destination maps to the linked lead's CRM team. A permitted mapped case
transition writes the native CRM stage; native CRM changes project back to linked
cases. Do not repair a mapping error by writing directly to link or stage ledgers.

Roster binding and pipeline binding have separate lifecycles. Disabling roster
synchronization does not remove commercial links or stop their stage projection.
Inspect managed-role provenance before changing grants.

Catalog matching and CRM evidence association are separate from these actions. See the
`CRM and attribution guide (Portuguese)
<https://github.com/soloztech/contact-center/blob/16.0/docs/crm-and-attribution.md>`_.
