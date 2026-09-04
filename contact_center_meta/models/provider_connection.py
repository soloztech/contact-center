from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..services.contracts import (
    META_PROVIDER_SCHEMA_VERSION,
    META_TRANSPORT_CONTRACTS,
    transport_mode_for_asset,
)

_META_CONFIGURATION_FIELDS = {"meta_webhook_asset_id"}


class ContactCenterProviderConnection(models.Model):
    _inherit = "contact.center.provider.connection"

    meta_transport_mode = fields.Selection(
        [
            ("messenger_page", "Facebook Page / Messenger"),
            ("instagram_page_linked", "Instagram linked to a Page"),
        ],
        string="Meta Channel Mode",
        compute="_compute_meta_route",
        store=True,
        readonly=True,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    meta_target_asset_id = fields.Char(
        related="meta_webhook_asset_id.external_asset_id",
        string="Destination Asset ID",
        store=True,
        readonly=True,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    meta_webhook_page_id = fields.Many2one(
        related="meta_webhook_asset_id.page_id",
        string="Meta Page",
        store=True,
        readonly=True,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )
    meta_api_app_id = fields.Many2one(
        related="meta_webhook_asset_id.page_id.app_id",
        string="Meta App",
        store=True,
        readonly=True,
        index=True,
        groups="contact_center_base.group_contact_center_admin",
    )

    @api.depends(
        "meta_webhook_asset_id",
        "meta_webhook_asset_id.platform",
        "meta_webhook_asset_id.object_type",
        "meta_webhook_asset_id.transport",
    )
    def _compute_meta_route(self):
        for connection in self:
            connection.meta_transport_mode = transport_mode_for_asset(
                connection.meta_webhook_asset_id
            )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for incoming in vals_list:
            values = dict(incoming)
            adapter_key = values.get(
                "adapter_key", self.env.context.get("default_adapter_key")
            )
            if adapter_key == "meta":
                values.setdefault(
                    "provider_schema_version", META_PROVIDER_SCHEMA_VERSION
                )
                if not values.get("meta_webhook_asset_id"):
                    raise ValidationError(
                        _("A Meta connection requires one canonical Meta asset.")
                    )
            elif any(values.get(name) for name in _META_CONFIGURATION_FIELDS):
                raise ValidationError(
                    _("Meta settings can only be stored on a Meta connection.")
                )
            prepared.append(values)
        connections = super().create(prepared)
        connections.filtered(
            lambda connection: connection.adapter_key == "meta"
        ).refresh_capabilities()
        return connections

    def write(self, values):
        values = dict(values)
        meta_connections = self.filtered(
            lambda connection: connection.adapter_key == "meta"
        )
        if any(connection.adapter_key != "meta" for connection in self) and any(
            values.get(name) for name in _META_CONFIGURATION_FIELDS
        ):
            raise ValidationError(
                _("Meta settings can only be stored on a Meta connection.")
            )
        if "meta_webhook_asset_id" in values:
            requested = values.get("meta_webhook_asset_id") or False
            for connection in meta_connections.sudo():
                current = connection.meta_webhook_asset_id.id or False
                if requested != current:
                    raise ValidationError(
                        _(
                            "A Meta route is immutable; archive it and create a "
                            "new connection."
                        )
                    )
        return super().write(values)

    @api.constrains(
        "adapter_key",
        "account_id",
        "company_id",
        "meta_webhook_asset_id",
        "meta_transport_mode",
        "meta_target_asset_id",
    )
    def _check_meta_connection_contract(self):
        for connection in self.filtered(lambda item: item.adapter_key == "meta"):
            asset = connection.sudo().meta_webhook_asset_id
            if not asset:
                raise ValidationError(
                    _("A Meta connection requires one canonical Meta asset.")
                )
            mode = transport_mode_for_asset(asset)
            contract = META_TRANSPORT_CONTRACTS.get(mode)
            if not contract or mode != connection.meta_transport_mode:
                raise ValidationError(_("The selected Meta asset is unsupported."))
            if asset.company_id != connection.company_id:
                raise ValidationError(
                    _("The Meta asset and connection must belong to one company.")
                )
            if connection.account_id.platform != contract["account_platform"]:
                raise ValidationError(
                    _("The Meta asset does not match the account platform.")
                )
            if connection.account_id.own_external_identity != asset.external_asset_id:
                raise ValidationError(
                    _("The Meta asset does not match the account identity.")
                )

    def _meta_inbound_is_ready(self):
        self.ensure_one()
        return self._meta_inbound_route_is_ready()

    def _meta_outbound_is_ready(self):
        self.ensure_one()
        return bool(
            self.adapter_key == "meta"
            and self.active
            and self.account_id.active
            and self.role == "primary"
            and self.inbound_active
            and self.outbound_active
            and not self.identity_mismatch_latched
            and self.state == "connected"
            and self.health_detail in {"healthy", "metadata_limited"}
            and self._contact_center_observation_is_fresh()
            and self._meta_runtime_topology_is_ready(require_subscriptions=False)
        )

    def _contact_center_activation_blockers(self, direction="inbound", now=None):
        blockers = list(super()._contact_center_activation_blockers(direction, now))
        self.ensure_one()
        if self.adapter_key != "meta":
            return blockers
        if not self._meta_runtime_topology_is_ready(
            require_subscriptions=(direction == "inbound")
        ):
            blockers.append(_("Meta App, Page, asset or subscriptions are not ready"))
        if direction == "outbound" and (
            self.state != "connected" or self.identity_mismatch_latched
        ):
            blockers.append(_("Meta credential health is not ready"))
        return blockers


class ContactCenterAccount(models.Model):
    _inherit = "contact.center.account"

    def write(self, values):
        scopes = set()
        lifecycle_changed = "active" in values and any(
            bool(values["active"]) != account.active for account in self
        )
        if lifecycle_changed:
            scopes.update(
                self.sudo()
                .with_context(active_test=False)
                .mapped("connection_ids")
                ._contact_center_meta_subscription_scopes()
            )
        if "own_external_identity" in values:
            candidate = values.get("own_external_identity") or ""
            connections = (
                self.env["contact.center.provider.connection"]
                .sudo()
                .with_context(active_test=False)
                .search(
                    [
                        ("account_id", "in", self.ids),
                        ("adapter_key", "=", "meta"),
                    ]
                )
            )
            for account in self:
                scoped = connections.filtered(
                    lambda connection, account=account: connection.account_id == account
                )
                if scoped and candidate != account.own_external_identity:
                    raise ValidationError(
                        _(
                            "A Meta account identity is immutable after a route "
                            "exists; archive it and create a new account."
                        )
                    )
        result = super().write(values)
        if lifecycle_changed:
            scopes.update(
                self.sudo()
                .with_context(active_test=False)
                .mapped("connection_ids")
                ._contact_center_meta_subscription_scopes()
            )
            self.env[
                "contact.center.provider.connection"
            ]._contact_center_meta_reconcile_subscription_lifecycle(scopes)
        return result
