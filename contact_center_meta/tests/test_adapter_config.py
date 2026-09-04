from unittest import mock

from lxml import etree
from psycopg2 import IntegrityError

from odoo.exceptions import ValidationError

from odoo.addons.contact_center_base.services.adapter import (
    AdapterError,
    adapter_registry,
)

from .common import MetaCase


def _valid_debug(case, *, profile_id=None, app_id=None, scopes=None):
    return {
        "data": {
            "is_valid": True,
            "app_id": app_id or case.app.external_app_id,
            "profile_id": profile_id or case.page.external_page_id,
            "type": "PAGE",
            "scopes": scopes or ["pages_manage_metadata", "pages_messaging"],
        }
    }


class TestMetaAdapterConfig(MetaCase):
    @mock.patch("odoo.addons.contact_center_meta.services.adapter.graph_debug_token")
    def test_adapter_is_registered_and_uses_the_canonical_meta_core(self, debug):
        debug.return_value = _valid_debug(self)
        choices = dict(
            self.env["contact.center.provider.connection"]._selection_adapter_key()
        )
        self.assertEqual(choices["meta"], "Meta")
        self.assertEqual(adapter_registry.owner("meta"), "contact_center_meta")

        adapter = self.connection.get_adapter()
        capabilities = adapter.get_capabilities(self.connection)
        self.assertEqual(self.connection.provider_schema_version, "meta.messaging.v2")
        self.assertEqual(
            capabilities["extensions"]["provider.meta"]["inbound_delivery_ledger"],
            "meta_webhook_base",
        )
        self.assertEqual(
            capabilities["extensions"]["provider.meta"]["graph_api_version"],
            self.app.graph_version,
        )
        self.assertEqual(
            adapter.get_health(self.connection),
            {
                "state": "connected",
                "reason": "healthy",
                "identity_matches": True,
            },
        )
        with self.assertRaises(AdapterError):
            adapter.normalize_event(self.connection, {"synthetic": True})

    def test_adapter_authentication_resolves_the_shared_app_secret(self):
        adapter = self.connection.get_adapter()
        body = b'{"object":"page","entry":[]}'
        headers = {"X-Hub-Signature-256": self.signature(body)}

        self.assertTrue(adapter.authenticate_webhook(self.connection, headers, body))
        self.assertFalse(
            adapter.authenticate_webhook(self.connection, headers, body + b" ")
        )

    def test_connection_derives_every_meta_dimension_from_one_asset(self):
        self.assertEqual(self.connection.meta_webhook_asset_id, self.page_asset)
        self.assertEqual(self.connection.meta_webhook_page_id, self.page)
        self.assertEqual(self.connection.meta_api_app_id, self.app)
        self.assertEqual(self.connection.meta_target_asset_id, self.ACTIVE_PAGE_ID)
        self.assertEqual(self.connection.meta_transport_mode, "messenger_page")

        instagram_account = self._create_account(
            "instagram", self.INSTAGRAM_ID, team=self.team
        )
        instagram = self._create_connection(
            instagram_account,
            self.instagram_asset,
        )
        self.assertEqual(instagram.meta_transport_mode, "instagram_page_linked")
        self.assertEqual(instagram.meta_webhook_page_id, self.page)
        self.assertEqual(instagram.meta_api_app_id, self.app)

    def test_configuration_rejects_missing_asset_and_platform_drift(self):
        model = self.env["contact.center.provider.connection"]
        with self.assertRaises(ValidationError):
            model.create(
                {
                    "name": "Invalid Meta connection",
                    "account_id": self.account.id,
                    "adapter_key": "meta",
                    "active": True,
                    "role": "standby",
                    "inbound_active": False,
                    "outbound_active": False,
                }
            )

        messenger_account = self._create_account(
            "messenger", self.INSTAGRAM_ID, team=self.team
        )
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._create_connection(messenger_account, self.instagram_asset)

    def test_one_active_primary_connection_claims_each_asset(self):
        duplicate_account = self._create_account(
            "messenger", self.ACTIVE_PAGE_ID, team=self.team
        )
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._create_connection(duplicate_account, self.page_asset)

        archived = self._create_connection(
            duplicate_account,
            self.page_asset,
            active=False,
        )
        self.assertFalse(archived.active)

    def test_meta_route_is_immutable_after_binding(self):
        second_page = self._create_page(self.endpoint, self.INACTIVE_PAGE_ID)
        second_asset = second_page.asset_ids.ensure_one()
        initial_revision = self.connection.health_configuration_revision
        for requested in (False, second_asset.id):
            with self.subTest(requested=requested), self.assertRaises(ValidationError):
                self.connection.write({"meta_webhook_asset_id": requested})
        self.assertEqual(self.connection.meta_webhook_asset_id, self.page_asset)
        self.assertEqual(
            self.connection.health_configuration_revision,
            initial_revision,
        )
        with self.assertRaises(ValidationError):
            self.account.write({"own_external_identity": self.INACTIVE_PAGE_ID})

    def test_meta_page_is_injected_in_the_generic_provider_form(self):
        view = self.env.ref(
            "contact_center_meta.view_contact_center_connection_form_meta"
        )
        arch = etree.fromstring(view.arch_db.encode())
        pages = arch.xpath("//page[@string='Meta']")

        self.assertEqual(len(pages), 1)
        self.assertIn("adapter_key", pages[0].get("attrs"))
        for field_name in (
            "meta_webhook_asset_id",
            "meta_webhook_page_id",
            "meta_api_app_id",
            "meta_transport_mode",
            "meta_target_asset_id",
        ):
            self.assertEqual(
                len(pages[0].xpath(".//field[@name='%s']" % field_name)),
                1,
            )

    @mock.patch("odoo.addons.contact_center_meta.services.adapter.graph_debug_token")
    def test_health_fails_closed_on_identity_or_scope_drift(self, debug):
        adapter = self.connection.get_adapter()
        debug.return_value = _valid_debug(self, profile_id=self.UNKNOWN_PAGE_ID)
        self.assertEqual(
            adapter.get_health(self.connection),
            {
                "state": "degraded",
                "reason": "identity_mismatch",
                "identity_matches": False,
            },
        )

        debug.return_value = _valid_debug(self, scopes=["pages_manage_metadata"])
        self.assertEqual(
            adapter.get_health(self.connection),
            {
                "state": "degraded",
                "reason": "provider_paused",
                "identity_matches": True,
            },
        )
