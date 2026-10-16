from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderRateLimitError,
    TransientAdapterError,
)
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import HEALTH_OBSERVATION_CONTEXT_KEY
from ..services.onboarding import WuzapiOnboardingClient

_INTERNAL_TOKEN = object()


class ContactCenterWuzapiReconnectWizard(models.TransientModel):
    _name = "contact.center.wuzapi.reconnect.wizard"
    _description = "Reconnect an Existing Contact Center WhatsApp Connection"
    _transient_max_hours = 24

    connection_id = fields.Many2one(
        "contact.center.provider.connection",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(related="connection_id.company_id", readonly=True)
    request_revision = fields.Integer(readonly=True)
    configuration_revision = fields.Integer(readonly=True)
    webhook_revision = fields.Integer(readonly=True)
    hmac_revision = fields.Integer(readonly=True)
    pairing_revision = fields.Integer(readonly=True)
    state = fields.Selection(
        [
            ("new", "Ready to Check"),
            ("queued", "Queued"),
            ("waiting", "Waiting"),
            ("qr", "Scan the QR"),
            ("ready", "Connected"),
            ("error", "Needs Attention"),
        ],
        default="new",
        readonly=True,
    )
    message = fields.Text(readonly=True)
    retry_at = fields.Datetime(readonly=True)
    pending_observation_json = fields.Json(readonly=True, copy=False)
    qr_url = fields.Char(compute="_compute_runtime")
    connected = fields.Boolean(compute="_compute_runtime")
    running = fields.Boolean(compute="_compute_runtime")
    quarantined_count = fields.Integer(compute="_compute_runtime")
    inbox_event_ids = fields.Many2many(
        "contact.center.inbox.event", "cc_wuzapi_reconnect_inbox_rel", readonly=True
    )
    confirm_origin = fields.Boolean(string="These events belong to this inbox's number")

    @api.model
    def _create_release_request(self, events):
        events._check_pairing_release_access()
        request = self.create({"connection_id": events.provider_connection_id.id})
        request._update({"inbox_event_ids": [(6, 0, events.ids)]})
        return request._reopen()

    def action_confirm_origins(self):
        self._connection()
        if not self.confirm_origin or not self.inbox_event_ids:
            raise ValidationError(
                _("Confirm that the selected events belong to this inbox's number.")
            )
        if self.inbox_event_ids.provider_connection_id != self.connection_id:
            raise ValidationError(
                _("The selected events belong to another connection.")
            )
        self.inbox_event_ids._contact_center_confirm_pairing_origin()
        return {"type": "ir.actions.act_window_close"}

    @api.model
    def _allowed_connection(self, connection_id, actor):
        actor = actor.with_user(actor)
        if (
            not isinstance(connection_id, int)
            or isinstance(connection_id, bool)
            or connection_id <= 0
        ):
            raise ValidationError(_("A valid provider connection is required."))
        if not actor.active or (
            actor.id != SUPERUSER_ID
            and not actor.has_group("contact_center_base.group_contact_center_admin")
        ):
            raise AccessError(_("Only Contact Center administrators can reconnect."))
        connection = (
            self.env["contact.center.provider.connection"]
            .with_user(actor)
            .browse(connection_id)
            .exists()
        )
        connection.check_access_rights("write")
        connection.check_access_rule("write")
        if (
            not connection
            or connection.company_id not in actor.company_ids
            or connection.adapter_key != "wuzapi"
            or connection.role != "primary"
            or not connection.active
            or not connection.account_id.active
        ):
            raise ValidationError(
                _("This connection is not available for reconnection.")
            )
        return connection.sudo().with_company(connection.company_id)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for values in vals_list:
            connection = self._allowed_connection(
                values.get("connection_id"), self.env.user
            )
            if connection.company_id not in self.env.companies:
                raise AccessError(
                    _("Select the connection's company before reconnecting.")
                )
            prepared.append({"connection_id": connection.id})
        return super().create(prepared)

    def write(self, values):
        if "connection_id" in values and any(
            item.connection_id.id != values["connection_id"] for item in self
        ):
            raise AccessError(_("A reconnect request cannot change its connection."))
        if (
            values
            and self.env.context.get("wuzapi_reconnect_token") is not _INTERNAL_TOKEN
        ):
            if (
                set(values) != {"confirm_origin"}
                or type(values["confirm_origin"]) is not bool
                or any(not item.inbox_event_ids for item in self)
            ):
                raise AccessError(
                    _("Reconnect observations are managed by the operation.")
                )
            self._connection()
        return super().write(values)

    def _update(self, values):
        return (
            self.sudo()
            .with_context(wuzapi_reconnect_token=_INTERNAL_TOKEN)
            .write(values)
        )

    def _connection(self, *, queued=False):
        self.ensure_one()
        request = self.sudo()
        if not queued and (
            self.env.user != request.create_uid
            or request.company_id not in self.env.companies
        ):
            raise AccessError(_("This reconnect request belongs to another session."))
        return self._allowed_connection(request.connection_id.id, request.create_uid)

    def _current(self, revision, *, lock=False):
        self.invalidate_recordset()
        connection = self._connection(queued=True)
        if lock:
            self.env.cr.execute(
                "SELECT id FROM contact_center_account WHERE id = %s FOR SHARE",
                [connection.account_id.id],
            )
            self.env.cr.execute(
                "SELECT id FROM contact_center_provider_connection WHERE id = %s FOR UPDATE",
                [connection.id],
            )
        connection.invalidate_recordset()
        connection.account_id.invalidate_recordset(["active"])
        if (
            not connection.active
            or not connection.account_id.active
            or connection.role != "primary"
            or connection.company_id != self.company_id
            or self.request_revision != revision
            or connection.wuzapi_onboarding_revision != revision
            or connection.wuzapi_onboarding_job_uuid != self.env.context.get("job_uuid")
            or connection.health_configuration_revision != self.configuration_revision
            or connection.wuzapi_webhook_sync_revision != self.webhook_revision
            or connection.wuzapi_hmac_rotation_revision != self.hmac_revision
            or connection.wuzapi_hmac_rotation_state != "stable"
            or connection.wuzapi_hmac_rotation_job_uuid
        ):
            return self.env["contact.center.provider.connection"]
        return connection

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Reconnect WhatsApp"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_start(self):
        connection = self._connection()
        if self.inbox_event_ids:
            raise ValidationError(
                _("Open a separate reconnect request to restore the session.")
            )
        connection._contact_center_lock_topology(connection.account_id.ids)
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection WHERE id = %s FOR UPDATE",
            [connection.id],
        )
        connection.invalidate_recordset()
        if connection._wuzapi_active_onboarding_job():
            self._update(
                {
                    "message": _(
                        "A connection operation is already running. Check again shortly."
                    )
                }
            )
            return self._reopen()
        if self.pending_observation_json and (
            self.configuration_revision != connection.health_configuration_revision
            or self.webhook_revision != connection.wuzapi_webhook_sync_revision
            or self.hmac_revision != connection.wuzapi_hmac_rotation_revision
            or self.request_revision != connection.wuzapi_onboarding_revision
            or self.pairing_revision != connection.wuzapi_pairing_revision
        ):
            self._finish_error(self.request_revision)
            return self._reopen()
        if (
            connection.wuzapi_hmac_rotation_state != "stable"
            or connection.wuzapi_hmac_rotation_job_uuid
        ):
            raise ValidationError(_("Wait for the webhook signature rotation."))
        if (
            self.env["contact.center.outbox.command"]
            .sudo()
            .search_count(
                [
                    ("provider_connection_id", "=", connection.id),
                    ("state", "=", "processing"),
                ]
            )
        ):
            raise ValidationError(_("Wait for the messages currently being sent."))
        now = fields.Datetime.now()
        eta = max(
            now,
            connection.health_retry_not_before or now,
            connection.outbound_dispatch_not_before or now
            if connection.outbound_dispatch_not_before_reason == "provider_retry_after"
            else now,
        )
        revision = connection.wuzapi_onboarding_revision + 1
        self._update(
            {
                "request_revision": revision,
                "configuration_revision": connection.health_configuration_revision,
                "webhook_revision": connection.wuzapi_webhook_sync_revision,
                "hmac_revision": connection.wuzapi_hmac_rotation_revision,
                "pairing_revision": connection.wuzapi_pairing_revision,
                "state": "queued",
                "message": False,
                "retry_at": eta,
            }
        )
        connection.write({"wuzapi_onboarding_revision": revision})
        delayed = self.with_delay(
            identity_key="contact_center:wuzapi_onboarding:%s:%s"
            % (connection.id, revision),
            priority=32,
            max_retries=6,
            eta=eta,
            description="Contact Center reconnect %s" % connection.id,
        )._job_reconnect(revision)
        connection.write({"wuzapi_onboarding_job_uuid": delayed.uuid})
        return self._reopen()

    def action_check(self):
        connection = self._connection()
        if self.pending_observation_json:
            return self.action_start()
        connection._enqueue_health_check()
        return self._reopen()

    def _compute_runtime(self):
        for request in self:
            connection = request.connection_id.sudo()
            request.connected = connection._contact_center_identity_is_verified()
            request.running = bool(
                connection.wuzapi_onboarding_job_uuid
                and self.env["queue.job"]
                .sudo()
                .search_count(
                    [
                        ("uuid", "=", connection.wuzapi_onboarding_job_uuid),
                        (
                            "state",
                            "in",
                            ("pending", "enqueued", "started", "wait_dependencies"),
                        ),
                    ]
                )
            )
            request.qr_url = (
                "/contact-center/reconnect/wuzapi/qr/%s/%s"
                % (request.id, request.pairing_revision)
                if request.state == "qr"
                and request.pairing_revision == connection.wuzapi_pairing_revision
                and connection.wuzapi_pairing_state == "qr"
                and connection.health_configuration_revision
                == request.configuration_revision
                else False
            )
            self.env.cr.execute(
                "SELECT COUNT(*) FROM contact_center_inbox_event "
                "WHERE provider_connection_id = %s AND state = 'blocked' "
                "AND metadata_json ->> 'blocked_reason' = 'pairing_unverified'",
                [connection.id],
            )
            request.quarantined_count = self.env.cr.fetchone()[0]

    def _health_snapshot(self, connection):
        observation = {}
        scoped = connection.with_context(
            **{HEALTH_OBSERVATION_CONTEXT_KEY: observation}
        )
        token = scoped._contact_center_health_observation_token()
        observed_at = fields.Datetime.now()
        health = scoped.get_adapter().get_health(scoped)
        return health, observation, token, observed_at

    def _qr_png(self, revision):
        connection = self._connection()
        expected = (
            self.configuration_revision,
            self.webhook_revision,
            self.hmac_revision,
            self.request_revision,
            revision,
        )
        self._check_qr_scope(connection, expected)
        content = WuzapiOnboardingClient(
            connection.wuzapi_base_url, api_token=connection.wuzapi_api_token
        ).qr_png()
        # The provider read owns no row lock. Fence its answer before exposing
        # the PNG; a concurrent change is retried by Odoo's HTTP transaction.
        self.env.cr.execute(
            "SELECT id FROM contact_center_account WHERE id = %s FOR SHARE",
            [connection.account_id.id],
        )
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection WHERE id = %s FOR UPDATE",
            [connection.id],
        )
        connection.invalidate_recordset()
        connection.account_id.invalidate_recordset(["active"])
        connection = self._connection()
        self._check_qr_scope(connection, expected)
        return content

    def _check_qr_scope(self, connection, expected):
        current = (
            connection.health_configuration_revision,
            connection.wuzapi_webhook_sync_revision,
            connection.wuzapi_hmac_rotation_revision,
            connection.wuzapi_onboarding_revision,
            connection.wuzapi_pairing_revision,
        )
        if (
            self.state != "qr"
            or self.pairing_revision != expected[-1]
            or current != expected
            or connection.wuzapi_pairing_state != "qr"
        ):
            raise ValidationError(_("This QR code is no longer available."))

    def _read_reconnect(self, connection):
        """Read/restore outside row locks, retaining an unfinished comparison."""

        network_changed = False
        baseline = self.pending_observation_json or False
        health, observation, token, observed_at = self._health_snapshot(connection)
        unavailable = (
            health.get("conclusive") is False or health.get("reason") == "unauthorized"
        )
        client = (
            None
            if unavailable
            else WuzapiOnboardingClient(
                connection.wuzapi_base_url, api_token=connection.wuzapi_api_token
            )
        )
        if not unavailable and not baseline and health.get("connected") is not True:
            if observation.get("events") is None:
                raise AdapterError("Provider event configuration is unavailable")
            if not self._current(self.request_revision):
                raise ValidationError(
                    _("The reconnect operation is no longer current.")
                )
            baseline = observation
            client.connect(())
            health, observation, token, observed_at = self._health_snapshot(connection)
        if baseline and observation.get("events") is not None:
            network_changed = any(
                baseline.get(key) != observation.get(key)
                for key in ("proxy_present", "proxy_digest", "proxy_pool_id")
            )
            if baseline["events"] != observation["events"]:
                health = dict(
                    health,
                    state="degraded",
                    reason="invalid_response",
                    conclusive=False,
                    wuzapi_events_changed=True,
                )
                result, qr = (
                    "error",
                    _(
                        "The provider's subscribed events changed. "
                        "Review the webhook configuration."
                    ),
                ), False
                return health, token, observed_at, result, qr, network_changed, False
            baseline = False
        if baseline:
            # No empty observation may masquerade as a subscription change.
            # Check reuses this operation with GET only and the saved safe baseline.
            if (
                health.get("conclusive") is not False
                and health.get("reason") != "unauthorized"
            ):
                health = dict(
                    health,
                    state="degraded",
                    reason="invalid_response",
                    conclusive=False,
                )
            result, qr = (
                "waiting",
                _(
                    "The provider's subscribed events could not be confirmed. "
                    "Wait, then click Check again."
                ),
            ), False
        elif (
            health.get("conclusive") is False or health.get("reason") == "unauthorized"
        ):
            result, qr = (
                "waiting",
                _("The provider is unavailable. Check the connection again."),
            ), False
        elif (
            health.get("identity_matches") is True
            and health.get("state") == "connected"
        ):
            result, qr = ("ready", False), False
        elif health.get("reason") == "identity_mismatch":
            result, qr = (
                "error",
                _(
                    "The paired number does not match this inbox. "
                    "Unlink this session on the incorrect phone, then "
                    "reconnect with the inbox's number."
                ),
            ), False
        elif observation.get("passkey_pending"):
            result, qr = (
                "error",
                _("Complete the additional authorization at the provider."),
            ), False
        elif health.get("logged_in") is not False:
            result, qr = (
                "waiting",
                _(
                    "The session identity could not be verified. "
                    "Check its configured number and try Check again."
                ),
            ), False
        else:
            qr = self._read_reconnect_qr(client)
            result = (
                (
                    "qr",
                    _(
                        "Scan with the number configured for this inbox, then click Check."
                    ),
                )
                if qr
                else ("waiting", _("The session is starting. Check again shortly."))
            )
        return health, token, observed_at, result, qr, network_changed, baseline

    def _read_reconnect_qr(self, client):
        if not self._current(self.request_revision):
            raise ValidationError(_("The reconnect operation is no longer current."))
        try:
            client.qr_png()
        except (ProviderRateLimitError, TransientAdapterError):
            raise
        except AdapterError:
            return False
        return True

    def _job_reconnect(self, revision):
        self.ensure_one()
        if not self.env.context.get("job_uuid"):
            raise ValidationError(_("Reconnect must run through queue_job."))
        try:
            connection = self._current(revision)
            if not connection:
                return self._finish_error(revision)
            (
                health,
                token,
                observed_at,
                result,
                qr,
                network_changed,
                pending_observation,
            ) = self._read_reconnect(connection)
            current = self._current(revision, lock=True)
            if not current:
                return self._finish_error(revision)
            if current._contact_center_health_observation_token() != token:
                pending_observation = False
                result, qr = (
                    "waiting",
                    _("The connection changed. Check again."),
                ), False
            if qr and self.env["contact.center.outbox.command"].sudo().search_count(
                [
                    ("provider_connection_id", "=", current.id),
                    ("state", "=", "processing"),
                ]
            ):
                result, qr = (
                    "waiting",
                    _("Wait for the messages currently being sent."),
                ), False
            if qr:
                epoch = current._wuzapi_begin_pairing_epoch()
                self._update({"pairing_revision": epoch})
                token = current._contact_center_health_observation_token()
                observed_at = fields.Datetime.now()
                health = {
                    "state": "authentication_required",
                    "reason": "pairing_required",
                }
            applied = current._apply_health_result(
                health,
                source="health_job",
                observed_at=observed_at,
                expected_configuration_revision=self.configuration_revision,
                expected_observation_token=token,
                records_health_probe=True,
            )
            if applied and health.get("wuzapi_events_changed"):
                current.write(
                    {
                        "wuzapi_webhook_sync_state": "drift",
                        "wuzapi_webhook_last_error": (
                            "Subscribed events changed during reconnect"
                        ),
                    }
                )
            current.write({"wuzapi_onboarding_job_uuid": False})
            message = result[1] or ""
            if network_changed:
                message = (
                    message
                    + "\n"
                    + _("The provider applied its configured network policy.")
                ).strip()
            self._update(
                {
                    "state": result[0],
                    "message": message or False,
                    "retry_at": False,
                    "pending_observation_json": pending_observation,
                }
            )
            return result[0] == "ready"
        except (ProviderRateLimitError, TransientAdapterError) as error:
            if connection._wuzapi_onboarding_attempt(self.env.context["job_uuid"]) < 6:
                raise RetryableJobError(
                    str(error), seconds=getattr(error, "retry_after_seconds", 0) or 60
                ) from error
            return self._finish_error(revision)
        except (AdapterError, AccessError, ValidationError):
            return self._finish_error(revision)

    def _finish_error(self, revision):
        try:
            current = self._current(revision, lock=True)
        except (AccessError, ValidationError):
            current = False
        if current:
            current.write({"wuzapi_onboarding_job_uuid": False})
        self._update(
            {
                "state": "error",
                "message": _(
                    "The provider could not restore the session. "
                    "Review its configuration and try again."
                )
                if current
                else _(
                    "This reconnect request is no longer current. Close and reopen it."
                ),
                "retry_at": False,
                "pending_observation_json": False,
            }
        )
        return False

    def action_quarantined_events(self):
        connection = self._connection()
        return {
            "type": "ir.actions.act_window",
            "name": _("Messages Awaiting Origin Confirmation"),
            "res_model": "contact.center.inbox.event",
            "view_mode": "tree,form",
            "domain": [
                ("provider_connection_id", "=", connection.id),
                ("state", "=", "blocked"),
                ("last_error_class", "=", "PairingUnverified"),
            ],
        }
