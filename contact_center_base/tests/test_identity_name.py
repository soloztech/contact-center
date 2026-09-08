import uuid
from unittest import mock

from odoo import fields
from odoo.tests.common import SavepointCase

from ..services.adapter import ProviderAdapter, adapter_registry
from ..services.dto import ActorDTO, AddressDTO, EventDTO


@adapter_registry.register("test.identity.name")
class IdentityNameAdapter(ProviderAdapter):
    display_name = "Test Identity Name"

    def authenticate_webhook(self, connection, headers, body):
        return True

    def normalize_event(self, connection, envelope):
        return EventDTO.from_dict(envelope)

    def execute_command(self, connection, command):
        raise NotImplementedError

    def prepare_request_snapshot(self, connection, command):
        raise NotImplementedError

    def get_capabilities(self, connection):
        return {"send_message": False}

    def get_health(self, connection):
        return {"state": "connected"}


class TestIdentityNameConvergence(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Identity Name Agent",
                    "login": "cc-identity-name-%s" % uuid.uuid4(),
                    "email": "cc-identity-name@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, agent_group.ids)],
                }
            )
        )
        cls.team = cls.env["contact.center.team"].create(
            {
                "name": "Identity Name Team",
                "company_id": cls.env.company.id,
                "agent_ids": [(6, 0, cls.agent.ids)],
            }
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "Identity Name Account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "external_ref": "identity-name-account-%s" % uuid.uuid4(),
                "access_team_ids": [(6, 0, cls.team.ids)],
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "Identity Name Connection",
                "account_id": cls.account.id,
                "adapter_key": "test.identity.name",
                "external_ref": "identity-name-connection-%s" % uuid.uuid4(),
                "provider_schema_version": "identity-name-v1",
                "state": "connected",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": False,
                "capabilities_json": {"send_message": False},
            }
        )

    def _event(
        self,
        jid,
        display_name,
        occurred_at,
        *,
        event_id=None,
        message_id=None,
        direction="inbound",
        is_from_me=False,
    ):
        return EventDTO.from_dict(
            {
                "schema_version": 1,
                "provider_schema_version": self.connection.provider_schema_version,
                "event_id": event_id or "identity-name-%s" % uuid.uuid4(),
                "event_type": "message.created",
                "occurred_at": occurred_at,
                "account_ref": self.account.external_ref,
                "connection_ref": self.connection.external_ref,
                "conversation_ref": jid,
                "platform": self.account.platform,
                "direction": direction,
                "is_from_me": is_from_me,
                "origin": "external_device" if is_from_me else "provider",
                "actor": {
                    "display_name": display_name,
                    "addresses": (
                        []
                        if is_from_me
                        else [
                            {
                                "namespace": "whatsapp.pn",
                                "value": jid,
                                "value_normalized": jid,
                                "role": "sender",
                                "confidence": "protocol",
                            }
                        ]
                    ),
                },
                "conversation": {
                    "conversation_type": "direct",
                    "addresses": [
                        {
                            "namespace": "whatsapp.pn",
                            "value": jid,
                            "value_normalized": jid,
                            "role": "primary",
                            "confidence": "protocol",
                        }
                    ],
                },
                "message": {
                    "external_message_id": message_id
                    or "identity-message-%s" % uuid.uuid4(),
                    "content_type": "text",
                    "text": "Identity name observation",
                },
            }
        )

    def _inbox(self, event, state="done"):
        return (
            self.env["contact.center.inbox.event"]
            .sudo()
            .create(
                {
                    "provider_connection_id": self.connection.id,
                    "inbox_dedupe_key": "identity-name:%s" % uuid.uuid4(),
                    "provider_schema_version": self.connection.provider_schema_version,
                    "raw_envelope_json": {"fixture": "identity_name"},
                    "normalized_dto_json": event.to_dict(),
                    "state": state,
                    "processed_at": fields.Datetime.now() if state == "done" else False,
                }
            )
        )

    def _process(self, event):
        inbox = self._inbox(event)
        message = self.env["contact.center.application"]._process_event(
            self.connection, event, inbox_event=inbox
        )
        return inbox, message

    def _identity_for(self, jid):
        alias = (
            self.env["contact.center.identity.alias"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("namespace", "=", "whatsapp.pn"),
                    ("value_normalized", "=", jid),
                ],
                limit=1,
            )
        )
        return alias.identity_id

    def _actor(self, lid, pn=None, display_name=""):
        addresses = [
            AddressDTO(
                namespace="whatsapp.lid",
                value=lid,
                value_normalized=lid,
                role="primary",
                confidence="protocol",
            )
        ]
        if pn:
            addresses.append(
                AddressDTO(
                    namespace="whatsapp.pn",
                    value=pn,
                    value_normalized=pn,
                    role="alternate",
                    confidence="protocol",
                    resolution_scope="company",
                )
            )
        return ActorDTO(addresses=tuple(addresses), display_name=display_name)

    def _resolve_actor(self, actor):
        return self.env["contact.center.application"]._resolve_identity(
            self.account,
            actor,
        )

    def _direct_channel(self, identity, conversation_ref):
        channel = self.env["mail.channel"]._contact_center_create_channel(
            account=self.account,
            identity=identity,
            teams=self.team,
            partner_ids=self.agent.partner_id.ids,
            guest_ids=identity.mail_guest_id.ids,
        )
        self.env["contact.center.channel.binding"].sudo().create(
            {
                "channel_id": channel.id,
                "account_id": self.account.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": conversation_ref,
            }
        )
        return channel

    def test_new_lid_identity_prefers_trusted_formatted_pn_fallback(self):
        lid = "700000000000474@lid"
        pn = "5511900000474@s.whatsapp.net"

        identity = self._resolve_actor(self._actor(lid, pn))

        self.assertEqual(identity.name, "+55 11 90000-0474")
        self.assertEqual(identity.name_source, "fallback")
        self.assertEqual(identity.mail_guest_id.name, "+55 11 90000-0474")
        self.assertEqual(
            set(identity.alias_ids.mapped(lambda alias: alias.value_normalized)),
            {lid, pn},
        )

        public_identity_model = self.env["contact.center.identity"].with_user(
            self.env.ref("base.public_user")
        )
        public_guest = self.env["mail.guest"].sudo().create({"name": "Public Guest"})
        managed = public_identity_model._contact_center_create_managed(
            {
                "name": "Public Fallback",
                "company_id": self.env.company.id,
                "mail_guest_id": public_guest.id,
            }
        )
        self.assertTrue(managed.env.su)
        managed.write({"name": "Public Manual Name"})
        self.assertEqual(managed.name_source, "manual")

    def test_name_provenance_is_mandatory_and_new_rows_never_need_inference(self):
        field = self.env["contact.center.identity"]._fields["name_source"]
        self.assertTrue(field.required)
        self.assertTrue(callable(field.default))
        self.assertEqual(
            field.default(self.env["contact.center.identity"]),
            "manual",
        )

        guest = self.env["mail.guest"].sudo().create({"name": "Manual identity"})
        identity = (
            self.env["contact.center.identity"]
            .sudo()
            .create(
                {
                    "name": "Manual identity",
                    "company_id": self.env.company.id,
                    "mail_guest_id": guest.id,
                }
            )
        )

        self.assertEqual(identity.name_source, "manual")
        self.assertFalse(
            hasattr(identity, "_contact_center_infer_name_source"),
            "greenfield runtime must not retain pre-provenance inference",
        )

    def test_first_mutation_name_is_not_replaced_by_phone_fallback(self):
        lid = "700000000000475@lid"
        pn = "5511900000475@s.whatsapp.net"
        observed_name = "Nome observado na mutação"
        event = self._event(
            pn,
            observed_name,
            "2026-08-29T12:00:00Z",
        )
        inbox = self._inbox(event)

        identity = self.env["contact.center.application"]._resolve_identity(
            self.account,
            self._actor(lid, pn, display_name=observed_name),
            inbox_event=inbox,
        )

        self.assertEqual(identity.name, observed_name)
        self.assertEqual(identity.name_source, "provider")
        self.assertEqual(identity.mail_guest_id.name, observed_name)
        self.assertEqual(identity.observed_name, observed_name)
        self.assertEqual(
            identity.observed_name_at,
            identity._contact_center_name_datetime(inbox.create_date),
        )
        self.assertEqual(identity.observed_name_inbox_event_id, inbox)

    def test_existing_lid_fallback_converges_when_trusted_pn_arrives(self):
        lid = "700000000000835@lid"
        pn = "5511900000835@s.whatsapp.net"
        identity = self._resolve_actor(self._actor(lid))
        channel = self._direct_channel(identity, lid)
        self.assertEqual(identity.name, lid)

        resolved = self._resolve_actor(self._actor(lid, pn))

        identity.invalidate_recordset()
        channel.invalidate_recordset(["name"])
        self.assertEqual(resolved, identity)
        self.assertEqual(identity.name, "+55 11 90000-0835")
        self.assertEqual(identity.name_source, "fallback")
        self.assertEqual(identity.mail_guest_id.name, "+55 11 90000-0835")
        self.assertEqual(channel.name, "+55 11 90000-0835")

        resolved = self._resolve_actor(self._actor(lid))

        identity.invalidate_recordset()
        channel.invalidate_recordset(["name"])
        self.assertEqual(resolved, identity)
        self.assertEqual(identity.name, "+55 11 90000-0835")
        self.assertEqual(identity.mail_guest_id.name, "+55 11 90000-0835")
        self.assertEqual(channel.name, "+55 11 90000-0835")

    def test_pn_fallback_never_overwrites_owned_identity_names(self):
        cases = ("provider", "manual", "partner")
        for index, ownership in enumerate(cases, start=1):
            with self.subTest(ownership=ownership):
                lid = "90000000000000%s@lid" % index
                pn = "55119876543%02d@s.whatsapp.net" % index
                identity = self._resolve_actor(self._actor(lid))
                channel = self._direct_channel(identity, lid)
                if ownership == "provider":
                    identity._contact_center_observe_name(
                        "Nome do Provider",
                        fields.Datetime.to_datetime("2026-08-28 12:00:00"),
                        False,
                    )
                    expected_name = "Nome do Provider"
                elif ownership == "manual":
                    identity.sudo().write({"name": "Nome Manual"})
                    expected_name = "Nome Manual"
                else:
                    partner = self.env["res.partner"].create(
                        {"name": "Contato Vinculado"}
                    )
                    identity.with_user(self.agent).action_link_partner(partner.id)
                    expected_name = lid

                self._resolve_actor(self._actor(lid, pn))

                identity.invalidate_recordset()
                channel.invalidate_recordset(["name"])
                self.assertEqual(identity.name, expected_name)
                self.assertEqual(identity.mail_guest_id.name, expected_name)
                self.assertEqual(channel.name, expected_name)
                if ownership == "partner":
                    self.assertEqual(identity.partner_id, partner)

    def test_phone_fallback_uses_existing_alias_without_replay(self):
        lid = "700000000001474@lid"
        pn = "5511900001474@s.whatsapp.net"
        identity = self._resolve_actor(self._actor(lid))
        channel = self._direct_channel(identity, lid)
        self.env["contact.center.identity.alias"].sudo().create(
            {
                "identity_id": identity.id,
                "account_id": self.account.id,
                "namespace": "whatsapp.pn",
                "value_raw": pn,
                "value_normalized": pn,
                "role": "alternate",
                "confidence": "protocol",
                "resolution_scope": "company",
            }
        )
        counts_before = {
            model: self.env[model].sudo().search_count([])
            for model in (
                "mail.guest",
                "mail.channel",
                "contact.center.inbox.event",
            )
        }

        updated = identity._contact_center_sync_fallback_name_from_addresses(
            identity.alias_ids
        )

        identity.invalidate_recordset()
        channel.invalidate_recordset(["name"])
        self.assertTrue(updated)
        self.assertEqual(identity.name, "+55 11 90000-1474")
        self.assertEqual(identity.name_source, "fallback")
        self.assertEqual(identity.mail_guest_id.name, "+55 11 90000-1474")
        self.assertEqual(channel.name, "+55 11 90000-1474")
        self.assertEqual(
            counts_before,
            {model: self.env[model].sudo().search_count([]) for model in counts_before},
        )

    def test_missing_then_named_event_converges_and_notifies(self):
        jid = "5511900000001@s.whatsapp.net"
        first, _message = self._process(self._event(jid, "", "2026-08-24T10:00:00Z"))
        identity = self._identity_for(jid)
        binding = identity.channel_binding_ids
        self.assertEqual(identity.name, "+55 11 90000-0001")
        self.assertEqual(identity.name_source, "fallback")
        self.assertFalse(identity.observed_name)

        application_class = type(self.env["contact.center.application"])
        with mock.patch.object(
            application_class,
            "_notify_ui",
            autospec=True,
        ) as notify:
            second, _message = self._process(
                self._event(jid, "Pessoa Observada", "2026-08-24T10:01:00Z")
            )

        identity.invalidate_recordset()
        binding.invalidate_recordset()
        self.assertEqual(identity.name, "Pessoa Observada")
        self.assertEqual(identity.name_source, "provider")
        self.assertEqual(identity.mail_guest_id.name, "Pessoa Observada")
        self.assertEqual(binding.channel_id.name, "Pessoa Observada")
        self.assertEqual(identity.observed_name, "Pessoa Observada")
        self.assertEqual(identity.observed_name_inbox_event_id, second)
        self.assertTrue(
            any(
                call.args[2] == "conversation_updated"
                and call.args[1] == binding.channel_id
                for call in notify.call_args_list
            )
        )
        self.assertNotEqual(first, second)

    def test_observed_name_order_uses_occurred_at_then_inbox_id(self):
        jid = "5511900000002@s.whatsapp.net"
        newest, _message = self._process(
            self._event(jid, "Nome Novo", "2026-08-24T12:00:00Z")
        )
        identity = self._identity_for(jid)

        older, _message = self._process(
            self._event(jid, "Nome Antigo", "2026-08-24T11:59:59Z")
        )
        identity.invalidate_recordset()
        self.assertEqual(identity.name, "Nome Novo")
        self.assertEqual(identity.observed_name_inbox_event_id, newest)

        tied, _message = self._process(
            self._event(jid, "Nome do Desempate", "2026-08-24T12:00:00Z")
        )
        identity.invalidate_recordset()
        self.assertEqual(identity.name, "Nome do Desempate")
        self.assertEqual(identity.observed_name_inbox_event_id, tied)
        self.assertGreater(tied.id, newest.id)
        self.assertGreater(older.id, newest.id)

    def test_late_mutation_name_uses_provider_time_not_inbox_arrival(self):
        jid = "5511900000012@s.whatsapp.net"
        target_external_id = "identity-name-mutation-order-target"
        newest, _message = self._process(
            self._event(
                jid,
                "Nome Atual",
                "2026-08-24T12:00:00Z",
                message_id=target_external_id,
            )
        )
        identity = self._identity_for(jid)

        values = self._event(
            jid,
            "Nome Antigo da Mutação",
            "2026-08-24T11:59:59Z",
        ).to_dict()
        values.update(
            {
                "event_id": "identity-name-late-mutation-%s" % uuid.uuid4(),
                "event_type": "message.reaction",
                "message": None,
                "mutation": {
                    "type": "react",
                    "target_external_message_id": target_external_id,
                    "emoji": "👍",
                    "operation": "add",
                },
            }
        )
        late, _message = self._process(EventDTO.from_dict(values))

        identity.invalidate_recordset()
        self.assertEqual(identity.name, "Nome Atual")
        self.assertEqual(identity.observed_name, "Nome Atual")
        self.assertEqual(identity.observed_name_inbox_event_id, newest)
        self.assertGreater(late.id, newest.id)

    def test_invalid_and_alias_names_never_replace_fallback(self):
        for index in (10, 11):
            jid = "55119000000%s@s.whatsapp.net" % index
            display_name = "." if index == 10 else jid
            self._process(self._event(jid, display_name, "2026-08-24T13:00:00Z"))
            identity = self._identity_for(jid)
            expected_fallback = "+55 11 90000-00%s" % index
            self.assertEqual(identity.name, expected_fallback)
            self.assertEqual(identity.name_source, "fallback")
            self.assertFalse(identity.observed_name)

    def test_manual_identity_and_channel_names_are_preserved(self):
        manual_jid = "5511900000003@s.whatsapp.net"
        self._process(self._event(manual_jid, "Nome Inicial", "2026-08-24T14:00:00Z"))
        manual_identity = self._identity_for(manual_jid)
        manual_channel = manual_identity.channel_binding_ids.channel_id
        manual_identity.sudo().write({"name": "Nome Escolhido"})
        self._process(
            self._event(manual_jid, "Nome do Provider", "2026-08-24T14:01:00Z")
        )
        manual_identity.invalidate_recordset()
        self.assertEqual(manual_identity.name_source, "manual")
        self.assertEqual(manual_identity.name, "Nome Escolhido")
        self.assertEqual(manual_identity.mail_guest_id.name, "Nome Escolhido")
        self.assertEqual(manual_channel.name, "Nome Escolhido")
        self.assertEqual(manual_identity.observed_name, "Nome do Provider")

        channel_jid = "5511900000004@s.whatsapp.net"
        self._process(self._event(channel_jid, "Canal Inicial", "2026-08-24T14:02:00Z"))
        channel_identity = self._identity_for(channel_jid)
        channel = channel_identity.channel_binding_ids.channel_id
        channel.sudo().write({"name": "Canal Escolhido"})
        self._process(
            self._event(channel_jid, "Identidade Nova", "2026-08-24T14:03:00Z")
        )
        channel_identity.invalidate_recordset()
        channel.invalidate_recordset()
        self.assertEqual(channel_identity.name, "Identidade Nova")
        self.assertEqual(channel_identity.mail_guest_id.name, "Identidade Nova")
        self.assertEqual(channel.name, "Canal Escolhido")

    def test_partner_name_is_never_replaced(self):
        jid = "5511900000005@s.whatsapp.net"
        self._process(self._event(jid, "Guest Inicial", "2026-08-24T15:00:00Z"))
        identity = self._identity_for(jid)
        partner = self.env["res.partner"].create({"name": "Contato Canônico"})
        identity.with_user(self.agent).action_link_partner(partner.id)

        self._process(self._event(jid, "PushName Novo", "2026-08-24T15:01:00Z"))
        identity.invalidate_recordset()
        partner.invalidate_recordset()
        self.assertEqual(partner.name, "Contato Canônico")
        self.assertEqual(identity.partner_id, partner)
        self.assertEqual(identity.name, "Guest Inicial")
        self.assertEqual(identity.mail_guest_id.name, "Guest Inicial")
        self.assertEqual(identity.observed_name, "PushName Novo")
