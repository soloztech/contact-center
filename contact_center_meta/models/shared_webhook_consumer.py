from psycopg2 import IntegrityError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.meta_webhook_base.services.contracts import META_WEBHOOK_FRESHNESS
from odoo.addons.meta_webhook_base.services.tokens import META_WEBHOOK_INTERNAL_TOKEN
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.contracts import META_PROVIDER_SCHEMA_VERSION, META_TRANSPORT_CONTRACTS
from ..services.shared_webhook import (
    META_MESSAGING_CONSUMER_KEY,
    META_MESSAGING_SUBSCRIPTIONS,
    inbox_dedupe_key,
    messaging_item_specs,
    route_contract,
    shared_private_media,
    subscription_contract,
)
from ..services.tokens import CONTACT_CENTER_META_INTERNAL_TOKEN

# Shared webhook routing and adapter configuration are separate service boundaries.
# pylint: disable=consider-merging-classes-inherited

_ROUTE_LIFECYCLE_FIELDS = {
    "active",
    "adapter_key",
    "inbound_active",
    "meta_transport_mode",
    "meta_webhook_asset_id",
    "role",
}
_SUBSCRIPTION_RECOVERY_LIMIT = 500


class _ContactCenterMetaRouteNotReady(RetryableJobError):
    """Private retry marker for an authenticated route awaiting readback."""


