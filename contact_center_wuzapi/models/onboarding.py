import datetime
import hashlib
import secrets

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderRateLimitError,
    TransientAdapterError,
)
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import WUZAPI_LIFECYCLE_EVENT_STATES, WUZAPI_VERSION
from ..services.onboarding import WuzapiOnboardingClient, is_valid_session_jid

_ACTIVE_JOB_STATES = ("pending", "enqueued", "started", "wait_dependencies")
_ONBOARDING_OPERATIONS = frozenset({"prepare", "check"})
_ONBOARDING_RETRY_CEILING = 6
_GUIDED_EXISTING_TOKEN_MIN_LENGTH = 32
_HEALTH_FRESHNESS = datetime.timedelta(minutes=5)
_RESTRICTIVE_LIFECYCLE_EVENTS = tuple(
    sorted(
        event_type
        for event_type, state in WUZAPI_LIFECYCLE_EVENT_STATES.items()
        if state != "connected"
    )
)
_GUIDED_STRUCTURAL_FIELDS = frozenset(
    {
        "wuzapi_server_id",
        "wuzapi_managed_instance",
        "wuzapi_instance_name",
    }
)
_ONBOARDING_CONCURRENT_MUTATION_FIELDS = _GUIDED_STRUCTURAL_FIELDS | frozenset(
    {
        "active",
        "role",
        "inbound_active",
        "outbound_active",
        "wuzapi_base_url",
        "wuzapi_api_token",
        "wuzapi_hmac_secret",
        "wuzapi_hmac_pending_secret",
        "wuzapi_hmac_rotation_state",
        "wuzapi_hmac_rotation_revision",
        "wuzapi_hmac_rotation_job_uuid",
        "wuzapi_webhook_event_ids",
    }
)
_WUZAPI_GUIDED_REPAIR_CONTEXT = "contact_center_wuzapi_guided_repair"
_WUZAPI_GUIDED_REPAIR_TOKEN = object()


def _is_header_safe_token(value, minimum_length):
    return bool(
        isinstance(value, str)
        and len(value) >= minimum_length
        and all(33 <= ord(character) <= 126 for character in value)
    )


class ContactCenterWuzapiServer(models.Model):
    _name = "contact.center.wuzapi.server"
    _description = "Contact Center WuzAPI Server"
    _order = "name, id"
    _check_company_auto = True

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
        ondelete="restrict",
    )
    base_url = fields.Char(string="Service URL", required=True, index=True)
    admin_token = fields.Char(
        string="Administrative Token",
        required=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    state = fields.Selection(
        [
            ("unchecked", "Not Checked"),
            ("available", "Available"),
            ("error", "Unavailable"),
        ],
        required=True,
        readonly=True,
        copy=False,
        default="unchecked",
    )
    last_check_at = fields.Datetime(readonly=True, copy=False)
    instance_count = fields.Integer(readonly=True, copy=False)
    last_error = fields.Char(readonly=True, copy=False)
    connection_ids = fields.One2many(
        "contact.center.provider.connection",
        "wuzapi_server_id",
        string="Connections",
    )

    _sql_constraints = [
        (
            "company_base_url_unique",
            "unique(company_id, base_url)",
            "This WuzAPI server is already configured for the company.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        normalizer = self.env[
            "contact.center.provider.connection"
        ]._normalize_wuzapi_base_url
        prepared = []
        for original in vals_list:
            values = dict(original)
            values["base_url"] = normalizer(values.get("base_url", ""))
            prepared.append(values)
        return super().create(prepared)

    def write(self, values):
        values = dict(values)
        if "base_url" in values:
            normalized = self.env[
                "contact.center.provider.connection"
            ]._normalize_wuzapi_base_url(values["base_url"])
            if any(
                server.connection_ids and server.base_url != normalized
                for server in self
            ):
                raise ValidationError(
                    _("A WuzAPI server URL cannot change while connections use it.")
                )
            values["base_url"] = normalized
        if "company_id" in values and any(
            server.connection_ids and values["company_id"] != server.company_id.id
            for server in self
        ):
            raise ValidationError(
                _("A WuzAPI server company cannot change while connections use it.")
            )
        if {"base_url", "admin_token"}.intersection(values):
            values.update({"state": "unchecked", "last_error": False})
        return super().write(values)

    @api.constrains("admin_token")
    def _check_admin_token(self):
        for server in self:
            token = server.admin_token or ""
            if not _is_header_safe_token(token, 12):
                raise ValidationError(
                    _("The WuzAPI administrative token is invalid or too short.")
                )

    def _require_admin(self):
        self.ensure_one()
        if not (
            self.env.is_superuser()
            or self.env.user.has_group("contact_center_base.group_contact_center_admin")
        ):
            raise AccessError(
                _("Only Contact Center administrators can manage WuzAPI servers.")
            )
        self.check_access_rights("write")
        self.check_access_rule("write")
        return True

    def _onboarding_client(self, api_token=None):
        self.ensure_one()
        return WuzapiOnboardingClient(
            self.base_url,
            admin_token=self.admin_token,
            api_token=api_token,
        )

    def action_check_server(self):
        self._require_admin()
        try:
            count = len(self._onboarding_client().list_users())
        except (AdapterError, TransientAdapterError):
            self.write(
                {
                    "state": "error",
                    "last_check_at": fields.Datetime.now(),
                    "last_error": _("The WuzAPI administrative API did not validate."),
                }
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("WuzAPI"),
                    "message": _(
                        "WuzAPI did not accept the configured server credentials."
                    ),
                    "type": "danger",
                    "sticky": True,
                },
            }
        self.write(
            {
                "state": "available",
                "last_check_at": fields.Datetime.now(),
                "instance_count": count,
                "last_error": False,
            }
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("WuzAPI"),
                "message": _("Server verified: %(count)s instance(s).", count=count),
                "type": "success",
                "sticky": False,
            },
        }


