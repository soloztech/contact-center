import datetime
import ipaddress
import math
import re
import secrets
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    ProviderRateLimitError,
    TransientAdapterError,
)
from odoo.addons.contact_center_base.services.job import provider_paused_retry_seconds
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import WUZAPI_COMMIT, WUZAPI_VERSION, WUZAPI_WEBHOOK_EVENT_TYPES

# Configuration and guided onboarding are separate service boundaries on purpose.
# pylint: disable=consider-merging-classes-inherited

_WEBHOOK_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,}$")
_DNS_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_WUZAPI_CONFIGURATION_FIELDS = (
    "wuzapi_base_url",
    "wuzapi_api_token",
    "wuzapi_hmac_secret",
    "wuzapi_webhook_key",
    "wuzapi_provider_version",
    "wuzapi_provider_commit",
    "wuzapi_webhook_event_ids",
    "wuzapi_hmac_pending_secret",
    "wuzapi_hmac_rotation_state",
    "wuzapi_hmac_rotation_revision",
    "wuzapi_hmac_rotation_job_uuid",
    "wuzapi_hmac_rotated_at",
    "wuzapi_hmac_rotation_last_error",
    "wuzapi_hmac_previous_secret",
    "wuzapi_hmac_previous_valid_until",
)
_WUZAPI_DISPATCH_CONFIGURATION_FIELDS = (
    "wuzapi_base_url",
    "wuzapi_api_token",
)
_WUZAPI_WEBHOOK_JOB_OPERATIONS = frozenset({"apply", "refresh"})
_WUZAPI_WEBHOOK_JOB_ATTEMPT_CEILING = 9
_WUZAPI_WEBHOOK_JOB_PRIORITY = 40
_WUZAPI_HMAC_JOB_ATTEMPT_CEILING = 9
_WUZAPI_HMAC_JOB_PRIORITY = 35
_WUZAPI_HMAC_INTERNAL_CONTEXT = "contact_center_wuzapi_hmac_internal"
_WUZAPI_HMAC_INTERNAL_TOKEN = object()
_WUZAPI_ONBOARDING_IDENTITY_CONTEXT = "contact_center_wuzapi_onboarding_identity"
_WUZAPI_ONBOARDING_IDENTITY_TOKEN = object()
_WUZAPI_HMAC_DRAIN_WINDOW = datetime.timedelta(minutes=5)
_WUZAPI_HMAC_MANAGED_FIELDS = frozenset(
    {
        "wuzapi_hmac_pending_secret",
        "wuzapi_hmac_rotation_state",
        "wuzapi_hmac_rotation_revision",
        "wuzapi_hmac_rotation_job_uuid",
        "wuzapi_hmac_rotated_at",
        "wuzapi_hmac_rotation_last_error",
        "wuzapi_hmac_previous_secret",
        "wuzapi_hmac_previous_valid_until",
    }
)
_CONNECTION_TOPOLOGY_FIELDS = frozenset(
    {"active", "role", "inbound_active", "outbound_active"}
)


def _is_header_safe_token(value, minimum_length=3):
    return bool(
        isinstance(value, str)
        and len(value) >= minimum_length
        and all(33 <= ord(character) <= 126 for character in value)
    )


class ContactCenterWuzapiWebhookEvent(models.Model):
    _name = "contact.center.wuzapi.webhook.event"
    _description = "WuzAPI Webhook Event"
    _order = "sequence, technical_name, id"

    name = fields.Char(required=True, translate=True)
    technical_name = fields.Char(required=True, index=True)
    category = fields.Selection(
        [
            ("communication", "Messages and Communication"),
            ("group_contact", "Groups and Contacts"),
            ("session", "Connection and Session"),
            ("privacy", "Privacy and Settings"),
            ("sync", "Synchronization and State"),
            ("call", "Calls"),
            ("presence", "Presence and Activity"),
            ("identity", "Identity"),
            ("error", "Errors"),
            ("newsletter", "WhatsApp Channels"),
            ("meta", "Facebook and Meta Bridge"),
            ("special", "Special"),
        ],
        required=True,
        index=True,
    )
    sequence = fields.Integer(default=100, required=True)
    adapter_supported = fields.Boolean(
        string="Normalized by Adapter",
        help=(
            "The pinned adapter normalizes this event into the provider-neutral "
            "EventDTO contract. This does not imply that the event creates a chat "
            "message. Other selected events remain auditable in the Inbox ledger "
            "as unsupported until their adapter mapping exists."
        ),
    )
    default_enabled = fields.Boolean(readonly=True)

    _sql_constraints = [
        (
            "technical_name_unique",
            "unique(technical_name)",
            "The WuzAPI webhook event name must be unique.",
        ),
    ]


