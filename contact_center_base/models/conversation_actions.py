"""Account-scoped conversation privacy operations and ingress admission."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.osv import expression

from ..services.dto import SCHEMA_VERSION
from ..services.tokens import (
    CONTACT_CENTER_DELETION_TOKEN,
    CONTACT_CENTER_MEMBERSHIP_TOKEN,
    CONTACT_CENTER_POST_TOKEN,
    CONTACT_CENTER_PRODUCTIVITY_TOKEN,
)

# Conversation policy is a cohesive service lane spanning the account, binding,
# inbox-event and UI API models. Odoo composes these `_inherit` fragments with
# the independently maintained group and productivity lanes at registry load.
# pylint: disable=consider-merging-classes-inherited

_POLICY_TOKEN = object()


def _policy_context():
    return {"contact_center_conversation_policy_token": _POLICY_TOKEN}


class ConversationPolicyAccount(models.Model):
    _inherit = "contact.center.account"

    conversation_policy_revision = fields.Integer(default=0, readonly=True, copy=False)

    @api.model_create_multi
    def create(self, values_list):
        if any("conversation_policy_revision" in values for values in values_list):
            raise AccessError(
                _("Conversation policy revisions are managed internally.")
            )
        return super().create(values_list)

    def write(self, values):
        if (
            "conversation_policy_revision" in values
            and self.env.context.get("contact_center_conversation_policy_token")
            is not _POLICY_TOKEN
        ):
            raise AccessError(
                _("Conversation policy revisions are managed internally.")
            )
        return super().write(values)

    def _lock_conversation_policy(self):
        self.ensure_one()
        self.env["contact.center.application"]._lock_inbound_account_scope(self.sudo())
        self.invalidate_recordset()
        self.sudo().with_context(**_policy_context()).write(
            {"conversation_policy_revision": self.conversation_policy_revision + 1}
        )


class ContactCenterConversationIgnore(models.Model):
    _name = "contact.center.conversation.ignore"
    _description = "Ignored Contact Center Conversation"
    _order = "account_id, name, id"

    name = fields.Char(required=True, readonly=True)
    active = fields.Boolean(default=True, readonly=True)
    account_id = fields.Many2one(
        "contact.center.account",
        required=True,
        ondelete="restrict",
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="account_id.company_id", store=True, index=True
    )
    identity_id = fields.Many2one(
        "contact.center.identity", ondelete="restrict", readonly=True, index=True
    )
    conversation_type = fields.Selection(
        [("direct", "Contact"), ("group", "Group")], required=True, readonly=True
    )
    conversation_ref = fields.Char(required=True, readonly=True, index=True)
    policy_key = fields.Char(required=True, readonly=True, index=True)
    address_keys_json = fields.Json(default=list, readonly=True)

    _sql_constraints = [
        (
            "account_policy_unique",
            "unique(account_id, policy_key)",
            "This conversation already has an inbox policy.",
        )
    ]

    @api.model_create_multi
    def create(self, values_list):
        self._check_service()
        return super().create(values_list)

    def write(self, values):
        self._check_service()
        return super().write(values)

    def unlink(self):
        self._check_service()
        return super().unlink()

    def _check_service(self):
        if (
            self.env.context.get("contact_center_conversation_policy_token")
            is not _POLICY_TOKEN
        ):
            raise AccessError(
                _("Manage ignored conversations through their explicit actions.")
            )

    @api.model
    def _for_binding(self, binding, active_test=True):
        binding.ensure_one()
        rules = (
            self.sudo()
            .with_context(active_test=active_test)
            .search(
                [
                    ("account_id", "=", binding.account_id.id),
                    ("conversation_type", "=", binding.conversation_type),
                ]
            )
        )
        return rules._matching_binding(binding)

    @api.model
    def _ignored_by_binding(self, bindings):
        """Load the ignore projection once for an authorized conversation page."""
        if not bindings:
            return {}
        rules = (
            self.sudo()
            .with_context(active_test=True)
            .search(
                [
                    ("account_id", "in", bindings.account_id.ids),
                    ("conversation_type", "in", bindings.mapped("conversation_type")),
                ]
            )
        )
        rules_by_scope = {}
        for rule in rules:
            scope = (rule.account_id.id, rule.conversation_type)
            rules_by_scope[scope] = rules_by_scope.get(scope, rules.browse()) | rule
        return {
            binding.id: bool(
                rules_by_scope.get(
                    (binding.account_id.id, binding.conversation_type), rules.browse()
                )._matching_binding(binding)
            )
            for binding in bindings
        }

    def _matching_binding(self, binding):
        """Match rules already scoped to the binding's inbox and conversation type."""
        if binding.conversation_type == "group":
            return self.filtered(
                lambda rule: rule.conversation_ref == binding.conversation_ref
            )
        identities = binding.identity_id
        for rule in self:
            identity = rule.identity_id
            seen = set()
            while identity and identity.id not in seen:
                if identity in identities:
                    identities |= rule.identity_id
                    break
                seen.add(identity.id)
                identity = identity.merged_into_id
        return self.filtered(
            lambda rule: rule.identity_id in identities
            or rule.conversation_ref == binding.conversation_ref
        )

    @api.model
    def _matches_route(self, account, route):
        """Read only: never create identities while deciding whether to admit data."""
        if not route or route.get("conversation_type") not in ("direct", "group"):
            return False
        reference = route.get("conversation_ref")
        if not reference:
            return False
        rules = self.sudo().search(
            [
                ("account_id", "=", account.id),
                ("company_id", "=", account.company_id.id),
                ("conversation_type", "=", route["conversation_type"]),
            ]
        )
        if not rules:
            return False
        if route["conversation_type"] == "group":
            return any(rule.conversation_ref == reference for rule in rules)
        keys = {tuple(value) for value in route.get("addresses", ())}
        aliases = (
            self.env["contact.center.identity.alias"]
            .sudo()
            .search(
                expression.AND(
                    [
                        [("account_id", "=", account.id)],
                        expression.OR(
                            [
                                [
                                    ("namespace", "=", key[0]),
                                    ("value_normalized", "=", key[1]),
                                ]
                                for key in keys
                            ]
                        ),
                    ]
                )
            )
            if keys
            else self.env["contact.center.identity.alias"]
        )
        identity_ids = set(aliases.identity_id.ids)
        for rule in rules:
            if rule.conversation_ref == reference or keys.intersection(
                tuple(value) for value in rule.address_keys_json or []
            ):
                return True
            identity = rule.identity_id
            seen = set()
            while identity and identity.id not in seen:
                if identity.id in identity_ids:
                    return True
                seen.add(identity.id)
                identity = identity.merged_into_id
        return False

    @api.model
    def _route(self, connection, envelope):
        reader = getattr(connection.get_adapter(), "conversation_route", None)
        return reader(connection, envelope) if reader else None

    @api.model
    def _ignored_envelope(self, connection, envelope):
        return self._matches_route(
            connection.account_id, self._route(connection, envelope)
        )

    def action_stop_ignoring(self):
        self.check_access_rights("read")
        self.check_access_rule("read")
        for rule in self:
            account = rule.account_id
            if (
                account.company_id not in self.env.companies
                or not account._contact_center_user_can_manage_conversation("ignore")
            ):
                raise AccessError(
                    _("You cannot manage ignored conversations in this inbox.")
                )
            account._lock_conversation_policy()
            rule.invalidate_recordset()
            rule.check_access_rule("read")
            if not account._contact_center_user_can_manage_conversation("ignore"):
                raise AccessError(
                    _("You cannot manage ignored conversations in this inbox.")
                )
            rule.sudo().with_context(**_policy_context()).write({"active": False})
            bindings = (
                self.env["contact.center.channel.binding"]
                .sudo()
                .search(
                    [("account_id", "=", account.id), ("merged_into_id", "=", False)]
                )
            )
            for binding in bindings:
                if rule in self._for_binding(binding, active_test=False):
                    self.env["contact.center.application"]._notify_ui(
                        binding.channel_id,
                        "conversation_updated",
                        {"changed_fields": ["ignored"]},
                    )
        return True


