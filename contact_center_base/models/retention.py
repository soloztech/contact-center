"""Opt-in, bounded history retention for WuzAPI groups.

The account/identity/channel fence is shared with admission and policy changes.
Only owned chat content is removed; the conversation and business records survive.
"""

import base64
import hashlib
import hmac
import json
import logging
from datetime import timedelta

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

_logger = logging.getLogger(__name__)
_RETENTION_TOKEN = object()
_POLICY_FIELDS = {"retention_enabled", "retention_days", "retention_revision"}
_TERMINAL_OUTBOX = ("done", "dead", "cancelled")


def _service_context():
    return {
        "contact_center_retention_token": _RETENTION_TOKEN,
        "contact_center_deletion_token": CONTACT_CENTER_DELETION_TOKEN,
        "contact_center_membership_token": CONTACT_CENTER_MEMBERSHIP_TOKEN,
        "contact_center_post_token": CONTACT_CENTER_POST_TOKEN,
        "contact_center_productivity_service_token": CONTACT_CENTER_PRODUCTIVITY_TOKEN,
        "contact_center_skip_enqueue": True,
    }


class ContactCenterRetentionAccount(models.Model):
    _inherit = "contact.center.account"

    retention_enabled = fields.Boolean(
        string="Excluir histórico antigo de grupos", default=False, copy=False
    )
    retention_days = fields.Integer(string="Manter histórico por (dias)", default=7)
    retention_revision = fields.Integer(default=0, readonly=True, copy=False)

    @api.constrains("retention_days")
    def _check_retention_days(self):
        if any(not 1 <= account.retention_days <= 36500 for account in self):
            raise ValidationError(_("Informe um prazo entre 1 e 36500 dias."))

    @api.model_create_multi
    def create(self, values_list):
        if any(
            values.get("retention_enabled") or "retention_revision" in values
            for values in values_list
        ):
            raise AccessError(_("Ative a retenção pela configuração com simulação."))
        return super().create(values_list)

    def write(self, values):
        if (
            _POLICY_FIELDS.intersection(values)
            and self.env.context.get("contact_center_retention_token")
            is not _RETENTION_TOKEN
        ):
            raise AccessError(_("Altere a retenção pela configuração com simulação."))
        return super().write(values)

    def action_configure_retention(self):
        self.ensure_one()
        self.env["contact.center.retention"]._check_manager(self)
        return {
            "type": "ir.actions.act_window",
            "name": _("Retenção do histórico de grupos"),
            "res_model": "contact.center.retention.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_account_id": self.id,
                "default_enabled": self.retention_enabled,
                "default_days": self.retention_days,
            },
        }


class ContactCenterRetentionBinding(models.Model):
    _inherit = "contact.center.channel.binding"

    retention_preserve = fields.Boolean(default=False, readonly=True, copy=False)
    retention_revision = fields.Integer(default=0, readonly=True, copy=False)
    retention_expired_before = fields.Datetime(readonly=True, copy=False)
    retention_last_run_at = fields.Datetime(
        readonly=True, copy=False, index=True, default="1970-01-01 00:00:00"
    )
    retention_last_purged_count = fields.Integer(readonly=True, copy=False)
    retention_last_blocked = fields.Boolean(readonly=True, copy=False)

    @api.model_create_multi
    def create(self, values_list):
        if (
            any(
                any(key.startswith("retention_") for key in values)
                for values in values_list
            )
            and self.env.context.get("contact_center_retention_token")
            is not _RETENTION_TOKEN
        ):
            raise AccessError(_("A retenção é administrada pelo serviço da Central."))
        return super().create(values_list)

    def write(self, values):
        if any(key.startswith("retention_") for key in values):
            if (
                self.env.context.get("contact_center_retention_token")
                is not _RETENTION_TOKEN
            ):
                raise AccessError(
                    _("A retenção é administrada pelo serviço da Central.")
                )
            if "retention_expired_before" in values:
                candidate = fields.Datetime.to_datetime(
                    values["retention_expired_before"]
                )
                if any(
                    item.retention_expired_before
                    and (not candidate or candidate < item.retention_expired_before)
                    for item in self
                ):
                    raise ValidationError(
                        _("O limite de histórico excluído não pode retroceder.")
                    )
        return super().write(values)


