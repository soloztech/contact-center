import hashlib
import hmac
import json
import os
import uuid

from odoo import fields
from odoo.tests.common import SavepointCase

from odoo.addons.meta_webhook_base.services.sanitizer import sanitized_webhook
from odoo.addons.meta_webhook_base.services.tokens import META_WEBHOOK_INTERNAL_TOKEN

from ..services.shared_webhook import (
    META_MESSAGING_CONSUMER_KEY,
    META_MESSAGING_SUBSCRIPTIONS,
)


class MetaFixtureMixin:
    APP_SECRET_REF = "ODOO_CC_META_TEST_APP_SECRET"
    VERIFY_TOKEN_REF = "ODOO_CC_META_TEST_VERIFY_TOKEN"
    PAGE_TOKEN_REF = "ODOO_CC_META_TEST_PAGE_TOKEN"
    APP_SECRET = "synthetic-meta-app-secret-for-tests"
    VERIFY_TOKEN = "synthetic-meta-verify-token-for-tests"
    PAGE_TOKEN = "synthetic-meta-page-token-for-tests"
    ACTIVE_PAGE_ID = "100000000000001"
    INACTIVE_PAGE_ID = "100000000000002"
    UNKNOWN_PAGE_ID = "100000000000099"
    INSTAGRAM_ID = "200000000000001"

    @classmethod
    def _numeric_id(cls):
        return str(100_000_000_000_000 + uuid.uuid4().int % 899_999_999_999_999)

    @classmethod
    def _create_user(cls, name, group, company=None):
        company = company or cls.env.company
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Meta %s" % name,
                    "login": "contact-center-meta-%s-%s"
                    % (name.lower().replace(" ", "-"), uuid.uuid4()),
                    "email": "contact-center-meta-%s@example.invalid"
                    % name.lower().replace(" ", "-"),
                    "company_id": company.id,
                    "company_ids": [(6, 0, company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )

    @classmethod
    def _create_team(cls, name, *, company=None, agents=None, supervisors=None):
        company = company or cls.env.company
        agents = agents or cls.env["res.users"]
        supervisors = supervisors or cls.env["res.users"]
        return cls.env["contact.center.team"].create(
            {
                "name": "Meta %s %s" % (name, uuid.uuid4()),
                "company_id": company.id,
                "agent_ids": [(6, 0, agents.ids)],
                "supervisor_ids": [(6, 0, supervisors.ids)],
            }
        )

    @classmethod
    def _create_app(cls, *, company=None, active=True, **values):
        company = company or cls.env.company
        app_values = {
            "name": "Meta App %s" % uuid.uuid4(),
            "company_id": company.id,
            "external_app_id": cls._numeric_id(),
            "graph_version": "v26.0",
            "credential_backend": "environment",
            "app_secret_ref": cls.APP_SECRET_REF,
            "active": active,
        }
        app_values.update(values)
        return cls.env["meta.api.app"].create(app_values)

    @classmethod
    def _create_endpoint(cls, app, *, company=None, active=True, **values):
        company = company or app.company_id
        endpoint_values = {
            "name": "Meta Endpoint %s" % uuid.uuid4(),
            "company_id": company.id,
            "app_id": app.id,
            "credential_backend": "environment",
            "verify_token_ref": cls.VERIFY_TOKEN_REF,
            "active": active,
        }
        endpoint_values.update(values)
        return cls.env["meta.webhook.endpoint"].create(endpoint_values)

    @classmethod
    def _create_page(cls, endpoint, page_id=None, *, active=True, **values):
        page_values = {
            "name": "Meta Page %s" % uuid.uuid4(),
            "endpoint_id": endpoint.id,
            "external_page_id": page_id or cls.ACTIVE_PAGE_ID,
            "credential_backend": "environment",
            "access_token_ref": cls.PAGE_TOKEN_REF,
            "active": active,
        }
        page_values.update(values)
        return cls.env["meta.webhook.page"].create(page_values)

    @classmethod
    def _create_instagram_asset(cls, page, asset_id=None, *, active=True):
        return cls.env["meta.webhook.asset"].create(
            {
                "page_id": page.id,
                "platform": "instagram",
                "object_type": "instagram",
                "transport": "page_linked",
                "external_asset_id": asset_id or cls.INSTAGRAM_ID,
                "active": active,
            }
        )

    @classmethod
    def _create_subscriptions(cls, page):
        return cls.env["meta.webhook.subscription"].create(
            [
                {
                    "page_id": page.id,
                    "consumer_key": META_MESSAGING_CONSUMER_KEY,
                    "object_type": contract["object_type"],
                    "field_name": field_name,
                }
                for contract in META_MESSAGING_SUBSCRIPTIONS.values()
                for field_name in sorted(contract["fields"])
            ]
        )

    @classmethod
    def _create_account(
        cls,
        platform,
        asset_id,
        *,
        team=None,
        owner=None,
        company=None,
        active=True,
    ):
        company = company or cls.env.company
        return cls.env["contact.center.account"].create(
            {
                "name": "Meta %s Inbox %s" % (platform, uuid.uuid4()),
                "company_id": company.id,
                "platform": platform,
                "own_external_identity": asset_id,
                "access_user_ids": [(6, 0, owner.ids if owner else [])],
                "access_team_ids": [(6, 0, team.ids if team else [])],
                "active": active,
            }
        )

    @classmethod
    def _create_connection(cls, account, asset, *, active=True, **values):
        projections = {
            "meta_webhook_page_id",
            "meta_api_app_id",
            "meta_transport_mode",
            "meta_target_asset_id",
        }
        if projections.intersection(values):
            raise AssertionError("Meta connection projections cannot be configured")
        connection_values = {
            "name": "Meta Connection %s" % uuid.uuid4(),
            "account_id": account.id,
            "adapter_key": "meta",
            "meta_webhook_asset_id": asset.id,
            "active": active,
            "role": "primary" if active else "historical",
            "inbound_active": bool(active),
            "outbound_active": False,
        }
        connection_values.update(values)
        return cls.env["contact.center.provider.connection"].create(connection_values)

    @classmethod
    def load_fixture(cls, filename):
        path = os.path.join(os.path.dirname(__file__), "fixtures", filename)
        with open(path, encoding="utf-8") as fixture_file:
            return json.load(fixture_file)

    @classmethod
    def canonical_body(cls, envelope):
        return json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def signature(cls, body):
        digest = hmac.new(
            cls.APP_SECRET.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        return "sha256=%s" % digest

    def create_delivery(self, envelope, *, body=None):
        """Create only the canonical shared delivery/item evidence."""

        body = body or self.canonical_body(envelope)
        sanitized = sanitized_webhook(envelope)
        delivery = (
            self.env["meta.webhook.delivery"]
            .sudo()
            .with_context(meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN)
            .create(
                {
                    "endpoint_id": self.endpoint.id,
                    "endpoint_revision": self.endpoint.revision,
                    "app_revision": self.app.revision,
                    "content_sha256": hashlib.sha256(body).hexdigest(),
                    "body_size_bytes": len(body),
                    "object_type": sanitized.object_type,
                    "graph_version": self.app.graph_version,
                    "sanitized_envelope_json": sanitized.envelope,
                }
            )
        )
        self.env["meta.webhook.dispatcher"]._ingest_delivery(
            self.endpoint,
            delivery,
            envelope,
            sanitized,
        )
        return delivery


class MetaCase(MetaFixtureMixin, SavepointCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._previous_environment = {
            reference: os.environ.get(reference)
            for reference in (
                cls.APP_SECRET_REF,
                cls.VERIFY_TOKEN_REF,
                cls.PAGE_TOKEN_REF,
            )
        }
        os.environ[cls.APP_SECRET_REF] = cls.APP_SECRET
        os.environ[cls.VERIFY_TOKEN_REF] = cls.VERIFY_TOKEN
        os.environ[cls.PAGE_TOKEN_REF] = cls.PAGE_TOKEN

        cls.agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.supervisor_group = cls.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        cls.admin_group = cls.env.ref("contact_center_base.group_contact_center_admin")
        cls.agent = cls._create_user("Agent", cls.agent_group)
        cls.supervisor = cls._create_user("Supervisor", cls.supervisor_group)
        cls.admin = cls._create_user("Administrator", cls.admin_group)
        cls.team = cls._create_team(
            "Main",
            agents=cls.agent,
            supervisors=cls.supervisor,
        )
        cls.app = cls._create_app()
        cls.endpoint = cls._create_endpoint(cls.app)
        cls.page = cls._create_page(cls.endpoint)
        cls.page_asset = cls.page.asset_ids.ensure_one()
        cls.instagram_asset = cls._create_instagram_asset(cls.page)
        cls.subscriptions = cls._create_subscriptions(cls.page)
        internal = {"meta_webhook_internal": META_WEBHOOK_INTERNAL_TOKEN}
        cls.page.sudo().with_context(**internal).write(
            {
                "subscription_state": "in_sync",
                "observed_fields_json": sorted(
                    set(cls.subscriptions.mapped("field_name"))
                ),
                "verified_at": fields.Datetime.now(),
            }
        )
        cls.endpoint.sudo().with_context(**internal).write(
            {
                "subscription_state": "in_sync",
                "observed_subscriptions_json": [
                    {
                        "object": contract["object_type"],
                        "fields": sorted(contract["fields"]),
                        "active": True,
                        "callback_matches": True,
                    }
                    for contract in META_MESSAGING_SUBSCRIPTIONS.values()
                ],
                "verified_at": fields.Datetime.now(),
            }
        )
        cls.account = cls._create_account(
            "messenger",
            cls.ACTIVE_PAGE_ID,
            team=cls.team,
        )
        cls.connection = cls._create_connection(cls.account, cls.page_asset)

    @classmethod
    def tearDownClass(cls):
        for reference, previous in cls._previous_environment.items():
            if previous is None:
                os.environ.pop(reference, None)
            else:
                os.environ[reference] = previous
        super().tearDownClass()