class ContactCenterMetaAssetConnection(models.Model):
    _inherit = "contact.center.provider.connection"

    meta_webhook_asset_id = fields.Many2one(
        "meta.webhook.asset",
        string="Meta Messaging Asset",
        index=True,
        ondelete="restrict",
        check_company=True,
        groups="base.group_system",
        help=(
            "Canonical Meta Page or Instagram asset. App, Page, credentials, "
            "callback and technical ledger are owned by the shared Meta core."
        ),
    )

    @api.constrains(
        "adapter_key",
        "company_id",
        "meta_webhook_asset_id",
        "meta_target_asset_id",
        "meta_transport_mode",
    )
    def _check_meta_webhook_asset_contract(self):
        for connection in self.sudo().filtered("meta_webhook_asset_id"):
            asset = connection.meta_webhook_asset_id
            expected = META_TRANSPORT_CONTRACTS.get(connection.meta_transport_mode)
            if connection.adapter_key != "meta" or not expected:
                raise ValidationError(
                    _("A shared Meta webhook asset requires a Meta connection.")
                )
            if asset.company_id != connection.company_id:
                raise ValidationError(
                    _("The shared Meta asset belongs to another company.")
                )
            observed = {
                "object_type": asset.object_type,
                "asset_platform": asset.platform,
                "transport": asset.transport,
            }
            expected_asset = {
                "object_type": expected["object_type"],
                "asset_platform": expected["asset_platform"],
                "transport": expected["transport"],
            }
            if observed != expected_asset:
                raise ValidationError(
                    _("The shared Meta asset does not match the connection mode.")
                )
            if asset.external_asset_id != connection.meta_target_asset_id:
                raise ValidationError(
                    _("The shared Meta asset does not match the destination asset.")
                )

    def _meta_runtime_topology_is_ready(self, *, require_subscriptions=True):
        """Fail closed until the canonical App/Page/asset route is coherent."""

        self.ensure_one()
        connection = self.sudo()
        if connection.adapter_key != "meta" or not connection.meta_webhook_asset_id:
            return False
        asset = connection.meta_webhook_asset_id
        page = asset.page_id
        endpoint = page.endpoint_id
        app = page.app_id
        contract = subscription_contract(connection.meta_transport_mode)
        if (
            not contract
            or asset.external_asset_id != connection.account_id.own_external_identity
        ):
            return False
        topology_ready = bool(
            connection.active
            and connection.account_id.active
            and asset.active
            and page.active
            and endpoint.active
            and app.active
            and asset.company_id == connection.company_id
        )
        if not topology_ready or not require_subscriptions:
            return topology_ready
        subscriptions = (
            self.env["meta.webhook.subscription"]
            .sudo()
            .search(
                [
                    ("page_id", "=", page.id),
                    ("consumer_key", "=", META_MESSAGING_CONSUMER_KEY),
                    ("object_type", "=", contract["object_type"]),
                    ("active", "=", True),
                ]
            )
        )
        cutoff = fields.Datetime.now() - META_WEBHOOK_FRESHNESS
        observed_objects = endpoint.observed_subscriptions_json or []
        observed = next(
            (
                item
                for item in observed_objects
                if isinstance(item, dict)
                and item.get("object") == contract["object_type"]
            ),
            {},
        )
        endpoint_object_ready = bool(
            observed.get("active") is True
            and observed.get("callback_matches") is True
            and set(observed.get("fields") or ()).issuperset(contract["fields"])
        )
        return bool(
            page.subscription_state == "in_sync"
            and page.verified_at
            and page.verified_at >= cutoff
            and endpoint.verified_at
            and endpoint.verified_at >= cutoff
            and endpoint_object_ready
            and set(subscriptions.mapped("field_name")) == set(contract["fields"])
        )

    def _meta_inbound_route_is_ready(self):
        self.ensure_one()
        return bool(
            self.role == "primary"
            and self.inbound_active
            and self._meta_runtime_topology_is_ready(require_subscriptions=True)
        )

    def action_meta_configure_webhook(self):
        """Register Contact Center fields and reconcile the canonical callback."""

        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(
                _("Only a system administrator can configure the Meta webhook.")
            )
        self.check_access_rights("write")
        self.check_access_rule("write")
        if self.adapter_key != "meta" or not self.meta_webhook_asset_id:
            raise ValidationError(_("Select a Meta messaging asset first."))
        self._check_meta_webhook_asset_contract()
        asset = self.meta_webhook_asset_id.sudo()
        page = asset.page_id
        contract = subscription_contract(self.meta_transport_mode)
        if not contract:
            raise ValidationError(_("The Meta messaging mode is unsupported."))
        subscription_model = self.env["meta.webhook.subscription"].sudo()
        existing = subscription_model.with_context(active_test=False).search(
            [
                ("page_id", "=", page.id),
                ("consumer_key", "=", META_MESSAGING_CONSUMER_KEY),
                ("object_type", "=", contract["object_type"]),
            ]
        )
        expected_fields = set(contract["fields"])
        by_field = {subscription.field_name: subscription for subscription in existing}
        missing = sorted(expected_fields - set(by_field))
        if missing:
            subscription_model.create(
                [
                    {
                        "page_id": page.id,
                        "consumer_key": META_MESSAGING_CONSUMER_KEY,
                        "object_type": contract["object_type"],
                        "field_name": field_name,
                    }
                    for field_name in missing
                ]
            )
        to_activate = existing.filtered(
            lambda item: item.field_name in expected_fields and not item.active
        )
        if to_activate:
            to_activate.write({"active": True})
        to_archive = existing.filtered(
            lambda item: item.field_name not in expected_fields and item.active
        )
        if to_archive:
            to_archive.write({"active": False})
        self.env["meta.webhook.subscription.service"]._enqueue_endpoint(
            page.endpoint_id
        )
        # Object-button RPC results must be JSON serializable; the queue service
        # returns a delayed-job proxy/record for internal callers.
        return True

    def write(self, values):
        lifecycle_changed = bool(_ROUTE_LIFECYCLE_FIELDS.intersection(values))
        scopes = self.sudo()._contact_center_meta_subscription_scopes()
        result = super().write(values)
        if lifecycle_changed:
            scopes.update(self.sudo()._contact_center_meta_subscription_scopes())
            self.sudo()._contact_center_meta_reconcile_subscription_lifecycle(scopes)
        return result

    def _contact_center_meta_subscription_scopes(self):
        scopes = set()
        for connection in self.sudo():
            asset = connection.meta_webhook_asset_id
            contract = subscription_contract(connection.meta_transport_mode)
            if connection.adapter_key == "meta" and asset and contract:
                scopes.add((asset.page_id.id, contract["object_type"]))
        return scopes

    @api.model
    def _contact_center_meta_reconcile_subscription_lifecycle(self, scopes):
        if not scopes:
            return True
        connection_model = self.sudo().with_context(active_test=False)
        subscription_model = self.env["meta.webhook.subscription"].sudo()
        archived = subscription_model.browse()
        for page_id, object_type in sorted(scopes):
            transport_modes = [
                mode
                for mode, contract in META_MESSAGING_SUBSCRIPTIONS.items()
                if contract["object_type"] == object_type
            ]
            active_routes = connection_model.search_count(
                [
                    ("adapter_key", "=", "meta"),
                    ("active", "=", True),
                    ("account_id.active", "=", True),
                    ("role", "=", "primary"),
                    ("inbound_active", "=", True),
                    ("meta_webhook_asset_id.page_id", "=", page_id),
                    ("meta_transport_mode", "in", transport_modes),
                ]
            )
            if active_routes:
                continue
            archived |= subscription_model.search(
                [
                    ("page_id", "=", page_id),
                    ("consumer_key", "=", META_MESSAGING_CONSUMER_KEY),
                    ("object_type", "=", object_type),
                    ("active", "=", True),
                ]
            )
        if not archived:
            return True
        endpoints = archived.mapped("page_id.endpoint_id")
        archived.write({"active": False})
        service = self.env["meta.webhook.subscription.service"]
        for endpoint in endpoints.filtered(
            lambda item: item.active and item.app_id.active
        ):
            service._enqueue_endpoint(endpoint)
        return True

    def init(self):
        result = super().init()
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                contact_center_meta_one_primary_asset
            ON contact_center_provider_connection (meta_webhook_asset_id)
            WHERE adapter_key = 'meta'
              AND active IS TRUE
              AND role = 'primary'
              AND meta_webhook_asset_id IS NOT NULL
            """
        )
        return result


class ContactCenterMetaWebhookDispatcher(models.AbstractModel):
    _inherit = "meta.webhook.dispatcher"

    @api.model
    def _dispatch_retry_limit_error_class(self, dispatch, error):
        error_class = super()._dispatch_retry_limit_error_class(dispatch, error)
        if dispatch.consumer_key == META_MESSAGING_CONSUMER_KEY and isinstance(
            error, _ContactCenterMetaRouteNotReady
        ):
            return "SubscriptionNotReady"
        return error_class

    @api.model
    def _after_subscription_reconcile(self, endpoint):
        result = super()._after_subscription_reconcile(endpoint)
        dispatch_model = self.env["meta.webhook.dispatch"].sudo()
        candidates = dispatch_model.search(
            [
                ("delivery_id.endpoint_id", "=", endpoint.id),
                ("consumer_key", "=", META_MESSAGING_CONSUMER_KEY),
                ("state", "=", "dead"),
                ("last_error_class", "=", "SubscriptionNotReady"),
            ],
            order="id",
            limit=_SUBSCRIPTION_RECOVERY_LIMIT,
        )
        recovered = dispatch_model.browse()
        for dispatch in candidates:
            dispatch.flush_recordset(["state", "last_error_class"])
            self.env.cr.execute(
                "SELECT state, last_error_class "
                "FROM meta_webhook_dispatch WHERE id = %s "
                "FOR UPDATE SKIP LOCKED",
                [dispatch.id],
            )
            row = self.env.cr.fetchone()
            if not row or row[:2] != ("dead", "SubscriptionNotReady"):
                continue
            dispatch.invalidate_recordset(
                ["state", "last_error_class", "queue_job_uuid"]
            )
            if dispatch._active_job() or not dispatch._current_policy_allows_dispatch():
                continue
            contract = route_contract(dispatch.item_id.payload_json)
            asset = self._contact_center_meta_routing_asset(dispatch, contract)
            if not asset or not self._contact_center_meta_item_matches_route(
                dispatch, contract, asset
            ):
                continue
            try:
                connections = self._contact_center_meta_route_connections(
                    dispatch, contract, asset
                )
            except (RetryableJobError, ValidationError):
                continue
            if len(connections) != 1:
                continue
            dispatch.with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            ).write(
                {
                    "state": "pending",
                    "attempts": 0,
                    "queue_job_uuid": False,
                    "processed_at": False,
                    "result_ref": False,
                    "last_error_class": False,
                    "last_error_message": False,
                }
            )
            recovered |= dispatch
        if recovered:
            recovered._enqueue()
        return result

    @api.model
    def _consumer_item_specs(self, endpoint, delivery, decoded_envelope):
        specs = list(
            super()._consumer_item_specs(endpoint, delivery, decoded_envelope) or ()
        )
        probe_specs = messaging_item_specs(
            decoded_envelope,
            delivery,
            private_locator_references={},
            private_locator_rejections=(),
        )
        eligible_item_keys = self._contact_center_meta_subscribed_item_keys(
            endpoint, probe_specs
        )
        eligible_item_keys = self._contact_center_meta_admitted_item_keys(
            endpoint, probe_specs, eligible_item_keys
        )
        if not eligible_item_keys:
            return tuple(specs)
        candidates, references, rejections = shared_private_media(
            decoded_envelope,
            delivery,
            eligible_item_keys=eligible_item_keys,
        )
        if candidates:
            (
                self.env["contact.center.meta.media.locator"]
                .sudo()
                .with_context(
                    contact_center_meta_internal=CONTACT_CENTER_META_INTERNAL_TOKEN
                )
                ._register_delivery_candidates(delivery, candidates)
            )
        specs.extend(
            messaging_item_specs(
                decoded_envelope,
                delivery,
                private_locator_references=references,
                private_locator_rejections=rejections,
                eligible_item_keys=eligible_item_keys,
            )
        )
        return tuple(specs)

    @api.model
    def _contact_center_meta_admitted_item_keys(self, endpoint, specs, eligible):
        """Filter before private URLs and shared message content are persisted."""
        admitted = set(eligible)
        policy = self.env["contact.center.conversation.ignore"]
        connections = (
            self.env["contact.center.provider.connection"]
            .sudo()
            .search(
                [
                    ("adapter_key", "=", "meta"),
                    ("role", "=", "primary"),
                    ("meta_webhook_asset_id.endpoint_id", "=", endpoint.id),
                ]
            )
        )
        if connections:
            connections._contact_center_lock_ingress_admission()
        for spec in specs:
            if spec["item_key"] not in admitted:
                continue
            route = route_contract(spec["payload_json"])
            routes = connections.filtered(
                lambda row: route
                and row.meta_target_asset_id == route["target_asset_id"]
                and row.meta_transport_mode == route["transport_mode"]
                and row.company_id == endpoint.company_id
            )
            # An ambiguous route cannot justify suppressing another inbox's data.
            if len(routes) == 1 and policy._ignored_envelope(
                routes, spec["payload_json"]
            ):
                admitted.discard(spec["item_key"])
        return frozenset(admitted)

    @api.model
    def _contact_center_meta_subscribed_item_keys(self, endpoint, item_specs):
        if not item_specs:
            return frozenset()
        target_ids = sorted(
            {spec["target_asset_id"] for spec in item_specs if spec["target_asset_id"]}
        )
        assets = (
            self.env["meta.webhook.asset"]
            .sudo()
            .search(
                [
                    ("endpoint_id", "=", endpoint.id),
                    ("external_asset_id", "in", target_ids),
                    ("active", "=", True),
                    ("page_id.active", "=", True),
                ]
            )
        )
        assets_by_route = {
            (asset.external_asset_id, asset.object_type): asset for asset in assets
        }
        page_ids = assets.mapped("page_id").ids
        subscriptions = (
            self.env["meta.webhook.subscription"]
            .sudo()
            .search(
                [
                    ("page_id", "in", page_ids),
                    ("consumer_key", "=", META_MESSAGING_CONSUMER_KEY),
                    ("active", "=", True),
                ]
            )
            if page_ids
            else self.env["meta.webhook.subscription"].browse()
        )
        subscribed = {
            (subscription.page_id.id, subscription.object_type, subscription.field_name)
            for subscription in subscriptions
        }
        return frozenset(
            spec["item_key"]
            for spec in item_specs
            if (spec["target_asset_id"], spec["object_type"]) in assets_by_route
            and (
                assets_by_route[
                    (spec["target_asset_id"], spec["object_type"])
                ].page_id.id,
                spec["object_type"],
                spec["event_field"],
            )
            in subscribed
        )

    @api.model
    def _dispatch_consumer(self, dispatch):
        result = super()._dispatch_consumer(dispatch)
        if result is not None or dispatch.consumer_key != META_MESSAGING_CONSUMER_KEY:
            return result
        if (dispatch.item_id.payload_json or {}).get(
            "reason"
        ) == "consumer_content_erased":
            return {"handled": True, "result_ref": "contact.center.content_erased"}
        inbox = self._contact_center_meta_project_inbox(dispatch)
        if not inbox:
            return None
        return {
            "handled": True,
            "result_ref": "contact.center.inbox.event:%s" % inbox.id,
        }

    @api.model
    def _contact_center_meta_project_inbox(self, dispatch):
        dispatch.ensure_one()
        item = dispatch.item_id
        contract = route_contract(item.payload_json)
        asset = self._contact_center_meta_routing_asset(dispatch, contract)
        if not asset or not self._contact_center_meta_item_matches_route(
            dispatch, contract, asset
        ):
            return self.env["contact.center.inbox.event"].browse()
        connections = self._contact_center_meta_route_connections(
            dispatch, contract, asset
        )
        if not connections:
            return self.env["contact.center.inbox.event"].browse()
        if len(connections) > 1:
            raise ValidationError(
                _("More than one Contact Center connection claims this Meta route.")
            )
        connection = connections.ensure_one()
        block_if_new = not connection.account_id._contact_center_access_is_ready()
        return self._contact_center_meta_find_or_create_inbox(
            dispatch,
            connection,
            contract["route"],
            block_if_new=block_if_new,
        )

    @api.model
    def _contact_center_meta_routing_asset(self, dispatch, contract):
        if not contract:
            return self.env["meta.webhook.asset"].browse()
        expected = META_TRANSPORT_CONTRACTS.get(contract["transport_mode"])
        if not expected:
            return self.env["meta.webhook.asset"].browse()
        assets = dispatch.page_id.asset_ids.filtered(
            lambda asset: asset.active
            and asset.external_asset_id == contract["target_asset_id"]
            and asset.object_type == expected["object_type"]
            and asset.platform == expected["asset_platform"]
            and asset.transport == expected["transport"]
        )
        if len(assets) > 1:
            raise ValidationError(_("The shared Meta asset route is ambiguous."))
        return assets

    @api.model
    def _contact_center_meta_item_matches_route(self, dispatch, contract, asset):
        item = dispatch.item_id
        page = dispatch.page_id
        return bool(
            item.kind == "messaging"
            and item.object_type == contract["route"]["object"]
            and item.target_asset_id == contract["target_asset_id"]
            and page.endpoint_id == item.delivery_id.endpoint_id
            and page.app_id == item.delivery_id.app_id
            and page.company_id == item.company_id
            and asset.page_id == page
        )

    @api.model
    def _contact_center_meta_route_connections(self, dispatch, contract, asset):
        connection_model = (
            self.env["contact.center.provider.connection"]
            .sudo()
            .with_context(active_test=False)
        )
        candidates = connection_model.search(
            [
                ("company_id", "=", dispatch.company_id.id),
                ("adapter_key", "=", "meta"),
                ("meta_webhook_asset_id", "=", asset.id),
                ("meta_transport_mode", "=", contract["transport_mode"]),
                ("meta_target_asset_id", "=", contract["target_asset_id"]),
            ],
            order="id",
        )
        connection_model._contact_center_lock_topology(
            candidates.mapped("account_id").ids
        )
        candidates = candidates.exists()
        if candidates:
            candidates.invalidate_recordset(
                [
                    "active",
                    "account_id",
                    "inbound_active",
                    "meta_target_asset_id",
                    "meta_transport_mode",
                    "meta_webhook_asset_id",
                    "role",
                ]
            )
            accounts = candidates.mapped("account_id")
            accounts.invalidate_recordset(
                ["active", "access_user_ids", "access_team_ids"]
            )
            accounts.mapped("access_team_ids").invalidate_recordset(
                ["active", "agent_ids", "supervisor_ids"]
            )
        canonical = candidates.filtered(
            lambda connection: connection.meta_webhook_asset_id == asset
            and connection.meta_transport_mode == contract["transport_mode"]
            and connection.meta_target_asset_id == contract["target_asset_id"]
            and connection.active
            and connection.account_id.active
            and connection.role == "primary"
            and connection.inbound_active
        )
        if len(canonical) > 1:
            raise ValidationError(
                _("More than one Contact Center connection claims this Meta route.")
            )
        ready = canonical.filtered(
            # Subscription rows are desired configuration, not proof
            # that Meta currently points at this endpoint. Dispatch must use the
            # same fail-closed observed-state gate exposed by provider health.
            lambda connection: connection._meta_inbound_route_is_ready()
        )
        if canonical and not ready:
            # The authenticated shared ledger already proved that this event is
            # for one active canonical route. A temporarily stale reconciliation
            # observation must delay projection, never turn valid evidence into a
            # terminal ``unrouted`` dispatch. No fixed ``seconds`` is supplied so
            # queue_job applies the channel retry_pattern.
            raise _ContactCenterMetaRouteNotReady(
                "Contact Center Meta route is temporarily not ready"
            )
        return ready

    @api.model
    def _contact_center_meta_find_or_create_inbox(
        self, dispatch, connection, route, *, block_if_new=False
    ):
        item = dispatch.item_id
        inbox_model = self.env["contact.center.inbox.event"].sudo()
        dedupe_key = inbox_dedupe_key(item.payload_json, route)
        domain = [
            ("provider_connection_id", "=", connection.id),
            ("inbox_dedupe_key", "=", dedupe_key),
        ]
        inbox = inbox_model.search(domain, limit=1)
        if inbox:
            self._contact_center_meta_bind_private_media(item, connection, inbox)
            return inbox
        metadata = {
            "meta_delivery_ref": item.delivery_id.public_ref,
            "meta_item_id": item.id,
            "meta_item_key": item.item_key,
            "meta_dispatch_id": dispatch.id,
            "object": item.object_type,
            "platform": route["platform"],
            "graph_version": item.delivery_id.graph_version,
            "event_sha256": item.event_sha256,
            "raw_envelope_sanitized": True,
            "technical_ledger": "meta_webhook_base",
        }
        values = {
            "provider_connection_id": connection.id,
            "inbox_dedupe_key": dedupe_key,
            "provider_schema_version": META_PROVIDER_SCHEMA_VERSION,
            "raw_envelope_json": item.payload_json,
            "metadata_json": metadata,
        }
        if block_if_new:
            values.update(
                {
                    "state": "blocked",
                    "last_error_class": "AccountNotReady",
                    "last_error_message": (
                        "The Contact Center account has no valid owner or access team."
                    ),
                }
            )
            metadata["blocked_reason"] = "account_not_ready"
        try:
            with self.env.cr.savepoint():
                inbox = inbox_model.with_context(
                    contact_center_skip_enqueue=True
                ).create(values)
                self._contact_center_meta_bind_private_media(item, connection, inbox)
                inbox._enqueue()
                return inbox
        except IntegrityError:
            inbox = inbox_model.search(domain, limit=1)
            if not inbox:
                raise
            self._contact_center_meta_bind_private_media(item, connection, inbox)
            return inbox

    @api.model
    def _contact_center_meta_bind_private_media(self, item, connection, inbox):
        return (
            self.env["contact.center.meta.media.locator"]
            .sudo()
            .with_context(
                contact_center_meta_internal=CONTACT_CENTER_META_INTERNAL_TOKEN
            )
            ._bind_item(item, connection, inbox)
        )


class ContactCenterMetaConversationDeletion(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    def _prepare_conversation_deletion_dependencies(
        self, channel, bindings, messages, inbox_events
    ):
        result = super()._prepare_conversation_deletion_dependencies(
            channel, bindings, messages, inbox_events
        )
        policy = self.env["contact.center.conversation.ignore"]
        connections = bindings.account_id.connection_ids.filtered(
            lambda row: row.adapter_key == "meta"
        )
        if not connections:
            return result
        refs = set(bindings.mapped("conversation_ref"))
        item_model = self.env["meta.webhook.item"].sudo()
        domain = [
            ("company_id", "=", channel.contact_center_company_id.id),
            ("kind", "=", "messaging"),
            ("target_asset_id", "in", connections.mapped("meta_target_asset_id")),
        ]
        selected = item_model.browse()
        last_id = 0
        while True:
            batch = item_model.search(
                domain + [("id", ">", last_id)], order="id", limit=500
            )
            if not batch:
                break
            for item in batch:
                for connection in connections:
                    route = policy._route(connection, item.payload_json)
                    if route and route["conversation_ref"] in refs:
                        selected |= item
                        break
            last_id = batch[-1].id
            batch.invalidate_recordset(["payload_json"])
        erased = selected.with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )._erase_consumer_message_content(META_MESSAGING_CONSUMER_KEY)
        for item in erased:
            locators = (
                self.env["contact.center.meta.media.locator"]
                .sudo()
                .search(
                    [
                        ("meta_delivery_id", "=", item.delivery_id.id),
                        ("meta_item_key", "=", item.item_key),
                    ]
                )
            )
            locators.with_context(
                contact_center_meta_internal=CONTACT_CENTER_META_INTERNAL_TOKEN
            ).write({"download_url": False, "state": "discarded"})
        return result
