import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.meta_api_base.services.signature import validate_graph_version

from ..services.adapter import HEALTH_OBSERVATION_CONTEXT_KEY
from ..services.contracts import (
    DEFAULT_GRAPH_VERSION,
    WHATSAPP_ACCOUNT_PLATFORM,
    WHATSAPP_ASSET_CONTRACT,
    WHATSAPP_CLOUD_ADAPTER_KEY,
    WHATSAPP_CLOUD_CONSUMER_KEY,
    WHATSAPP_CLOUD_PROVIDER_SCHEMA_VERSION,
    WHATSAPP_OBJECT_TYPE,
    WHATSAPP_OWNER_KIND,
    WHATSAPP_WEBHOOK_FIELD,
)

_ADMIN = "contact_center_base.group_contact_center_admin"
_PHONE_NUMBER_ID_RE = re.compile(r"^[0-9]{1,40}$")
_CONFIGURATION_FIELDS = {
    "wa_webhook_asset_id",
    "wa_phone_number_id",
    "wa_graph_version",
}
_IMMUTABLE_ROUTE_FIELDS = ("wa_webhook_asset_id", "wa_phone_number_id")
_OBSERVED_FIELDS = {
    "display_phone_number": "wa_display_phone_number",
    "verified_name": "wa_verified_name",
    "quality_rating": "wa_quality_rating",
    "status": "wa_phone_status",
    "throughput_level": "wa_throughput_level",
}
_ROUTE_LIFECYCLE_FIELDS = {
    "active",
    "adapter_key",
    "inbound_active",
    "role",
    "wa_webhook_asset_id",
}
_DEFERRED_SCOPES_CONTEXT = "contact_center_whatsapp_cloud_deferred_subscription_scopes"


class _DeferredSubscriptionScopes:
    """Owner scopes collected while a controlled primary switch runs.

    Only this module can build one, so an RPC context can never defer the
    subscription lifecycle. Identity equality keeps two collectors apart in
    Odoo's environment cache even while both are empty.
    """

    __slots__ = ("page_ids",)

    def __init__(self):
        self.page_ids = set()


