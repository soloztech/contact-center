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

Integration ownership
=====================

``contact.center.crm.conversation.link`` records explicit historical associations
between conversations and native ``crm.lead`` records. The local API's
``link_crm_opportunity`` and ``unlink_crm_opportunity`` operations maintain that
ledger independently of customer-panel reads. Native CRM remains authoritative for
the business record. An existing document being visible in the panel does not mean
it is associated with this conversation.

This addon does not turn messages, ad referrals or ``fbads`` hints into leads and
does not assign native UTM fields. Optional acquisition-evidence and CRM-link projection belong to `Marketing Center
<https://github.com/soloztech/marketing-center/tree/16.0>`_ and its separately
installed bridges.

Automatic external-campaign mapping to native CRM UTM fields and GCLID-to-campaign
lookup are not implemented product workflows. Catalog evidence and a CRM link do
not imply either result.
