Open a conversation in the Contact Center inbox and click the handshake icon
next to the contact details button. The CRM panel lists related opportunities
and distinguishes those already linked to the current conversation.

Use ``Vincular`` or ``Desvincular`` to manage the association. Opening a CRM
record uses the native Odoo form. The panel refreshes when that form closes.
Linking an opportunity does not change its customer, sales team or pipeline
stage. An agent needs access both to the conversation and to the CRM record.

The optional Kanban addon presents its records as ``Atendimentos``. Unlinking
one Kanban record does not remove the conversation's CRM association. Removing
the conversation association also disconnects the corresponding Kanban stage
projections, while preserving their history and unrelated associations.

For existing pre-release installations, use the explicit CRM extraction and
backfill scripts in ``release_migrations`` through the atomic release runner.
Do not upgrade only this addon over the former CRM/Kanban bridge installation.
