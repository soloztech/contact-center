import hashlib
import logging

from psycopg2 import IntegrityError

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services.adapter import conversation_capabilities
from ..services.dto import CommandDTO, ConversationDTO, DTOValidationError

_logger = logging.getLogger(__name__)

_MAX_EXTERNAL_MESSAGE_ID_CHARS = 512
_MARK_READ_BATCH_SIZE = 100
_MARK_READ_MAX_BATCHES_PER_POINTER = 10
_CONTROL_CONTENT_TYPES = (
    "call.offer",
    "call.accept",
    "call.terminate",
    "identity.security.changed",
)

# The application and UI API are intentionally split by cohesive service lane. Odoo
# composes these `_inherit` fragments into the same runtime models.
# pylint: disable=consider-merging-classes-inherited


class ContactCenterApplicationReadReceipt(models.AbstractModel):
    _inherit = "contact.center.application"

    def _mark_read_connection(self, binding):
        """Return the active direct transport only when it explicitly supports read."""

        connection = (
            self.env["contact.center.provider.connection"]
            .sudo()
            .search(
                [
                    ("account_id", "=", binding.account_id.id),
                    ("active", "=", True),
                    ("role", "=", "primary"),
                    ("outbound_active", "=", True),
                ],
                limit=1,
            )
        )
        if not connection:
            return connection
        capabilities = conversation_capabilities(
            connection.capabilities_json or {}, "direct"
        )
        return (
            connection if capabilities.get("mark_read") is True else connection.browse()
        )

    def _mark_read_progress_message_ids(self, binding, connection):
        """Return the contiguous watermark and first retryable terminal target."""

        # This method deliberately crosses from ORM-managed outbox state into a
        # raw aggregate.  Flush the exact columns used by the query first: a
        # terminal ``dead``/``cancelled`` transition must be visible before we
        # decide that a read batch is covered.  Otherwise a command that never
        # reached the provider can transiently advance the watermark and can no
        # longer be selected for safe, idempotent revival.
        self.env["contact.center.outbox.command"].sudo().flush_model(
            [
                "channel_binding_id",
                "provider_connection_id",
                "target_message_binding_id",
                "command_type",
                "state",
                "resolution",
            ]
        )
        self.env["contact.center.message.binding"].sudo().flush_model(["message_id"])
        self.env.cr.execute(
            """
            WITH scoped AS (
                SELECT command.id AS command_id,
                       target.message_id,
                       command.state,
                       command.resolution
                  FROM contact_center_outbox_command AS command
                  JOIN contact_center_message_binding AS target
                    ON target.id = command.target_message_binding_id
                 WHERE command.channel_binding_id = %s
                   AND command.provider_connection_id = %s
                   AND command.command_type = 'mark_read'
            ), terminal AS (
                SELECT command_id, message_id
                  FROM scoped
                 WHERE state IN ('dead', 'cancelled')
                   AND resolution IS NULL
                 ORDER BY message_id, command_id
                 LIMIT 1
            )
            SELECT COALESCE(
                       MAX(scoped.message_id) FILTER (
                           WHERE (
                               scoped.state IN (
                                   'pending',
                                   'retry',
                                   'processing',
                                   'done',
                                   'uncertain'
                               )
                               OR (
                                   scoped.state = 'cancelled'
                                   AND scoped.resolution IS NOT NULL
                               )
                           )
                           AND (
                               (SELECT message_id FROM terminal) IS NULL
                               OR scoped.message_id < (
                                   SELECT message_id FROM terminal
                               )
                           )
                       ),
                       0
                   ) AS watermark_message_id,
                   COALESCE((SELECT message_id FROM terminal), 0)
                       AS terminal_message_id,
                   COALESCE((SELECT command_id FROM terminal), 0)
                       AS terminal_command_id
              FROM scoped
            """,
            [binding.id, connection.id],
        )
        row = self.env.cr.fetchone() or (0, 0, 0)
        return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)

    def _mark_read_watermark_message_id(self, binding, connection):
        """Return the highest contiguous local message covered by read batches."""

        (
            watermark,
            _terminal_message_id,
            _terminal_command_id,
        ) = self._mark_read_progress_message_ids(binding, connection)
        return watermark

    def _mark_read_targets(
        self, binding, connection, *, after_message_id, through_message_id, limit
    ):
        """Return one ordered provider-ID batch covered by the local pointer."""

        target_model = self.env["contact.center.message.binding"].sudo()
        target_model.flush_model(
            [
                "channel_binding_id",
                "provider_connection_id",
                "message_id",
                "direction",
                "external_message_id",
                "message_state",
                "content_type",
            ]
        )
        # Odoo 16 applies comparison and ordering operators on a Many2one through
        # the target model's display/order expression.  Here the watermark is the
        # actual mail.message primary key, and mail.message itself orders newest
        # first.  Use the stored FK explicitly so both bounds and batch order are
        # numeric, stable, and independent from display-name semantics.
        self.env.cr.execute(
            """
            SELECT id
              FROM contact_center_message_binding
             WHERE channel_binding_id = %s
               AND provider_connection_id = %s
               AND message_id > %s
               AND message_id <= %s
               AND direction = 'inbound'
               AND external_message_id IS NOT NULL
               AND external_message_id <> ''
               AND message_state <> 'deleted'
               AND NOT (content_type = ANY(%s))
             ORDER BY message_id, id
             LIMIT %s
            """,
            [
                binding.id,
                connection.id,
                after_message_id,
                through_message_id,
                list(_CONTROL_CONTENT_TYPES),
                limit,
            ],
        )
        return target_model.browse([row[0] for row in self.env.cr.fetchall()])

    @staticmethod
    def _valid_mark_read_external_id(external_message_id):
        return bool(
            isinstance(external_message_id, str)
            and external_message_id
            and len(external_message_id) <= _MAX_EXTERNAL_MESSAGE_ID_CHARS
            and not any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in external_message_id
            )
        )

    @staticmethod
    def _mark_read_idempotency_key(binding, connection, external_message_ids):
        digest = hashlib.sha256(
            ("%s\0%s" % (connection.id, "\0".join(external_message_ids))).encode(
                "utf-8"
            )
        ).hexdigest()
        return "mark-read:%s:%s" % (binding.id, digest)

    def _persist_mark_read_command(
        self,
        outbox_model,
        binding,
        connection,
        target,
        idempotency_key,
        command,
    ):
        """Create a read batch or revive its exact terminal ledger row."""

        existing = outbox_model.search(
            [
                ("account_id", "=", binding.account_id.id),
                ("outbox_idempotency_key", "=", idempotency_key),
            ],
            limit=1,
        )
        if existing:
            existing._revive_terminal_mark_read(command, target)
            return existing
        values = {
            "account_id": binding.account_id.id,
            "provider_connection_id": connection.id,
            "channel_binding_id": binding.id,
            "target_message_binding_id": target.id,
            "outbox_idempotency_key": idempotency_key,
            "command_type": "mark_read",
            "command_json": command.to_dict(),
        }
        try:
            with self.env.cr.savepoint():
                return outbox_model.create(values)
        except IntegrityError:
            # Under PostgreSQL REPEATABLE READ the concurrent winner may not be
            # visible in this transaction. The read receipt is optional; never
            # roll back the already-valid local seen pointer.
            return outbox_model.search(
                [
                    ("account_id", "=", binding.account_id.id),
                    ("outbox_idempotency_key", "=", idempotency_key),
                ],
                limit=1,
            )

    def _queue_direct_mark_read(self, channel, message):
        """Persist a provider-neutral direct read command without doing provider I/O."""

        channel.ensure_one()
        message.ensure_one()
        binding = self._active_channel_binding(channel)
        if (
            not binding
            or binding.conversation_type != "direct"
            or not binding.account_id.mark_read_enabled
        ):
            return self.env["contact.center.outbox.command"]
        outbox_model = self.env["contact.center.outbox.command"].sudo()
        # Follow the global parent-first order used by group/avatar projections:
        # account -> connections -> channel -> binding. Re-reading policy and
        # transport under those locks makes admission linearizable with a pause
        # or controlled primary switch without introducing an ABBA cycle.
        account = binding.account_id
        account.flush_recordset(["active", "mark_read_enabled"])
        connection_model = self.env["contact.center.provider.connection"].sudo()
        connections = connection_model._contact_center_lock_operational_admission(
            account.ids
        )
        account.invalidate_recordset(["active", "mark_read_enabled"])
        connections.flush_recordset(["capabilities_json"])
        connections.invalidate_recordset(
            [
                "active",
                "role",
                "inbound_active",
                "outbound_active",
                "capabilities_json",
            ]
        )
        if not account.active or not account.mark_read_enabled:
            return self.env["contact.center.outbox.command"]
        if not binding._contact_center_lock_channel_then_binding():
            raise ValidationError(_("The conversation no longer exists."))
        binding.invalidate_recordset(
            ["active", "merged_into_id", "channel_id", "account_id"]
        )
        channel.invalidate_recordset(["active", "channel_type"])
        if (
            not binding.active
            or binding.merged_into_id
            or binding.channel_id != channel
            or binding.account_id != account
            or not channel.active
            or channel.channel_type != "contact_center"
        ):
            raise ValidationError(_("The conversation is no longer active."))
        connection = self._mark_read_connection(binding)
        if not connection:
            return self.env["contact.center.outbox.command"]

        (
            watermark,
            terminal_message_id,
            terminal_command_id,
        ) = self._mark_read_progress_message_ids(binding, connection)
        address_dtos, target_address = self._outbound_route(
            binding, _("The conversation has no read-receipt routing address.")
        )
        commands = outbox_model.browse()
        for _batch_number in range(_MARK_READ_MAX_BATCHES_PER_POINTER):
            batch_terminal_message_id = (
                terminal_message_id
                if terminal_message_id and terminal_message_id <= message.id
                else 0
            )
            targets = self._mark_read_targets(
                binding,
                connection,
                after_message_id=watermark,
                through_message_id=batch_terminal_message_id or message.id,
                limit=_MARK_READ_BATCH_SIZE,
            )
            if not targets:
                break
            watermark = targets[-1].message_id.id
            valid_targets = targets.filtered(
                lambda target: self._valid_mark_read_external_id(
                    target.external_message_id
                )
            )
            if len(valid_targets) != len(targets):
                _logger.warning(
                    "Skipped invalid Contact Center read targets "
                    "(binding=%s, skipped=%s)",
                    binding.id,
                    len(targets) - len(valid_targets),
                )
            if not valid_targets:
                if batch_terminal_message_id:
                    break
                continue
            external_message_ids = valid_targets.mapped("external_message_id")
            idempotency_key = self._mark_read_idempotency_key(
                binding, connection, external_message_ids
            )
            target = valid_targets[-1]
            terminal_command = (
                outbox_model.browse(terminal_command_id).exists()
                if batch_terminal_message_id
                else outbox_model.browse()
            )
            if batch_terminal_message_id and (
                not terminal_command
                or terminal_command.outbox_idempotency_key != idempotency_key
                or terminal_command.target_message_binding_id != target
            ):
                # A deleted/invalidated member of the persisted snapshot changes
                # its digest.  Do not create a wider or partial replacement:
                # only the exact terminal ledger row may close this gap.
                break
            command = CommandDTO(
                command_id=idempotency_key,
                command_type="mark_read",
                account_ref=binding.account_id.external_ref,
                connection_ref=connection.external_ref,
                conversation_ref=binding.conversation_ref,
                conversation=ConversationDTO(
                    addresses=address_dtos,
                    conversation_type="direct",
                ),
                target_address=target_address,
                options={"external_message_ids": external_message_ids},
            )
            commands |= self._persist_mark_read_command(
                outbox_model,
                binding,
                connection,
                target,
                idempotency_key,
                command,
            )
            terminal_reconciled = False
            if batch_terminal_message_id and watermark >= batch_terminal_message_id:
                (
                    refreshed_watermark,
                    refreshed_terminal_message_id,
                    refreshed_terminal_command_id,
                ) = self._mark_read_progress_message_ids(binding, connection)
                if refreshed_terminal_message_id == batch_terminal_message_id:
                    # The exact terminal snapshot could not be revived.  Keep
                    # the gap as a hard barrier instead of issuing a wider,
                    # differently keyed read batch beyond it.
                    break
                watermark = max(watermark, refreshed_watermark)
                terminal_message_id = refreshed_terminal_message_id
                terminal_command_id = refreshed_terminal_command_id
                terminal_reconciled = True
            if len(targets) < _MARK_READ_BATCH_SIZE and not terminal_reconciled:
                break
        return commands