class ContactCenterBindingPrivacy(models.Model):
    _inherit = "contact.center.channel.binding"

    def _contact_center_is_ignored(self):
        self.ensure_one()
        return bool(self.env["contact.center.conversation.ignore"]._for_binding(self))


class ContactCenterInboxPrivacy(models.Model):
    _inherit = "contact.center.inbox.event"

    def _erase_conversation_content(self, reason):
        if (
            self.env.context.get("contact_center_deletion_token")
            is not CONTACT_CENTER_DELETION_TOKEN
            and self.env.context.get("contact_center_conversation_policy_token")
            is not _POLICY_TOKEN
        ):
            raise AccessError(
                _("Only conversation privacy operations can erase inbox content.")
            )
        self.sudo().write(
            {
                # Odoo Json converts an empty object to SQL NULL; this field
                # is required. Keep an explicit, content-free receipt marker.
                "raw_envelope_json": {"content_erased": True},
                "normalized_dto_json": False,
                "metadata_json": {"content_erased": True, "reason": reason},
                "state": "blocked",
                "queue_job_uuid": False,
                "last_error_class": "ConversationContentErased",
                "last_error_message": False,
                "waiting_group_profile_id": False,
                "waiting_group_roster_after": False,
            }
        )

    def _conversation_content_is_blocked(self):
        self.ensure_one()
        self.invalidate_recordset(["metadata_json", "raw_envelope_json"])
        if (self.metadata_json or {}).get("content_erased"):
            return True
        if self.env["contact.center.conversation.ignore"]._ignored_envelope(
            self.provider_connection_id, self.raw_envelope_json
        ):
            self.with_context(**_policy_context())._erase_conversation_content(
                "ignored"
            )
            return True
        return False


