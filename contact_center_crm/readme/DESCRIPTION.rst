Agents can open the customer panel from the handshake button in the Contact
Center inbox. Four tabs show opportunities, quotations, orders and customer
invoices, with links to their native Odoo forms.

Records belong to the contact and their commercial company. Conversations
associated with the same customer show the same records within the agent's
native access rights and the inbox's company. Reading the panel does not create
conversation associations or change marketing attribution. Existing historical
conversation-to-lead associations remain separate from this customer view.

This addon requires the chat UI and native CRM. It does not install Kanban,
create service cases, choose sales teams or change opportunity stages. Optional
pipeline and stage synchronization lives in ``contact_center_kanban``, which
depends on this addon.

Sales and Accounting are optional: their tabs become available when the native
models are installed and the agent has read access. No financial access is
granted by this addon. Supplier bills and cancelled documents are excluded.
