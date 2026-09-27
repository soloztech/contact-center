import copy
import datetime

from psycopg2 import IntegrityError

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.contact_center_base.services.ad_origin_preview import thumbnail_url
from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    TransientAdapterError,
)
from odoo.addons.contact_center_base.services.dto import DTOValidationError
from odoo.addons.meta_webhook_base.services.tokens import META_WEBHOOK_INTERNAL_TOKEN
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.contracts import (
    WHATSAPP_ASSET_CONTRACT,
    WHATSAPP_CLOUD_ADAPTER_KEY,
    WHATSAPP_CLOUD_CONSUMER_KEY,
    WHATSAPP_CLOUD_PROVIDER_SCHEMA_VERSION,
    WHATSAPP_OBJECT_TYPE,
    WHATSAPP_OWNER_KIND,
    WHATSAPP_WEBHOOK_FIELD,
)
from ..services.normalizer import status_correlation_id
from ..services.shared_webhook import (
    claim_account_id,
    error_occurrence_digest,
    inbox_dedupe_key,
    occurrence_digest,
    raw_row,
    route_contract,
    whatsapp_item_specs,
)

# Shared webhook routing and privacy hooks are separate service boundaries.
# pylint: disable=consider-merging-classes-inherited

_ROUTE_FIELDS = [
    "active",
    "account_id",
    "inbound_active",
    "role",
    "wa_business_account_id",
    "wa_phone_number_id",
    "wa_webhook_asset_id",
]


class _WhatsAppCloudRouteNotReady(RetryableJobError):
    """Private retry marker: the authenticated route is not live yet."""