class ContactCenterConversationActions(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    def _privacy_authorized_binding(self, channel_id, operation):
        channel, _member = self._authorized_channel(channel_id)
        binding = self._binding_for_channel(channel)
        if (
            not binding
            or not binding.account_id._contact_center_user_can_manage_conversation(
                operation
            )
        ):
            raise AccessError(
                _("This conversation action is not enabled for you in this inbox.")
            )
        binding.account_id._lock_conversation_policy()
        channel, _member = self._authorized_channel(channel_id)
        binding = self._binding_for_channel(channel)
        if (
            not binding
            or not binding.account_id._contact_center_user_can_manage_conversation(
                operation
            )
        ):
            raise AccessError(
                _("This conversation action is not enabled for you in this inbox.")
            )
        return channel, binding

    @api.model
    def set_conversation_ignored(self, channel_id, ignored):
        if type(ignored) is not bool:
            raise ValidationError(_("The ignored value must be a boolean."))
        channel, binding = self._privacy_authorized_binding(channel_id, "ignore")
        if binding.conversation_type not in ("direct", "group"):
            raise ValidationError(
                _("Only contact and group conversations can be ignored.")
            )
        rules = self.env["contact.center.conversation.ignore"]
        existing = rules._for_binding(binding, active_test=False)
        if existing:
            existing.with_context(**_policy_context()).write({"active": ignored})
        elif ignored:
            aliases = binding.identity_id.alias_ids.filtered(
                lambda alias: alias.account_id == binding.account_id
            )
            rules.sudo().with_context(**_policy_context()).create(
                {
                    "name": channel.name,
                    "account_id": binding.account_id.id,
                    "identity_id": binding.identity_id.id,
                    "conversation_type": binding.conversation_type,
                    "conversation_ref": binding.conversation_ref,
                    "policy_key": (
                        ("identity:%s" % binding.identity_id.id)
                        if binding.identity_id
                        else "group:%s" % binding.conversation_ref
                    ),
                    "address_keys_json": [
                        [alias.namespace, alias.value_normalized] for alias in aliases
                    ],
                }
            )
        self._application()._notify_ui(
            channel, "conversation_updated", {"changed_fields": ["ignored"]}
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "item": self._serialize_conversation(channel),
        }

    def _prepare_conversation_deletion_dependencies(
        self, channel, bindings, messages, inbox_events
    ):
        self.env[
            "contact.center.attribution.touchpoint"
        ]._contact_center_prepare_conversation_deletion(
            channel, bindings, messages, inbox_events
        )
        return True

    def _conversation_inbox_events(self, bindings, message_bindings):
        """Include unprojected/unsupported payloads, scoped by their route headers."""
        policy = self.env["contact.center.conversation.ignore"]
        account = bindings.account_id
        result = message_bindings.source_inbox_event_id
        event_model = self.env["contact.center.inbox.event"].sudo()
        aliases = bindings.alias_ids
        keys = {(alias.namespace, alias.value_normalized) for alias in aliases}
        if bindings.identity_id:
            keys.update(
                (alias.namespace, alias.value_normalized)
                for alias in bindings.identity_id.alias_ids
                if alias.account_id == account
            )
        refs = set(bindings.mapped("conversation_ref"))
        last_id = 0
        while True:
            batch = event_model.search(
                [("account_id", "=", account.id), ("id", ">", last_id)],
                order="id",
                limit=500,
            )
            if not batch:
                break
            for event in batch - result:
                if (event.metadata_json or {}).get("content_erased"):
                    continue
                route = policy._route(
                    event.provider_connection_id, event.raw_envelope_json
                )
                if route and (
                    route.get("conversation_ref") in refs
                    or (
                        route.get("conversation_type") == "direct"
                        and keys.intersection(
                            tuple(value) for value in route.get("addresses", [])
                        )
                    )
                ):
                    result |= event
            last_id = batch[-1].id
            batch.invalidate_recordset(
                ["raw_envelope_json", "normalized_dto_json", "metadata_json"]
            )
        return result

    @api.model
    def delete_conversation(self, channel_id):
        channel, binding = self._privacy_authorized_binding(channel_id, "delete")
        channel_id = channel.id
        context = {
            "contact_center_deletion_token": CONTACT_CENTER_DELETION_TOKEN,
            "contact_center_membership_token": CONTACT_CENTER_MEMBERSHIP_TOKEN,
            "contact_center_post_token": CONTACT_CENTER_POST_TOKEN,
            "contact_center_productivity_service_token": CONTACT_CENTER_PRODUCTIVITY_TOKEN,
        }
        service = self.sudo().with_context(**context)
        channel = channel.sudo().with_context(**context)
        bindings = channel.contact_center_binding_ids.with_context(**context)
        messages = service.env["mail.message"].search(
            [("model", "=", "mail.channel"), ("res_id", "=", channel.id)]
        )
        message_bindings = service.env["contact.center.message.binding"].search(
            [("channel_binding_id", "in", bindings.ids)]
        )
        inbox_events = service._conversation_inbox_events(bindings, message_bindings)
        outbox = service.env["contact.center.outbox.command"].search(
            [("channel_binding_id", "in", bindings.ids)]
        )
        if any(
            command.dispatch_started_at
            and command.state not in ("done", "dead", "cancelled")
            for command in outbox
        ):
            raise ValidationError(
                _(
                    "A provider send is in progress or has an uncertain outcome. "
                    "Resolve it before deleting this conversation."
                )
            )
        uploads = service.env["contact.center.media.upload"].search(
            [("channel_binding_id", "in", bindings.ids)]
        )
        media = service.env["contact.center.media.binding"].search(
            [("message_binding_id", "in", message_bindings.ids)]
        )
        scheduled = service.env["contact.center.scheduled.message"].search(
            [("channel_id", "=", channel.id)]
        )
        profiles = bindings.group_profile_ids
        job_uuids = set()
        for records in (inbox_events, outbox, media, scheduled, profiles, bindings):
            for field in ("queue_job_uuid", "direct_avatar_queue_job_uuid"):
                if field in records._fields:
                    job_uuids.update(value for value in records.mapped(field) if value)
        jobs = service.env["queue.job"].search([("uuid", "in", sorted(job_uuids))])
        if any(job.state == "started" for job in jobs):
            raise ValidationError(
                _(
                    "Conversation processing is in progress. Try deleting it again shortly."
                )
            )
        pending_jobs = jobs.filtered(
            lambda job: job.state in ("pending", "enqueued", "wait_dependencies")
        )
        if pending_jobs:
            pending_jobs.button_cancelled()
        service._prepare_conversation_deletion_dependencies(
            channel, bindings, messages, inbox_events
        )
        note_requests = service.env["contact.center.internal.note.request"].search(
            [("channel_id", "=", channel.id)]
        )
        attachments = (
            messages.attachment_ids | uploads.attachment_id | media.attachment_id
        )
        owned_attachments = attachments.filtered(
            lambda item: (
                item.res_model == "mail.message" and item.res_id in messages.ids
            )
            or (
                item.res_model == "contact.center.media.upload"
                and item.res_id in uploads.ids
            )
        )
        shared_attachments = (
            service.env["mail.message"]
            .search(
                [
                    ("attachment_ids", "in", owned_attachments.ids),
                    ("id", "not in", messages.ids),
                ]
            )
            .attachment_ids
        )
        # The native message unlink deletes message-owned attachments. Re-home any
        # genuinely shared object before it runs; never touch business attachments.
        for attachment in owned_attachments & shared_attachments:
            survivor = service.env["mail.message"].search(
                [
                    ("attachment_ids", "in", attachment.id),
                    ("id", "not in", messages.ids),
                ],
                limit=1,
            )
            attachment.write({"res_model": "mail.message", "res_id": survivor.id})
        note_requests.unlink()
        scheduled.unlink()
        uploads.unlink()
        outbox.unlink()
        inbox_events._erase_conversation_content("conversation_deleted")
        service._application()._notify_ui(
            channel, "conversation_deleted", {"removed_from_conversation": True}
        )
        # mail.thread removes messages before its generic record; clear restrict
        # dependencies first and retain all unrelated CRM/business records.
        messages.unlink()
        profiles.unlink()
        redirects = (
            service.env["contact.center.channel.binding"]
            .with_context(active_test=False)
            .search([("merged_into_id", "in", bindings.ids)])
        )
        if any(redirect.active for redirect in redirects):
            raise ValidationError(
                _(
                    "An active conversation redirects to this conversation. "
                    "Resolve its binding before deleting it."
                )
            )
        redirects.write({"merged_into_id": False})
        bindings.unlink()
        channel.unlink()
        (owned_attachments - shared_attachments).exists().unlink()
        jobs.exists().unlink()
        return {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel_id,
            "removed_from_conversation": True,
        }
