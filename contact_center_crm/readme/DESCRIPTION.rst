CRM integration belongs to the conversation. Agents can open a customer CRM
panel from the handshake button in the Contact Center inbox, inspect the
customer's opportunities, open their native CRM forms and explicitly link or
unlink them.

A conversation may reference multiple leads or opportunities. Related customer
records are suggestions, not automatic associations. The panel includes records
for the linked person and their commercial company, subject to native CRM access
rules and the conversation's company. Existing explicit links remain visible
when the contact changes, if the agent still has access to those CRM records.

This addon requires the chat UI and native CRM. It does not install Kanban,
create service cases, choose sales teams or change opportunity stages. Optional
pipeline and stage synchronization lives in ``contact_center_kanban``, which
depends on this addon.