class ContactCenterWhatsAppCloudDispatcher(models.AbstractModel):
    _inherit = "meta.webhook.dispatcher"

    @api.model
    def _dispatch_retry_limit_error_class(self, dispatch, error):
        error_class = super()._dispatch_retry_limit_error_class(dispatch, error)
        if dispatch.consumer_key == WHATSAPP_CLOUD_CONSUMER_KEY and isinstance(
            error, _WhatsAppCloudRouteNotReady
        ):
            return "RouteNotReady"
        return error_class

    # -- Ingestion ---------------------------------------------------------------

    @api.model
    def _consumer_item_specs(self, endpoint, delivery, decoded_envelope):
        specs = list(
            super()._consumer_item_specs(endpoint, delivery, decoded_envelope) or ()
        )
        probe_specs = whatsapp_item_specs(decoded_envelope, delivery)
        if not probe_specs:
            return tuple(specs)
        eligible = self._wac_subscribed_item_keys(endpoint, probe_specs)
        eligible = self._wac_configured_item_keys(endpoint, probe_specs, eligible)
        eligible = self._wac_admitted_item_keys(endpoint, probe_specs, eligible)
        eligible = self._wac_unerased_item_keys(endpoint, probe_specs, eligible)
        if not eligible:
            return tuple(specs)
        claimed = self._wac_owned_claims(
            endpoint, [spec for spec in probe_specs if spec["item_key"] in eligible]
        )
        self._wac_capture_referrals(endpoint, decoded_envelope, claimed)
        specs.extend(claimed)
        return tuple(specs)

    @api.model
    def _wac_subscribed_item_keys(self, endpoint, item_specs):
        """Claim only for an active asset whose owner subscribes this consumer."""

        target_ids = sorted({spec["target_asset_id"] for spec in item_specs})
        assets = (
            self.env["meta.webhook.asset"]
            .sudo()
            .search(
                [
                    ("endpoint_id", "=", endpoint.id),
                    ("external_asset_id", "in", target_ids),
                    ("object_type", "=", WHATSAPP_OBJECT_TYPE),
                    ("platform", "=", WHATSAPP_ASSET_CONTRACT["platform"]),
                    ("active", "=", True),
                    ("page_id.active", "=", True),
                    ("page_id.owner_kind", "=", WHATSAPP_OWNER_KIND),
                ]
            )
        )
        if not assets:
            return frozenset()
        subscriptions = (
            self.env["meta.webhook.subscription"]
            .sudo()
            .search(
                [
                    ("page_id", "in", assets.mapped("page_id").ids),
                    ("consumer_key", "=", WHATSAPP_CLOUD_CONSUMER_KEY),
                    ("object_type", "=", WHATSAPP_OBJECT_TYPE),
                    ("field_name", "=", WHATSAPP_WEBHOOK_FIELD),
                    ("active", "=", True),
                ]
            )
        )
        subscribed_pages = set(subscriptions.mapped("page_id").ids)
        subscribed_assets = {
            asset.external_asset_id
            for asset in assets
            if asset.page_id.id in subscribed_pages
        }
        return frozenset(
            spec["item_key"]
            for spec in item_specs
            if spec["target_asset_id"] in subscribed_assets
        )

    @api.model
    def _wac_configured_item_keys(self, endpoint, item_specs, eligible):
        """Claim only phone numbers that have a connection on this endpoint.

        One business account subscription covers every phone number of the
        account. A number used by another tool, or not set up here, stays a
        content-free placeholder instead of an undeliverable copy of its
        messages and recipients (L11-ROUTE-01).
        """

        if not eligible:
            return frozenset()
        connections = self._wac_claimable_connections(endpoint)
        routes = {
            (connection.wa_business_account_id, connection.wa_phone_number_id)
            for connection in connections
        }
        configured = set()
        for spec in item_specs:
            if spec["item_key"] not in eligible:
                continue
            contract = route_contract(spec["payload_json"])
            if (
                contract
                and (contract["waba_id"], contract["phone_number_id"]) in routes
            ):
                configured.add(spec["item_key"])
        return frozenset(configured)

    @api.model
    def _wac_claimable_connections(self, endpoint):
        """Active connections of this endpoint whose numbers may be claimed.

        Any role counts: a standby or migration connection keeps its number
        claimable, so it also keeps its privacy admission (CC-WAC-09). The
        unique active phone index leaves at most one connection per number.
        """

        return (
            self.env["contact.center.provider.connection"]
            .sudo()
            .search(
                [
                    ("adapter_key", "=", WHATSAPP_CLOUD_ADAPTER_KEY),
                    ("wa_webhook_asset_id.endpoint_id", "=", endpoint.id),
                    ("company_id", "=", endpoint.company_id.id),
                ]
            )
        )

    @api.model
    def _wac_owned_claims(self, endpoint, specs):
        """Record, in each claim, the inbox whose number received it.

        The owner is the inbox of the one connection configured for the number
        at claim time, whatever its role; replacement connections of that
        inbox keep it. Dispatch projects the claim only there and a
        conversation deletion only erases its own inbox's claims, pending or
        dispatched, after the number moved to another inbox (CC-WAC-13). A
        route without exactly one such connection is not claimed at all.

        An occurrence (company, business account and ``wamid``) belongs to its
        first claim's inbox for good: a later copy in another delivery body,
        once the number serves another inbox, stays a content-free placeholder
        instead of becoming that inbox's (CC-WAC-16).
        """

        connections = self._wac_claimable_connections(endpoint)
        first_owners = self._wac_first_claim_owners(
            endpoint.company_id,
            [(spec["target_asset_id"], spec["occurrence_ref"]) for spec in specs],
        )
        owned = []
        for spec in specs:
            routes = self._wac_routes_for(connections, endpoint, spec["payload_json"])
            if len(routes) != 1:
                continue
            first_owner = first_owners.get(
                (spec["target_asset_id"], spec["occurrence_ref"])
            )
            if first_owner is not None and first_owner != routes.account_id.id:
                continue
            spec["payload_json"]["claim"] = {"account_id": routes.account_id.id}
            owned.append(spec)
        return owned

    @api.model
    def _wac_first_claim_owners(self, company, occurrences):
        """Return the inbox of each occurrence's first claim that recorded one.

        Every earlier copy counts, whatever its delivery or dispatch state
        (pending, unrouted or projected). An erased copy no longer carries its
        owner; its occurrence is refused by the deletion marker instead.
        """

        occurrences = {
            (target, occurrence)
            for target, occurrence in occurrences
            if isinstance(occurrence, str)
            and occurrence.startswith(("wac:message:", "wac:status:"))
        }
        if not occurrences:
            return {}
        items = (
            self.env["meta.webhook.item"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("kind", "=", "messaging"),
                    ("object_type", "=", WHATSAPP_OBJECT_TYPE),
                    ("event_field", "=", WHATSAPP_WEBHOOK_FIELD),
                    (
                        "target_asset_id",
                        "in",
                        sorted({target for target, _occurrence in occurrences}),
                    ),
                    (
                        "occurrence_ref",
                        "in",
                        sorted({occurrence for _target, occurrence in occurrences}),
                    ),
                ],
                order="id",
            )
        )
        owners = {}
        for item in items:
            key = (item.target_asset_id, item.occurrence_ref)
            owner = claim_account_id(item.payload_json)
            if key in occurrences and key not in owners and owner is not None:
                owners[key] = owner
        items.invalidate_recordset(["payload_json"])
        return owners

    @api.model
    def _wac_endpoint_connections(self, endpoint, extra_domain=None):
        return (
            self.env["contact.center.provider.connection"]
            .sudo()
            .search(
                [
                    ("adapter_key", "=", WHATSAPP_CLOUD_ADAPTER_KEY),
                    ("role", "=", "primary"),
                    ("wa_webhook_asset_id.endpoint_id", "=", endpoint.id),
                ]
                + list(extra_domain or [])
            )
        )

    @api.model
    def _wac_routes_for(self, connections, endpoint, payload):
        route = route_contract(payload)
        if not route:
            return connections.browse()
        return connections.filtered(
            lambda row: row.wa_business_account_id == route["waba_id"]
            and row.wa_phone_number_id == route["phone_number_id"]
            and row.company_id == endpoint.company_id
        )

    @api.model
    def _wac_admitted_item_keys(self, endpoint, specs, eligible):
        """Drop ignored conversations before any content is persisted.

        Admission and its inbox fence cover exactly the routes that may claim
        content, primary or not: a demoted number keeps claiming, so its
        ignored conversations must stay unclaimed as well (CC-WAC-09).
        """

        admitted = set(eligible)
        policy = self.env["contact.center.conversation.ignore"]
        connections = self._wac_claimable_connections(endpoint)
        if connections:
            connections._contact_center_lock_ingress_admission()
        for spec in specs:
            if spec["item_key"] not in admitted:
                continue
            routes = self._wac_routes_for(connections, endpoint, spec["payload_json"])
            # An ambiguous route cannot justify suppressing another inbox's data.
            if len(routes) == 1 and policy._ignored_envelope(
                routes, spec["payload_json"]
            ):
                admitted.discard(spec["item_key"])
        return frozenset(admitted)

    @api.model
    def _wac_erased_occurrences(self, company, occurrences):
        """Return the ``(business account, occurrence)`` pairs already erased.

        Erasing a shared item keeps its semantic ``occurrence_ref`` (a digest of
        the phone number ID and ``wamid``, never content), whether or not the
        item had produced an inbox event yet. That erased row is the durable,
        content-free deletion marker: a redelivery in another body carries the
        same occurrence and must never bring the content back (CC-WAC-01).
        Company isolation holds; a later connection of the route sees it too.
        """

        occurrences = {
            (target, occurrence)
            for target, occurrence in occurrences
            if isinstance(occurrence, str)
            and occurrence.startswith(("wac:message:", "wac:status:"))
        }
        if not occurrences:
            return set()
        items = (
            self.env["meta.webhook.item"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("kind", "=", "messaging"),
                    ("object_type", "=", WHATSAPP_OBJECT_TYPE),
                    ("event_field", "=", WHATSAPP_WEBHOOK_FIELD),
                    (
                        "target_asset_id",
                        "in",
                        sorted({target for target, _occurrence in occurrences}),
                    ),
                    (
                        "occurrence_ref",
                        "in",
                        sorted({occurrence for _target, occurrence in occurrences}),
                    ),
                ]
            )
        )
        erased = {
            (item.target_asset_id, item.occurrence_ref)
            for item in items
            if (item.payload_json or {}).get("reason") == "consumer_content_erased"
            and (item.payload_json or {}).get("consumer_key")
            == WHATSAPP_CLOUD_CONSUMER_KEY
        }
        items.invalidate_recordset(["payload_json"])
        return erased & occurrences

    @api.model
    def _wac_unerased_item_keys(self, endpoint, specs, eligible):
        """Never reclaim content a conversation deletion erased (CC-WAC-01).

        It runs after the admission fence: a deletion committed earlier is seen
        here, and the redelivered row stays a content-free placeholder with no
        private link captured. A deletion racing this ingestion is caught again
        at dispatch, before any inbox event exists.
        """

        if not eligible:
            return frozenset()
        candidates = [spec for spec in specs if spec["item_key"] in eligible]
        erased = self._wac_erased_occurrences(
            endpoint.company_id,
            [(spec["target_asset_id"], spec["occurrence_ref"]) for spec in candidates],
        )
        return frozenset(
            spec["item_key"]
            for spec in candidates
            if (spec["target_asset_id"], spec["occurrence_ref"]) not in erased
        )

    @api.model
    def _wac_capture_referrals(self, endpoint, decoded_envelope, specs):
        """Keep ad URLs in private stores; the shared item keeps only references.

        The public permalink goes to the module's referral link store (R17); a
        CDN thumbnail goes to the core thumbnail locator. Both only for an
        admitted, unambiguous route whose event is a paid ad click.
        """

        candidates = [
            spec
            for spec in specs
            if spec["payload_json"].get("collection") == "messages"
            and isinstance(
                (spec["payload_json"].get("message") or {}).get("referral"), dict
            )
        ]
        if not candidates:
            return
        connections = self._wac_endpoint_connections(
            endpoint,
            [
                ("company_id", "=", endpoint.company_id.id),
                ("inbound_active", "=", True),
                ("account_id.active", "=", True),
            ],
        )
        link_model = self.env["contact.center.whatsapp.cloud.referral.link"].sudo()
        locator_model = self.env["contact.center.ad.preview.locator"].sudo()
        for spec in candidates:
            payload = spec["payload_json"]
            routed = self._wac_routes_for(connections, endpoint, payload).filtered(
                lambda connection: connection._wac_runtime_topology_is_ready(
                    require_subscription=False
                )
            )
            if len(routed) != 1:
                continue
            connection = routed.ensure_one()
            try:
                event = connection.get_adapter().normalize_event(connection, payload)
            except (AdapterError, DTOValidationError):
                continue
            if not event.message or not any(
                attribution.touchpoint_type == "paid_ad_click"
                for attribution in event.attribution
            ):
                continue
            row = raw_row(decoded_envelope, payload)
            raw_referral = row.get("referral") if isinstance(row, dict) else None
            if not isinstance(raw_referral, dict):
                continue
            source_key = event.message.external_message_id
            referral = payload["message"]["referral"]
            reference = link_model._register_source_link(
                connection, source_key, raw_referral.get("source_url")
            )
            if reference:
                referral["source_link_ref"] = reference
            for field_name in ("thumbnail_url", "image_url"):
                private_url = thumbnail_url(raw_referral.get(field_name))
                if not private_url:
                    continue
                thumbnail_ref = locator_model._register_thumbnail_locator(
                    connection, source_key, private_url
                )
                if thumbnail_ref:
                    referral["thumbnail_ref"] = thumbnail_ref
                break

    # -- Dispatch ----------------------------------------------------------------

    @api.model
    def _dispatch_consumer(self, dispatch):
        result = super()._dispatch_consumer(dispatch)
        if result is not None or dispatch.consumer_key != WHATSAPP_CLOUD_CONSUMER_KEY:
            return result
        payload = dispatch.item_id.payload_json or {}
        if payload.get("reason") == "consumer_content_erased":
            return {"handled": True, "result_ref": "contact.center.content_erased"}
        contract = route_contract(payload)
        if not contract:
            return None
        asset = self._wac_routing_asset(dispatch, contract)
        if not asset or not self._wac_item_matches_route(dispatch, contract, asset):
            return None
        item = dispatch.item_id
        if self._wac_erased_occurrences(
            item.company_id, [(item.target_asset_id, item.occurrence_ref)]
        ):
            # A claim that raced a deletion: erase it now instead of projecting
            # content the deletion already removed (CC-WAC-01).
            self._wac_erase_items(item)
            return {"handled": True, "result_ref": "contact.center.content_erased"}
        connection = self._wac_route_connection(dispatch, contract, asset)
        if not connection:
            return None
        if contract["collection"] == "errors":
            failure = self._wac_record_webhook_error(dispatch, connection, payload)
            return {"handled": True, "result_ref": "wac-error:%s" % failure.id}
        if (
            contract["collection"] == "statuses"
            and (payload.get("status") or {}).get("status") == "failed"
        ):
            failure = self._wac_record_failed_status(dispatch, connection, payload)
            return {"handled": True, "result_ref": "wac-failure:%s" % failure.id}
        if contract["collection"] == "statuses":
            reaction = self._wac_reaction_of_status(connection, payload)
            if reaction:
                # A status of our own reaction updates no message.
                return {"handled": True, "result_ref": "wac-reaction:%s" % reaction.id}
        inbox = self._wac_find_or_create_inbox(
            dispatch,
            connection,
            block_if_new=not connection.account_id._contact_center_access_is_ready(),
        )
        if not inbox:
            return None
        return {
            "handled": True,
            "result_ref": "contact.center.inbox.event:%s" % inbox.id,
        }

    @api.model
    def _wac_reaction_of_status(self, connection, payload):
        status = payload.get("status") or {}
        wamid, by_callback = status_correlation_id(status)
        if by_callback:
            return self.env["contact.center.outbox.command"].browse()
        return self.env["contact.center.outbox.command"]._wac_sent_reaction(
            connection, wamid
        )

    @api.model
    def _wac_routing_asset(self, dispatch, contract):
        assets = dispatch.page_id.asset_ids.filtered(
            lambda asset: asset.active
            and asset.external_asset_id == contract["waba_id"]
            and asset.object_type == WHATSAPP_ASSET_CONTRACT["object_type"]
            and asset.platform == WHATSAPP_ASSET_CONTRACT["platform"]
            and asset.transport == WHATSAPP_ASSET_CONTRACT["transport"]
        )
        if len(assets) > 1:
            raise ValidationError(_("The shared WhatsApp asset route is ambiguous."))
        return assets

    @api.model
    def _wac_item_matches_route(self, dispatch, contract, asset):
        item = dispatch.item_id
        page = dispatch.page_id
        return bool(
            item.kind == "messaging"
            and item.object_type == WHATSAPP_OBJECT_TYPE
            and item.event_field == WHATSAPP_WEBHOOK_FIELD
            and item.target_asset_id == contract["waba_id"]
            and page.owner_kind == WHATSAPP_OWNER_KIND
            and page.endpoint_id == item.delivery_id.endpoint_id
            and page.app_id == item.delivery_id.app_id
            and page.company_id == item.company_id
            and asset.page_id == page
        )

    @api.model
    def _wac_route_connection(self, dispatch, contract, asset):
        """Return the single live connection of this exact route.

        Every candidate phone number connection is locked with the Meta
        consumer's topology order before the decision. A live connection of
        another WABA or company refuses the item; two live connections are a
        route error; a missing or not yet ready route is retried (R04). Only
        the inbox that owns the claim can receive it: once the number serves
        another inbox, the claim is unrouted there (CC-WAC-13).
        """

        connection_model = (
            self.env["contact.center.provider.connection"]
            .sudo()
            .with_context(active_test=False)
        )
        candidates = connection_model.search(
            [
                ("adapter_key", "=", WHATSAPP_CLOUD_ADAPTER_KEY),
                ("wa_phone_number_id", "=", contract["phone_number_id"]),
            ],
            order="id",
        )
        connection_model._contact_center_lock_topology(
            candidates.mapped("account_id").ids
        )
        candidates = candidates.exists()
        if candidates:
            candidates.invalidate_recordset(_ROUTE_FIELDS)
            accounts = candidates.mapped("account_id")
            accounts.invalidate_recordset(
                ["active", "access_user_ids", "access_team_ids"]
            )
            accounts.mapped("access_team_ids").invalidate_recordset(
                ["active", "agent_ids", "supervisor_ids"]
            )
        live = candidates.filtered(
            lambda connection: connection.active
            and connection.account_id.active
            and connection.role == "primary"
            and connection.inbound_active
        )
        owner = claim_account_id(dispatch.item_id.payload_json)
        configured = candidates.filtered(
            lambda connection: connection.company_id == dispatch.company_id
            and connection.wa_webhook_asset_id == asset
            and connection.wa_business_account_id == contract["waba_id"]
            and (owner is None or connection.account_id.id == owner)
        )
        foreign = live.filtered(
            lambda connection: connection.company_id != dispatch.company_id
            or connection.wa_webhook_asset_id != asset
            or connection.wa_business_account_id != contract["waba_id"]
        )
        if foreign:
            # The phone number belongs to another business account or company:
            # never deliver there, and never retry into it.
            return connection_model.browse()
        if len(live) > 1:
            raise ValidationError(
                _("More than one Contact Center connection claims this WhatsApp route.")
            )
        if not configured:
            # No connection of this route exists (anymore): there is nothing to
            # wait for, so the item is unrouted instead of retried (L11-ROUTE-01).
            return connection_model.browse()
        if live and live not in configured:
            # The number moved to another inbox: this claim stays its own
            # inbox's and never reaches the new one.
            return connection_model.browse()
        if not live or not live._wac_inbound_route_is_ready():
            # The authenticated ledger proves the event exists; a route that is
            # not live yet delays projection instead of dropping evidence. No
            # fixed delay: queue_job applies the channel retry pattern.
            raise _WhatsAppCloudRouteNotReady(
                "Contact Center WhatsApp Cloud route is not ready"
            )
        return live

    @api.model
    def _wac_private_envelope(self, connection, payload):
        """Resolve the private permalink into the inbox envelope only (MC-04).

        The link row is found on any connection of the dispatching
        connection's stable route, as a replacement may dispatch (CC-WAC-17).
        """

        envelope = copy.deepcopy(payload)
        message = envelope.get("message")
        referral = message.get("referral") if isinstance(message, dict) else None
        if not isinstance(referral, dict):
            return "", envelope
        reference = referral.pop("source_link_ref", "")
        if reference:
            url = self.env[
                "contact.center.whatsapp.cloud.referral.link"
            ]._resolve_source_link(reference, connection, message.get("id"))
            if url:
                referral["source_url"] = url
        return reference, envelope

    @api.model
    def _wac_rebind_thumbnail(self, connection, envelope):
        """Give the preview a thumbnail reference of the dispatching connection.

        The core locator is keyed by the connection that received the message,
        and the preview worker resolves it with the touchpoint's connection:
        the one dispatching. When a replacement of the same stable route (same
        inbox, business account and number) dispatches, the original locator's
        CDN URL is registered again for it and the envelope carries the new
        reference; the caller consumes the original once the inbox event is
        durable. A locator outside that route, missing or spent yields no
        thumbnail (CC-WAC-17). Returns the original locator to consume.
        """

        locators = self.env["contact.center.ad.preview.locator"].sudo()
        message = envelope.get("message")
        referral = message.get("referral") if isinstance(message, dict) else None
        reference = (
            referral.get("thumbnail_ref") if isinstance(referral, dict) else None
        )
        if not isinstance(reference, str) or not reference:
            return locators.browse()
        source_key = message.get("id")
        origin = locators.search(
            [
                ("reference", "=", reference),
                ("source_key", "=", source_key),
                (
                    "connection_id",
                    "in",
                    connection._wac_stable_route_connections().ids,
                ),
            ],
            limit=1,
        )
        if origin and origin.connection_id == connection:
            return locators.browse()
        rebound = ""
        if (
            origin
            and not origin.consumed
            and origin.download_url
            and origin.expires_at > fields.Datetime.now()
        ):
            rebound = locators._register_thumbnail_locator(
                connection, source_key, origin.download_url
            )
        if rebound:
            referral["thumbnail_ref"] = rebound
            return origin
        referral.pop("thumbnail_ref", None)
        return locators.browse()

    @api.model
    def _wac_discard_link(self, connection, payload, reference):
        if reference:
            self.env[
                "contact.center.whatsapp.cloud.referral.link"
            ]._discard_source_link(
                reference, connection, (payload.get("message") or {}).get("id")
            )

    @api.model
    def _wac_find_or_create_inbox(self, dispatch, connection, *, block_if_new=False):
        item = dispatch.item_id
        payload = item.payload_json
        dedupe_key = inbox_dedupe_key(payload)
        if not dedupe_key:
            return self.env["contact.center.inbox.event"].browse()
        inbox_model = self.env["contact.center.inbox.event"].sudo()
        # The core key is per connection; a replaced connection of the same
        # stable route keeps the receipt, erased or not (CC-WAC-02).
        domain = [
            (
                "provider_connection_id",
                "in",
                connection._wac_stable_route_connections().ids,
            ),
            ("inbox_dedupe_key", "=", dedupe_key),
        ]
        reference, envelope = self._wac_private_envelope(connection, payload)
        inbox = inbox_model.search(domain, limit=1)
        if inbox:
            return self._wac_reuse_inbox(inbox, item, connection, payload, reference)
        origin_locator = self._wac_rebind_thumbnail(connection, envelope)
        metadata = {
            "meta_delivery_ref": item.delivery_id.public_ref,
            "meta_item_id": item.id,
            "meta_item_key": item.item_key,
            "meta_dispatch_id": dispatch.id,
            "object": item.object_type,
            "platform": "whatsapp",
            "graph_version": connection.sudo().wa_graph_version,
            "event_sha256": item.event_sha256,
            "raw_envelope_sanitized": True,
            "private_referral_link": bool(
                ((envelope.get("message") or {}).get("referral") or {}).get(
                    "source_url"
                )
            ),
            "technical_ledger": "meta_webhook_base",
        }
        values = {
            "provider_connection_id": connection.id,
            "inbox_dedupe_key": dedupe_key,
            "provider_schema_version": WHATSAPP_CLOUD_PROVIDER_SCHEMA_VERSION,
            "raw_envelope_json": envelope,
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
                inbox._enqueue()
        except IntegrityError:
            inbox = inbox_model.search(domain, limit=1)
            if not inbox:
                raise
            return self._wac_reuse_inbox(inbox, item, connection, payload, reference)
        # The inbox envelope is durable in this transaction: drop the link row
        # and a thumbnail locator the envelope no longer refers to.
        self._wac_discard_link(connection, payload, reference)
        if origin_locator:
            origin_locator._consume()
        return inbox

    @api.model
    def _wac_reuse_inbox(self, inbox, item, connection, payload, reference):
        """A redelivery reuses the inbox and never resurrects erased content."""

        self._wac_discard_link(connection, payload, reference)
        if (inbox.metadata_json or {}).get("content_erased"):
            self._wac_erase_items(item)
        return inbox

    @api.model
    def _wac_erase_items(self, items):
        """Erase this consumer's items, judging each once by all its owners.

        The shared core erases an item only for an exclusive consumer: its
        dispatches plus the subscriptions it finds. Every owner must be known
        before anything is erased (CC-WAC-10), and who can still receive an
        item depends on its delivery:

        - a ``dispatched`` delivery is final (it is never fanned out or
          requeued again): an item dispatched there reaches only its
          dispatches, and the core also counts the active subscriptions;
        - any other item may still be fanned out, or requeued, to every
          subscription, an archived one included, since reactivating it
          meanwhile makes it an owner. Those are judged with the archived
          subscriptions too. Claiming required this consumer's subscription,
          which the core only archives, so a Cloud-only item is erased whatever
          the subscription state now is (CC-WAC-06).

        Anything another consumer could own stays untouched. Returns the items
        erased.
        """

        erased = self.env["meta.webhook.item"]
        for company in items.mapped("company_id"):
            owned = (
                items.filtered(lambda item, company=company: item.company_id == company)
                .sudo()
                .with_context(
                    allowed_company_ids=company.ids,
                    meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN,
                )
            )
            settled = owned.filtered(
                lambda item: item.delivery_id.state == "dispatched"
                and item.dispatch_ids
            )
            if settled:
                erased |= settled._erase_consumer_message_content(
                    WHATSAPP_CLOUD_CONSUMER_KEY
                )
            open_items = owned - settled
            if open_items:
                erased |= open_items.with_context(
                    active_test=False
                )._erase_consumer_message_content(WHATSAPP_CLOUD_CONSUMER_KEY)
        return erased

    @api.model
    def _wac_occurred_at(self, value, fallback):
        # The sanitizer bounds provider timestamps to 13 digits: a seconds value
        # below 10**11 is a valid UTC date, anything larger falls back.
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 < value < 100_000_000_000
        ):
            return datetime.datetime.fromtimestamp(
                value, tz=datetime.timezone.utc
            ).replace(tzinfo=None)
        return fallback or fields.Datetime.now()

    @api.model
    def _wac_record_webhook_error(self, dispatch, connection, payload):
        item = dispatch.item_id
        error = payload.get("error") or {}
        code = error.get("code")
        return self.env[
            "contact.center.whatsapp.cloud.delivery.failure"
        ]._record_occurrence(
            {
                "kind": "webhook_error",
                "connection_id": connection.id,
                "code": code if isinstance(code, int) else 0,
                "title": str(error.get("title") or "")[:256] or False,
                "occurred_at": self._wac_occurred_at(
                    (payload.get("entry") or {}).get("time"),
                    item.delivery_id.create_date,
                ),
                "meta_delivery_ref": item.delivery_id.public_ref,
                "occurrence_sha256": error_occurrence_digest(
                    connection, item.delivery_id, item.item_key
                ),
            }
        )

    @api.model
    def _wac_record_failed_status(self, dispatch, connection, payload):
        """``failed`` is not a core state: record it locally, message stays sent.

        The status still proves provider acceptance: it correlates through the
        same locked path as the other statuses, so a send left uncertain by a
        timeout is reconciled even when ``failed`` is its only status.
        """

        item = dispatch.item_id
        status = payload.get("status") or {}
        wamid = status.get("id")
        errors = status.get("errors") or []
        first = errors[0] if errors and isinstance(errors[0], dict) else {}
        occurred_at = self._wac_occurred_at(
            status.get("timestamp"), item.delivery_id.create_date
        )
        try:
            binding = self.env[
                "contact.center.application"
            ]._wac_correlate_failed_status(connection, payload, occurred_at=occurred_at)
        except TransientAdapterError as error:
            # The send worker still owns its provider boundary.
            raise RetryableJobError(
                "WhatsApp failed status is waiting for dispatch finalization"
            ) from error
        code = first.get("code")
        return self.env[
            "contact.center.whatsapp.cloud.delivery.failure"
        ]._record_occurrence(
            {
                "kind": "status_failed",
                "connection_id": connection.id,
                "code": code if isinstance(code, int) else 0,
                "title": str(first.get("title") or "")[:256] or False,
                "occurred_at": occurred_at,
                "wamid": wamid,
                "message_binding_id": binding.id,
                "meta_delivery_ref": item.delivery_id.public_ref,
                "occurrence_sha256": occurrence_digest(payload),
            },
            stable_route=True,
        )


