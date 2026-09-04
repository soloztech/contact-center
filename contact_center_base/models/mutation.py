from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools import html_escape

from ..services.dto import MAX_MUTATION_PROVIDER_REVISION
from ..services.tokens import CONTACT_CENTER_POST_TOKEN


class ContactCenterMessageMutation(models.Model):
    _name = "contact.center.message.mutation"
    _description = "Contact Center Message Mutation"
    _order = "occurred_at desc, id desc"

    target_message_binding_id = fields.Many2one(
        "contact.center.message.binding", required=True, index=True, ondelete="cascade"
    )
    channel_binding_id = fields.Many2one(
        related="target_message_binding_id.channel_binding_id",
        store=True,
        readonly=True,
        index=True,
    )
    account_id = fields.Many2one(
        related="target_message_binding_id.account_id",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="target_message_binding_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    provider_connection_id = fields.Many2one(
        "contact.center.provider.connection",
        required=True,
        index=True,
        ondelete="restrict",
    )
    external_event_id = fields.Char(index=True, copy=False)
    client_request_id = fields.Char(index=True, copy=False)
    mutation_type = fields.Selection(
        [("react", "Reaction"), ("edit", "Edit"), ("delete", "Delete")],
        required=True,
        index=True,
    )
    direction = fields.Selection(
        [("inbound", "Inbound"), ("outbound", "Outbound")],
        required=True,
        index=True,
    )
    actor_user_id = fields.Many2one("res.users", index=True, ondelete="set null")
    actor_partner_id = fields.Many2one("res.partner", index=True, ondelete="set null")
    actor_guest_id = fields.Many2one("mail.guest", index=True, ondelete="set null")
    reaction_emoji = fields.Char()
    reaction_operation = fields.Selection(
        [("add", "Add"), ("remove", "Remove")], default="add"
    )
    new_text = fields.Text()
    deletion_display_mode = fields.Selection(
        [("redact", "Redact"), ("strike", "Strike Through")],
        required=True,
        default="redact",
        readonly=True,
        copy=False,
        help=(
            "Immutable inbox-policy snapshot captured when this mutation is "
            "created. It is meaningful only for delete mutations."
        ),
    )
    provider_revision = fields.Integer(copy=False, index=True)
    has_provider_revision = fields.Boolean(copy=False, default=False)
    occurred_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True
    )
    state = fields.Selection(
        [("pending", "Pending"), ("applied", "Applied"), ("failed", "Failed")],
        required=True,
        default="pending",
        index=True,
        copy=False,
    )
    applied_at = fields.Datetime(copy=False)
    details_json = fields.Json(default=dict, copy=False)

    _sql_constraints = [
        (
            "provider_event_unique",
            "unique(provider_connection_id, external_event_id)",
            "This provider mutation event already exists.",
        ),
        (
            "connection_account_match",
            "check(provider_connection_id IS NOT NULL)",
            "A provider connection is required.",
        ),
    ]

    @api.constrains("provider_connection_id", "target_message_binding_id")
    def _check_scope(self):
        for mutation in self:
            target = mutation.target_message_binding_id
            if mutation.provider_connection_id.account_id != target.account_id:
                raise ValidationError(
                    _("The mutation provider belongs to another account.")
                )
            if mutation.provider_connection_id != target.provider_connection_id:
                raise ValidationError(
                    _(
                        "A message mutation must use the provider connection that "
                        "owns the target message."
                    )
                )

    @api.constrains("mutation_type", "reaction_emoji", "reaction_operation", "new_text")
    def _check_payload(self):
        for mutation in self:
            if mutation.mutation_type == "react":
                if (
                    mutation.reaction_operation == "add"
                    and not (mutation.reaction_emoji or "").strip()
                ):
                    raise ValidationError(_("An added reaction requires an emoji."))
            elif (
                mutation.mutation_type == "edit"
                and not (mutation.new_text or "").strip()
            ):
                raise ValidationError(_("An edited message cannot be empty."))

    @api.constrains("mutation_type", "provider_revision", "has_provider_revision")
    def _check_provider_revision(self):
        for mutation in self:
            if not mutation.has_provider_revision:
                continue
            if mutation.mutation_type != "edit":
                raise ValidationError(
                    _("A provider revision can only be recorded for an edit.")
                )
            if not 0 <= mutation.provider_revision <= MAX_MUTATION_PROVIDER_REVISION:
                raise ValidationError(
                    _("The provider revision must be a bounded non-negative integer.")
                )

    @staticmethod
    def _validated_provider_revision(value):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > MAX_MUTATION_PROVIDER_REVISION
        ):
            raise ValidationError(
                _("The provider revision must be a bounded non-negative integer.")
            )
        return value

    @api.model_create_multi
    def create(self, vals_list):
        prepared_values = []
        for incoming_values in vals_list:
            values = dict(incoming_values)
            target = self.env["contact.center.message.binding"]
            if values.get("mutation_type") == "delete" and values.get(
                "target_message_binding_id"
            ):
                target = (
                    target.sudo().browse(values["target_message_binding_id"]).exists()
                )
            values["deletion_display_mode"] = (
                "strike"
                if target and target.account_id.show_deleted_message_content
                else "redact"
            )
            revision_provided = "provider_revision" in values
            provider_revision = values.get("provider_revision")
            if not revision_provided:
                details = values.get("details_json")
                if isinstance(details, dict) and "provider_revision" in details:
                    provider_revision = details["provider_revision"]
                    revision_provided = True
            if revision_provided:
                provider_revision = self._validated_provider_revision(provider_revision)
                values.update(
                    {
                        "provider_revision": provider_revision,
                        "has_provider_revision": True,
                    }
                )
            elif values.get("has_provider_revision"):
                raise ValidationError(
                    _("A provider revision value is required when it is present.")
                )
            else:
                values["has_provider_revision"] = False
            prepared_values.append(values)
        return super().create(prepared_values)

    def unlink(self):  # pylint: disable=method-required-super
        """Keep provider mutation evidence append-only.

        Projection state evolves internally, but deleting its originating fact
        would make message edits, removals and reactions unauditable.  Cascades
        during table teardown remain a database concern and do not need an ORM
        escape hatch.
        """

        raise AccessError(_("Message mutation evidence cannot be deleted."))

    def _actor_reaction_domain(self):
        self.ensure_one()
        domain = [("message_id", "=", self.target_message_binding_id.message_id.id)]
        if self.actor_guest_id:
            return domain + [("guest_id", "=", self.actor_guest_id.id)]
        partner = self.actor_partner_id or self.actor_user_id.partner_id
        if partner:
            if self._is_self_reaction():
                # The provider account is one external actor even when several
                # operators use it or its configured technical author changes.
                # Only partners evidenced by an applied self-side ledger entry
                # belong to this lane; unrelated native Odoo reactions survive.
                self_reactions = self.search(
                    [
                        (
                            "target_message_binding_id",
                            "=",
                            self.target_message_binding_id.id,
                        ),
                        ("mutation_type", "=", "react"),
                        ("direction", "=", "outbound"),
                        ("actor_guest_id", "=", False),
                        ("state", "=", "applied"),
                    ]
                )
                partner_ids = set(self_reactions.actor_partner_id.ids)
                partner_ids.update(self_reactions.actor_user_id.partner_id.ids)
                partner_ids.add(partner.id)
                technical_author = self.account_id.technical_author_id
                if technical_author:
                    partner_ids.add(technical_author.id)
                return domain + [("partner_id", "in", sorted(partner_ids))]
            return domain + [("partner_id", "=", partner.id)]
        raise ValidationError(_("A reaction actor is required."))

    def _is_self_reaction(self):
        self.ensure_one()
        return (
            self.mutation_type == "react"
            and self.direction == "outbound"
            and not self.actor_guest_id
        )

    def _reaction_lane_domain(self):
        self.ensure_one()
        domain = [
            ("target_message_binding_id", "=", self.target_message_binding_id.id),
            ("mutation_type", "=", "react"),
            ("state", "=", "applied"),
        ]
        if self._is_self_reaction():
            return domain + [
                ("direction", "=", "outbound"),
                ("actor_guest_id", "=", False),
            ]
        if self.actor_guest_id:
            return domain + [("actor_guest_id", "=", self.actor_guest_id.id)]
        partner = self.actor_partner_id or self.actor_user_id.partner_id
        if not partner:
            raise ValidationError(_("A reaction actor is required."))
        return domain + [
            ("actor_guest_id", "=", False),
            ("actor_partner_id", "=", partner.id),
        ]

    def _latest_applied(self, mutation_type):
        self.ensure_one()
        if mutation_type == "react":
            domain = self._reaction_lane_domain()
        else:
            domain = [
                ("target_message_binding_id", "=", self.target_message_binding_id.id),
                ("mutation_type", "=", mutation_type),
                ("state", "=", "applied"),
            ]
        return self.search(domain, order="occurred_at desc, id desc", limit=1)

    def _is_superseded_by(self, latest):
        self.ensure_one()
        if not latest:
            return False
        return (latest.occurred_at, latest.id) >= (self.occurred_at, self.id)

    def _superseding_applied_edit(self):
        """Find an edit that authoritatively precedes this projection.

        Numeric provider revisions are compared only when both edits expose one.
        Providers without revisions retain the existing timestamp/id ordering.
        """

        self.ensure_one()
        domain = [
            ("target_message_binding_id", "=", self.target_message_binding_id.id),
            ("mutation_type", "=", "edit"),
            ("state", "=", "applied"),
        ]
        if not self.has_provider_revision:
            latest = self.search(domain, order="occurred_at desc, id desc", limit=1)
            return latest if self._is_superseded_by(latest) else self.browse()

        revisioned = self.search(
            domain + [("has_provider_revision", "=", True)],
            order="provider_revision desc, occurred_at desc, id desc",
            limit=1,
        )
        if revisioned and (
            revisioned.provider_revision > self.provider_revision
            or (
                revisioned.provider_revision == self.provider_revision
                and self._is_superseded_by(revisioned)
            )
        ):
            return revisioned

        # A provider may start emitting revisions mid-stream. Revisionless edits
        # remain ordered by their timestamp/id evidence instead of being discarded.
        revisionless = self.search(
            domain + [("has_provider_revision", "=", False)],
            order="occurred_at desc, id desc",
            limit=1,
        )
        if self._is_superseded_by(revisionless):
            return revisionless
        return self.browse()

    def _mark_applied(self, *, reason=None, superseded_by=None):
        self.ensure_one()
        values = {"state": "applied", "applied_at": fields.Datetime.now()}
        if reason:
            details = dict(self.details_json or {})
            projection = {"applied": False, "reason": reason}
            if superseded_by:
                projection["superseded_by_id"] = superseded_by.id
            details["projection"] = projection
            values["details_json"] = details
        self.write(values)

    def _apply_reaction(self):
        self.ensure_one()
        if self.target_message_binding_id.message_state == "deleted":
            self._mark_applied(reason="target_deleted")
            return False
        latest = self._latest_applied("react")
        if self._is_superseded_by(latest):
            self._mark_applied(reason="superseded", superseded_by=latest)
            return False
        reaction_model = self.env["mail.message.reaction"].sudo()
        existing = reaction_model.search(self._actor_reaction_domain())
        if existing:
            existing.unlink()
        if self.reaction_operation == "add":
            values = {
                "message_id": self.target_message_binding_id.message_id.id,
                "content": self.reaction_emoji,
            }
            if self.actor_guest_id:
                values["guest_id"] = self.actor_guest_id.id
            else:
                partner = self.actor_partner_id or self.actor_user_id.partner_id
                values["partner_id"] = partner.id
            reaction_model.create(values)
        self._mark_applied()
        return True

    def _apply_edit(self):
        self.ensure_one()
        target = self.target_message_binding_id
        if target.message_state == "deleted":
            self._mark_applied(reason="target_deleted")
            return False
        superseding_edit = self._superseding_applied_edit()
        if superseding_edit:
            self._mark_applied(reason="superseded", superseded_by=superseding_edit)
            return False
        values = {"message_state": "edited", "edited_at": self.occurred_at}
        if target.message_state == "active":
            values["original_body"] = str(target.message_id.body or "")
        target.write(values)
        target.message_id.with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).write({"body": html_escape((self.new_text or "").strip())})
        self._mark_applied()
        return True

    def _purge_redacted_operational_content(self, target):
        """Remove operator-facing content while retaining the correlation shell."""

        self.ensure_one()
        message = target.message_id.sudo()
        media_items = target.media_ids.sudo()
        attachments = (message.attachment_ids | media_items.attachment_id).exists()
        reactions = message.reaction_ids
        if reactions:
            reactions.unlink()
        if attachments:
            attachments.with_context(
                contact_center_post_token=CONTACT_CENTER_POST_TOKEN
            ).unlink()
        if media_items:
            media_items.write(
                {
                    "attachment_id": False,
                    "state": "discarded",
                    "last_error_class": "DeletedMessageContentHidden",
                    "last_error_message": (
                        "Media removed by the inbox deletion policy."
                    ),
                }
            )

    def _apply_delete(self):
        self.ensure_one()
        target = self.target_message_binding_id
        if target.message_state == "deleted":
            if not target.deleted_at or self.occurred_at > target.deleted_at:
                target.write({"deleted_at": self.occurred_at})
            self._mark_applied(reason="already_deleted")
            return False
        values = {
            "message_state": "deleted",
            "deleted_at": self.occurred_at,
            "deleted_display_mode": self.deletion_display_mode,
            "deleted_body": (
                str(target.message_id.body or "")
                if self.deletion_display_mode == "strike"
                else False
            ),
        }
        if self.deletion_display_mode == "redact":
            values["original_body"] = False
        elif target.message_state == "active":
            values["original_body"] = str(target.message_id.body or "")
        target.write(values)
        target.message_id.with_context(
            contact_center_post_token=CONTACT_CENTER_POST_TOKEN
        ).write({"body": html_escape(_("Message deleted"))})
        if self.deletion_display_mode == "redact":
            self._purge_redacted_operational_content(target)
        self._mark_applied()
        return True

    def _apply_projection(self):
        for mutation in self.sudo():
            if mutation.state == "applied":
                continue
            target = mutation.target_message_binding_id
            self.env.cr.execute(
                "SELECT id FROM contact_center_message_binding WHERE id = %s FOR UPDATE",
                [target.id],
            )
            target.invalidate_recordset(
                [
                    "message_state",
                    "edited_at",
                    "deleted_at",
                    "deleted_display_mode",
                    "deleted_body",
                    "original_body",
                    "mutation_projection_revision",
                ]
            )
            mutation.invalidate_recordset(["state", "applied_at", "details_json"])
            if mutation.state == "applied":
                continue
            # The row lock alone is insufficient under PostgreSQL REPEATABLE READ
            # when a reaction changes only mail.message.reaction.  Persisting a
            # revision makes every projection update the locked row, so a worker
            # that established an older snapshot is aborted and retried instead of
            # applying stale reaction state.
            target.write(
                {
                    "mutation_projection_revision": (
                        target.mutation_projection_revision + 1
                    )
                }
            )
            if mutation.mutation_type == "react":
                changed = mutation._apply_reaction()
            elif mutation.mutation_type == "edit":
                changed = mutation._apply_edit()
            elif mutation.mutation_type == "delete":
                changed = mutation._apply_delete()
            if changed:
                self.env["contact.center.application"]._notify_ui(
                    target.channel_binding_id.channel_id,
                    "message_updated",
                    {"message_id": target.message_id.id},
                )
        return True
