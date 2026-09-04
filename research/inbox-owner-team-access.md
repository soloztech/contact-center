# Inbox access: individual owner and access team

Date: 2026-08-31

Status: implemented and deployed to SERVIDOR05 on 2026-09-01 in
`contact_center_base 16.0.1.31.0` and `contact_center_crm 16.0.2.0.1`. Release evidence
and test totals are recorded in
[`../reviews/2026-09-01-owner-team-pipeline-crm-release-validation.md`](../reviews/2026-09-01-owner-team-pipeline-crm-release-validation.md).

## Reference inspected

The `18.0` branch of `lcsztl/discuss-hub` separates direct and team grants on a gateway:

- `mail.gateway.discuss_agent_ids` grants selected users directly;
- `mail.gateway.discuss_team_ids` grants the members of selected Discuss teams;
- every gateway and team owns a generated `res.groups` record;
- team groups imply gateway groups, so the effective gateway audience is the union of
  direct agents and team members;
- `mail.discuss.team.user_id` is the team leader and is automatically included in the
  team members. It is not an exclusive owner of a gateway;
- CRM and Helpdesk teams are integrations of a Discuss team. They do not replace the
  gateway authorization group.

This is a useful authorization pattern, but copying the dynamic group graph is not
necessary in the Odoo 16 Contact Center because its canonical conversations already use
exact `mail.channel.member` reconciliation.

## Contact Center decision

`contact.center.account` is the authorization boundary for an inbox:

- `owner_user_id`: optional direct individual grant;
- `default_team_id`: optional access team. The technical name is retained for schema
  compatibility; its product meaning is "Access Team", not owner;
- effective operational users are the union of the owner and the team's agents and
  supervisors;
- owner only means an exclusive inbox;
- team only means a shared inbox;
- owner plus team means a primary owner with shared access;
- an inbox with neither grant fails closed and cannot be activated for traffic;
- Contact Center/System administrators keep their explicit company-level exception.

Provider connections inherit their account scope and never define an independent
audience. Conversations project the account owner/team and reconcile their partner
members exactly, including retired/inactive bindings whose history remains readable
through native channel membership. Changing either grant removes obsolete memberships
and invalid responsible assignments while preserving remote guests and message history.

An owner may read the roster of the optional team attached to their inbox, but this
direct grant never authorizes roster mutation. Team agents/supervisors manage teams only
under their normal role and team record rules; Contact Center administrators retain the
explicit company-level exception.

The upgrade never guesses ownership from old channel members. A legacy inbox that has
conversations or a live provider route but neither owner nor team aborts migration with
its technical account IDs, so an administrator can assign the intended grant before
retrying. Empty draft inboxes may remain unscoped and fail closed.

CRM and future Helpdesk bindings remain optional adapters around the native Contact
Center authorization boundary. For CRM, an explicit active 1:1 team binding makes the
CRM roster authoritative and projects the sales leader to Contact Center supervisor and
active sales members to Contact Center agents. Access is still granted by the inbox's
`default_team_id` and native channel membership; no CRM record rule is reused inside
messaging.

The projection is exact and immediate for membership removal, while Contact Center role
groups are granted additively because a user may participate in another inbox. The bound
Contact Center roster is read-only. Deactivating or removing the binding freezes the
last projected roster and returns the team to native/manual management. The inbox owner
remains an independent direct grant throughout this lifecycle.

## Optional automatic assignment

Each inbox may optionally select one `auto_assignment_user_id`. The selector is bounded
to the same effective `owner_user_id ∪ access-team roster` used for authorization; it is
not a second access grant and never makes an otherwise unauthorized user eligible.

When enabled, a newly created conversation is assigned before its default service case
is projected. An existing unassigned conversation is assigned only when a genuinely new
inbound message is accepted. Exact webhook replays return before assignment, an existing
responsible is never overwritten, and enabling the option does not bulk-change
historical conversations. The rule is provider-neutral and applies equally to direct and
group conversations supported by the account.

Owner, team or roster revocation clears the optional policy automatically if the
selected user leaves the effective scope. The existing membership reconciliation also
clears any conversation responsibility that became unauthorized. Leaving the field empty
preserves the manual **Assumir** workflow.
