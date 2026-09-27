import importlib

from odoo.tests import tagged

from odoo.addons.contact_center_base.services.dto import ActorDTO, AddressDTO

from ..services.identity import phone_address
from .common import WhatsAppCloudCase


# After installation, so that the WuzAPI adapter of the same run is installed.
@tagged("post_install", "-at_install")
class TestWhatsAppCloudIdentity(WhatsAppCloudCase):
    def _identity(self, wamid, connection=None):
        return self.binding_for(wamid, connection).channel_binding_id.identity_id

    def _wuzapi_style_address(self):
        """The exact phone address the WuzAPI adapter emits for one chat."""

        jid = "%s@s.whatsapp.net" % self.CUSTOMER
        module = (
            self.env["ir.module.module"]
            .sudo()
            .search([("name", "=", "contact_center_wuzapi")], limit=1)
        )
        if module.state != "installed":
            self.skipTest("contact_center_wuzapi is not installed")
        wuzapi = importlib.import_module(
            "odoo.addons.contact_center_wuzapi.services.adapter"
        )
        return wuzapi._address(jid, "primary", "event.Info.Chat")

    def test_same_phone_converges_with_the_wuzapi_identity_of_the_company(self):
        wuzapi_address = self._wuzapi_style_address()
        cloud_address = phone_address(
            self.CUSTOMER, role="primary", source_field="message.from"
        )
        for field_name in ("namespace", "value_normalized", "resolution_scope"):
            self.assertEqual(
                getattr(cloud_address, field_name), getattr(wuzapi_address, field_name)
            )
        # Another inbox of the company already knows the phone (WuzAPI path).
        other_account = self._create_account(team=self.team)
        existing = self.env["contact.center.application"]._resolve_identity(
            other_account,
            ActorDTO(
                addresses=(
                    AddressDTO(
                        namespace=wuzapi_address.namespace,
                        value=wuzapi_address.value,
                        value_normalized=wuzapi_address.value_normalized,
                        role="sender",
                        source_field="event.Info.Sender",
                        confidence="protocol",
                        resolution_scope="company",
                    ),
                )
            ),
        )
        message = self.text_message(body="Mesmo telefone, outro transporte")
        self.deliver(self.envelope(self.value(messages=[message])))
        self.assertEqual(self._identity(message["id"]), existing)

    def test_contact_known_only_by_bsuid_is_accepted(self):
        message = self.text_message(body="Sem telefone", sender=False)
        contacts = [self.contact_block(wa_id=False, name="Usuário com nome")]
        _delivery, inbox = self.deliver(
            self.envelope(self.value(messages=[message], contacts=contacts))
        )
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        binding = self.binding_for(message["id"]).channel_binding_id
        self.assertEqual(
            binding.conversation_ref, "%s:%s" % (self.WABA_ID, self.CUSTOMER_BSUID)
        )
        alias = binding.identity_id.alias_ids.ensure_one()
        self.assertEqual(alias.namespace, "whatsapp.bsuid")
        self.assertEqual(alias.value_raw, self.CUSTOMER_BSUID)
        self.assertEqual(alias.resolution_scope, "account")

    def _replace_connection(self, asset, phone_id=None):
        self.connection.write({"active": False})
        connection = self._create_connection(self.account, asset, phone_id)
        connection.action_whatsapp_cloud_configure_webhook()
        return connection

    def test_same_bsuid_in_two_business_accounts_of_one_inbox_stays_separate(self):
        first = self.text_message(body="Pela primeira WABA", sender=False)
        contacts = [self.contact_block(wa_id=False)]
        self.deliver(self.envelope(self.value(messages=[first], contacts=contacts)))
        first_identity = self._identity(first["id"])
        other_owner = self._create_waba(self.endpoint, self.OTHER_WABA_ID)
        other = self._replace_connection(other_owner.asset_ids, self.OTHER_PHONE_ID)
        second = self.text_message(body="Pela segunda WABA", sender=False)
        _delivery, inbox = self.deliver(
            self.envelope(
                self.value(
                    messages=[second], contacts=contacts, phone_id=self.OTHER_PHONE_ID
                ),
                waba_id=self.OTHER_WABA_ID,
            )
        )
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        second_identity = self._identity(second["id"], other)
        self.assertTrue(first_identity and second_identity)
        self.assertNotEqual(first_identity, second_identity)

    def test_connection_replacement_in_the_same_business_account_keeps_identity(self):
        first = self.text_message(body="Antes da troca", sender=False)
        contacts = [self.contact_block(wa_id=False)]
        self.deliver(self.envelope(self.value(messages=[first], contacts=contacts)))
        replacement = self._replace_connection(self.waba_asset)
        second = self.text_message(body="Depois da troca", sender=False)
        _delivery, inbox = self.deliver(
            self.envelope(self.value(messages=[second], contacts=contacts))
        )
        self.assertEqual(inbox.provider_connection_id, replacement)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        self.assertEqual(
            self._identity(second["id"], replacement), self._identity(first["id"])
        )

    def test_phone_and_bsuid_of_one_sender_share_one_identity(self):
        message = self.text_message(body="Com os dois identificadores")
        self.deliver(self.envelope(self.value(messages=[message])))
        identity = self._identity(message["id"])
        self.assertEqual(
            sorted(identity.alias_ids.mapped("namespace")),
            ["whatsapp.bsuid", "whatsapp.pn"],
        )
        self.assertEqual(identity.name, "Cliente Sintético")

    def test_sender_bsuid_is_completed_from_its_contact_block(self):
        message = self.text_message(body="Sem from_user_id", user_id=False)
        _delivery, inbox = self.deliver(self.envelope(self.value(messages=[message])))
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        addresses = {
            (address["namespace"], address["value_normalized"])
            for address in inbox.normalized_dto_json["conversation"]["addresses"]
        }
        self.assertIn(
            ("whatsapp.bsuid", "%s:%s" % (self.WABA_ID, self.CUSTOMER_BSUID)),
            addresses,
        )
