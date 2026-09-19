Agents can open the customer panel from the handshake button in the Contact
Center inbox. Four tabs show leads and opportunities, quotations, orders and customer
invoices, with links to their native Odoo forms.

CRM records include explicit conversation associations and exact phone candidates,
including prospects without a customer record. The panel labels links separately
from suggestions and requires explicit selection before linking. Customer sales
documents belong to the contact and their commercial company. Every projection
respects native access rights and the inbox company. Reading the panel does not
create associations or change marketing attribution.

The **Conversar** action on a CRM record reuses the existing Contact Center
conversation admission API and links the result. It does not send messages,
create a partner or change the lead's type or stage. Optional automation addons
can reuse ``crm.lead._contact_center_start_and_link(account, phone=None)``, which
returns an authorized ``mail.channel`` record. Message dispatch remains separate.

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

External-campaign mapping to native CRM UTM fields and GCLID enrichment belong
to Marketing Center. This module does not perform those lookups while opening
or linking a conversation.
