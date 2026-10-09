import uuid

from odoo.exceptions import ValidationError
from odoo.tests.common import SavepointCase

from odoo.addons.contact_center_base.services.adapter import (
    ProviderAdapter,
    adapter_registry,
)


@adapter_registry.register("test.wuz.copy.guard")
class OtherAdapter(ProviderAdapter):
    display_name = "Synthetic other provider"

    def get_capabilities(self, connection):
        return {}


class TestWuzapiProviderCopyGuard(SavepointCase):
    def _values(self, **values):
        account = self.env["contact.center.account"].create(
            {
                "name": "Synthetic copy guard",
                "platform": "whatsapp",
                "company_id": self.env.company.id,
                "external_ref": uuid.uuid4().hex,
            }
        )
        return {
            "name": "Synthetic connection",
            "account_id": account.id,
            "adapter_key": "test.wuz.copy.guard",
            "external_ref": uuid.uuid4().hex,
            "role": "migration",
            "inbound_active": False,
            "outbound_active": False,
            **values,
        }

    def test_empty_relation_command_and_copy_do_not_configure_another_provider(self):
        connection = self.env["contact.center.provider.connection"].create(
            self._values(wuzapi_webhook_event_ids=[(6, 0, [])])
        )
        copied = connection.copy({"external_ref": uuid.uuid4().hex})
        self.assertEqual(copied.adapter_key, connection.adapter_key)
        self.assertFalse(copied.wuzapi_webhook_event_ids)
        self.assertFalse(copied.wuzapi_api_token)

    def test_real_settings_and_relation_commands_remain_provider_specific(self):
        model = self.env["contact.center.provider.connection"]
        event = self.env["contact.center.wuzapi.webhook.event"].search([], limit=1)
        self.assertTrue(event)
        for values in (
            {"wuzapi_api_token": "synthetic-secret"},
            {"wuzapi_base_url": "https://synthetic.invalid"},
            {"wuzapi_webhook_event_ids": [(6, 0, event.ids)]},
            {"wuzapi_webhook_event_ids": [(4, event.id)]},
        ):
            with self.subTest(fields=list(values)), self.assertRaises(
                ValidationError
            ), self.cr.savepoint():
                model.create(self._values(**values))
        connection = model.create(self._values())
        with self.assertRaises(ValidationError), self.cr.savepoint():
            connection.write({"wuzapi_api_token": "synthetic-secret"})

    def test_filled_wuzapi_configuration_cannot_move_to_another_adapter(self):
        connection = self.env["contact.center.provider.connection"].create(
            self._values(
                adapter_key="wuzapi",
                wuzapi_base_url="https://synthetic.invalid",
                wuzapi_api_token="synthetic-secret",
                wuzapi_hmac_secret="synthetic-hmac-secret-of-at-least-32-characters",
            )
        )
        with self.assertRaises(ValidationError), self.cr.savepoint():
            connection.write({"adapter_key": "test.wuz.copy.guard"})