class ContactCenterProviderConnection(models.Model):
    _inherit = "contact.center.provider.connection"

    def _default_wuzapi_webhook_event_ids(self):
        return self.env["contact.center.wuzapi.webhook.event"].search(
            [("default_enabled", "=", True)]
        )

    wuzapi_base_url = fields.Char(
        string="Service URL",
        help=(
            "WuzAPI service root URL. Credentials, query strings and fragments are "
            "not accepted in this field."
        ),
    )
    wuzapi_api_token = fields.Char(
        string="API Token",
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_hmac_secret = fields.Char(
        string="Webhook HMAC Secret",
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Set only while creating the connection. Use the Rotate HMAC action "
            "afterwards so WuzAPI and Odoo change keys as one fenced operation."
        ),
    )
    wuzapi_hmac_pending_secret = fields.Char(
        string="Pending Webhook HMAC Secret",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_hmac_rotation_state = fields.Selection(
        [
            ("stable", "Configured"),
            ("pending", "Rotation Queued"),
            ("error", "Rotation Failed"),
        ],
        string="HMAC Rotation",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_hmac_rotation_revision = fields.Integer(
        string="HMAC Rotation Revision",
        readonly=True,
        copy=False,
        default=0,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Monotonic revision that fences stale HMAC rotation jobs from "
            "promoting a provider response."
        ),
    )
    wuzapi_hmac_rotation_job_uuid = fields.Char(
        string="HMAC Rotation Job UUID",
        readonly=True,
        copy=False,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_hmac_rotated_at = fields.Datetime(
        string="HMAC Rotated At",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_hmac_rotation_last_error = fields.Char(
        string="HMAC Rotation Last Error",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_hmac_previous_secret = fields.Char(
        string="Previous Webhook HMAC Secret",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_hmac_previous_valid_until = fields.Datetime(
        string="Previous HMAC Valid Until",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Short, non-renewable drain window for callbacks signed before the "
            "coordinated key cutover."
        ),
    )
    wuzapi_webhook_key = fields.Char(
        string="Webhook Routing Key",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Opaque routing key reserved for the authenticated webhook endpoint. "
            "It is not a replacement for HMAC authentication."
        ),
    )
    wuzapi_webhook_url = fields.Char(
        string="Webhook URL",
        compute="_compute_wuzapi_webhook_url",
        groups="contact_center_base.group_contact_center_admin",
        help="Public callback URL to configure on this WuzAPI instance.",
    )
    wuzapi_provider_version = fields.Char(
        string="Validated Provider Version",
        readonly=True,
    )
    wuzapi_provider_commit = fields.Char(
        string="Validated Provider Commit",
        readonly=True,
    )
    wuzapi_webhook_event_ids = fields.Many2many(
        "contact.center.wuzapi.webhook.event",
        "contact_center_wuzapi_connection_event_rel",
        "connection_id",
        "event_id",
        string="Desired Webhook Events",
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Events Odoo will configure on this WuzAPI instance. Events not yet "
            "mapped by the adapter are retained only in the sanitized Inbox ledger."
        ),
    )
    wuzapi_webhook_observed_events_json = fields.Json(
        string="Observed Webhook Events",
        readonly=True,
        copy=False,
        default=list,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_webhook_url_matches = fields.Boolean(
        string="Webhook URL Matches",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_webhook_sync_state = fields.Selection(
        [
            ("unknown", "Not Checked"),
            ("pending", "Local Changes"),
            ("in_sync", "In Sync"),
            ("drift", "Different at WuzAPI"),
            ("error", "Check Failed"),
        ],
        string="Webhook Sync",
        readonly=True,
        copy=False,
        default="unknown",
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_webhook_last_sync_at = fields.Datetime(
        string="Webhook Checked At",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_webhook_last_error = fields.Char(
        string="Webhook Last Error",
        readonly=True,
        copy=False,
        groups="contact_center_base.group_contact_center_admin",
    )
    wuzapi_webhook_sync_revision = fields.Integer(
        string="Webhook Configuration Revision",
        readonly=True,
        copy=False,
        default=0,
        groups="contact_center_base.group_contact_center_admin",
        help=(
            "Monotonic revision used to discard a provider response when the "
            "desired webhook configuration changed while a job was in flight."
        ),
    )
    wuzapi_webhook_job_uuid = fields.Char(
        string="Webhook Sync Job UUID",
        readonly=True,
        copy=False,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )

    _sql_constraints = [
        (
            "wuzapi_webhook_key_uniq",
            "UNIQUE(wuzapi_webhook_key)",
            "The WuzAPI webhook routing key must be unique.",
        ),
        (
            "wuzapi_webhook_sync_revision_nonnegative",
            "CHECK(wuzapi_webhook_sync_revision >= 0)",
            "The WuzAPI webhook configuration revision cannot be negative.",
        ),
        (
            "wuzapi_hmac_rotation_revision_nonnegative",
            "CHECK(wuzapi_hmac_rotation_revision >= 0)",
            "The WuzAPI HMAC rotation revision cannot be negative.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        prepared_values = []
        for original_values in vals_list:
            if _WUZAPI_HMAC_MANAGED_FIELDS.intersection(original_values) and (
                self.env.context.get(_WUZAPI_HMAC_INTERNAL_CONTEXT)
                is not _WUZAPI_HMAC_INTERNAL_TOKEN
            ):
                raise AccessError(_("HMAC rotation state is managed internally."))
            values = dict(original_values)
            adapter_key = values.get(
                "adapter_key", self.env.context.get("default_adapter_key")
            )
            if adapter_key == "wuzapi":
                values.setdefault("wuzapi_webhook_key", secrets.token_urlsafe(32))
                values.setdefault("wuzapi_provider_version", WUZAPI_VERSION)
                values.setdefault("wuzapi_provider_commit", WUZAPI_COMMIT)
                values.setdefault("wuzapi_hmac_rotation_state", "stable")
                values.setdefault(
                    "wuzapi_webhook_event_ids",
                    [(6, 0, self._default_wuzapi_webhook_event_ids().ids)],
                )
                if "wuzapi_base_url" in values:
                    values["wuzapi_base_url"] = self._normalize_wuzapi_base_url(
                        values["wuzapi_base_url"]
                    )
            else:
                configured_fields = [
                    field_name
                    for field_name in _WUZAPI_CONFIGURATION_FIELDS
                    if values.get(field_name)
                ]
                if configured_fields:
                    raise ValidationError(
                        _(
                            "WuzAPI settings can only be stored on a WuzAPI provider "
                            "connection."
                        )
                    )
            prepared_values.append(values)
        connections = super().create(prepared_values)
        connections.filtered(
            lambda connection: connection.adapter_key == "wuzapi"
        ).refresh_capabilities()
        return connections

    def _wuzapi_lock_topology_for_write(self, values):
        """Take the core account-first lock before any provider-local row lock."""

        if not _CONNECTION_TOPOLOGY_FIELDS.intersection(values):
            return
        account_ids = self.mapped("account_id").ids
        self._contact_center_lock_topology(account_ids)
        # A transaction may have waited for the account topology lock. Discard
        # every cached route flag under the locked accounts before this addon or
        # the core role normalization makes a decision from those fields.
        locked_connections = (
            self.env["contact.center.provider.connection"]
            .sudo()
            .with_context(active_test=False)
            .search([("account_id", "in", account_ids)], order="account_id, id")
        )
        locked_connections.invalidate_recordset(
            ["account_id", "active", "role", "inbound_active", "outbound_active"]
        )

    def _wuzapi_lock_configuration_candidates(self, candidates):
        if not candidates:
            return
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            [candidates.ids],
        )
        candidates.invalidate_recordset(
            [
                "active",
                "role",
                "inbound_active",
                "outbound_active",
                "wuzapi_base_url",
                "wuzapi_api_token",
                "health_check_pending",
                "health_job_uuid",
                "health_configuration_revision",
                "wuzapi_webhook_event_ids",
                "wuzapi_webhook_sync_revision",
                "wuzapi_webhook_job_uuid",
                "wuzapi_hmac_secret",
                "wuzapi_hmac_pending_secret",
                "wuzapi_hmac_rotation_state",
                "wuzapi_hmac_rotation_revision",
                "wuzapi_hmac_rotation_job_uuid",
                "wuzapi_hmac_previous_secret",
                "wuzapi_hmac_previous_valid_until",
            ]
        )

    def _wuzapi_validate_hmac_write(self, values, candidates):
        """Keep an established secret behind the coordinated rotation action."""

        internal = (
            self.env.context.get(_WUZAPI_HMAC_INTERNAL_CONTEXT)
            is _WUZAPI_HMAC_INTERNAL_TOKEN
        )
        if _WUZAPI_HMAC_MANAGED_FIELDS.intersection(values) and not internal:
            raise AccessError(_("HMAC rotation state is managed internally."))
        if "wuzapi_hmac_secret" not in values or internal:
            return
        for connection in candidates:
            replacement = values.get("wuzapi_hmac_secret")
            if (
                connection.wuzapi_hmac_secret
                and len(connection.wuzapi_hmac_secret) >= 32
                and replacement != connection.wuzapi_hmac_secret
            ):
                raise ValidationError(
                    _(
                        "Use the Rotate HMAC action after the initial WuzAPI "
                        "secret has been configured."
                    )
                )

    def _wuzapi_hmac_internal(self):
        return self.with_context(
            **{_WUZAPI_HMAC_INTERNAL_CONTEXT: _WUZAPI_HMAC_INTERNAL_TOKEN}
        )

    def write(self, values):
        values = dict(values)
        self._wuzapi_lock_topology_for_write(values)
        if "wuzapi_base_url" in values:
            values["wuzapi_base_url"] = self._normalize_wuzapi_base_url(
                values["wuzapi_base_url"]
            )
        if any(connection.adapter_key != "wuzapi" for connection in self) and any(
            values.get(field_name) for field_name in _WUZAPI_CONFIGURATION_FIELDS
        ):
            raise ValidationError(
                _(
                    "WuzAPI settings can only be stored on a WuzAPI provider "
                    "connection."
                )
            )
        selection_candidates = (
            self.filtered(lambda connection: connection.adapter_key == "wuzapi")
            if "wuzapi_webhook_event_ids" in values
            else self.browse()
        )
        configuration_candidates = self.filtered(
            lambda connection: connection.adapter_key == "wuzapi"
            and any(
                field_name in values
                for field_name in _WUZAPI_DISPATCH_CONFIGURATION_FIELDS
            )
        )
        hmac_candidates = (
            self.filtered(lambda connection: connection.adapter_key == "wuzapi")
            if "wuzapi_hmac_secret" in values
            or _WUZAPI_HMAC_MANAGED_FIELDS.intersection(values)
            else self.browse()
        )
        lock_candidates = (
            selection_candidates | configuration_candidates | hmac_candidates
        )
        self._wuzapi_lock_configuration_candidates(lock_candidates)
        self._wuzapi_validate_hmac_write(values, hmac_candidates)
        dispatch_configuration_changed = configuration_candidates.filtered(
            lambda connection: any(
                field_name in values and values[field_name] != connection[field_name]
                for field_name in _WUZAPI_DISPATCH_CONFIGURATION_FIELDS
            )
        )
        webhook_configuration_changed = (
            selection_candidates | dispatch_configuration_changed
        )
        if webhook_configuration_changed:
            values.setdefault("wuzapi_webhook_sync_state", "pending")
            values.setdefault("wuzapi_webhook_last_error", False)
            values.setdefault("wuzapi_webhook_job_uuid", False)
            values.setdefault(
                "wuzapi_webhook_sync_revision",
                max(
                    webhook_configuration_changed.mapped("wuzapi_webhook_sync_revision")
                    or [0]
                )
                + 1,
            )
        if dispatch_configuration_changed:
            if dispatch_configuration_changed.filtered(
                lambda connection: connection.wuzapi_hmac_rotation_job_uuid
            ):
                raise ValidationError(
                    _(
                        "Wait for the WuzAPI HMAC rotation before changing its "
                        "service URL or API token."
                    )
                )
            for connection in dispatch_configuration_changed:
                if connection.active and connection.outbound_active:
                    raise ValidationError(
                        _(
                            "Disable outbound dispatch before changing the WuzAPI "
                            "service URL or API token."
                        )
                    )
            if (
                self.env["contact.center.outbox.command"]
                .sudo()
                .search_count(
                    [
                        (
                            "provider_connection_id",
                            "in",
                            dispatch_configuration_changed.ids,
                        ),
                        ("state", "=", "processing"),
                    ]
                )
            ):
                raise ValidationError(
                    _(
                        "Wait for in-flight WuzAPI commands before changing its "
                        "service URL or API token."
                    )
                )
            values.update(
                {
                    "state": "degraded",
                    "health_detail": "identity_unverified",
                    "identity_mismatch_latched": True,
                    "last_state_change_at": fields.Datetime.now(),
                    "health_retry_not_before": False,
                    "next_health_check_at": fields.Datetime.now(),
                    "health_configuration_revision": max(
                        dispatch_configuration_changed.mapped(
                            "health_configuration_revision"
                        )
                        or [0]
                    )
                    + 1,
                }
            )
        result = super().write(values)
        if dispatch_configuration_changed:
            dispatch_configuration_changed._notify_health_updated()
            dispatch_configuration_changed._enqueue_health_check(priority=30)
        return result

    @api.depends("wuzapi_webhook_key")
    def _compute_wuzapi_webhook_url(self):
        base_url = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("web.base.url", "")
            .rstrip("/")
        )
        for connection in self:
            connection.wuzapi_webhook_url = (
                "%s/contact-center/webhook/wuzapi/%s"
                % (base_url, connection.wuzapi_webhook_key)
                if base_url and connection.wuzapi_webhook_key
                else False
            )

    def _require_wuzapi_webhook_admin(self):
        self.ensure_one()
        if not self.env.is_superuser() and not self.env.user.has_group(
            "contact_center_base.group_contact_center_admin"
        ):
            raise AccessError(
                _("Only Contact Center administrators can configure webhooks.")
            )
        self.check_access_rights("write")
        self.check_access_rule("write")
        if self.adapter_key != "wuzapi":
            raise ValidationError(
                _("Webhook event synchronization requires a WuzAPI connection.")
            )
        return True

    def _wuzapi_desired_webhook_events(self):
        self.ensure_one()
        return tuple(sorted(self.wuzapi_webhook_event_ids.mapped("technical_name")))

    def _wuzapi_webhook_current_job(
        self, expected_revision, expected_job_uuid, *, lock=False
    ):
        """Return the current fenced record, optionally taking a short row lock."""

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
                "adapter_key",
                "company_id",
                "wuzapi_webhook_event_ids",
                "wuzapi_webhook_job_uuid",
                "wuzapi_webhook_sync_revision",
                "wuzapi_webhook_url",
            ]
        )
        if (
            connection.adapter_key != "wuzapi"
            or connection.wuzapi_webhook_sync_revision != expected_revision
            or connection.wuzapi_webhook_job_uuid != expected_job_uuid
        ):
            return self.browse()
        return connection.with_company(connection.company_id)

    def _enqueue_wuzapi_webhook_sync(self, operation):
        """Persist one revision and enqueue provider I/O after the action commits."""

        self.ensure_one()
        if operation not in _WUZAPI_WEBHOOK_JOB_OPERATIONS:
            raise ValidationError(_("The WuzAPI webhook operation is invalid."))
        connection = self.sudo().with_company(self.company_id)
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE id = %s FOR UPDATE",
            [connection.id],
        )
        connection.invalidate_recordset(
            ["wuzapi_webhook_sync_revision", "wuzapi_webhook_job_uuid"]
        )
        revision = connection.wuzapi_webhook_sync_revision + 1
        connection.write(
            {
                "wuzapi_webhook_sync_revision": revision,
                "wuzapi_webhook_job_uuid": False,
                "wuzapi_webhook_sync_state": "pending",
                "wuzapi_webhook_last_error": False,
            }
        )
        delayed = connection.with_delay(
            identity_key="contact_center:wuzapi_webhook:%s:%s"
            % (connection.id, revision),
            max_retries=0,
            priority=_WUZAPI_WEBHOOK_JOB_PRIORITY,
            description="Contact Center WuzAPI webhook %s %s"
            % (operation, connection.id),
        )._job_wuzapi_sync_webhook(revision, operation)
        connection.write({"wuzapi_webhook_job_uuid": delayed.uuid})
        return revision, delayed.uuid

    def _wuzapi_webhook_scheduled_action(self, operation):
        self._enqueue_wuzapi_webhook_sync(operation)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("WuzAPI Webhook"),
                "message": (
                    _("Webhook verification was queued.")
                    if operation == "refresh"
                    else _("Webhook update and verification were queued.")
                ),
                "type": "info",
                "sticky": False,
            },
        }

    def action_wuzapi_refresh_webhook(self):
        """Queue a read-back without calling WuzAPI in the HTTP request."""

        self._require_wuzapi_webhook_admin()
        return self._wuzapi_webhook_scheduled_action("refresh")

    def action_wuzapi_apply_webhook(self):
        """Queue replacement and read-back without provider I/O in the action."""

        self._require_wuzapi_webhook_admin()
        return self._wuzapi_webhook_scheduled_action("apply")

    def _wuzapi_hmac_current_job(
        self, expected_revision, expected_job_uuid, *, lock=False
    ):
        """Return only the HMAC rotation still owning this connection revision."""

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
                "company_id",
                "role",
                "wuzapi_api_token",
                "wuzapi_base_url",
                "wuzapi_hmac_secret",
                "wuzapi_hmac_pending_secret",
                "wuzapi_hmac_rotation_job_uuid",
                "wuzapi_hmac_rotation_revision",
                "wuzapi_hmac_rotation_state",
                "wuzapi_hmac_previous_secret",
                "wuzapi_hmac_previous_valid_until",
            ]
        )
        if (
            connection.adapter_key != "wuzapi"
            or connection.wuzapi_hmac_rotation_revision != expected_revision
            or connection.wuzapi_hmac_rotation_job_uuid != expected_job_uuid
            or connection.wuzapi_hmac_rotation_state != "pending"
            or not connection.wuzapi_hmac_pending_secret
        ):
            return self.browse()
        return connection.with_company(connection.company_id)

    def _enqueue_wuzapi_hmac_rotation(self):
        """Stage a private key and enqueue only its non-secret fencing revision."""

        self.ensure_one()
        connection = self.sudo().with_company(self.company_id)
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE id = %s FOR UPDATE",
            [connection.id],
        )
        connection.invalidate_recordset(
            [
                "active",
                "adapter_key",
                "role",
                "wuzapi_hmac_pending_secret",
                "wuzapi_hmac_rotation_job_uuid",
                "wuzapi_hmac_rotation_revision",
                "wuzapi_hmac_rotation_state",
                "wuzapi_hmac_previous_secret",
                "wuzapi_hmac_previous_valid_until",
            ]
        )
        if connection.adapter_key != "wuzapi":
            raise ValidationError(_("HMAC rotation requires a WuzAPI connection."))
        if not connection.active or connection.role == "historical":
            raise ValidationError(
                _("Activate the WuzAPI connection before rotating its HMAC key.")
            )
        if connection.wuzapi_hmac_rotation_job_uuid:
            raise ValidationError(_("A WuzAPI HMAC rotation is already queued."))
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        previous_valid_until = fields.Datetime.to_datetime(
            connection.wuzapi_hmac_previous_valid_until
        )
        if (
            connection.wuzapi_hmac_previous_secret
            and previous_valid_until
            and previous_valid_until > now
        ):
            raise ValidationError(
                _(
                    "Wait for the previous WuzAPI HMAC callback drain window "
                    "before rotating again."
                )
            )
        pending_secret = connection.wuzapi_hmac_pending_secret
        if not pending_secret:
            pending_secret = secrets.token_urlsafe(48)
        revision = connection.wuzapi_hmac_rotation_revision + 1
        connection._wuzapi_hmac_internal().write(
            {
                "wuzapi_hmac_pending_secret": pending_secret,
                "wuzapi_hmac_rotation_state": "pending",
                "wuzapi_hmac_rotation_revision": revision,
                "wuzapi_hmac_rotation_job_uuid": False,
                "wuzapi_hmac_rotation_last_error": False,
                "wuzapi_hmac_previous_secret": False,
                "wuzapi_hmac_previous_valid_until": False,
            }
        )
        delayed = connection.with_delay(
            identity_key="contact_center:wuzapi_hmac:%s:%s" % (connection.id, revision),
            max_retries=0,
            priority=_WUZAPI_HMAC_JOB_PRIORITY,
            description="Contact Center WuzAPI HMAC rotation %s" % connection.id,
        )._job_wuzapi_rotate_hmac(revision)
        connection._wuzapi_hmac_internal().write(
            {"wuzapi_hmac_rotation_job_uuid": delayed.uuid}
        )
        return revision, delayed.uuid

    def action_wuzapi_rotate_hmac(self):
        """Queue a generated HMAC key without exposing it to the browser or job."""

        self._require_wuzapi_webhook_admin()
        self._enqueue_wuzapi_hmac_rotation()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("WuzAPI HMAC"),
                "message": _("A coordinated HMAC rotation was queued."),
                "type": "info",
                "sticky": False,
            },
        }

    def _wuzapi_hmac_job_attempt(self, job_uuid):
        self.ensure_one()
        job = self.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
        return (job.retry if job else 0) + 1

    def _finish_wuzapi_hmac_rotation(self, expected_revision, expected_job_uuid):
        connection = self._wuzapi_hmac_current_job(
            expected_revision, expected_job_uuid, lock=True
        )
        if not connection:
            return False
        pending_secret = connection.wuzapi_hmac_pending_secret
        previous_secret = connection.wuzapi_hmac_secret
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        connection._wuzapi_hmac_internal().write(
            {
                "wuzapi_hmac_secret": pending_secret,
                "wuzapi_hmac_pending_secret": False,
                "wuzapi_hmac_rotation_state": "stable",
                "wuzapi_hmac_rotation_job_uuid": False,
                "wuzapi_hmac_rotated_at": now,
                "wuzapi_hmac_rotation_last_error": False,
                "wuzapi_hmac_previous_secret": previous_secret,
                "wuzapi_hmac_previous_valid_until": (now + _WUZAPI_HMAC_DRAIN_WINDOW),
            }
        )
        connection.with_delay(
            eta=now + _WUZAPI_HMAC_DRAIN_WINDOW,
            identity_key="contact_center:wuzapi_hmac_cleanup:%s:%s"
            % (connection.id, expected_revision),
            max_retries=0,
            priority=_WUZAPI_HMAC_JOB_PRIORITY,
            description="Contact Center WuzAPI HMAC drain cleanup %s" % connection.id,
        )._job_wuzapi_expire_hmac_previous(expected_revision)
        return True

    def _job_wuzapi_expire_hmac_previous(self, expected_revision):
        """Erase the retired key after its fixed callback drain window."""

        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValidationError(
                _("The WuzAPI HMAC cleanup job arguments are invalid.")
            )
        if not self.env.context.get("job_uuid"):
            raise ValidationError(_("WuzAPI HMAC cleanup must run through queue_job."))
        connection = self.sudo().exists()
        if not connection:
            return False
        connection.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM contact_center_provider_connection "
            "WHERE id = %s FOR UPDATE",
            [connection.id],
        )
        if not self.env.cr.fetchone():
            return False
        connection.invalidate_recordset(
            [
                "wuzapi_hmac_rotation_revision",
                "wuzapi_hmac_previous_secret",
                "wuzapi_hmac_previous_valid_until",
            ]
        )
        if (
            connection.wuzapi_hmac_rotation_revision != expected_revision
            or not connection.wuzapi_hmac_previous_secret
        ):
            return False
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        valid_until = fields.Datetime.to_datetime(
            connection.wuzapi_hmac_previous_valid_until
        )
        if valid_until and valid_until > now:
            raise RetryableJobError(
                "WuzAPI HMAC drain window has not elapsed",
                seconds=max(1, math.ceil((valid_until - now).total_seconds())),
                ignore_retry=True,
            )
        connection._wuzapi_hmac_internal().write(
            {
                "wuzapi_hmac_previous_secret": False,
                "wuzapi_hmac_previous_valid_until": False,
            }
        )
        return True

    def _finish_wuzapi_hmac_rotation_error(
        self, error, expected_revision, expected_job_uuid
    ):
        connection = self._wuzapi_hmac_current_job(
            expected_revision, expected_job_uuid, lock=True
        )
        if not connection:
            return False
        if isinstance(error, TransientAdapterError):
            safe_message = _(
                "WuzAPI remained unavailable after the HMAC rotation retry limit."
            )
        elif isinstance(error, AdapterError):
            safe_message = _("WuzAPI rejected the HMAC rotation request.")
        else:
            safe_message = _("HMAC rotation failed after repeated attempts.")
        connection._wuzapi_hmac_internal().write(
            {
                "wuzapi_hmac_rotation_state": "error",
                "wuzapi_hmac_rotation_job_uuid": False,
                "wuzapi_hmac_rotation_last_error": safe_message,
            }
        )
        return False

    def _handle_wuzapi_hmac_job_error(
        self, error, expected_revision, expected_job_uuid
    ):
        if not self._wuzapi_hmac_current_job(expected_revision, expected_job_uuid):
            return False
        if isinstance(error, ProviderRateLimitError):
            raise RetryableJobError(
                "WuzAPI HMAC rotation was rate-limited",
                seconds=provider_paused_retry_seconds(
                    error,
                    ("wuzapi_hmac", self.id),
                ),
                ignore_retry=True,
            ) from error
        retryable = isinstance(error, TransientAdapterError) or not isinstance(
            error, (AdapterError, ValidationError)
        )
        if (
            retryable
            and self._wuzapi_hmac_job_attempt(expected_job_uuid)
            < _WUZAPI_HMAC_JOB_ATTEMPT_CEILING
        ):
            raise RetryableJobError(
                "WuzAPI HMAC rotation failed temporarily", seconds=None
            ) from error
        return self._finish_wuzapi_hmac_rotation_error(
            error, expected_revision, expected_job_uuid
        )

    def _job_wuzapi_rotate_hmac(self, expected_revision):
        """Rotate remotely and promote the staged key only while the fence owns it."""

        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValidationError(_("The WuzAPI HMAC job arguments are invalid."))
        job_uuid = str(self.env.context.get("job_uuid") or "")
        if not job_uuid:
            raise ValidationError(_("WuzAPI HMAC rotation must run through queue_job."))
        connection = self._wuzapi_hmac_current_job(expected_revision, job_uuid)
        if not connection:
            return False
        try:
            connection.get_adapter().set_hmac_configuration(
                connection,
                connection.wuzapi_hmac_pending_secret,
            )
        except Exception as error:  # provider isolation boundary
            return connection._handle_wuzapi_hmac_job_error(
                error, expected_revision, job_uuid
            )
        return connection._finish_wuzapi_hmac_rotation(expected_revision, job_uuid)

    def _wuzapi_webhook_job_attempt(self, job_uuid):
        self.ensure_one()
        job = self.env["queue.job"].sudo().search([("uuid", "=", job_uuid)], limit=1)
        return (job.retry if job else 0) + 1

    def _finish_wuzapi_webhook_observation(
        self, observation, expected_revision, expected_job_uuid
    ):
        connection = self._wuzapi_webhook_current_job(
            expected_revision, expected_job_uuid, lock=True
        )
        if not connection:
            return False
        desired_events = connection._wuzapi_desired_webhook_events()
        observed_events = tuple(observation["events"])
        url_matches = observation["webhook_url"] == (
            connection.wuzapi_webhook_url or ""
        )
        in_sync = url_matches and observed_events == desired_events
        connection.write(
            {
                "wuzapi_webhook_observed_events_json": list(observed_events),
                "wuzapi_webhook_url_matches": url_matches,
                "wuzapi_webhook_sync_state": "in_sync" if in_sync else "drift",
                "wuzapi_webhook_last_sync_at": fields.Datetime.now(),
                "wuzapi_webhook_last_error": False,
                "wuzapi_webhook_job_uuid": False,
            }
        )
        return in_sync

    def _finish_wuzapi_webhook_error(self, error, expected_revision, expected_job_uuid):
        connection = self._wuzapi_webhook_current_job(
            expected_revision, expected_job_uuid, lock=True
        )
        if not connection:
            return False
        if isinstance(error, TransientAdapterError):
            safe_message = _(
                "WuzAPI remained unavailable after the webhook retry limit."
            )
        elif isinstance(error, AdapterError):
            safe_message = _("WuzAPI rejected or returned an invalid webhook state.")
        else:
            safe_message = _("Webhook synchronization failed after repeated attempts.")
        connection.write(
            {
                "wuzapi_webhook_sync_state": "error",
                "wuzapi_webhook_last_sync_at": fields.Datetime.now(),
                "wuzapi_webhook_last_error": safe_message,
                "wuzapi_webhook_job_uuid": False,
            }
        )
        return False

    def _handle_wuzapi_webhook_job_error(
        self, error, expected_revision, expected_job_uuid
    ):
        if not self._wuzapi_webhook_current_job(expected_revision, expected_job_uuid):
            return False
        if isinstance(error, ProviderRateLimitError):
            raise RetryableJobError(
                "WuzAPI webhook synchronization was rate-limited",
                seconds=provider_paused_retry_seconds(
                    error,
                    ("wuzapi_webhook", self.id),
                ),
                ignore_retry=True,
            ) from error
        retryable = isinstance(error, TransientAdapterError) or not isinstance(
            error, (AdapterError, ValidationError)
        )
        if (
            retryable
            and self._wuzapi_webhook_job_attempt(expected_job_uuid)
            < _WUZAPI_WEBHOOK_JOB_ATTEMPT_CEILING
        ):
            raise RetryableJobError(
                "WuzAPI webhook synchronization failed temporarily", seconds=None
            ) from error
        return self._finish_wuzapi_webhook_error(
            error, expected_revision, expected_job_uuid
        )

    def _job_wuzapi_sync_webhook(self, expected_revision, operation):
        """Perform provider I/O, then persist only if this job is still current."""

        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
            or operation not in _WUZAPI_WEBHOOK_JOB_OPERATIONS
        ):
            raise ValidationError(_("The WuzAPI webhook job arguments are invalid."))
        job_uuid = str(self.env.context.get("job_uuid") or "")
        if not job_uuid:
            raise ValidationError(
                _("WuzAPI webhook synchronization must run through queue_job.")
            )
        connection = self._wuzapi_webhook_current_job(expected_revision, job_uuid)
        if not connection:
            return False
        try:
            adapter = connection.get_adapter()
            observation = (
                adapter.get_webhook_configuration(connection)
                if operation == "refresh"
                else adapter.set_webhook_configuration(
                    connection,
                    connection.wuzapi_webhook_url,
                    connection._wuzapi_desired_webhook_events(),
                )
            )
        except Exception as error:  # provider isolation boundary
            return connection._handle_wuzapi_webhook_job_error(
                error, expected_revision, job_uuid
            )
        return connection._finish_wuzapi_webhook_observation(
            observation, expected_revision, job_uuid
        )

    @api.constrains(
        "adapter_key",
        "account_id",
        "wuzapi_base_url",
        "wuzapi_api_token",
        "wuzapi_hmac_secret",
        "wuzapi_hmac_pending_secret",
        "wuzapi_hmac_rotation_state",
        "wuzapi_hmac_rotation_revision",
        "wuzapi_hmac_rotation_job_uuid",
        "wuzapi_hmac_rotated_at",
        "wuzapi_hmac_rotation_last_error",
        "wuzapi_hmac_previous_secret",
        "wuzapi_hmac_previous_valid_until",
        "wuzapi_webhook_key",
        "wuzapi_provider_version",
        "wuzapi_provider_commit",
        "wuzapi_webhook_event_ids",
    )
    def _check_wuzapi_configuration(self):
        for connection in self:
            if connection.adapter_key != "wuzapi":
                if any(
                    connection[field_name]
                    for field_name in _WUZAPI_CONFIGURATION_FIELDS
                ):
                    raise ValidationError(
                        _(
                            "WuzAPI settings can only be stored on a WuzAPI provider "
                            "connection."
                        )
                    )
                continue
            if connection.account_id.platform != "whatsapp":
                raise ValidationError(
                    _("A WuzAPI connection requires a WhatsApp account.")
                )
            if not connection.wuzapi_base_url:
                raise ValidationError(_("The WuzAPI service URL cannot be empty."))
            if not _is_header_safe_token(connection.wuzapi_api_token):
                raise ValidationError(
                    _("The WuzAPI API token is not safe for an HTTP header.")
                )
            if (
                not connection.wuzapi_hmac_secret
                or not connection.wuzapi_hmac_secret.strip()
            ):
                raise ValidationError(_("The WuzAPI HMAC secret cannot be empty."))
            if len(connection.wuzapi_hmac_secret) < 32:
                raise ValidationError(
                    _("The WuzAPI HMAC secret must contain at least 32 characters.")
                )
            if not _WEBHOOK_KEY_PATTERN.fullmatch(connection.wuzapi_webhook_key or ""):
                raise ValidationError(
                    _(
                        "The webhook routing key must contain at least 32 URL-safe "
                        "characters."
                    )
                )
            if (
                connection.wuzapi_provider_version != WUZAPI_VERSION
                or connection.wuzapi_provider_commit != WUZAPI_COMMIT
            ):
                raise ValidationError(
                    _(
                        "This adapter is validated only against WuzAPI %(version)s "
                        "(%(commit)s).",
                        version=WUZAPI_VERSION,
                        commit=WUZAPI_COMMIT,
                    )
                )
            event_names = set(
                connection.wuzapi_webhook_event_ids.mapped("technical_name")
            )
            if not event_names:
                raise ValidationError(_("Select at least one WuzAPI webhook event."))
            if not event_names.issubset(WUZAPI_WEBHOOK_EVENT_TYPES):
                raise ValidationError(
                    _("The WuzAPI webhook event selection is invalid.")
                )
            if "All" in event_names and len(event_names) != 1:
                raise ValidationError(_("The WuzAPI All event must be selected alone."))

    @api.model
    def _normalize_wuzapi_base_url(self, value):
        if not isinstance(value, str):
            raise ValidationError(_("The WuzAPI service URL must be text."))
        normalized = (value or "").strip().rstrip("/")
        if any(
            ord(character) < 32 or ord(character) == 127 for character in normalized
        ):
            raise ValidationError(
                _("The WuzAPI service URL contains control characters.")
            )
        try:
            parsed = urlsplit(normalized)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise ValidationError(_("The WuzAPI service URL is invalid.")) from error
        if (
            parsed.scheme not in ("http", "https")
            or not hostname
            or "\\" in normalized
            or "@" in parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or port == 0
        ):
            raise ValidationError(
                _(
                    "Use an HTTP(S) WuzAPI service URL without credentials, query "
                    "parameters, fragments or paths."
                )
            )
        try:
            ip_value = ipaddress.ip_address(hostname)
            canonical_hostname = ip_value.compressed
            if ip_value.version == 6:
                canonical_hostname = "[%s]" % canonical_hostname
        except ValueError:
            try:
                ascii_hostname = hostname.encode("idna").decode("ascii")
            except UnicodeError as error:
                raise ValidationError(
                    _("The WuzAPI service URL hostname is invalid.")
                ) from error
            labels = ascii_hostname.rstrip(".").split(".")
            if len(ascii_hostname) > 253 or not all(
                _DNS_LABEL_PATTERN.fullmatch(label) for label in labels
            ):
                raise ValidationError(
                    _("The WuzAPI service URL hostname is invalid.")
                ) from None
            canonical_hostname = ascii_hostname.rstrip(".").lower()
        scheme = parsed.scheme.lower()
        canonical_port = (
            port
            if port
            and not (scheme == "http" and port == 80)
            and not (scheme == "https" and port == 443)
            else None
        )
        canonical_netloc = canonical_hostname
        if canonical_port:
            canonical_netloc = "%s:%s" % (canonical_netloc, canonical_port)
        return "%s://%s" % (scheme, canonical_netloc)


