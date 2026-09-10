"""Prepare one authorized direct conversation without sending a message."""

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
from ..services.dto import SCHEMA_VERSION, ActorDTO, DirectAddressResult
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
            .browse(self._positive_id(account_id, _("caixa de saída")))
            .exists()
        )
        if not account:
            raise ValidationError(_("A caixa de saída não está disponível."))
        if account.company_id not in self.env.companies:
            raise AccessError(_("A empresa da caixa não está ativa nesta sessão."))
        account.check_access_rights("read")
        account.check_access_rule("read")
        # Record rules alone are insufficient for administrators/sudo callers.
        account._contact_center_check_user_scope()
        if not account.active:
            raise ValidationError(_("A caixa de saída está desativada."))
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
        connections = self.env["contact.center.provider.connection"].search(
            [
                ("account_id", "=", account.id),
                ("company_id", "=", account.company_id.id),
                ("active", "=", True),
                ("role", "=", "primary"),
                ("outbound_active", "=", True),
            ],
            limit=2,
        )
        if len(connections) == 1:
            connection = connections
            try:
                adapter = connection.get_adapter()
            except ValidationError:
                if required:
                    raise UserError(
                        _(
                            "O provedor desta caixa está indisponível. Selecione outra caixa."
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
                    "Esta caixa não está pronta para iniciar conversa por telefone. "
                    "Selecione uma caixa WhatsApp conectada ou peça ao supervisor "
                    "para verificar a conexão."
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
            raise ValidationError(_("Informe o número do cliente, não o da caixa."))
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
                raise ValidationError(_("Informe o número do cliente, não o da caixa."))

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
                    "Este contato está ignorado nesta caixa. Peça ao supervisor "
                    "para revisar a regra antes de iniciar a conversa."
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
                _("O WhatsApp limitou as consultas. Aguarde e tente novamente.")
            ) from error
        except ProviderPausedError as error:
            raise UserError(
                _("A conexão precisa de atenção. Peça ao supervisor para verificá-la.")
            ) from error
        except TransientAdapterError as error:
            raise UserError(
                _("Não foi possível consultar o WhatsApp agora. Tente novamente.")
            ) from error
        except AdapterError as error:
            raise UserError(
                _("O WhatsApp não confirmou um destinatário válido para este número.")
            ) from error
        if not isinstance(resolved, DirectAddressResult):
            raise ValidationError(_("A conexão retornou um destinatário inválido."))
        if resolved.state == "not_registered":
            raise UserError(_("Este número não está cadastrado no WhatsApp."))
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
                            "A caixa mudou durante a consulta. Confira e tente novamente."
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
                    raise UserError(_("Esta conversa está ignorada nesta caixa."))
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
                    "Este número tem identificações conflitantes. Peça ao supervisor "
                    "para revisar os vínculos; nenhuma conversa foi criada."
                )
            ) from error
