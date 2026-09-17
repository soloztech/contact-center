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

Configuration and diagnosis
===========================

#. Install ``contact_center_crm`` with its declared ``contact_center_ui`` and ``crm``
   dependencies. No Kanban mapping is required for the customer panel.
#. Grant the agent the intended inbox access and native document permissions.
   Installing this addon does not grant Sales or Accounting access.
#. Link the conversation's identity to the correct contact. The panel searches
   through ``commercial_partner_id`` within the inbox company and native rules.
#. Treat a panel read and a commercial association as different operations. The
   explicit link API requires a permitted, active CRM record belonging to the
   customer; a repeated existing link reuses its association.
#. Use Kanban's own actions if an Atendimento must create or link a CRM record.
   Its company, sales-team and stage prerequisites are additional to panel access.

An empty panel can mean no linked contact, a different commercial customer, a company
restriction or no readable documents. An unavailable tab means a missing native model
or read permission. Neither condition should be repaired by creating a duplicate lead.
A missing conversation association must be diagnosed in the association ledger,
not inferred from the customer's list of opportunities.

When allowed by native Sales, **New Quotation** opens the native form with the
commercial customer and inbox company as defaults. The user saves the quotation
explicitly; opening this form does not create a CRM lead.

For campaign evidence, see the `cross-project intake and attribution contract
<https://github.com/soloztech/marketing-center/blob/16.0/docs/crm-intake-and-attribution.md>`_.
