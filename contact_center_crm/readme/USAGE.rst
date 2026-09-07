Open a conversation in the Contact Center inbox and click the handshake icon
next to the contact details button. Select ``Oportunidades``, ``Cotações``,
``Pedidos`` or ``Faturas`` to browse the linked contact's commercial records.

Search and pagination apply to the selected tab. Opening a record uses the
native Odoo form; the panel refreshes when that form closes. An agent needs
access both to the conversation and to the document. If there is no linked
contact, the panel asks for one instead of searching all customers.

Quotations are draft or sent sale orders; confirmed and locked sale orders
appear under orders. Customer invoices and credit notes appear under invoices,
with credit notes distinguished by type and negative amounts. A tab explains
when its module or read permission is unavailable.

The optional Kanban addon presents its records as ``Atendimentos``. Its
conversation-to-lead associations and stage projections have their own
lifecycle. Browsing customer records does not change those associations.

For existing pre-release installations, use the explicit CRM extraction and
backfill scripts in ``release_migrations`` through the atomic release runner.
Do not upgrade only this addon over the former CRM/Kanban bridge installation.
