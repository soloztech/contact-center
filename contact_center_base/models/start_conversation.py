"""Prepare one authorized direct conversation without sending a message."""

# Direct-conversation admission is a separate UI API concern from retention and
# its policy lifecycle; the Odoo registry composes these functional fragments.
# pylint: disable=consider-merging-classes-inherited

import json

from odoo import _, api, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.adapter import (
    AdapterError,
    ProviderPausedError,
    ProviderRateLimitError,
    TransientAdapterError,
    conversation_capabilities,
)
from ..services.dto import (
    SCHEMA_VERSION,
    ActorDTO,
    DirectAddressResult,
    DTOValidationError,
)
from ..services.phone import normalize_start_phone
from .application import IdentityConflictError


class ContactCenterStartConversation(models.AbstractModel):
    _inherit = "contact.center.ui.api"

    @api.model
    def _start_account(self, account_id):
        self._application()._check_agent()
        account = (
            self.env["contact.center.account"]
            .with_context(active_test=False)
            .browse(self._positive_id(account_id, _("outgoing inbox")))
            .exists()
        )
        if not account:
            raise ValidationError(_("The outgoing inbox is unavailable."))
        if account.company_id not in self.env.companies:
            raise AccessError(_("The inbox company is not active in this session."))
        account.check_access_rights("read")
        account.check_access_rule("read")
        # Record rules alone are insufficient for administrators/sudo callers.
        account._contact_center_check_user_scope()
        if not account.active:
            raise ValidationError(_("The outgoing inbox is inactive."))
        return account

    @api.model
    def _start_phone_country(self, account):
        country = account.company_id.country_id or self.env.ref("base.br")
        return {
            "code": country.code,
            "calling_code": country.phone_code,
            "name": country.name,
        }

    @api.model
    def _start_connection(self, account, required=True):
        # Phone addressing is offered only for WhatsApp. Do not instantiate
        # unrelated channel adapters while bootstrapping the shared inbox.
        connection_model = self.env["contact.center.provider.connection"]
        connections = (
            connection_model.search(
                [
                    ("account_id", "=", account.id),
                    ("company_id", "=", account.company_id.id),
                    ("active", "=", True),
                    ("role", "=", "primary"),
                    ("outbound_active", "=", True),
                ],
                limit=2,
            )
            if account.platform == "whatsapp"
            else connection_model.browse()
        )
        if len(connections) == 1:
            connection = connections
            try:
                adapter = connection.get_adapter()
            except ValidationError:
                if required:
                    raise UserError(
                        _(
                            "The provider for this inbox is unavailable. Select another inbox."
                        )
                    ) from None
                return connections.browse()
            capabilities = conversation_capabilities(
                connection.capabilities_json or {}, "direct"
            )
            if (
                capabilities.get("send_message")
                and adapter.supports_direct_conversation_start(connection)
                and connection._contact_center_outbound_is_available()
                and adapter.is_provider_read_ready(connection, "direct_start")
            ):
                return connection
        if required:
            raise UserError(
                _(
                    "This inbox is not ready to start a conversation by phone. Select "
                    "a connected WhatsApp inbox or ask a supervisor to check the "
                    "connection."
                )
            )
        return connections.browse()

    @api.model
    def _start_configuration(self, account, connection):
        """Pin the provider configuration, not volatile health observations."""
        connection = connection.sudo()
        return (
            account.id,
            account.company_id.id,
            account.own_external_identity,
            self._start_phone_country(account)["code"],
            connection.id,
            connection.account_id.id,
            connection.company_id.id,
            connection.adapter_key,
            connection.external_ref,
            connection.provider_schema_version,
            connection.health_configuration_revision,
            json.dumps(connection.capabilities_json or {}, sort_keys=True),
        )

    @api.model
    def _start_normalized_phone(self, account, phone):
        country = self._start_phone_country(account)
        try:
            normalized = normalize_start_phone(phone, country_code=country["code"])
        except ValueError as error:
            raise ValidationError(str(error)) from error
        return {
            "schema_version": SCHEMA_VERSION,
            "normalized_phone": normalized,
            "formatted_phone": "+" + normalized,
            "country_code": country["code"],
        }

    @api.model
    def normalize_start_phone(self, account_id, phone):
        """Read-only preview: no provider lookup and no business records."""
        account = self._start_account(account_id)
        self._start_connection(account)
        return self._start_normalized_phone(account, phone)

    @api.model
    def bootstrap(self):
        result = super().bootstrap()
        for item in result["accounts"]:
            account = self._start_account(item["id"])
            item["can_start_conversation"] = bool(
                self._start_connection(account, required=False)
            )
            item["start_phone_country"] = self._start_phone_country(account)
        result["capabilities"]["start_conversation"] = any(
            item["can_start_conversation"] for item in result["accounts"]
        )
        return result

    @api.model
    def _start_check_own_address(self, account, phone, addresses=()):
        if account.platform != "whatsapp":
            return
        own = (account.own_external_identity or "").strip()
        if not own:
            return
        own_display = account._contact_center_display_address()
        if own_display == "+" + phone:
            raise ValidationError(_("Enter the customer number, not the inbox number."))
        own_local, separator, own_domain = own.partition("@")
        own_local = own_local.split(":", 1)[0].lstrip("+")
        own_domain = own_domain if separator else "s.whatsapp.net"
        if own_domain == "c.us":
            own_domain = "s.whatsapp.net"
        own_key = own_local + "@" + own_domain
        for address in addresses:
            if address.namespace in ("whatsapp.pn", "whatsapp.lid") and (
                address.value_normalized == own_key
            ):
                raise ValidationError(
                    _("Enter the customer number, not the inbox number.")
                )

    @api.model
    def _start_check_ignored(self, account, reference, addresses):
        route = {
            "conversation_type": "direct",
            "conversation_ref": reference,
            "addresses": [
                (address.namespace, address.value_normalized) for address in addresses
            ],
        }
        if self.env["contact.center.conversation.ignore"]._matches_route(
            account, route
        ):
            raise UserError(
                _(
                    "This contact is ignored in this inbox. Ask a supervisor to review"
                    " the rule before starting the conversation."
                )
            )

    @api.model
    def start_conversation(self, account_id, phone):
        account = self._start_account(account_id)
        connection = self._start_connection(account)
        normalized = self._start_normalized_phone(account, phone)["normalized_phone"]
        self._start_check_own_address(account, normalized)
        adapter = connection.get_adapter()
        configuration = self._start_configuration(account, connection)
        # Network I/O happens only after authorization and before any row lock.
        # Credentials never cross this adapter boundary into the UI response.
        try:
            resolved = adapter.resolve_direct_address(connection.sudo(), normalized)
        except ProviderRateLimitError as error:
            raise UserError(
                _("WhatsApp limited the requests. Wait and try again.")
            ) from error
        except ProviderPausedError as error:
            raise UserError(
                _("The connection needs attention. Ask a supervisor to check it.")
            ) from error
        except TransientAdapterError as error:
            raise UserError(
                _("WhatsApp could not be reached right now. Try again.")
            ) from error
        except (AdapterError, DTOValidationError) as error:
            raise UserError(
                _("WhatsApp did not confirm a valid recipient for this number.")
            ) from error
        if not isinstance(resolved, DirectAddressResult):
            raise ValidationError(_("The connection returned an invalid recipient."))
        if resolved.state == "not_registered":
            raise UserError(_("This number is not registered on WhatsApp."))
        application = self._application()
        try:
            # A caller catching UserError must not accidentally commit a partial
            # guest, alias, merged identity, channel or Kanban case.
            with self.env.cr.savepoint():
                application._lock_inbound_account_scope(account)
                connections = self.env[
                    "contact.center.provider.connection"
                ]._contact_center_lock_operational_admission(account.ids)
                account.invalidate_recordset()
                connections.invalidate_recordset()
                current_account = self._start_account(account.id)
                current_connection = self._start_connection(current_account)
                if configuration != self._start_configuration(
                    current_account, current_connection
                ):
                    raise UserError(
                        _(
                            "The inbox changed during the lookup. Check it and try again."
                        )
                    )
                self._start_check_own_address(
                    current_account, normalized, resolved.addresses
                )
                self._start_check_ignored(
                    current_account, resolved.conversation_ref, resolved.addresses
                )
                identity = application._resolve_identity(
                    current_account, ActorDTO(addresses=resolved.addresses)
                )
                existing = (
                    self.env["contact.center.channel.binding"]
                    .sudo()
                    .search(
                        [
                            ("account_id", "=", current_account.id),
                            ("identity_id", "=", identity.id),
                            ("conversation_type", "=", "direct"),
                            ("active", "=", True),
                            ("merged_into_id", "=", False),
                        ],
                        limit=1,
                    )
                )
                binding = application._resolve_channel(
                    current_account,
                    identity,
                    conversation_ref=resolved.conversation_ref,
                )
                application._lock_inbound_projection_binding(binding, current_account)
                if self.env["contact.center.conversation.ignore"]._for_binding(binding):
                    raise UserError(_("This conversation is ignored in this inbox."))
                application._enrich_channel_aliases(binding, resolved.addresses)
                channel, member = self._authorized_channel(binding.channel_id.id)
                if not existing:
                    binding._request_identity_avatar_sync(current_connection)
                    application._notify_ui(channel, "conversation_updated", {})
                return {
                    "schema_version": SCHEMA_VERSION,
                    "channel_id": channel.id,
                    "created": not bool(existing),
                    "normalized_phone": normalized,
                    "item": self._serialize_conversation(channel, member=member),
                }
        except IdentityConflictError as error:
            raise UserError(
                _(
                    "This number has conflicting identities. Ask a supervisor to "
                    "review the links; no conversation was created."
                )
            ) from error