class ContactCenterUiApiReadReceipt(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    def _lock_seen_conversation(self, channel):
        """Lock provider topology and conversation before the native pointer."""

        channel.ensure_one()
        binding = self._binding_for_channel(channel)
        if not binding:
            self.env.cr.execute(
                "SELECT id FROM mail_channel WHERE id = %s FOR UPDATE", [channel.id]
            )
            if not self.env.cr.fetchone():
                raise ValidationError(_("The conversation no longer exists."))
            return binding
        account = binding.account_id
        account.flush_recordset(["active", "mark_read_enabled"])
        connection_model = self.env["contact.center.provider.connection"].sudo()
        connections = connection_model._contact_center_lock_operational_admission(
            account.ids
        )
        account.invalidate_recordset(["active", "mark_read_enabled"])
        connections.invalidate_recordset(
            ["active", "role", "inbound_active", "outbound_active"]
        )
        if not binding._contact_center_lock_channel_then_binding():
            raise ValidationError(_("The conversation no longer exists."))
        channel.invalidate_recordset(["active", "channel_type"])
        binding.invalidate_recordset(
            ["active", "merged_into_id", "channel_id", "account_id"]
        )
        if (
            not binding.active
            or binding.merged_into_id
            or binding.channel_id != channel
            or binding.account_id != account
            or not channel.active
            or channel.channel_type != "contact_center"
        ):
            raise ValidationError(_("The conversation is no longer active."))
        return binding

    def _mark_member_pointer(self, channel_id, message_id=None, seen=False):
        if seen:
            # Authorization deliberately runs before the raw row locks, and the
            # native implementation below repeats it. No ACL or membership check
            # is bypassed while establishing channel -> binding -> member order.
            channel, _member = self._authorized_channel(channel_id)
            self._lock_seen_conversation(channel)
        result = super()._mark_member_pointer(
            channel_id, message_id=message_id, seen=seen
        )
        if not seen or not result.get("message_id"):
            return result
        channel = self.env["mail.channel"].browse(result["channel_id"]).exists()
        message = self.env["mail.message"].browse(result["message_id"]).exists()
        if not channel or not message:
            return result
        try:
            self._application()._queue_direct_mark_read(channel, message)
        except (DTOValidationError, UserError, ValidationError) as error:
            # Local read state remains useful even when optional provider routing is
            # incomplete. The command never performs network I/O in this request.
            _logger.warning(
                "Could not enqueue optional Contact Center read receipt "
                "(channel=%s, error_class=%s)",
                channel.id,
                error.__class__.__name__,
            )
        return result


class ContactCenterAccountReadReceipt(models.Model):
    _inherit = "contact.center.account"

    def write(self, values):
        result = super().write(values)
        if {"active", "mark_read_enabled"}.intersection(values):
            obsolete_accounts = self.filtered(
                lambda account: not account.active or not account.mark_read_enabled
            )
            if obsolete_accounts:
                commands = (
                    self.env["contact.center.outbox.command"]
                    .sudo()
                    .search(
                        [
                            ("account_id", "in", obsolete_accounts.ids),
                            ("command_type", "=", "mark_read"),
                            ("state", "in", ("pending", "retry")),
                        ]
                    )
                )
                commands._cancel_pending_mark_reads()
        return result


class ContactCenterProviderConnectionReadReceipt(models.Model):
    _inherit = "contact.center.provider.connection"

    def write(self, values):
        result = super().write(values)
        if {"active", "role", "outbound_active"}.intersection(values):
            obsolete_connections = self.filtered(
                lambda connection: not connection.active
                or connection.role != "primary"
                or not connection.outbound_active
            )
            if obsolete_connections:
                commands = (
                    self.env["contact.center.outbox.command"]
                    .sudo()
                    .search(
                        [
                            (
                                "provider_connection_id",
                                "in",
                                obsolete_connections.ids,
                            ),
                            ("command_type", "=", "mark_read"),
                            ("state", "in", ("pending", "retry")),
                        ]
                    )
                )
                commands._cancel_pending_mark_reads()
        return result


class ContactCenterOutboxReadReceipt(models.Model):
    _inherit = "contact.center.outbox.command"

    def _revive_terminal_mark_read(self, command_dto, target):
        """Reuse one safely retryable terminal read command for a fresh dispatch."""

        self.ensure_one()
        if self.command_type != "mark_read":
            return False
        self.env.cr.execute(
            "SELECT id FROM contact_center_outbox_command WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset(
            [
                "state",
                "attempts",
                "queue_job_uuid",
                "dispatch_job_uuid",
                "dispatch_started_at",
                "processed_at",
                "provider_request_json",
                "provider_response_json",
                "last_error_class",
                "last_error_message",
                "resolution",
            ]
        )
        if self.state not in ("cancelled", "dead") or self.resolution:
            return False
        if (
            self.account_id.external_ref != command_dto.account_ref
            or self.provider_connection_id.external_ref != command_dto.connection_ref
            or self.channel_binding_id.conversation_ref != command_dto.conversation_ref
            or target.channel_binding_id != self.channel_binding_id
            or target.provider_connection_id != self.provider_connection_id
        ):
            raise ValidationError(
                _("The terminal read command no longer matches its conversation.")
            )
        self.sudo().write(
            {
                "state": "pending",
                "attempts": 0,
                "queue_job_uuid": False,
                "dispatch_job_uuid": False,
                "dispatch_started_at": False,
                "processed_at": False,
                "provider_request_json": False,
                "provider_response_json": False,
                "last_error_class": False,
                "last_error_message": False,
                "target_message_binding_id": target.id,
                "command_json": command_dto.to_dict(),
            }
        )
        if not self.env.context.get("contact_center_skip_enqueue"):
            self._enqueue()
        return True

    def _cancel_pending_mark_reads(self):
        commands = self.filtered(
            lambda command: command.command_type == "mark_read"
            and command.state in ("pending", "retry")
        )
        if commands:
            commands.write(
                {
                    "state": "cancelled",
                    "processed_at": fields.Datetime.now(),
                    "last_error_class": "SupersededReadReceipt",
                    "last_error_message": (
                        "Read receipt routing was superseded before provider dispatch."
                    ),
                }
            )
        return len(commands)

    @api.constrains(
        "command_type",
        "message_binding_id",
        "target_message_binding_id",
        "mutation_id",
        "provider_connection_id",
        "channel_binding_id",
    )
    def _check_mark_read_shape(self):
        for command in self.filtered(lambda item: item.command_type == "mark_read"):
            target = command.target_message_binding_id
            if (
                command.channel_binding_id.conversation_type != "direct"
                or command.message_binding_id
                or command.mutation_id
                or not target
                or target.channel_binding_id != command.channel_binding_id
                or target.provider_connection_id != command.provider_connection_id
                or target.direction != "inbound"
                or not target.external_message_id
                or target.content_type in _CONTROL_CONTENT_TYPES
            ):
                raise ValidationError(
                    _(
                        "A read command requires one inbound target on the same "
                        "direct provider connection."
                    )
                )

    def _mark_read_scope_mismatches(self, command_dto):
        self.ensure_one()
        if command_dto.command_type != "mark_read":
            return []
        mismatches = []
        binding = self.channel_binding_id
        target = self.target_message_binding_id
        capabilities = conversation_capabilities(
            self.provider_connection_id.capabilities_json or {}, "direct"
        )
        if binding.conversation_type != "direct":
            mismatches.append("conversation.conversation_type")
        if not self.account_id.mark_read_enabled:
            mismatches.append("account.mark_read_enabled")
        if capabilities.get("mark_read") is not True:
            mismatches.append("capabilities.mark_read")
        if (
            not target
            or target.channel_binding_id != binding
            or target.provider_connection_id != self.provider_connection_id
            or target.direction != "inbound"
            or not target.external_message_id
            or target.content_type in _CONTROL_CONTENT_TYPES
        ):
            mismatches.append("target_message_binding")
        options = command_dto.options or {}
        external_ids = options.get("external_message_ids")
        if (
            set(options) != {"external_message_ids"}
            or not isinstance(external_ids, list)
            or not 1 <= len(external_ids) <= _MARK_READ_BATCH_SIZE
            or any(
                not self.env["contact.center.application"]._valid_mark_read_external_id(
                    external_id
                )
                for external_id in external_ids
            )
            or len(set(external_ids)) != len(external_ids)
        ):
            mismatches.append("options.external_message_ids")
        else:
            target_bindings = (
                self.env["contact.center.message.binding"]
                .sudo()
                .search(
                    [
                        ("channel_binding_id", "=", binding.id),
                        (
                            "provider_connection_id",
                            "=",
                            self.provider_connection_id.id,
                        ),
                        ("direction", "=", "inbound"),
                        ("message_state", "!=", "deleted"),
                        ("content_type", "not in", _CONTROL_CONTENT_TYPES),
                        ("external_message_id", "in", external_ids),
                    ],
                    order="message_id, id",
                )
            )
            persisted_ids = target_bindings.mapped("external_message_id")
            if (
                persisted_ids != external_ids
                or not target_bindings
                or target_bindings[-1] != target
            ):
                mismatches.append("options.external_message_ids")
        if not command_dto.target_address or command_dto.target_address.role not in (
            "primary",
            "routing",
        ):
            mismatches.append("target_address")
        if (
            command_dto.message
            or command_dto.reply_to
            or command_dto.extensions
            or command_dto.client_message_id
            or command_dto.own_protocol_participant
            or command_dto.target_protocol_participant
        ):
            mismatches.append("command.shape")
        return mismatches

    def _cancel_obsolete_mark_read(self):
        """Cancel an ephemeral read command whose routing policy was superseded."""

        self.ensure_one()
        if self.command_type != "mark_read" or self.state not in ("pending", "retry"):
            return False
        connection = self.provider_connection_id
        connection.invalidate_recordset(
            ["active", "role", "outbound_active", "account_id", "capabilities_json"]
        )
        connection.account_id.invalidate_recordset(["active", "mark_read_enabled"])
        capabilities = conversation_capabilities(
            connection.capabilities_json or {}, "direct"
        )
        if (
            connection.active
            and connection.account_id.active
            and connection.role == "primary"
            and connection.outbound_active
            and connection.account_id.mark_read_enabled
            and capabilities.get("mark_read") is True
        ):
            return False
        self.sudo()._cancel_pending_mark_reads()
        return True

    def _prepare_dispatch_for_job(self):
        self.ensure_one()
        if self._cancel_obsolete_mark_read():
            self._commit_job_transaction()
            return False
        return super()._prepare_dispatch_for_job()

    def _validate_command_scope(self, command_dto):
        result = super()._validate_command_scope(command_dto)
        mismatches = self._mark_read_scope_mismatches(command_dto)
        if mismatches:
            raise ValidationError(
                _(
                    "The persisted read command does not match its outbox scope: %s",
                    ", ".join(sorted(set(mismatches))),
                )
            )
        return result

    def _notify_delivery_ui(self, refresh_message=False):
        if self.command_type == "mark_read":
            return False
        return super()._notify_delivery_ui(refresh_message=refresh_message)

    def _validate_uncertain_resolution(self, resolution):
        if (
            self.command_type == "mark_read"
            and resolution == "external_application_confirmed"
        ):
            return "done"
        return super()._validate_uncertain_resolution(resolution)