class ContactCenterWhatsAppCloudConnection(models.Model):
    _inherit = "contact.center.provider.connection"

    wa_webhook_asset_id = fields.Many2one(
        "meta.webhook.asset",
        string="WhatsApp Business Account Asset",
        index=True,
        ondelete="restrict",
        check_company=True,
        groups="base.group_system",
        help=(
            "Canonical WhatsApp Business Account asset. App, credentials, callback "
            "and technical ledger belong to the shared Meta core."
        ),
    )
    wa_business_account_id = fields.Char(
        related="wa_webhook_asset_id.external_asset_id",
        string="WhatsApp Business Account ID",
        store=True,
        readonly=True,
        index=True,
        groups=_ADMIN,
    )
    wa_webhook_page_id = fields.Many2one(
        related="wa_webhook_asset_id.page_id",
        string="WhatsApp Business Account Owner",
        store=True,
        readonly=True,
        index=True,
        groups=_ADMIN,
    )
    wa_api_app_id = fields.Many2one(
        related="wa_webhook_asset_id.page_id.app_id",
        string="Meta App",
        store=True,
        readonly=True,
        index=True,
        groups=_ADMIN,
    )
    wa_phone_number_id = fields.Char(
        string="Phone Number ID",
        size=40,
        index=True,
        copy=False,
        groups=_ADMIN,
        help="Cloud API phone number ID; unique among active connections.",
    )
    wa_graph_version = fields.Char(
        string="Graph API Version",
        size=16,
        default=DEFAULT_GRAPH_VERSION,
        groups=_ADMIN,
    )
    wa_display_phone_number = fields.Char(
        string="Display Phone Number",
        readonly=True,
        copy=False,
        groups=_ADMIN,
        help="Observed by the last successful phone number read.",
    )
    wa_verified_name = fields.Char(
        string="Verified Name", readonly=True, copy=False, groups=_ADMIN
    )
    wa_quality_rating = fields.Char(
        string="Quality Rating", readonly=True, copy=False, groups=_ADMIN
    )
    wa_phone_status = fields.Char(
        string="Phone Number Status", readonly=True, copy=False, groups=_ADMIN
    )
    wa_throughput_level = fields.Char(
        string="Throughput Level", readonly=True, copy=False, groups=_ADMIN
    )
    wa_observed_at = fields.Datetime(
        string="Phone Number Observed At", readonly=True, copy=False, groups=_ADMIN
    )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for incoming in vals_list:
            values = dict(incoming)
            adapter_key = values.get(
                "adapter_key", self.env.context.get("default_adapter_key")
            )
            if adapter_key == WHATSAPP_CLOUD_ADAPTER_KEY:
                values.setdefault(
                    "provider_schema_version", WHATSAPP_CLOUD_PROVIDER_SCHEMA_VERSION
                )
                values.setdefault("wa_graph_version", DEFAULT_GRAPH_VERSION)
                if values.get("wa_phone_number_id"):
                    values["wa_phone_number_id"] = str(
                        values["wa_phone_number_id"]
                    ).strip()
                if not values.get("wa_webhook_asset_id") or not values.get(
                    "wa_phone_number_id"
                ):
                    raise ValidationError(
                        _(
                            "A WhatsApp Cloud connection requires a business account "
                            "asset and a phone number ID."
                        )
                    )
            elif any(
                values.get(name)
                for name in _CONFIGURATION_FIELDS - {"wa_graph_version"}
            ):
                raise ValidationError(
                    _(
                        "WhatsApp Cloud settings can only be stored on a WhatsApp "
                        "Cloud connection."
                    )
                )
            else:
                values["wa_graph_version"] = False
            prepared.append(values)
        connections = super().create(prepared)
        cloud = connections.filtered(
            lambda connection: connection.adapter_key == WHATSAPP_CLOUD_ADAPTER_KEY
        ).sudo()
        cloud.refresh_capabilities()
        cloud._wac_fence_new_live_routes()
        return connections

    def _wac_fence_new_live_routes(self):
        """Take part in the lifecycle fence when a route is created live.

        The core create locks only the new connection's inbox. A retirement in
        another inbox of the same business account may already hold a snapshot
        in which its route is the last one, and would archive the subscription
        despite this new route (CC-WAC-14). Versioning the shared subscription
        rows here makes that decision fail and retry on a new snapshot, where
        this route is live. It never reactivates a subscription: an inactive
        one is left for an administrator to register again.
        """

        live = self.filtered(
            lambda connection: connection.active
            and connection.account_id.active
            and connection.role == "primary"
            and connection.inbound_active
        )
        page_ids = live._wac_subscription_scopes()
        if not page_ids:
            return True
        subscriptions = self._wac_active_subscriptions(page_ids)
        if subscriptions:
            self._wac_fence_subscriptions(subscriptions)
        return True

    def write(self, values):
        values = dict(values)
        if "wa_phone_number_id" in values and values["wa_phone_number_id"]:
            values["wa_phone_number_id"] = str(values["wa_phone_number_id"]).strip()
        if any(
            connection.adapter_key != WHATSAPP_CLOUD_ADAPTER_KEY for connection in self
        ) and any(
            values.get(name) for name in _CONFIGURATION_FIELDS - {"wa_graph_version"}
        ):
            raise ValidationError(
                _(
                    "WhatsApp Cloud settings can only be stored on a WhatsApp Cloud "
                    "connection."
                )
            )
        cloud = self.sudo().filtered(
            lambda connection: connection.adapter_key == WHATSAPP_CLOUD_ADAPTER_KEY
        )
        for field_name in _IMMUTABLE_ROUTE_FIELDS:
            if field_name not in values:
                continue
            requested = values.get(field_name) or False
            for connection in cloud:
                current = connection[field_name]
                current = current.id if hasattr(current, "id") else current
                if (current or False) != requested:
                    raise ValidationError(
                        _(
                            "A WhatsApp Cloud route is immutable; archive it and "
                            "create a new connection."
                        )
                    )
        lifecycle_changed = bool(_ROUTE_LIFECYCLE_FIELDS.intersection(values))
        scopes = self.sudo()._wac_subscription_scopes() if lifecycle_changed else set()
        result = super().write(values)
        if "wa_graph_version" in values:
            cloud.refresh_capabilities()
        if lifecycle_changed:
            scopes.update(self.sudo()._wac_subscription_scopes())
            deferred = self.env.context.get(_DEFERRED_SCOPES_CONTEXT)
            if isinstance(deferred, _DeferredSubscriptionScopes):
                deferred.page_ids.update(scopes)
            else:
                self.sudo()._wac_reconcile_subscription_lifecycle(scopes)
        return result

    def action_use_as_primary(self):
        """Reconcile subscriptions against the completed switch (CC-WAC-03).

        The core demotes the current primary before promoting this connection.
        Reconciling at the demotion would see no live route and archive the
        subscription the promotion then relies on; the lifecycle therefore runs
        once, after the switch, on the final topology. It only ever archives:
        a subscription an administrator disabled stays disabled.
        """

        deferred = _DeferredSubscriptionScopes()
        result = super(
            ContactCenterWhatsAppCloudConnection,
            self.with_context(**{_DEFERRED_SCOPES_CONTEXT: deferred}),
        ).action_use_as_primary()
        self.sudo()._wac_reconcile_subscription_lifecycle(deferred.page_ids)
        return result

    @api.constrains(
        "adapter_key",
        "account_id",
        "company_id",
        "wa_webhook_asset_id",
        "wa_phone_number_id",
        "wa_graph_version",
    )
    def _check_wac_connection_contract(self):
        for connection in self.sudo():
            if connection.adapter_key != WHATSAPP_CLOUD_ADAPTER_KEY:
                if connection.wa_webhook_asset_id or connection.wa_phone_number_id:
                    raise ValidationError(
                        _(
                            "WhatsApp Cloud settings can only be stored on a "
                            "WhatsApp Cloud connection."
                        )
                    )
                continue
            asset = connection.wa_webhook_asset_id
            if not asset:
                raise ValidationError(
                    _("A WhatsApp Cloud connection requires a business account asset.")
                )
            observed = {
                "platform": asset.platform,
                "object_type": asset.object_type,
                "transport": asset.transport,
            }
            if (
                observed != WHATSAPP_ASSET_CONTRACT
                or asset.page_id.owner_kind != WHATSAPP_OWNER_KIND
            ):
                raise ValidationError(
                    _("The selected asset is not a WhatsApp Business Account.")
                )
            if asset.company_id != connection.company_id:
                raise ValidationError(
                    _(
                        "The WhatsApp asset and the connection belong to other companies."
                    )
                )
            if connection.account_id.platform != WHATSAPP_ACCOUNT_PLATFORM:
                raise ValidationError(
                    _("A WhatsApp Cloud connection requires a WhatsApp inbox.")
                )
            if not _PHONE_NUMBER_ID_RE.fullmatch(connection.wa_phone_number_id or ""):
                raise ValidationError(_("The WhatsApp phone number ID is invalid."))
            if not validate_graph_version(connection.wa_graph_version or ""):
                raise ValidationError(_("The Graph API version must look like v26.0."))

    def init(self):
        result = super().init()
        # Two active connections for one phone number would make the webhook
        # route ambiguous; replaced (archived) connections of the route remain.
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                contact_center_whatsapp_cloud_one_active_phone
            ON contact_center_provider_connection (wa_phone_number_id)
            WHERE adapter_key = 'whatsapp_cloud'
              AND active IS TRUE
              AND wa_phone_number_id IS NOT NULL
            """
        )
        return result

    # -- Readiness -------------------------------------------------------------

    def _wac_subscription(self):
        self.ensure_one()
        page = self.sudo().wa_webhook_asset_id.page_id
        if not page:
            return self.env["meta.webhook.subscription"].sudo().browse()
        return (
            self.env["meta.webhook.subscription"]
            .sudo()
            .search(
                [
                    ("page_id", "=", page.id),
                    ("consumer_key", "=", WHATSAPP_CLOUD_CONSUMER_KEY),
                    ("object_type", "=", WHATSAPP_OBJECT_TYPE),
                    ("field_name", "=", WHATSAPP_WEBHOOK_FIELD),
                    ("active", "=", True),
                ]
            )
        )

    def _wac_runtime_topology_is_ready(self, *, require_subscription=True):
        """WhatsApp-specific gate: WABA owners stay ``manual`` by contract.

        The Page read-back (``in_sync`` and fresh ``verified_at``) never happens
        for a WABA owner, so readiness is the coherent active topology plus, for
        inbound, this consumer's active ``messages`` subscription.
        """

        self.ensure_one()
        connection = self.sudo()
        asset = connection.wa_webhook_asset_id
        if connection.adapter_key != WHATSAPP_CLOUD_ADAPTER_KEY or not asset:
            return False
        page = asset.page_id
        endpoint = page.endpoint_id
        app = page.app_id
        ready = bool(
            connection.active
            and connection.account_id.active
            and connection.account_id.platform == WHATSAPP_ACCOUNT_PLATFORM
            and _PHONE_NUMBER_ID_RE.fullmatch(connection.wa_phone_number_id or "")
            and asset.active
            and page.active
            and page.owner_kind == WHATSAPP_OWNER_KIND
            and endpoint.active
            and app.active
            and asset.company_id == connection.company_id
            and {
                "platform": asset.platform,
                "object_type": asset.object_type,
                "transport": asset.transport,
            }
            == WHATSAPP_ASSET_CONTRACT
        )
        if not ready or not require_subscription:
            return ready
        return bool(connection._wac_subscription())

    def _wac_stable_route_connections(self):
        """Every connection, archived included, of this stable route.

        The stable route is the inbox, business account and phone number ID
        (as for the response window, R15): a replacement connection keeps the
        route's receipts, never another number's or another inbox's.
        """

        self.ensure_one()
        connection = self.sudo()
        if (
            connection.adapter_key != WHATSAPP_CLOUD_ADAPTER_KEY
            or not connection.wa_business_account_id
            or not connection.wa_phone_number_id
        ):
            return connection
        return connection.with_context(active_test=False).search(
            [
                ("adapter_key", "=", WHATSAPP_CLOUD_ADAPTER_KEY),
                ("account_id", "=", connection.account_id.id),
                ("company_id", "=", connection.company_id.id),
                ("wa_business_account_id", "=", connection.wa_business_account_id),
                ("wa_phone_number_id", "=", connection.wa_phone_number_id),
            ]
        )

    def _wac_inbound_route_is_ready(self):
        self.ensure_one()
        return bool(
            self.role == "primary"
            and self.inbound_active
            and self._wac_runtime_topology_is_ready(require_subscription=True)
        )

    def _wac_outbound_is_ready(self):
        self.ensure_one()
        return bool(
            self.adapter_key == WHATSAPP_CLOUD_ADAPTER_KEY
            and self.active
            and self.account_id.active
            and self.role == "primary"
            and self.inbound_active
            and self.outbound_active
            and not self.identity_mismatch_latched
            and self.state == "connected"
            and self.health_detail in {"healthy", "metadata_limited"}
            and self._contact_center_observation_is_fresh()
            and self._wac_runtime_topology_is_ready(require_subscription=False)
        )

    def _contact_center_activation_blockers(self, direction="inbound", now=None):
        blockers = list(super()._contact_center_activation_blockers(direction, now))
        self.ensure_one()
        if self.adapter_key != WHATSAPP_CLOUD_ADAPTER_KEY:
            return blockers
        if not self._wac_runtime_topology_is_ready(
            require_subscription=(direction == "inbound")
        ):
            blockers.append(
                _("WhatsApp App, business account, asset or subscription is not ready")
            )
        if direction == "outbound" and (
            self.state != "connected" or self.identity_mismatch_latched
        ):
            blockers.append(_("WhatsApp Cloud health is not ready"))
        return blockers

    # -- Configuration ---------------------------------------------------------

    def action_whatsapp_cloud_configure_webhook(self):
        """Register this consumer's ``messages`` subscription on the WABA owner.

        The remote app subscription (``subscribed_apps``) is an activation step
        outside this module; the owner remains ``manual`` in the shared core.
        """

        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(
                _("Only a system administrator can configure the WhatsApp webhook.")
            )
        self.check_access_rights("write")
        self.check_access_rule("write")
        if (
            self.adapter_key != WHATSAPP_CLOUD_ADAPTER_KEY
            or not self.sudo().wa_webhook_asset_id
        ):
            raise ValidationError(_("Select a WhatsApp Business Account asset first."))
        self._check_wac_connection_contract()
        page = self.sudo().wa_webhook_asset_id.page_id
        subscription_model = self.env["meta.webhook.subscription"].sudo()
        existing = subscription_model.with_context(active_test=False).search(
            [
                ("page_id", "=", page.id),
                ("consumer_key", "=", WHATSAPP_CLOUD_CONSUMER_KEY),
                ("object_type", "=", WHATSAPP_OBJECT_TYPE),
                ("field_name", "=", WHATSAPP_WEBHOOK_FIELD),
            ],
            limit=1,
        )
        if not existing:
            subscription_model.create(
                {
                    "page_id": page.id,
                    "consumer_key": WHATSAPP_CLOUD_CONSUMER_KEY,
                    "object_type": WHATSAPP_OBJECT_TYPE,
                    "field_name": WHATSAPP_WEBHOOK_FIELD,
                }
            )
        elif not existing.active:
            existing.write({"active": True})
        else:
            # Seen active, but a concurrent retirement may be archiving it on
            # an older snapshot: version it so that decision retries (CC-WAC-14).
            self._wac_fence_subscriptions(existing)
        return True

    def _wac_subscription_scopes(self):
        return {
            connection.wa_webhook_asset_id.page_id.id
            for connection in self.sudo().with_context(active_test=False)
            if connection.adapter_key == WHATSAPP_CLOUD_ADAPTER_KEY
            and connection.wa_webhook_asset_id
        }

    @api.model
    def _wac_reconcile_subscription_lifecycle(self, page_ids):
        """Stop claiming WABA content once its last live route retires.

        Every caller runs inside a topology change that already holds the
        inbox and connection locks, while webhook ingestion and dispatch lock
        the Meta routing rows (endpoint, then owner) before the topology. No
        routing row is ever waited for here (CC-WAC-07). The decision itself is
        fenced before the live routes are counted, archive or not (CC-WAC-11).
        """

        if not page_ids:
            return True
        connection_model = self.sudo().with_context(active_test=False)
        subscriptions = self._wac_active_subscriptions(page_ids)
        if not subscriptions:
            # Nothing is claimed for these owners: there is nothing to decide.
            return True
        self._wac_fence_subscriptions(subscriptions)
        archived = subscriptions.browse()
        for page in subscriptions.mapped("page_id").sorted("id"):
            if connection_model.search_count(
                [
                    ("adapter_key", "=", WHATSAPP_CLOUD_ADAPTER_KEY),
                    ("active", "=", True),
                    ("account_id.active", "=", True),
                    ("role", "=", "primary"),
                    ("inbound_active", "=", True),
                    ("wa_webhook_asset_id.page_id", "=", page.id),
                ]
            ):
                continue
            archived |= subscriptions.filtered(
                lambda subscription, page=page: subscription.page_id == page
            )
        if archived:
            self._wac_lock_routing_nowait(archived)
            archived.write({"active": False})
        return True

    @api.model
    def _wac_active_subscriptions(self, page_ids):
        return (
            self.env["meta.webhook.subscription"]
            .sudo()
            .search(
                [
                    ("page_id", "in", sorted(page_ids)),
                    ("consumer_key", "=", WHATSAPP_CLOUD_CONSUMER_KEY),
                    ("object_type", "=", WHATSAPP_OBJECT_TYPE),
                    ("active", "=", True),
                ]
            )
        )

    @api.model
    def _wac_fence_subscriptions(self, subscriptions):
        """Serialize every lifecycle decision of these WhatsApp owners.

        Two inboxes can retire the last two routes of one business account
        under disjoint topology locks, each still seeing the other's route as
        live, and both keep the subscription. The fence is this consumer's own
        active subscription row of the owner: every route of the business
        account shares it, a live one exists whenever there is anything to
        archive, and ingestion and dispatch only read it (no row lock), so the
        fence never contends with webhook traffic.

        Each decision locks the rows with ``NOWAIT`` (never waiting while the
        topology is held, CC-WAC-07) and writes a new row version without
        changing any value. Under REPEATABLE READ, a concurrent decision still
        holding the row aborts this one with ``LockNotAvailable``, and one that
        committed after this transaction's snapshot makes the lock fail with
        ``SerializationFailure``: a bare row lock would not, as the snapshot
        would still show the other route live. The request and queue job
        layers retry both errors on a new transaction and snapshot.

        Every writer that can change whether a route is live fences first: a
        live route's creation, connection lifecycle writes and the primary
        switch (through the reconciliation), inbox archive and unarchive, and
        the consumer registration of an already active subscription.
        """

        ids = tuple(sorted(subscriptions.ids))
        # Expected contention, not a bad query: do not log it as an error.
        self.env.cr.execute(
            "SELECT id FROM meta_webhook_subscription WHERE id IN %s "
            "ORDER BY id FOR UPDATE NOWAIT",
            [ids],
            log_exceptions=False,
        )
        self.env.cr.execute(
            "UPDATE meta_webhook_subscription SET write_date = write_date "
            "WHERE id IN %s",
            [ids],
        )
        return True

    @api.model
    def _wac_lock_routing_nowait(self, subscriptions):
        """Take the rows a subscription archive locks, without ever waiting.

        The shared core archives under endpoint -> owner -> subscription row
        locks. Waiting for them while this transaction holds the topology would
        invert the order of a dispatch or ingestion that holds the endpoint and
        waits for the inbox: a deadlock. Every one of them is taken here with
        ``NOWAIT`` (the subscriptions are already held by the fence); a busy
        row aborts this transaction with ``LockNotAvailable``, releasing the
        topology at once, and the retried transaction decides again on the
        committed topology. Once held, the core's own locks no longer wait.
        """

        subscriptions = subscriptions.sudo()
        pages = subscriptions.mapped("page_id")
        endpoints = pages.mapped("endpoint_id")
        # Expected contention, not a bad query: do not log it as an error.
        self.env.cr.execute(
            "SELECT id FROM meta_webhook_endpoint WHERE id IN %s "
            "ORDER BY id FOR UPDATE NOWAIT",
            [tuple(sorted(endpoints.ids))],
            log_exceptions=False,
        )
        self.env.cr.execute(
            "SELECT id FROM meta_webhook_page WHERE id IN %s "
            "ORDER BY id FOR UPDATE NOWAIT",
            [tuple(sorted(pages.ids))],
            log_exceptions=False,
        )
        self.env.cr.execute(
            "SELECT id FROM meta_webhook_subscription WHERE id IN %s "
            "ORDER BY id FOR UPDATE NOWAIT",
            [tuple(sorted(subscriptions.ids))],
            log_exceptions=False,
        )
        return True

    # -- Health observation ----------------------------------------------------

    def _job_check_health(self):
        """Persist phone-number observations returned by one real health read."""

        if self.adapter_key != WHATSAPP_CLOUD_ADAPTER_KEY:
            return super()._job_check_health()
        observation = {}
        result = super(
            ContactCenterWhatsAppCloudConnection,
            self.with_context(**{HEALTH_OBSERVATION_CONTEXT_KEY: observation}),
        )._job_check_health()
        if observation:
            self.sudo()._wac_record_observation(observation)
        return result

    def _wac_record_observation(self, observation):
        self.ensure_one()
        values = {
            field_name: observation.get(key) or False
            for key, field_name in _OBSERVED_FIELDS.items()
        }
        changed = {
            field_name: value
            for field_name, value in values.items()
            if (self[field_name] or False) != value
        }
        changed["wa_observed_at"] = fields.Datetime.now()
        # Observed provider metadata only: never configuration, route or role.
        return super(ContactCenterWhatsAppCloudConnection, self).write(changed)


class ContactCenterWhatsAppCloudAccount(models.Model):
    _inherit = "contact.center.account"

    def write(self, values):
        lifecycle_changed = "active" in values and any(
            bool(values["active"]) != account.active for account in self
        )
        scopes = set()
        connections = self.env["contact.center.provider.connection"]
        if lifecycle_changed:
            connections = (
                self.sudo().with_context(active_test=False).mapped("connection_ids")
            )
            scopes = connections._wac_subscription_scopes()
        if "platform" in values:
            cloud = (
                self.env["contact.center.provider.connection"]
                .sudo()
                .with_context(active_test=False)
                .search(
                    [
                        ("account_id", "in", self.ids),
                        ("adapter_key", "=", WHATSAPP_CLOUD_ADAPTER_KEY),
                    ],
                    limit=1,
                )
            )
            if cloud and values["platform"] != WHATSAPP_ACCOUNT_PLATFORM:
                raise ValidationError(
                    _("An inbox with a WhatsApp Cloud route must stay WhatsApp.")
                )
        result = super().write(values)
        if lifecycle_changed:
            self.env[
                "contact.center.provider.connection"
            ]._wac_reconcile_subscription_lifecycle(scopes)
        return result