class ContactCenterWhatsAppCloudConversationDeletion(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    def _prepare_conversation_deletion_dependencies(
        self, channel, bindings, messages, inbox_events
    ):
        """Erase only this contact's WhatsApp items, keeping dedupe hashes.

        Every selected item is this consumer's own claim (WhatsApp object and
        field, a route of the inbox) made for this inbox: after a number moved
        to another inbox, that inbox's claims, pending or dispatched, are never
        selected here (CC-WAC-13). The deletion fails instead of succeeding
        while any of them keeps its content (CC-WAC-06).
        """

        result = super()._prepare_conversation_deletion_dependencies(
            channel, bindings, messages, inbox_events
        )
        connections = (
            self.env["contact.center.provider.connection"]
            .sudo()
            .with_context(active_test=False)
            .search(
                [
                    ("account_id", "in", bindings.mapped("account_id").ids),
                    ("adapter_key", "=", WHATSAPP_CLOUD_ADAPTER_KEY),
                ]
            )
        )
        if not connections:
            return result
        policy = self.env["contact.center.conversation.ignore"]
        account_ids = set(bindings.mapped("account_id").ids)
        refs = set(bindings.mapped("conversation_ref"))
        keys = {
            (alias.namespace, alias.value_normalized) for alias in bindings.alias_ids
        }
        keys.update(
            (alias.namespace, alias.value_normalized)
            for alias in bindings.mapped("identity_id").alias_ids
            if alias.account_id in bindings.mapped("account_id")
        )
        item_model = self.env["meta.webhook.item"].sudo()
        domain = [
            ("company_id", "=", channel.contact_center_company_id.id),
            ("kind", "=", "messaging"),
            ("object_type", "=", WHATSAPP_OBJECT_TYPE),
            ("event_field", "=", WHATSAPP_WEBHOOK_FIELD),
            (
                "target_asset_id",
                "in",
                sorted(set(connections.mapped("wa_business_account_id")) - {False}),
            ),
        ]
        selected = item_model.browse()
        source_keys = set()
        last_id = 0
        while True:
            batch = item_model.search(
                domain + [("id", ">", last_id)], order="id", limit=500
            )
            if not batch:
                break
            for item in batch:
                owner = claim_account_id(item.payload_json)
                if owner is not None and owner not in account_ids:
                    continue
                for connection in connections:
                    route = policy._route(connection, item.payload_json)
                    if route and (
                        route["conversation_ref"] in refs
                        or keys.intersection(
                            tuple(value) for value in route.get("addresses", ())
                        )
                    ):
                        selected |= item
                        message = (item.payload_json or {}).get("message") or {}
                        source_keys.add(message.get("id"))
                        break
            last_id = batch[-1].id
            batch.invalidate_recordset(["payload_json"])
        if selected:
            erased = self.env["meta.webhook.dispatcher"]._wac_erase_items(selected)
            if selected - erased:
                raise ValidationError(
                    _(
                        "Part of this conversation's WhatsApp content is shared "
                        "with another webhook consumer and cannot be erased; the "
                        "conversation was not deleted."
                    )
                )
        message_bindings = (
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("channel_binding_id", "in", bindings.ids)])
        )
        source_keys.update(message_bindings.mapped("external_message_id"))
        self.env["contact.center.whatsapp.cloud.referral.link"]._purge_source_keys(
            connections, source_keys
        )
        self._wac_consume_thumbnail_locators(connections, source_keys)
        return result

    def _wac_consume_thumbnail_locators(self, connections, source_keys):
        """Drop the private CDN URL of this conversation's ad thumbnails.

        A locator is registered at ingestion, long before a touchpoint exists,
        so the core's preview hook cannot reach it while the item waits for
        dispatch or its inbox event for the worker (CC-WAC-18). Only this
        inbox's connections count, replaced ones and a rebound locator
        (CC-WAC-17) included, and only this conversation's message IDs: other
        contacts and other inboxes keep theirs. ``_consume`` keeps the row as a
        content-free marker.
        """

        source_keys = sorted({key for key in source_keys if key})
        if not connections or not source_keys:
            return True
        self.env["contact.center.ad.preview.locator"].sudo().search(
            [
                ("connection_id", "in", connections.ids),
                ("source_key", "in", source_keys),
                ("consumed", "=", False),
            ]
        )._consume()
        return True
