import ast
from pathlib import Path

import requests
import urllib3

from odoo.exceptions import AccessError, ValidationError
from odoo.tools import mute_logger

from odoo.addons.contact_center_base.services.adapter import adapter_registry
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.contracts import WHATSAPP_CLOUD_CONSUMER_KEY
from .common import UnpatchedNetworkError, WhatsAppCloudCase

_MODULE = "contact_center_whatsapp_cloud"
_FAILURE_MODEL = "contact.center.whatsapp.cloud.delivery.failure"
_LINK_MODEL = "contact.center.whatsapp.cloud.referral.link"


class TestWhatsAppCloudArchitectureContract(WhatsAppCloudCase):
    _FORBIDDEN_LOCAL_MODELS = (
        "contact.center.whatsapp.cloud.app",
        "contact.center.whatsapp.cloud.webhook.delivery",
        "contact.center.whatsapp.cloud.webhook.item",
        "contact.center.whatsapp.cloud.media.locator",
    )
    _FORBIDDEN_PATHS = (
        "controllers",
        "migrations",
        "data/health_cron.xml",
        "data/queue_job.xml",
        "models/delivery.py",
        "models/meta_app.py",
        "services/graph.py",
        "services/webhook.py",
        "tests/test_upgrade_migration.py",
    )
    _ALLOWED_SECURITY_FILES = {
        "ir.model.access.csv",
        "delivery_failure_security.xml",
    }
    _FORBIDDEN_PRODUCTION_TOKENS = (
        "contact.center.meta.media.locator",
        "http.route",
        "/whatsapp/webhook",
        "whatsapp_access_token",
    )
    _SECRET_FIELD_PARTS = ("token", "secret", "password", "apikey", "api_key")

    @classmethod
    def _addon_root(cls):
        return Path(__file__).resolve().parents[1]

    def test_manifest_depends_only_on_the_shared_cores(self):
        manifest = ast.literal_eval(
            (self._addon_root() / "__manifest__.py").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["depends"],
            ["contact_center_base", "meta_api_base", "meta_webhook_base"],
        )
        self.assertEqual(manifest["version"], "16.0.1.0.0")
        self.assertEqual(adapter_registry.owner("whatsapp_cloud"), _MODULE)

    def test_no_controller_migration_or_duplicate_ledger(self):
        root = self._addon_root()
        for relative_path in self._FORBIDDEN_PATHS:
            with self.subTest(path=relative_path):
                self.assertFalse((root / relative_path).exists())
        for model_name in self._FORBIDDEN_LOCAL_MODELS:
            self.assertNotIn(model_name, self.env.registry.models)
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in {".py", ".xml", ".csv"}
            and "tests" not in path.relative_to(root).parts
        )
        for token in self._FORBIDDEN_PRODUCTION_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, sources)

    def test_security_exists_only_for_the_failure_model(self):
        security = self._addon_root() / "security"
        self.assertEqual(
            {path.name for path in security.iterdir()}, self._ALLOWED_SECURITY_FILES
        )
        module_access = (
            self.env["ir.model.data"]
            .sudo()
            .search([("module", "=", _MODULE), ("model", "=", "ir.model.access")])
        )
        accesses = (
            self.env["ir.model.access"].sudo().browse(module_access.mapped("res_id"))
        )
        self.assertEqual(len(accesses), 1)
        self.assertEqual(accesses.model_id.model, _FAILURE_MODEL)
        self.assertEqual(
            accesses.group_id,
            self.env.ref("contact_center_base.group_contact_center_admin"),
        )
        self.assertEqual(
            (
                accesses.perm_read,
                accesses.perm_write,
                accesses.perm_create,
                accesses.perm_unlink,
            ),
            (True, False, False, False),
        )
        self.assertFalse(
            self.env["ir.model.access"]
            .sudo()
            .search([("model_id.model", "=", _LINK_MODEL)])
        )
        module_rules = (
            self.env["ir.model.data"]
            .sudo()
            .search([("module", "=", _MODULE), ("model", "=", "ir.rule")])
        )
        rule = self.env["ir.rule"].sudo().browse(module_rules.mapped("res_id"))
        self.assertEqual(rule.model_id.model, _FAILURE_MODEL)
        self.assertIn("company_ids", rule.domain_force)

    def test_addon_persists_secret_references_never_secret_values(self):
        field_data = (
            self.env["ir.model.data"]
            .sudo()
            .search([("module", "=", _MODULE), ("model", "=", "ir.model.fields")])
        )
        module_fields = (
            self.env["ir.model.fields"].sudo().browse(field_data.mapped("res_id"))
        )
        self.assertIn("wa_phone_number_id", module_fields.mapped("name"))
        for field in module_fields:
            with self.subTest(field=field.name):
                self.assertFalse(
                    any(part in field.name for part in self._SECRET_FIELD_PARTS)
                )
        connection_fields = [
            name
            for name in module_fields.filtered(
                lambda field: field.model == "contact.center.provider.connection"
            ).mapped("name")
        ]
        serialized = str(self.connection.sudo().read(connection_fields))
        for secret in (self.WABA_TOKEN, self.APP_SECRET, self.VERIFY_TOKEN):
            self.assertNotIn(secret, serialized)
        self.assertEqual(self.waba.sudo().access_token_ref, self.WABA_TOKEN_REF)

    def test_unpatched_network_io_fails_the_test(self):
        with self.assertRaises(UnpatchedNetworkError):
            requests.get("https://graph.facebook.com/v26.0/me", timeout=1)
        pool = urllib3.HTTPSConnectionPool("scontent.whatsapp.net", port=443)
        with self.assertRaises(UnpatchedNetworkError):
            pool.urlopen("GET", "/")

    # -- Connection configuration contract ----------------------------------------

    def test_connection_requires_a_whatsapp_inbox_and_business_account(self):
        messenger = self._create_account(team=self.team, platform="messenger")
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._create_connection(
                messenger, self.waba_asset, self.OTHER_PHONE_ID, active=False
            )
        page_app = self.env["meta.webhook.page"].create(
            {
                "name": "Página comum",
                "endpoint_id": self.endpoint.id,
                "external_page_id": "100000000000931",
                "credential_backend": "environment",
                "access_token_ref": self.WABA_TOKEN_REF,
            }
        )
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._create_connection(
                self._create_account(team=self.team),
                page_app.asset_ids,
                self.OTHER_PHONE_ID,
                active=False,
            )
        for values in (
            {"wa_phone_number_id": "12ab"},
            {"wa_graph_version": "v26"},
            {"wa_phone_number_id": False},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValidationError), self.env.cr.savepoint():
                    self._create_connection(
                        self._create_account(team=self.team),
                        self.waba_asset,
                        active=False,
                        **values,
                    )

    def test_route_is_immutable_and_settings_belong_to_cloud_connections(self):
        with self.assertRaises(ValidationError):
            self.connection.write({"wa_phone_number_id": self.OTHER_PHONE_ID})
        other_owner = self._create_waba(self.endpoint, self.OTHER_WABA_ID)
        with self.assertRaises(ValidationError):
            self.connection.write({"wa_webhook_asset_id": other_owner.asset_ids.id})
        self.connection.write({"wa_graph_version": "v25.0"})
        self.assertEqual(
            self.connection.capabilities_json["extensions"]["provider.whatsapp_cloud"][
                "graph_api_version"
            ],
            "v25.0",
        )
        with self.assertRaises(ValidationError):
            self.connection.write({"wa_graph_version": "latest"})
        with self.assertRaises(ValidationError):
            self.env["contact.center.provider.connection"].create(
                {
                    "name": "Outro provedor",
                    "account_id": self._create_account(team=self.team).id,
                    "adapter_key": "whatsapp_cloud",
                    "role": "standby",
                }
            )

    def test_configure_webhook_is_idempotent_and_system_only(self):
        subscriptions = self.env["meta.webhook.subscription"].with_context(
            active_test=False
        )
        self.subscription.write({"active": False})
        self.assertTrue(self.connection.action_whatsapp_cloud_configure_webhook())
        self.assertTrue(self.connection.action_whatsapp_cloud_configure_webhook())
        rows = subscriptions.search(
            [
                ("page_id", "=", self.waba.id),
                ("consumer_key", "=", WHATSAPP_CLOUD_CONSUMER_KEY),
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows.active)
        self.assertEqual(self.waba.subscription_state, "manual")
        with self.assertRaises(AccessError):
            self.connection.with_user(
                self.admin
            ).action_whatsapp_cloud_configure_webhook()

    def test_retiring_the_last_route_archives_only_this_consumer(self):
        foreign = self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.waba.id,
                "consumer_key": "another.consumer",
                "object_type": "whatsapp_business_account",
                "field_name": "messages",
            }
        )
        with trap_jobs(), mute_logger("odoo.models"):
            self.connection.write({"outbound_active": False})
            self.connection.write({"active": False})
        self.assertFalse(self.subscription.active)
        self.assertTrue(foreign.active)

    def test_readiness_does_not_depend_on_page_reconciliation(self):
        self.assertEqual(self.waba.subscription_state, "manual")
        self.assertFalse(self.waba.verified_at)
        self.assertTrue(self.connection._wac_inbound_route_is_ready())
        self.subscription.write({"active": False})
        self.assertFalse(self.connection._wac_inbound_route_is_ready())
        self.assertTrue(
            self.connection._wac_runtime_topology_is_ready(require_subscription=False)
        )