class ContactCenterAccount(models.Model):
    _inherit = "contact.center.account"

    def write(self, values):
        onboarding_identity_write = (
            self.env.context.get(_WUZAPI_ONBOARDING_IDENTITY_CONTEXT)
            is _WUZAPI_ONBOARDING_IDENTITY_TOKEN
        )
        if onboarding_identity_write:
            if set(values) != {"own_external_identity"}:
                raise AccessError(_("Invalid guided WuzAPI identity write."))
            return super().write(values)
        connections = self.env["contact.center.provider.connection"]
        if "own_external_identity" in values:
            self.env.cr.execute(
                "SELECT id FROM contact_center_account WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [self.ids],
            )
            self.invalidate_recordset(["own_external_identity"])
        identity_changed = "own_external_identity" in values and any(
            values["own_external_identity"] != account.own_external_identity
            for account in self
        )
        if identity_changed:
            connections = (
                self.env["contact.center.provider.connection"]
                .sudo()
                .with_context(active_test=False)
                .search(
                    [
                        ("account_id", "in", self.ids),
                        ("adapter_key", "=", "wuzapi"),
                    ],
                    order="id",
                )
            )
            if connections:
                self.env.cr.execute(
                    "SELECT id FROM contact_center_provider_connection "
                    "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                    [connections.ids],
                )
                connections.invalidate_recordset(
                    [
                        "active",
                        "outbound_active",
                        "health_check_pending",
                        "health_job_uuid",
                        "health_configuration_revision",
                    ]
                )
            for connection in connections:
                if connection.active and connection.outbound_active:
                    raise ValidationError(
                        _(
                            "Disable outbound dispatch before changing the WhatsApp "
                            "identity expected by WuzAPI."
                        )
                    )
            if connections and self.env[
                "contact.center.outbox.command"
            ].sudo().search_count(
                [
                    ("provider_connection_id", "in", connections.ids),
                    ("state", "=", "processing"),
                ]
            ):
                raise ValidationError(
                    _(
                        "Wait for in-flight WuzAPI commands before changing the "
                        "expected WhatsApp identity."
                    )
                )
        result = super().write(values)
        if identity_changed and connections:
            connections.write(
                {
                    "state": "degraded",
                    "health_detail": "identity_unverified",
                    "identity_mismatch_latched": True,
                    "last_state_change_at": fields.Datetime.now(),
                    "health_retry_not_before": False,
                    "next_health_check_at": fields.Datetime.now(),
                    "health_configuration_revision": max(
                        connections.mapped("health_configuration_revision") or [0]
                    )
                    + 1,
                }
            )
            connections._notify_health_updated()
            connections._enqueue_health_check(priority=30)
        return result

    def _wuzapi_bind_guided_identity(self, connection, observed_identity):
        """Bind the first proved session identity without spawning a stale probe."""

        self.ensure_one()
        connection.ensure_one()
        if (
            connection.account_id != self
            or connection.adapter_key != "wuzapi"
            or not connection.onboarding_ref
            or connection.role not in ("migration", "standby")
            or connection.inbound_active
            or connection.outbound_active
            or self.own_external_identity
        ):
            raise ValidationError(_("The guided WuzAPI identity cannot be bound."))
        normalized = connection.get_adapter()._provider_normalize_session_identity(
            observed_identity
        )
        if not normalized:
            raise ValidationError(_("WuzAPI did not prove a valid session identity."))
        return self.with_context(
            **{_WUZAPI_ONBOARDING_IDENTITY_CONTEXT: (_WUZAPI_ONBOARDING_IDENTITY_TOKEN)}
        ).write({"own_external_identity": observed_identity})
