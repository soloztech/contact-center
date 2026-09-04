from pathlib import Path

from .common import MetaCase


class TestContactCenterMetaArchitectureContract(MetaCase):
    _FORBIDDEN_LOCAL_MODELS = (
        "contact.center.meta.app",
        "contact.center.meta.authorization",
        "contact.center.meta.webhook.delivery",
        "contact.center.meta.webhook.item",
    )
    _FORBIDDEN_LOCAL_XMLIDS = (
        "contact_center_meta.action_contact_center_meta_apps",
        "contact_center_meta.menu_contact_center_meta_apps",
        "contact_center_meta.action_contact_center_meta_deliveries",
        "contact_center_meta.menu_contact_center_meta_deliveries",
    )
    _FORBIDDEN_PROVIDER_FIELDS = (
        "meta_authorization_id",
        "meta_app_id",
    )
    _SECRET_VALUE_FIELDS = (
        "app_secret",
        "webhook_verify_token",
        "access_token",
    )
    _FORBIDDEN_GREENFIELD_PATHS = (
        "controllers/webhook.py",
        "data/health_cron.xml",
        "data/queue_job.xml",
        "migrations",
        "models/delivery.py",
        "models/health.py",
        "models/meta_app.py",
        "security/contact_center_meta_security.xml",
        "security/ir.model.access.csv",
        "services/graph.py",
        "services/health.py",
        "services/webhook.py",
        "tests/test_upgrade_migration.py",
        "views/meta_delivery_views.xml",
    )
    _FORBIDDEN_PRODUCTION_TOKENS = (
        "/contact-center/webhook/meta/",
        "contact.center.meta.app",
        "contact.center.meta.authorization",
        "contact.center.meta.webhook.delivery",
        "contact.center.meta.webhook.item",
        "meta_authorization_id",
        "meta_app_id",
        "action_contact_center_meta_deliveries",
        "menu_contact_center_meta_deliveries",
    )

    def test_only_shared_meta_models_own_app_page_and_delivery_evidence(self):
        for model_name in self._FORBIDDEN_LOCAL_MODELS:
            self.assertNotIn(model_name, self.env.registry.models)

        self.assertEqual(self.app._name, "meta.api.app")
        self.assertEqual(self.endpoint._name, "meta.webhook.endpoint")
        self.assertEqual(self.page._name, "meta.webhook.page")
        delivery = self.create_delivery(
            {
                "object": "page",
                "entry": [
                    {
                        "id": self.ACTIVE_PAGE_ID,
                        "time": 1_800_000_000,
                        "messaging": [
                            {
                                "sender": {"id": "900000000000001"},
                                "recipient": {"id": self.ACTIVE_PAGE_ID},
                                "timestamp": 1_800_000_000_000,
                                "message": {
                                    "mid": "m_greenfield_architecture",
                                    "text": "greenfield",
                                },
                            }
                        ],
                    }
                ],
            }
        )
        self.assertEqual(delivery._name, "meta.webhook.delivery")
        self.assertEqual(delivery.item_ids._name, "meta.webhook.item")
        self.assertEqual(len(delivery.item_ids), 1)

    def test_provider_connection_contains_only_asset_binding_and_projections(self):
        model = self.env["contact.center.provider.connection"]
        for field_name in self._FORBIDDEN_PROVIDER_FIELDS:
            self.assertNotIn(field_name, model._fields)
        projection_fields = (
            "meta_webhook_asset_id",
            "meta_webhook_page_id",
            "meta_api_app_id",
            "meta_transport_mode",
            "meta_target_asset_id",
        )
        for field_name in projection_fields:
            self.assertIn(field_name, model._fields)
        for field_name in projection_fields[1:]:
            self.assertTrue(model._fields[field_name].compute)
            self.assertTrue(model._fields[field_name].readonly)

        self.assertEqual(self.connection.meta_webhook_asset_id, self.page_asset)
        self.assertEqual(self.connection.meta_webhook_page_id, self.page)
        self.assertEqual(self.connection.meta_api_app_id, self.app)
        self.assertEqual(self.connection.meta_transport_mode, "messenger_page")
        self.assertEqual(self.connection.meta_target_asset_id, self.ACTIVE_PAGE_ID)

    def test_duplicate_controller_route_actions_and_menus_do_not_exist(self):
        addon_root = Path(__file__).resolve().parents[1]
        duplicate_controller = addon_root / "controllers" / "webhook.py"
        self.assertFalse(duplicate_controller.exists())
        controller_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (addon_root / "controllers").glob("*.py")
        )
        self.assertNotIn("/contact-center/webhook/meta/", controller_sources)

        for xmlid in self._FORBIDDEN_LOCAL_XMLIDS:
            self.assertFalse(self.env.ref(xmlid, raise_if_not_found=False))
        self.assertFalse((addon_root / "views" / "meta_delivery_views.xml").exists())

    def test_greenfield_tree_has_no_upgrade_or_retired_runtime_surface(self):
        addon_root = Path(__file__).resolve().parents[1]
        for relative_path in self._FORBIDDEN_GREENFIELD_PATHS:
            with self.subTest(path=relative_path):
                self.assertFalse((addon_root / relative_path).exists())

        production_sources = []
        for path in addon_root.rglob("*"):
            if not path.is_file() or path.suffix not in {".csv", ".py", ".xml"}:
                continue
            if "tests" in path.relative_to(addon_root).parts:
                continue
            production_sources.append(path.read_text(encoding="utf-8"))
        joined_sources = "\n".join(production_sources)
        for token in self._FORBIDDEN_PRODUCTION_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, joined_sources)

    def test_addon_persists_secret_references_never_secret_values(self):
        provider = self.env["contact.center.provider.connection"]
        locator = self.env["contact.center.meta.media.locator"]
        for field_name in self._SECRET_VALUE_FIELDS:
            self.assertNotIn(field_name, provider._fields)
            self.assertNotIn(field_name, locator._fields)
        secret_fields = (
            self.env["ir.model.fields"]
            .sudo()
            .search([("name", "in", self._SECRET_VALUE_FIELDS)])
        )
        # ``ir.model.fields.modules`` is computed and intentionally has no
        # search method in Odoo 16. Filter it in memory so a rejected domain
        # cannot silently broaden this assertion to every installed addon.
        addon_secret_fields = secret_fields.filtered(
            lambda field: "contact_center_meta"
            in {
                module.strip()
                for module in (field.modules or "").split(",")
                if module.strip()
            }
        )
        self.assertFalse(addon_secret_fields)

        self.assertEqual(self.app.app_secret_ref, self.APP_SECRET_REF)
        self.assertEqual(self.endpoint.verify_token_ref, self.VERIFY_TOKEN_REF)
        self.assertEqual(self.page.access_token_ref, self.PAGE_TOKEN_REF)
        persisted_references = {
            self.app.app_secret_ref,
            self.endpoint.verify_token_ref,
            self.page.access_token_ref,
        }
        for secret in (self.APP_SECRET, self.VERIFY_TOKEN, self.PAGE_TOKEN):
            self.assertNotIn(secret, persisted_references)