class ContactCenterRetentionAudit(models.Model):
    _name = "contact.center.retention.audit"
    _description = "Contact Center Retention Policy Audit"
    _order = "id desc"

    account_id = fields.Many2one(
        "contact.center.account", required=True, ondelete="cascade"
    )
    channel_binding_id = fields.Many2one(
        "contact.center.channel.binding", ondelete="set null"
    )
    changed_by_id = fields.Many2one("res.users", required=True, ondelete="restrict")
    before_json = fields.Json(required=True)
    after_json = fields.Json(required=True)

    @api.model_create_multi
    def create(self, values_list):
        if (
            self.env.context.get("contact_center_retention_token")
            is not _RETENTION_TOKEN
        ):
            raise AccessError(_("O histórico da política é administrado pelo serviço."))
        return super().create(values_list)

    def write(self, _values):  # pylint: disable=method-required-super
        raise AccessError(_("O histórico da política é imutável."))

    def unlink(self):  # pylint: disable=method-required-super
        raise AccessError(_("O histórico da política é imutável."))


class ContactCenterRetention(models.AbstractModel):
    _name = "contact.center.retention"
    _description = "Contact Center History Retention Service"

    def _check_manager(self, account):
        if not self.env.user.has_group(
            "contact_center_base.group_contact_center_supervisor"
        ):
            raise AccessError(
                _("Somente supervisores e administradores podem alterar a retenção.")
            )
        account = account.with_env(self.env)
        if account.company_id not in self.env.companies:
            raise AccessError(_("A empresa desta caixa não está ativa."))
        account.check_access_rights("write")
        account.check_access_rule("write")

    def _supported(self, binding):
        return bool(
            binding
            and binding.active
            and not binding.merged_into_id
            and binding.conversation_type == "group"
            and binding.account_id.platform == "whatsapp"
            and any(
                connection.adapter_key == "wuzapi"
                for connection in binding.account_id.connection_ids
            )
        )

    def _policy(self, binding):
        account = binding.account_id
        supported = self._supported(binding)
        can_manage = False
        if supported:
            try:
                self._check_manager(account)
                can_manage = True
            except AccessError:
                can_manage = False
        return {
            "supported": supported,
            "enabled": bool(account.retention_enabled),
            "effective": bool(
                supported
                and account.retention_enabled
                and not binding.retention_preserve
            ),
            "preserve": bool(binding.retention_preserve),
            "days": account.retention_days or 7,
            "can_manage": can_manage,
            "revision": account.retention_revision + binding.retention_revision,
            "expired_before": fields.Datetime.to_string(
                binding.retention_expired_before
            )
            or False,
        }

    def _message_domain(self, binding, cutoff):
        return [
            ("model", "=", "mail.channel"),
            ("res_id", "=", binding.channel_id.id),
            ("date", "<", cutoff),
        ]

    def _preview(self, binding, now=None, force=True, days=None):
        cutoff = (now or fields.Datetime.now()) - timedelta(
            days=days or binding.account_id.retention_days
        )
        active = self._supported(binding) and (
            force or self._policy(binding)["effective"]
        )
        domain = self._message_domain(binding, cutoff)
        message_model = self.env["mail.message"].sudo()
        count = message_model.search_count(domain) if active else 0
        media_count = (
            self.env["contact.center.media.binding"]
            .sudo()
            .search_count(
                [
                    ("message_binding_id.channel_binding_id", "=", binding.id),
                    ("message_binding_id.message_id.date", "<", cutoff),
                ]
            )
            if count
            else 0
        )
        return {
            "message_count": count,
            "media_count": media_count,
            "cutoff": fields.Datetime.to_string(cutoff),
        }

    def _sign(self, payload):
        secret = self.env["ir.config_parameter"].sudo().get_param("database.secret")
        if not secret:
            raise ValidationError(
                _("A chave da base para confirmar a simulação está indisponível.")
            )
        body = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True).encode()
        ).decode()
        digest = hmac.new(
            secret.encode(), ("cc-retention:" + body).encode(), hashlib.sha256
        ).hexdigest()
        return "%s.%s" % (body, digest)

    def _confirmation(self, account, binding=None, enabled=True, days=None):
        return self._sign(
            {
                "uid": self.env.uid,
                "account": account.id,
                "account_revision": account.retention_revision,
                "binding": binding.id if binding else False,
                "binding_revision": binding.retention_revision if binding else 0,
                "enabled": bool(enabled),
                "days": days or account.retention_days,
                "expires": fields.Datetime.to_string(
                    fields.Datetime.now() + timedelta(minutes=10)
                ),
            }
        )

    def _verify_confirmation(
        self, token, account, binding=None, enabled=True, days=None
    ):
        try:
            body, _signature = token.split(".")
            payload = json.loads(base64.urlsafe_b64decode(body))
            expected = {
                "uid": self.env.uid,
                "account": account.id,
                "account_revision": account.retention_revision,
                "binding": binding.id if binding else False,
                "binding_revision": binding.retention_revision if binding else 0,
                "enabled": bool(enabled),
                "days": days or account.retention_days,
            }
            valid = (
                hmac.compare_digest(token, self._sign(payload))
                and all(payload.get(key) == value for key, value in expected.items())
                and fields.Datetime.to_datetime(payload["expires"])
                >= fields.Datetime.now()
            )
        except (AttributeError, ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise ValidationError(
                _("A simulação mudou ou expirou. Simule novamente antes de confirmar.")
            )

    def _audit(self, account, before, after, binding=None):
        self.env["contact.center.retention.audit"].sudo().with_context(
            **_service_context()
        ).create(
            {
                "account_id": account.id,
                "channel_binding_id": binding.id if binding else False,
                "changed_by_id": self.env.uid,
                "before_json": before,
                "after_json": after,
            }
        )

    def _notify(self, binding, **payload):
        self.env["contact.center.application"]._notify_ui(
            binding.channel_id, "conversation_updated", payload
        )

    def _retention_prepare_dependencies(
        self, binding, messages, message_bindings, inbox_events
    ):
        """Optional bridges detach only expired message pointers or block the batch."""
        return True

    def _related_content(self, binding, messages, cutoff, limit):
        projections = self.env["contact.center.message.binding"].search(
            [("message_id", "in", messages.ids)]
        )
        outbox = self.env["contact.center.outbox.command"].search(
            [
                "|",
                ("message_binding_id", "in", projections.ids),
                ("target_message_binding_id", "in", projections.ids),
            ]
        )
        uploads = self.env["contact.center.media.upload"].search(
            [("consumed_message_binding_id", "in", projections.ids)]
        )
        media = self.env["contact.center.media.binding"].search(
            [("message_binding_id", "in", projections.ids)]
        )
        scheduled = self.env["contact.center.scheduled.message"].search(
            [
                "|",
                ("message_id", "in", messages.ids),
                ("outbox_command_id", "in", outbox.ids),
            ]
        )
        scheduled |= self.env["contact.center.scheduled.message"].search(
            [
                ("channel_id", "=", binding.channel_id.id),
                ("state", "in", ("cancelled", "failed")),
                ("scheduled_at", "<", cutoff),
                ("message_id", "=", False),
                ("outbox_command_id", "=", False),
            ],
            order="scheduled_at, id",
            limit=limit,
        )
        return projections, outbox, uploads, media, scheduled

    def _inbox_content(self, binding, projections, cutoff, limit):
        inbox = self.env["contact.center.inbox.event"]
        events = projections.source_inbox_event_id
        staged_mutations = inbox.browse()
        if "retention_route_ref" in inbox._fields:
            scope = [
                ("account_id", "=", binding.account_id.id),
                ("retention_route_ref", "=", binding.conversation_ref),
                ("last_error_class", "!=", "ConversationContentErased"),
            ]
            candidates = inbox.search(
                scope
                + [
                    ("retention_occurred_at", "<", cutoff),
                    ("retention_event_kind", "in", ("message", "control")),
                ],
                order="retention_occurred_at, id",
                limit=limit,
            )
            surviving_sources = (
                self.env["contact.center.message.binding"]
                .search(
                    [
                        ("source_inbox_event_id", "in", candidates.ids),
                        ("id", "not in", projections.ids),
                    ]
                )
                .source_inbox_event_id
            )
            events |= candidates - surviving_sources
            expired_ids = set(filter(None, projections.mapped("external_message_id")))
            expired_ids.update(
                events.filtered(
                    lambda item: item.retention_event_kind == "message"
                ).mapped("retention_message_id")
            )
            expired_ids.discard(False)
            if expired_ids:
                # Edits/revocations are dated at the change, not at their target's
                # original message. Their private copies expire with that target.
                mutations = inbox.search(
                    scope
                    + [
                        ("retention_event_kind", "=", "mutation"),
                        "|",
                        ("retention_target_id", "in", sorted(expired_ids)),
                        ("retention_multiple_targets", "=", True),
                    ],
                    order="id",
                    limit=1001,
                )
                matching = inbox.browse()
                for event in mutations:
                    ids = set(event.retention_external_ids_json or [])
                    if not ids.intersection(expired_ids):
                        continue
                    previously_expired = self._retention_expired_ids(
                        binding.account_id, binding.conversation_ref, ids - expired_ids
                    )
                    if not ids <= expired_ids | previously_expired:
                        raise ValidationError(
                            _(
                                "Uma alteração em lote também contém mensagens preservadas."
                            )
                        )
                    matching |= event
                if len(mutations) > 1000:
                    if not matching:
                        raise ValidationError(
                            _(
                                "Há alterações de múltiplos alvos que precisam ser revisadas."
                            )
                        )
                    staged_mutations = matching[:1000]
                else:
                    events |= matching
            # Receipt batches have no body, and may include recent/unknown IDs.
            # Preserve mixed evidence; it must not erase a surviving projection.
            receipts = inbox.search(
                scope
                + [
                    ("retention_event_kind", "=", "receipt"),
                    ("retention_occurred_at", "<", cutoff),
                ],
                order="retention_occurred_at, id",
                limit=limit,
            )
            for event in receipts:
                ids = set(event.retention_external_ids_json or [])
                previously_expired = self._retention_expired_ids(
                    binding.account_id, binding.conversation_ref, ids - expired_ids
                )
                if ids and ids <= expired_ids | previously_expired:
                    events |= event
        return events, staged_mutations

    def _stage_mutation_content(self, binding, projections, events, now):
        """Bound the cleanup of heavily edited history without deleting its source."""
        jobs = self._jobs((events,), self.env["mail.message"])
        self._protect_inflight(
            projections,
            self.env["contact.center.outbox.command"],
            self.env["contact.center.scheduled.message"],
            events,
            jobs,
            self.env["contact.center.media.binding"],
        )
        pending = jobs.filtered(
            lambda job: job.state in ("pending", "enqueued", "wait_dependencies")
        )
        if pending:
            pending.button_cancelled()
        events._erase_conversation_content("history_retention_mutation_staging")
        jobs.exists().unlink()
        binding.write({"retention_last_run_at": now})
        return {"message_count": 0, "media_count": 0, "staging": True}

    def _jobs(self, records, messages):
        uuids = set()
        identities = set()
        lanes = {
            "contact.center.inbox.event": "inbox",
            "contact.center.outbox.command": "outbox",
            "contact.center.media.binding": "media",
            "contact.center.scheduled.message": "scheduled",
        }
        for recordset in records:
            if "queue_job_uuid" in recordset._fields:
                uuids.update(
                    value for value in recordset.mapped("queue_job_uuid") if value
                )
            lane = lanes.get(recordset._name)
            if lane:
                identities.update(
                    "contact_center:%s:%s" % (lane, record_id)
                    for record_id in recordset.ids
                )
        # Retries can replace queue_job_uuid. Their terminal traceback/arguments
        # also belong to this content, so collect the stable identity's history.
        domains = [
            [("uuid", "in", sorted(uuids))],
            [("identity_key", "in", sorted(identities))],
        ]
        domains.extend(
            [
                (
                    "identity_key",
                    "=like",
                    "contact_center.link_preview:%s:%%" % message.id,
                )
            ]
            for message in messages
        )
        jobs = self.env["queue.job"].search(expression.OR(domains))
        if jobs:
            self.env.cr.execute(
                "SELECT id FROM queue_job WHERE id IN %s ORDER BY id FOR UPDATE",
                [tuple(jobs.ids)],
            )
            jobs.invalidate_recordset()
        return jobs

    def _protect_inflight(self, projections, outbox, scheduled, events, jobs, media):
        if any(command.state not in _TERMINAL_OUTBOX for command in outbox):
            raise ValidationError(
                _("O lote contém um envio pendente ou de resultado incerto.")
            )
        if any(item.state in ("scheduled", "processing") for item in scheduled):
            raise ValidationError(
                _("O lote contém uma mensagem agendada em andamento.")
            )
        if any(job.state == "started" for job in jobs):
            raise ValidationError(_("O lote tem processamento em andamento."))
        if any(event.state == "processing" for event in events):
            raise ValidationError(
                _("O lote tem processamento de entrada em andamento.")
            )
        if any(item.state == "downloading" for item in media):
            raise ValidationError(_("O lote tem um download de mídia em andamento."))
        # A single envelope may project more than one message. Never erase the
        # envelope of a surviving projection, or delete one side of a retry chain.
        if self.env["contact.center.message.binding"].search_count(
            [
                ("source_inbox_event_id", "in", events.ids),
                ("id", "not in", projections.ids),
            ]
        ):
            raise ValidationError(
                _("Um envelope do lote ainda pertence a uma mensagem preservada.")
            )
        if self.env["contact.center.outbox.command"].search_count(
            [("retry_of_id", "in", outbox.ids), ("id", "not in", outbox.ids)]
        ):
            raise ValidationError(
                _("Uma tentativa posterior ainda depende deste lote.")
            )
        replies = self.env["contact.center.message.binding"].search(
            [
                ("reply_to_binding_id", "in", projections.ids),
                ("id", "not in", projections.ids),
            ]
        )
        if self.env["contact.center.outbox.command"].search_count(
            [
                ("message_binding_id", "in", replies.ids),
                ("state", "not in", _TERMINAL_OUTBOX),
            ]
        ):
            raise ValidationError(
                _("Uma resposta pendente ainda cita uma mensagem deste lote.")
            )
        return replies

    def _prepare_attachments(self, messages, uploads, media):
        attachments = (
            messages.attachment_ids | uploads.attachment_id | media.attachment_id
        )
        attachments |= self.env["ir.attachment"].search(
            [
                "|",
                "&",
                ("res_model", "=", "mail.message"),
                ("res_id", "in", messages.ids),
                "&",
                ("res_model", "=", "contact.center.media.upload"),
                ("res_id", "in", uploads.ids),
            ]
        )
        owned = attachments.filtered(
            lambda attachment: (
                attachment.res_model == "mail.message"
                and attachment.res_id in messages.ids
            )
            or (
                attachment.res_model == "contact.center.media.upload"
                and attachment.res_id in uploads.ids
            )
        )
        exclusive = self.env["ir.attachment"]
        for attachment in owned:
            survivor = self.env["mail.message"].search(
                [
                    ("attachment_ids", "in", attachment.id),
                    ("id", "not in", messages.ids),
                ],
                limit=1,
            )
            if survivor:
                attachment.write({"res_model": "mail.message", "res_id": survivor.id})
                continue
            surviving_media = self.env["contact.center.media.binding"].search(
                [("attachment_id", "=", attachment.id), ("id", "not in", media.ids)],
                limit=1,
            )
            if surviving_media:
                attachment.write(
                    {
                        "res_model": "mail.message",
                        "res_id": surviving_media.message_binding_id.message_id.id,
                    }
                )
                continue
            surviving_upload = self.env["contact.center.media.upload"].search(
                [("attachment_id", "=", attachment.id), ("id", "not in", uploads.ids)],
                limit=1,
            )
            if surviving_upload:
                attachment.write(
                    {
                        "res_model": "contact.center.media.upload",
                        "res_id": surviving_upload.id,
                    }
                )
                continue
            # Business-owned files are already outside `owned`. Optional addons
            # must block/re-home reused chat-owned files through the bridge hook.
            exclusive |= attachment
        return exclusive

    def _repair_read_pointers(self, channel, messages):
        for member in channel.channel_member_ids:
            values = {}
            for field in ("seen_message_id", "fetched_message_id"):
                pointer = member[field]
                if pointer in messages:
                    domain = [
                        ("model", "=", "mail.channel"),
                        ("res_id", "=", channel.id),
                        ("id", "not in", messages.ids),
                        "|",
                        ("date", "<", pointer.date),
                        "&",
                        ("date", "=", pointer.date),
                        ("id", "<=", pointer.id),
                    ]
                    previous = self.env["mail.message"].search(
                        domain, order="date desc, id desc", limit=1
                    )
                    values[field] = previous.id or False
            if values:
                member.write(values)

    def _purge_binding(self, binding, limit=100, dry_run=False, now=None):
        """One atomic, local-only batch. Caller owns transaction/cron boundary."""
        if dry_run:
            return self._preview(binding, now=now, force=False)
        service = self.sudo().with_context(**_service_context())
        binding = binding.with_env(service.env)
        binding.account_id._lock_conversation_policy()
        if not binding._contact_center_lock_identity_channel_binding():
            return {"message_count": 0, "media_count": 0}
        binding.invalidate_recordset()
        binding.account_id.invalidate_recordset()
        if not service._policy(binding)["effective"]:
            return {"message_count": 0, "media_count": 0}
        now = now or fields.Datetime.now()
        cutoff = now - timedelta(days=binding.account_id.retention_days)
        messages = service.env["mail.message"].search(
            service._message_domain(binding, cutoff),
            order="date, id",
            limit=max(1, min(int(limit), 500)),
        )
        projections, outbox, uploads, media, scheduled = service._related_content(
            binding, messages, cutoff, max(1, min(int(limit), 500))
        )
        events, staged_mutations = service._inbox_content(
            binding, projections, cutoff, max(1, min(int(limit), 500))
        )
        if staged_mutations:
            return service._stage_mutation_content(
                binding, projections, staged_mutations, now
            )
        jobs = service._jobs((outbox, uploads, media, scheduled, events), messages)
        replies = service._protect_inflight(
            projections, outbox, scheduled, events, jobs, media
        )
        if (
            service._retention_prepare_dependencies(
                binding, messages, projections, events
            )
            is False
        ):
            binding.write({"retention_last_run_at": now})
            return {"message_count": 0, "media_count": 0, "staging": True}
        pending_jobs = jobs.filtered(
            lambda job: job.state in ("pending", "enqueued", "wait_dependencies")
        )
        if pending_jobs:
            pending_jobs.button_cancelled()
        service._record_expired_messages(binding, projections)
        exclusive_attachments = service._prepare_attachments(messages, uploads, media)
        service._repair_read_pointers(binding.channel_id, messages)
        replies.write({"reply_to_binding_id": False})
        notes = service.env["contact.center.internal.note.request"].search(
            [("message_id", "in", messages.ids)]
        )
        notes.unlink()
        scheduled.unlink()
        uploads.unlink()
        # Delete leaf retries first; readonly retry_of_id must not be rewritten.
        remaining = outbox
        while remaining:
            leaves = remaining - remaining.retry_of_id
            if not leaves:
                raise ValidationError(
                    _("O lote contém uma cadeia de tentativas inválida.")
                )
            leaves.unlink()
            remaining -= leaves
        events._erase_conversation_content("history_retention")
        result = {"message_count": len(messages), "media_count": len(media)}
        removed_ids = messages.ids
        messages.unlink()
        exclusive_attachments.exists().unlink()
        jobs.exists().unlink()
        binding.channel_id.channel_member_ids.invalidate_recordset(
            ["message_unread_counter"]
        )
        surviving_notes = (
            service.env["contact.center.internal.note.request"]
            .search(
                [
                    ("channel_id", "=", binding.channel_id.id),
                ]
            )
            .message_id
        )
        newest = service.env["mail.message"].search(
            [
                ("model", "=", "mail.channel"),
                ("res_id", "=", binding.channel_id.id),
                ("message_type", "not in", ("notification", "user_notification")),
                ("id", "not in", surviving_notes.ids),
            ],
            order="date desc, id desc",
            limit=1,
        )
        binding.channel_id.write(
            {
                "contact_center_last_message_id": newest.id or False,
                "contact_center_last_message_at": newest.date or False,
            }
        )
        oldest_remaining = service.env["mail.message"].search(
            service._message_domain(binding, cutoff), order="date, id", limit=1
        )
        watermark = oldest_remaining.date if oldest_remaining else cutoff
        binding.write(
            {
                "retention_expired_before": max(
                    binding.retention_expired_before or watermark, watermark
                ),
                "retention_last_run_at": now,
                "retention_last_purged_count": result["message_count"],
                "retention_last_blocked": False,
            }
        )
        if removed_ids:
            service._notify(
                binding, retention_purged=True, removed_message_ids=removed_ids
            )
        return result

    def _cron_purge(self):
        # One group per invocation bounds the transaction and releases its locks
        # at the native cron commit. Ordering rotates groups, including busy ones.
        service = self.sudo().with_context(**_service_context())
        binding = service.env["contact.center.channel.binding"].search(
            [
                ("active", "=", True),
                ("merged_into_id", "=", False),
                ("conversation_type", "=", "group"),
                ("retention_preserve", "=", False),
                ("account_id.active", "=", True),
                ("account_id.retention_enabled", "=", True),
                ("account_id.connection_ids.adapter_key", "=", "wuzapi"),
            ],
            order="retention_last_run_at, id",
            limit=1,
        )
        if not binding:
            return False
        try:
            with self.env.cr.savepoint():
                inbox = service.env["contact.center.inbox.event"]
                inbox._retention_index_routes(binding.account_id, limit=500)
                if inbox.search_count(
                    [
                        ("account_id", "=", binding.account_id.id),
                        ("retention_route_indexed", "=", False),
                    ]
                ):
                    # Commit this bounded backfill before advancing any cutoff.
                    binding.account_id._lock_conversation_policy()
                    binding.write({"retention_last_run_at": fields.Datetime.now()})
                    return {"indexing": True}
                return service._purge_binding(binding)
        except ValidationError:
            # Retrying on the next round is intentional; never delete a business
            # dependency or uncertain send to make an expiration counter shrink.
            binding.account_id._lock_conversation_policy()
            binding.write(
                {
                    "retention_last_run_at": fields.Datetime.now(),
                    "retention_last_blocked": True,
                }
            )
            _logger.info(
                "Contact Center retention batch deferred for binding %s", binding.id
            )
            return False


class ContactCenterRetentionUiApi(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def get_retention_policy(self, channel_id, preview=False):
        channel, _member = self._authorized_channel(channel_id)
        binding = self._binding_for_channel(channel)
        service = self.env["contact.center.retention"]
        result = {
            "schema_version": SCHEMA_VERSION,
            "channel_id": channel.id,
            "policy": service._policy(binding),
        }
        if preview:
            service._check_manager(binding.account_id)
            result["preview"] = service._preview(binding)
            result["preview"]["confirmation_token"] = service._confirmation(
                binding.account_id, binding
            )
        return result

    @api.model
    def set_retention_preserve(self, channel_id, preserve, confirmation_token=False):
        if not isinstance(preserve, bool):
            raise ValidationError(_("Informe se o histórico deve ser preservado."))
        channel, _member = self._authorized_channel(channel_id)
        binding = self._binding_for_channel(channel)
        service = self.env["contact.center.retention"]
        service._check_manager(binding.account_id)
        binding.account_id._lock_conversation_policy()
        if not binding._contact_center_lock_identity_channel_binding():
            raise ValidationError(_("A conversa não está mais disponível."))
        channel, _member = self._authorized_channel(channel_id)
        binding = self._binding_for_channel(channel)
        binding.invalidate_recordset()
        service._check_manager(binding.account_id)
        if not service._supported(binding):
            raise ValidationError(_("A retenção está disponível para grupos WuzAPI."))
        if (
            not preserve
            and binding.retention_preserve
            and binding.account_id.retention_enabled
        ):
            service._verify_confirmation(
                confirmation_token, binding.account_id, binding
            )
        before = {"preserve": binding.retention_preserve}
        if before["preserve"] != preserve:
            binding.sudo().with_context(**_service_context()).write(
                {
                    "retention_preserve": preserve,
                    "retention_revision": binding.retention_revision + 1,
                }
            )
            service._audit(binding.account_id, before, {"preserve": preserve}, binding)
            service._notify(binding, retention_updated=True)
        return self.get_retention_policy(channel.id)


class ContactCenterRetentionWizard(models.TransientModel):
    _name = "contact.center.retention.wizard"
    _description = "Preview and Configure Group Retention"

    account_id = fields.Many2one("contact.center.account", required=True, readonly=True)
    enabled = fields.Boolean(string="Excluir histórico antigo de grupos")
    days = fields.Integer(string="Manter por (dias)", required=True, default=7)
    preview_text = fields.Text(string="Simulação", readonly=True)
    confirmation_token = fields.Char(readonly=True)

    def _service(self):
        self.ensure_one()
        service = self.env["contact.center.retention"]
        service._check_manager(self.account_id)
        if not 1 <= self.days <= 36500:
            raise ValidationError(_("Informe um prazo entre 1 e 36500 dias."))
        return service

    def action_preview(self):
        service = self._service()
        bindings = (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account_id.id),
                    ("active", "=", True),
                    ("merged_into_id", "=", False),
                    ("conversation_type", "=", "group"),
                    ("retention_preserve", "=", False),
                ]
            )
        )
        messages = media = groups = 0
        for binding in bindings:
            if not service._supported(binding):
                continue
            groups += 1
            preview = service._preview(binding, days=self.days)
            if self.enabled:
                messages += preview["message_count"]
                media += preview["media_count"]
        self.write(
            {
                "preview_text": _(
                    "%(groups)s grupos abrangidos. %(messages)s mensagens e "
                    "%(media)s registros de mídia já ultrapassam o prazo. "
                    "O cadastro dos grupos e os documentos comerciais serão "
                    "preservados. A exclusão ocorre em lotes na rotina automática; "
                    "itens compartilhados ou em processamento podem ser "
                    "preservados. Desativar não recupera conteúdo excluído.",
                    groups=groups,
                    messages=messages,
                    media=media,
                ),
                "confirmation_token": service._confirmation(
                    self.account_id, enabled=self.enabled, days=self.days
                ),
            }
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_apply(self):
        service = self._service()
        account = self.account_id
        account._lock_conversation_policy()
        service._check_manager(account)
        service._verify_confirmation(
            self.confirmation_token, account, enabled=self.enabled, days=self.days
        )
        before = {"enabled": account.retention_enabled, "days": account.retention_days}
        after = {"enabled": self.enabled, "days": self.days}
        account.with_context(**_service_context()).write(
            {
                "retention_enabled": self.enabled,
                "retention_days": self.days,
                "retention_revision": account.retention_revision + 1,
            }
        )
        service._audit(account, before, after)
        for binding in (
            self.env["contact.center.channel.binding"]
            .sudo()
            .search(
                [
                    ("account_id", "=", account.id),
                    ("active", "=", True),
                    ("conversation_type", "=", "group"),
                ]
            )
        ):
            service._notify(binding, retention_updated=True)
        return {"type": "ir.actions.act_window_close"}