class ContactCenterProviderConnection(models.Model):
    _inherit = "contact.center.provider.connection"

    wuzapi_server_id = fields.Many2one(
        "contact.center.wuzapi.server",
        string="Managed Server",
        ondelete="restrict",
        check_company=True,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_managed_instance = fields.Boolean(
        string="Managed Instance",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_remote_user_id = fields.Char(
        string="Remote Instance ID",
        readonly=True,
        copy=False,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_instance_name = fields.Char(
        string="Instance Name",
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_instance_fingerprint = fields.Char(
        string="Instance Fingerprint",
        compute="_compute_wuzapi_instance_fingerprint",
        store=True,
        readonly=True,
        copy=False,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_onboarding_state = fields.Selection(
        [
            ("none", "Not Started"),
            ("queued", "Queued"),
            ("provisioning", "Preparing Provider"),
            ("awaiting_scan", "Waiting for QR Scan"),
            ("verifying", "Verifying"),
            ("ready", "Ready to Activate"),
            ("error", "Needs Attention"),
        ],
        string="Guided Setup",
        required=True,
        readonly=True,
        copy=False,
        default="none",
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_onboarding_revision = fields.Integer(
        readonly=True,
        copy=False,
        default=0,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_onboarding_job_uuid = fields.Char(
        readonly=True,
        copy=False,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_onboarding_last_error = fields.Char(
        string="Setup Detail",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_hmac_verified_at = fields.Datetime(
        string="HMAC Verified At",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_pairing_revision = fields.Integer(
        readonly=True,
        copy=False,
        default=0,
        groups="contact_center_base.group_contact_center_admin",
    )

    _sql_constraints = [
        (
            "wuzapi_onboarding_revision_nonnegative",
            "check(wuzapi_onboarding_revision >= 0 and wuzapi_pairing_revision >= 0)",
            "WuzAPI setup revisions cannot be negative.",
        ),
    ]

    @api.depends("adapter_key", "wuzapi_base_url", "wuzapi_api_token")
    def _compute_wuzapi_instance_fingerprint(self):
        for connection in self:
            if (
                connection.adapter_key == "wuzapi"
                and connection.wuzapi_base_url
                and connection.wuzapi_api_token
            ):
                material = "%s\0%s" % (
                    connection.wuzapi_base_url.rstrip("/"),
                    connection.wuzapi_api_token,
                )
                connection.wuzapi_instance_fingerprint = hashlib.sha256(
                    material.encode("utf-8")
                ).hexdigest()
            else:
                connection.wuzapi_instance_fingerprint = False

    def write(self, values):
        if _ONBOARDING_CONCURRENT_MUTATION_FIELDS.intersection(values):
            running = self.filtered(
                lambda item: item.adapter_key == "wuzapi"
                and item.onboarding_ref
                and item._wuzapi_active_onboarding_job()
            )
            if running:
                raise ValidationError(
                    _(
                        "Wait for the guided WuzAPI verification before changing "
                        "the connection or its instance settings."
                    )
                )
        guided_repair = (
            self.env.context.get(_WUZAPI_GUIDED_REPAIR_CONTEXT)
            is _WUZAPI_GUIDED_REPAIR_TOKEN
        )
        immutable = (
            frozenset()
            if guided_repair
            else _GUIDED_STRUCTURAL_FIELDS.intersection(values)
        )
        if immutable:
            for connection in self.filtered(
                lambda item: item.adapter_key == "wuzapi" and item.onboarding_ref
            ):
                for field_name in immutable:
                    current = connection[field_name]
                    field = connection._fields[field_name]
                    if field.type == "many2one":
                        current = current.id
                    if values[field_name] != current:
                        raise ValidationError(
                            _(
                                "The server and remote instance selected by guided "
                                "WuzAPI setup cannot be replaced in place."
                            )
                        )
        return super().write(values)

    def init(self):
        result = super().init()
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                contact_center_wuzapi_active_instance_unique
            ON contact_center_provider_connection (wuzapi_instance_fingerprint)
            WHERE adapter_key = 'wuzapi'
              AND active IS TRUE
              AND role != 'historical'
              AND wuzapi_instance_fingerprint IS NOT NULL
            """
        )
        return result

    @api.constrains(
        "adapter_key",
        "wuzapi_server_id",
        "wuzapi_base_url",
        "wuzapi_instance_name",
    )
    def _check_wuzapi_onboarding_configuration(self):
        for connection in self.filtered(lambda item: item.adapter_key == "wuzapi"):
            if connection.wuzapi_server_id:
                if connection.wuzapi_server_id.company_id != connection.company_id:
                    raise ValidationError(
                        _("WuzAPI server and connection must use the same company.")
                    )
                if connection.wuzapi_server_id.base_url != connection.wuzapi_base_url:
                    raise ValidationError(
                        _("WuzAPI server and connection service URLs must match.")
                    )
            if (
                connection.wuzapi_instance_name
                and len(connection.wuzapi_instance_name.strip()) > 120
            ):
                raise ValidationError(_("The WuzAPI instance name is too long."))

    def _contact_center_activation_blockers(self, direction="inbound", now=None):
        blockers = list(super()._contact_center_activation_blockers(direction, now))
        self.ensure_one()
        if self.adapter_key != "wuzapi":
            return blockers
        now = fields.Datetime.to_datetime(now or fields.Datetime.now())
        last_health_at = fields.Datetime.to_datetime(self.last_health_at)
        if self.onboarding_ref and self.wuzapi_onboarding_state != "ready":
            blockers.append(_("guided setup has not completed"))
        if self.state != "connected" or self.health_detail != "healthy":
            blockers.append(_("provider session is not healthy"))
        if (
            not last_health_at
            or not self._contact_center_observation_is_fresh(now=now)
            or self.health_check_pending
        ):
            blockers.append(_("provider health proof is stale"))
        if self.identity_mismatch_latched or not self.account_id.own_external_identity:
            blockers.append(_("WhatsApp identity is not verified"))
        if (
            self.wuzapi_hmac_rotation_state != "stable"
            or self.wuzapi_hmac_rotation_job_uuid
        ):
            blockers.append(_("webhook signature is not verified"))
        if self.onboarding_ref and (
            not self.wuzapi_hmac_verified_at
            or fields.Datetime.to_datetime(self.wuzapi_hmac_verified_at)
            < now - _HEALTH_FRESHNESS
        ):
            blockers.append(_("guided webhook signature proof is stale"))
        if (
            self.wuzapi_webhook_sync_state != "in_sync"
            or not self.wuzapi_webhook_url_matches
            or not self.wuzapi_webhook_last_sync_at
        ):
            blockers.append(_("webhook configuration is not synchronized"))
        elif (
            self.onboarding_ref
            and fields.Datetime.to_datetime(self.wuzapi_webhook_last_sync_at)
            < now - _HEALTH_FRESHNESS
        ):
            blockers.append(_("guided webhook synchronization proof is stale"))
        if self.onboarding_ref and (
            not self.wuzapi_server_id
            or not self.wuzapi_server_id.active
            or self.wuzapi_server_id.state != "available"
        ):
            blockers.append(_("managed WuzAPI server is not verified"))
        if self.onboarding_ref and self.last_health_at:
            self.env.cr.execute(
                """
                SELECT 1
                  FROM contact_center_inbox_event
                 WHERE provider_connection_id = %s
                   AND state = 'blocked'
                   AND metadata_json ->> 'blocked_reason' =
                       'onboarding_not_activated'
                   AND metadata_json ->> 'onboarding_ref' = %s
                   AND metadata_json ->> 'event_type' = ANY(%s)
                   AND create_date >= %s
                 LIMIT 1
                """,
                [
                    self.id,
                    self.onboarding_ref,
                    list(_RESTRICTIVE_LIFECYCLE_EVENTS),
                    self.last_health_at,
                ],
            )
            if self.env.cr.fetchone():
                blockers.append(
                    _("a newer provider disconnection requires revalidation")
                )
        return blockers

    def _wuzapi_onboarding_require_admin(self):
        self.ensure_one()
        if not (
            self.env.is_superuser()
            or self.env.user.has_group("contact_center_base.group_contact_center_admin")
        ):
            raise AccessError(
                _("Only Contact Center administrators can pair WuzAPI instances.")
            )
        self.check_access_rights("write")
        self.check_access_rule("write")
        if self.adapter_key != "wuzapi":
            raise ValidationError(_("Guided pairing requires a WuzAPI connection."))
        if self.role not in ("migration", "standby"):
            raise ValidationError(
                _("Only a staged WuzAPI connection can enter guided pairing.")
            )
        if (
            self.wuzapi_hmac_rotation_state != "stable"
            or self.wuzapi_hmac_rotation_job_uuid
        ):
            raise ValidationError(
                _("Wait for the WuzAPI HMAC rotation before continuing setup.")
            )
        return True

    def _contact_center_onboarding_resume_values(self):
        values = super()._contact_center_onboarding_resume_values()
        self.ensure_one()
        if self.adapter_key == "wuzapi":
            values.update(
                {
                    "wuzapi_server_id": self.wuzapi_server_id.id,
                    "wuzapi_instance_mode": (
                        "managed" if self.wuzapi_managed_instance else "existing"
                    ),
                    "wuzapi_instance_name": self.wuzapi_instance_name,
                }
            )
        return values

    def _contact_center_onboarding_resume_step(self):
        self.ensure_one()
        step = super()._contact_center_onboarding_resume_step()
        if step != "done" and self.adapter_key == "wuzapi":
            return "ready" if self.wuzapi_onboarding_state == "ready" else "connect"
        return step

    def _wuzapi_active_onboarding_job(self):
        self.ensure_one()
        job = (
            self.env["queue.job"]
            .sudo()
            .search(
                [
                    (
                        "identity_key",
                        "=",
                        "contact_center:wuzapi_onboarding:%s:%s"
                        % (self.id, self.wuzapi_onboarding_revision),
                    ),
                    ("state", "in", _ACTIVE_JOB_STATES),
                ],
                order="id desc",
                limit=1,
            )
        )
        if job and self.wuzapi_onboarding_job_uuid != job.uuid:
            self.sudo().write({"wuzapi_onboarding_job_uuid": job.uuid})
        return job

    def _enqueue_wuzapi_onboarding(self, operation):
        self.ensure_one()
        if operation not in _ONBOARDING_OPERATIONS:
            raise ValidationError(_("The WuzAPI setup operation is invalid."))
        connection = self.sudo().with_company(self.company_id)
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection WHERE id = %s FOR UPDATE",
            [connection.id],
        )
        connection.invalidate_recordset(
            [
                "wuzapi_onboarding_job_uuid",
                "wuzapi_onboarding_revision",
                "health_configuration_revision",
                "wuzapi_webhook_sync_revision",
                "wuzapi_hmac_rotation_revision",
                "wuzapi_hmac_rotation_state",
                "wuzapi_hmac_rotation_job_uuid",
            ]
        )
        if connection._wuzapi_active_onboarding_job():
            return False
        if (
            connection.wuzapi_hmac_rotation_state != "stable"
            or connection.wuzapi_hmac_rotation_job_uuid
        ):
            raise ValidationError(
                _("Wait for the WuzAPI HMAC rotation before continuing setup.")
            )
        revision = connection.wuzapi_onboarding_revision + 1
        configuration_revision = connection.health_configuration_revision
        webhook_revision = connection.wuzapi_webhook_sync_revision
        hmac_revision = connection.wuzapi_hmac_rotation_revision
        connection.write(
            {
                "wuzapi_onboarding_state": "queued",
                "wuzapi_onboarding_revision": revision,
                "wuzapi_onboarding_job_uuid": False,
                "wuzapi_onboarding_last_error": False,
            }
        )
        delayed = connection.with_delay(
            identity_key="contact_center:wuzapi_onboarding:%s:%s"
            % (connection.id, revision),
            max_retries=0,
            priority=32,
            description="Contact Center WuzAPI setup %s %s"
            % (operation, connection.id),
        )._job_wuzapi_onboarding(
            revision,
            operation,
            configuration_revision,
            webhook_revision,
            hmac_revision,
        )
        connection.write({"wuzapi_onboarding_job_uuid": delayed.uuid})
        return True

    def action_wuzapi_start_onboarding(self):
        self._wuzapi_onboarding_require_admin()
        self._enqueue_wuzapi_onboarding("prepare")
        return True

    def action_wuzapi_check_onboarding(self):
        self._wuzapi_onboarding_require_admin()
        self._enqueue_wuzapi_onboarding("check")
        return True

    def _wuzapi_onboarding_current(
        self,
        revision,
        job_uuid,
        configuration_revision,
        webhook_revision,
        hmac_revision,
        *,
        lock=False,
    ):
        connection = self.sudo().exists()
        if not connection:
            return connection
        connection.ensure_one()
        if lock:
            self.env.cr.execute(
                "SELECT id FROM contact_center_provider_connection "
                "WHERE id = %s FOR UPDATE",
                [connection.id],
            )
            if not self.env.cr.fetchone():
                return self.browse()
        connection.invalidate_recordset(
            [
                "active",
                "adapter_key",
                "role",
                "wuzapi_onboarding_job_uuid",
                "wuzapi_onboarding_revision",
                "health_configuration_revision",
                "wuzapi_webhook_sync_revision",
                "wuzapi_hmac_rotation_revision",
                "wuzapi_hmac_rotation_state",
                "wuzapi_hmac_rotation_job_uuid",
            ]
        )
        if (
            not connection.active
            or connection.adapter_key != "wuzapi"
            or connection.role not in ("migration", "standby")
            or connection.wuzapi_onboarding_revision != revision
            or connection.wuzapi_onboarding_job_uuid != job_uuid
            or connection.health_configuration_revision != configuration_revision
            or connection.wuzapi_webhook_sync_revision != webhook_revision
            or connection.wuzapi_hmac_rotation_revision != hmac_revision
            or connection.wuzapi_hmac_rotation_state != "stable"
            or connection.wuzapi_hmac_rotation_job_uuid
        ):
            return self.browse()
        return connection.with_company(connection.company_id)

    def _wuzapi_onboarding_attempt(self, job_uuid):
        job = self.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
        return (job.retry if job else 0) + 1

    def _wuzapi_onboarding_client(self):
        self.ensure_one()
        server = self.wuzapi_server_id
        if not server or not server.active:
            raise AdapterError("WuzAPI managed server is unavailable")
        return server._onboarding_client(api_token=self.wuzapi_api_token)

    def _wuzapi_prepare_remote(self):
        self.ensure_one()
        client = self._wuzapi_onboarding_client()
        desired_events = self._wuzapi_desired_webhook_events()
        remote_user = None
        if self.wuzapi_managed_instance:
            remote_user = client.create_or_find_user(
                name=self.wuzapi_instance_name,
                token=self.wuzapi_api_token,
                webhook_url=self.wuzapi_webhook_url,
                events=desired_events,
                hmac_key=self.wuzapi_hmac_secret,
            )
        adapter = self.get_adapter()
        adapter.set_hmac_configuration(self, self.wuzapi_hmac_secret)
        observed = adapter.set_webhook_configuration(
            self,
            self.wuzapi_webhook_url,
            desired_events,
        )
        client.connect(desired_events)
        status_observed_at = fields.Datetime.now()
        return remote_user, observed, client.status(), status_observed_at

    def _wuzapi_check_remote(self):
        self.ensure_one()
        client = self._wuzapi_onboarding_client()
        adapter = self.get_adapter()
        # WuzAPI only exposes whether *some* HMAC key exists. Reapply our
        # desired key on every explicit verification and then read status back;
        # this makes the cached proof about this setup revision, not an unknown
        # key left by an older integration.
        adapter.set_hmac_configuration(self, self.wuzapi_hmac_secret)
        desired_events = self._wuzapi_desired_webhook_events()
        observed = adapter.set_webhook_configuration(
            self,
            self.wuzapi_webhook_url,
            desired_events,
        )
        status_observed_at = fields.Datetime.now()
        status = client.status()
        return None, observed, status, status_observed_at

    def _wuzapi_apply_onboarding_observation(
        self,
        revision,
        job_uuid,
        configuration_revision,
        webhook_revision,
        hmac_revision,
        remote_user,
        observed,
        status,
        status_observed_at,
    ):
        self.ensure_one()
        desired_events = self._wuzapi_desired_webhook_events()
        url_matches = observed.get("webhook_url") == self.wuzapi_webhook_url
        events_match = tuple(observed.get("events") or ()) == desired_events
        hmac_matches = status.get("hmac_configured") is True
        ready = bool(
            status.get("connected")
            and status.get("logged_in")
            and status.get("jid")
            and hmac_matches
            and url_matches
            and events_match
        )
        observed_identity = status.get("jid") if status.get("jid") else False
        if observed_identity and not is_valid_session_jid(observed_identity):
            raise AdapterError("WuzAPI returned an invalid session identity")
        self._contact_center_lock_topology(self.account_id.ids)
        current = self._wuzapi_onboarding_current(
            revision,
            job_uuid,
            configuration_revision,
            webhook_revision,
            hmac_revision,
            lock=True,
        )
        if not current:
            return False
        if ready:
            configured_identity = current.account_id.own_external_identity
            normalize = current.get_adapter()._provider_normalize_session_identity
            if configured_identity and normalize(configured_identity) != normalize(
                observed_identity
            ):
                raise AdapterError("WuzAPI session identity does not match the inbox")
            if not configured_identity:
                current.account_id._wuzapi_bind_guided_identity(
                    current, observed_identity
                )
        if status.get("logged_in") is not True:
            health = {
                "state": "authentication_required",
                "reason": "session_not_authenticated",
            }
        elif status.get("connected") is not True:
            health = {"state": "disconnected", "reason": "session_disconnected"}
        elif not observed_identity:
            health = {
                "state": "degraded",
                "reason": "identity_unverified",
                "identity_matches": False,
            }
        else:
            configured_identity = current.account_id.own_external_identity
            normalize = current.get_adapter()._provider_normalize_session_identity
            identity_matches = normalize(configured_identity) == normalize(
                observed_identity
            )
            health = {
                "state": "connected" if identity_matches else "degraded",
                "reason": "ready" if identity_matches else "identity_mismatch",
                "identity_matches": identity_matches,
            }
        current._apply_health_result(
            health,
            source="health_job",
            observed_at=status_observed_at,
            records_health_probe=True,
        )
        values = {
            "wuzapi_onboarding_job_uuid": False,
            "wuzapi_onboarding_state": "ready" if ready else "awaiting_scan",
            "wuzapi_onboarding_last_error": False,
            "wuzapi_webhook_observed_events_json": list(observed.get("events") or ()),
            "wuzapi_webhook_url_matches": url_matches,
            "wuzapi_webhook_sync_state": (
                "in_sync" if url_matches and events_match else "drift"
            ),
            "wuzapi_webhook_last_sync_at": status_observed_at,
            "wuzapi_webhook_last_error": False,
            "wuzapi_hmac_verified_at": (status_observed_at if hmac_matches else False),
            "wuzapi_pairing_revision": current.wuzapi_pairing_revision + 1,
        }
        if remote_user and remote_user.get("id"):
            values["wuzapi_remote_user_id"] = remote_user["id"]
        current.write(values)
        return ready

    def _wuzapi_finish_onboarding_error(
        self,
        revision,
        job_uuid,
        configuration_revision,
        webhook_revision,
        hmac_revision,
        message,
    ):
        self._contact_center_lock_topology(self.account_id.ids)
        current = self._wuzapi_onboarding_current(
            revision,
            job_uuid,
            configuration_revision,
            webhook_revision,
            hmac_revision,
            lock=True,
        )
        if not current:
            return False
        current.write(
            {
                "wuzapi_onboarding_state": "error",
                "wuzapi_onboarding_job_uuid": False,
                "wuzapi_onboarding_last_error": message,
            }
        )
        return False

    def _job_wuzapi_onboarding(
        self,
        revision,
        operation,
        configuration_revision,
        webhook_revision,
        hmac_revision,
    ):
        self.ensure_one()
        job_uuid = self.env.context.get("job_uuid")
        if not job_uuid:
            raise ValidationError(_("WuzAPI setup must run through queue_job."))
        connection = self._wuzapi_onboarding_current(
            revision,
            job_uuid,
            configuration_revision,
            webhook_revision,
            hmac_revision,
        )
        if not connection or operation not in _ONBOARDING_OPERATIONS:
            return False
        try:
            result = (
                connection._wuzapi_prepare_remote()
                if operation == "prepare"
                else connection._wuzapi_check_remote()
            )
            return connection._wuzapi_apply_onboarding_observation(
                revision,
                job_uuid,
                configuration_revision,
                webhook_revision,
                hmac_revision,
                *result,
            )
        except (ProviderRateLimitError, TransientAdapterError) as error:
            if (
                connection._wuzapi_onboarding_attempt(job_uuid)
                < _ONBOARDING_RETRY_CEILING
            ):
                raise RetryableJobError(str(error), seconds=None) from error
            return connection._wuzapi_finish_onboarding_error(
                revision,
                job_uuid,
                configuration_revision,
                webhook_revision,
                hmac_revision,
                _("The provider remained unavailable after the safe retries."),
            )
        except (AdapterError, ValidationError):
            return connection._wuzapi_finish_onboarding_error(
                revision,
                job_uuid,
                configuration_revision,
                webhook_revision,
                hmac_revision,
                _("The provider rejected the configuration or returned invalid proof."),
            )

    def _wuzapi_onboarding_qr_png(self):
        self.ensure_one()
        if self.wuzapi_onboarding_state != "awaiting_scan":
            raise ValidationError(
                _("This WuzAPI connection is not waiting for a QR scan.")
            )
        return self._wuzapi_onboarding_client().qr_png()


class ContactCenterAccountSetupWizard(models.TransientModel):
    _inherit = "contact.center.account.setup.wizard"

    wuzapi_server_id = fields.Many2one(
        "contact.center.wuzapi.server",
        string="WuzAPI Server",
        check_company=True,
        domain="[('company_id', '=', company_id), ('active', '=', True)]",
    )
    wuzapi_instance_mode = fields.Selection(
        [
            ("managed", "Create a new instance"),
            ("existing", "Connect an existing instance"),
        ],
        string="Instance",
        default="managed",
    )
    wuzapi_instance_name = fields.Char(string="Instance Name")
    wuzapi_existing_token = fields.Char(
        string="Instance Token",
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
        help="Use the private token of the existing instance (at least 32 characters).",
    )
    wuzapi_onboarding_state = fields.Selection(
        [
            ("none", "Not Started"),
            ("queued", "Queued"),
            ("provisioning", "Preparing Provider"),
            ("awaiting_scan", "Waiting for QR Scan"),
            ("verifying", "Verifying"),
            ("ready", "Ready to Activate"),
            ("error", "Needs Attention"),
        ],
        compute="_compute_wuzapi_onboarding_state",
        readonly=True,
    )
    wuzapi_onboarding_last_error = fields.Char(
        related="connection_id.wuzapi_onboarding_last_error",
        readonly=True,
    )
    wuzapi_pairing_revision = fields.Integer(
        related="connection_id.wuzapi_pairing_revision",
        readonly=True,
    )
    wuzapi_qr_url = fields.Char(compute="_compute_wuzapi_qr_url")
    wuzapi_connected_identity = fields.Char(
        related="account_id.own_external_identity",
        readonly=True,
    )

    @api.depends("connection_id.wuzapi_onboarding_state")
    def _compute_wuzapi_onboarding_state(self):
        for wizard in self:
            wizard.wuzapi_onboarding_state = (
                wizard.connection_id.wuzapi_onboarding_state or "none"
            )

    @api.model
    def _contact_center_onboarding_provider_choices(self):
        choices = list(super()._contact_center_onboarding_provider_choices())
        if not any(key == "wuzapi" for key, _label in choices):
            choices.append(("wuzapi", "WuzAPI · WhatsApp"))
        return choices

    @api.depends("connection_id", "wuzapi_pairing_revision", "setup_ref")
    def _compute_wuzapi_qr_url(self):
        for wizard in self:
            wizard.wuzapi_qr_url = (
                "/contact-center/onboarding/wuzapi/qr/%s/%s"
                % (wizard.setup_ref, wizard.wuzapi_pairing_revision)
                if wizard.connection_id
                and wizard.wuzapi_onboarding_state == "awaiting_scan"
                else False
            )

    def _contact_center_onboarding_platform(self):
        self.ensure_one()
        if self.provider_key == "wuzapi":
            return "whatsapp"
        return super()._contact_center_onboarding_platform()

    def _contact_center_onboarding_validate_provider(self):
        self.ensure_one()
        if self.provider_key != "wuzapi":
            return super()._contact_center_onboarding_validate_provider()
        server = self.wuzapi_server_id
        if not server or not server.active or server.company_id != self.company_id:
            raise ValidationError(_("Select an active WuzAPI server for the company."))
        if server.state != "available":
            raise ValidationError(_("Verify the WuzAPI server before continuing."))
        if not (self.wuzapi_instance_name or "").strip():
            raise ValidationError(_("Enter a name for the WuzAPI instance."))
        if self.wuzapi_instance_mode == "existing":
            connection = self.connection_id.exists()
            if not connection and self.setup_ref:
                connection = (
                    self.env["contact.center.provider.connection"]
                    .with_context(active_test=False)
                    .search([("onboarding_ref", "=", self.setup_ref)], limit=1)
                )
            token = self.wuzapi_existing_token or ""
            if not connection and not _is_header_safe_token(
                token, _GUIDED_EXISTING_TOKEN_MIN_LENGTH
            ):
                raise ValidationError(_("Enter a valid existing WuzAPI token."))
        elif self.wuzapi_instance_mode != "managed":
            raise ValidationError(_("Select how the WuzAPI instance will be created."))
        return True

    def _contact_center_onboarding_connection_values(self):
        self.ensure_one()
        if self.provider_key != "wuzapi":
            return super()._contact_center_onboarding_connection_values()
        token = (
            secrets.token_urlsafe(36)
            if self.wuzapi_instance_mode == "managed"
            else self.wuzapi_existing_token.strip()
        )
        values = {
            "provider_schema_version": WUZAPI_VERSION,
            "wuzapi_server_id": self.wuzapi_server_id.id,
            "wuzapi_base_url": self.wuzapi_server_id.base_url,
            "wuzapi_api_token": token,
            "wuzapi_hmac_secret": secrets.token_urlsafe(48),
            "wuzapi_instance_name": self.wuzapi_instance_name.strip(),
            "wuzapi_managed_instance": self.wuzapi_instance_mode == "managed",
        }
        self._contact_center_internal().write({"wuzapi_existing_token": False})
        return values

    def _contact_center_onboarding_start(self):
        self.ensure_one()
        if self.provider_key != "wuzapi":
            return super()._contact_center_onboarding_start()
        self.connection_id.action_wuzapi_start_onboarding()
        return True

    def _contact_center_onboarding_refresh(self):
        self.ensure_one()
        if self.provider_key != "wuzapi":
            return super()._contact_center_onboarding_refresh()
        connection = self.connection_id
        connection.invalidate_recordset(
            ["wuzapi_onboarding_state", "wuzapi_onboarding_job_uuid"]
        )
        active_job = connection._wuzapi_active_onboarding_job()
        state = connection.wuzapi_onboarding_state
        if state == "ready":
            self._contact_center_internal().write({"step": "ready"})
        elif active_job:
            return self.step
        elif state == "awaiting_scan":
            connection.action_wuzapi_check_onboarding()
        elif state in ("queued", "provisioning", "verifying", "error"):
            # A worker restart may leave a durable setup state without a live
            # queue.job. Prepare is idempotent and reconciles the remote user.
            connection.action_wuzapi_start_onboarding()
        return self.step

    def _contact_center_onboarding_revalidate(self):
        self.ensure_one()
        if self.provider_key != "wuzapi":
            return super()._contact_center_onboarding_revalidate()
        connection = self.connection_id
        if not connection._wuzapi_active_onboarding_job():
            connection.action_wuzapi_check_onboarding()
        return self.step

    def _contact_center_onboarding_restart(self):
        self.ensure_one()
        if self.provider_key != "wuzapi":
            return super()._contact_center_onboarding_restart()
        self.connection_id.action_wuzapi_start_onboarding()
        return self.step

    def _contact_center_onboarding_prepare_repair(self):
        self.ensure_one()
        if self.provider_key != "wuzapi":
            return super()._contact_center_onboarding_prepare_repair()
        connection = self.connection_id
        if (
            connection.adapter_key != "wuzapi"
            or connection.wuzapi_onboarding_state != "error"
            or connection._wuzapi_active_onboarding_job()
            or connection.role not in ("migration", "standby")
            or connection.inbound_active
            or connection.outbound_active
        ):
            raise ValidationError(
                _("Only a failed staged WuzAPI setup can be repaired here.")
            )
        return True

    def _contact_center_onboarding_update_staged_connection(self, connection):
        self.ensure_one()
        if self.provider_key != "wuzapi":
            return super()._contact_center_onboarding_update_staged_connection(
                connection
            )
        if (
            connection != self.connection_id
            or connection.onboarding_ref != self.setup_ref
        ):
            raise ValidationError(_("The staged WuzAPI connection does not match."))
        expected_mode = "managed" if connection.wuzapi_managed_instance else "existing"
        if (
            self.wuzapi_server_id != connection.wuzapi_server_id
            or self.wuzapi_instance_mode != expected_mode
        ):
            raise ValidationError(
                _("The WuzAPI server and instance mode cannot change during repair.")
            )
        values = {}
        instance_name = (self.wuzapi_instance_name or "").strip()
        if instance_name != connection.wuzapi_instance_name:
            values["wuzapi_instance_name"] = instance_name
        token = (self.wuzapi_existing_token or "").strip()
        if token:
            if not _is_header_safe_token(token, _GUIDED_EXISTING_TOKEN_MIN_LENGTH):
                raise ValidationError(_("Enter a valid existing WuzAPI token."))
            values["wuzapi_api_token"] = token
        if values:
            connection.with_context(
                **{_WUZAPI_GUIDED_REPAIR_CONTEXT: _WUZAPI_GUIDED_REPAIR_TOKEN}
            ).write(values)
        self._contact_center_internal().write({"wuzapi_existing_token": False})
        return True
