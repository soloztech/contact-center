Open a conversation in the Contact Center inbox and click the handshake icon
next to the contact details button. Select ``Leads e oportunidades``, ``Cotações``,
``Pedidos`` or ``Faturas`` to browse the linked contact's commercial records.

Search and pagination apply to the selected tab. Opening a record uses the
native Odoo form; the panel refreshes when that form closes. An agent needs
access both to the conversation and to the document. CRM records include explicit
conversation links and exact normalized phone matches, even without a contact.
Phone suggestions require clicking **Vincular à conversa**; no match is linked
automatically, including when there is a single candidate. Sales and invoice tabs
still require a linked contact.

From a lead or opportunity, click **Conversar**, select an eligible WhatsApp inbox
and one of the lead's phone numbers. The wizard opens or reuses the conversation
and records the association, without creating a customer or sending a message.
Update the lead first if its phone is missing or invalid. Existing CRM type and
stage are preserved.

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
#. Link the conversation's identity to the correct contact for sales documents.
   CRM also supports explicit conversation links and exact registered phones.
#. Treat a panel read and a commercial association as different operations. The
   explicit link API requires a permitted, active CRM record belonging to the
   customer or an exact normalized phone; a repeated existing link reuses its
   association. CRM ACLs and company scope apply to every suggestion and link.
#. Use Kanban's own actions if an Atendimento must create or link a CRM record.
   Its company, sales-team and stage prerequisites are additional to panel access.

An empty CRM tab means there are no readable linked records, customer records or
exact phone candidates in this company. An unavailable tab means a missing native
model or read permission. Neither condition should be repaired by creating a
duplicate lead. **Vinculado a esta conversa** identifies an explicit association;
**Mesmo telefone — confirme o vínculo** identifies a suggestion only.

When allowed by native Sales, **New Quotation** opens the native form with the
commercial customer and inbox company as defaults. The user saves the quotation
explicitly; opening this form does not create a CRM lead.

For campaign evidence, see the `cross-project intake and attribution contract
<https://github.com/soloztech/marketing-center/blob/16.0/docs/crm-intake-and-attribution.md>`_.

Initial salesperson for commercial WhatsApp intake
==================================================

Automatic intake is opt-in per commercial inbox. On the first eligible customer
message, it reuses an unambiguous accessible business or creates a lead. A new
automatic lead has **Created by** empty, while its intake receipt and conversation
association preserve the technical executor audit. Earlier creator history is
unchanged.

The conversation's eligible current assignee becomes the new lead's salesperson.
If the conversation is unassigned, the lead's salesperson stays empty until its
first eligible assignment. Processing uses the persisted assignment history in
sequence order and checks permissions when the job runs. Events already examined
as ineligible are consumed: a later permission grant does not revive them.
Events not yet examined use the permissions available when they are processed.
The salesperson remains editable in CRM; choosing one manually cancels waiting
automatic assignment. Converting to an opportunity without choosing a salesperson
keeps that initial assignment pending.

Later transfers or unassignments in the inbox do not synchronize the salesperson.
Reused businesses keep their existing commercial ownership. An intake administrator
can recover pending initial assignment with the existing recovery action on at
most 100 selected conversations, within the available companies. Recovery checks
the original active association, current permissions and pending state again; it
does not overwrite a manual choice, detached association or merged business.
After fixing permissions for an already-consumed assignment, select the salesperson
in CRM or make a new conversation assignment. Recovery does not replay that event.
Disabling automatic intake also pauses initial salesperson assignment, even if
the executor stays configured. Pending state is preserved; after re-enabling,
explicit recovery can continue. The same policy and automation guards required
for lead creation are checked before automatic salesperson assignment.
