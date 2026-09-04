import json
import os
import uuid

from odoo.tests.common import SavepointCase


class WuzapiCase(SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.agent = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "WuzAPI Test Agent",
                    "login": "wuzapi-test-agent-%s" % uuid.uuid4(),
                    "email": "wuzapi-test-agent@example.invalid",
                    "company_id": cls.env.company.id,
                    "company_ids": [(6, 0, cls.env.company.ids)],
                    "groups_id": [(6, 0, agent_group.ids)],
                }
            )
        )
        cls.account = cls.env["contact.center.account"].create(
            {
                "name": "WuzAPI test account",
                "company_id": cls.env.company.id,
                "platform": "whatsapp",
                "owner_user_id": cls.agent.id,
            }
        )
        cls.connection = cls.env["contact.center.provider.connection"].create(
            {
                "name": "WuzAPI test connection",
                "account_id": cls.account.id,
                "adapter_key": "wuzapi",
                "active": True,
                "role": "primary",
                "inbound_active": True,
                "outbound_active": False,
                "wuzapi_base_url": "https://wuzapi.invalid/",
                "wuzapi_api_token": "not-a-real-api-token",
                "wuzapi_hmac_secret": "unit-test-hmac-secret-at-least-32-chars",
            }
        )

    @classmethod
    def update_config(cls, **values):
        cls.connection.write(values)
        return cls.connection

    @classmethod
    def load_fixture(cls, filename):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", filename)
        with open(fixture_path, encoding="utf-8") as fixture_file:
            return json.load(fixture_file)
