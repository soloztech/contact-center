import copy
import hashlib
import hmac
import json
import os
import time
import uuid
from unittest import mock
from urllib.parse import urlsplit

import requests
import urllib3

from odoo.tests.common import TransactionCase

from odoo.addons.meta_webhook_base.services.sanitizer import sanitized_webhook
from odoo.addons.meta_webhook_base.services.tokens import META_WEBHOOK_INTERNAL_TOKEN
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.contracts import (
    WHATSAPP_CLOUD_CONSUMER_KEY,
    WHATSAPP_OBJECT_TYPE,
    WHATSAPP_OWNER_KIND,
    WHATSAPP_WEBHOOK_FIELD,
)

GRAPH_PATCH = "odoo.addons.meta_api_base.services.graph.requests.request"
MEDIA_PATCH = (
    "odoo.addons.contact_center_whatsapp_cloud.services.media.requests.request"
)

_REAL_HTTP_SEND = requests.adapters.HTTPAdapter.send
_REAL_URLOPEN = urllib3.connectionpool.HTTPConnectionPool.urlopen
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class UnpatchedNetworkError(AssertionError):
    """A test tried to reach a real host without a fake transport (criterion 11)."""


def _guarded_http_send(adapter, request, *args, **kwargs):
    host = (urlsplit(request.url).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        # Only HttpCase talks to the local test server.
        return _REAL_HTTP_SEND(adapter, request, *args, **kwargs)
    raise UnpatchedNetworkError("Unpatched network I/O to %s" % host)


def _guarded_urlopen(pool, method, url, *args, **kwargs):
    if (pool.host or "").lower().strip("[]") in _LOOPBACK_HOSTS:
        return _REAL_URLOPEN(pool, method, url, *args, **kwargs)
    raise UnpatchedNetworkError("Unpatched network I/O to %s" % pool.host)


class FakeResponse:
    """Minimal streamed ``requests`` response used by every fake transport."""

    def __init__(self, payload=None, *, status=200, headers=None, content=None):
        self.status_code = status
        self.headers = dict(headers or {})
        if content is None:
            content = json.dumps(payload or {}, separators=(",", ":")).encode()
        self._content = content
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class WhatsAppCloudFixtureMixin:
    APP_SECRET_REF = "ODOO_CC_WAC_TEST_APP_SECRET"
    VERIFY_TOKEN_REF = "ODOO_CC_WAC_TEST_VERIFY_TOKEN"
    WABA_TOKEN_REF = "ODOO_CC_WAC_TEST_WABA_TOKEN"
    APP_SECRET = "synthetic-wac-app-secret-for-tests"
    VERIFY_TOKEN = "synthetic-wac-verify-token-for-tests"
    WABA_TOKEN = "synthetic-wac-system-user-token-for-tests"
    WABA_ID = "100000000000901"
    OTHER_WABA_ID = "100000000000911"
    PHONE_ID = "100000000000902"
    OTHER_PHONE_ID = "100000000000912"
    DISPLAY_PHONE = "5511999990000"
    CUSTOMER = "5511988887777"
    OTHER_CUSTOMER = "5511977776666"
    CUSTOMER_BSUID = "BR.1a2b3c4d5e6f7a8b"
    OTHER_BSUID = "BR.9f8e7d6c5b4a3f2e"

    @classmethod
    def _install_network_guard(cls):
        cls.startClassPatcher(
            mock.patch.object(requests.adapters.HTTPAdapter, "send", _guarded_http_send)
        )
        cls.startClassPatcher(
            mock.patch.object(
                urllib3.connectionpool.HTTPConnectionPool, "urlopen", _guarded_urlopen
            )
        )

    @classmethod
    def _set_environment(cls):
        cls._previous_environment = {
            reference: os.environ.get(reference)
            for reference in (
                cls.APP_SECRET_REF,
                cls.VERIFY_TOKEN_REF,
                cls.WABA_TOKEN_REF,
            )
        }
        os.environ[cls.APP_SECRET_REF] = cls.APP_SECRET
        os.environ[cls.VERIFY_TOKEN_REF] = cls.VERIFY_TOKEN
        os.environ[cls.WABA_TOKEN_REF] = cls.WABA_TOKEN

    @classmethod
    def _restore_environment(cls):
        for reference, previous in cls._previous_environment.items():
            if previous is None:
                os.environ.pop(reference, None)
            else:
                os.environ[reference] = previous

    @classmethod
    def _numeric_id(cls):
        return str(100_000_000_000_000 + uuid.uuid4().int % 899_999_999_999_999)

    @classmethod
    def _create_user(cls, name, group, company=None):
        company = company or cls.env.company
        slug = name.lower().replace(" ", "-")
        return (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "WhatsApp Cloud %s" % name,
                    "login": "cc-wac-%s-%s" % (slug, uuid.uuid4()),
                    "email": "cc-wac-%s@example.invalid" % slug,
                    "company_id": company.id,
                    "company_ids": [(6, 0, company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
            )
        )

    @classmethod
    def _create_team(cls, name, *, company=None, agents=None, supervisors=None):
        company = company or cls.env.company
        return cls.env["contact.center.team"].create(
            {
                "name": "WhatsApp Cloud %s %s" % (name, uuid.uuid4()),
                "company_id": company.id,
                "agent_ids": [(6, 0, (agents or cls.env["res.users"]).ids)],
                "supervisor_ids": [(6, 0, (supervisors or cls.env["res.users"]).ids)],
            }
        )

    @classmethod
    def _create_app(cls, *, company=None, **values):
        company = company or cls.env.company
        app_values = {
            "name": "WhatsApp App %s" % uuid.uuid4(),
            "company_id": company.id,
            "external_app_id": cls._numeric_id(),
            "graph_version": "v26.0",
            "credential_backend": "environment",
            "app_secret_ref": cls.APP_SECRET_REF,
        }
        app_values.update(values)
        return cls.env["meta.api.app"].create(app_values)

    @classmethod
    def _create_endpoint(cls, app, **values):
        endpoint_values = {
            "name": "WhatsApp Endpoint %s" % uuid.uuid4(),
            "company_id": app.company_id.id,
            "app_id": app.id,
            "credential_backend": "environment",
            "verify_token_ref": cls.VERIFY_TOKEN_REF,
        }
        endpoint_values.update(values)
        return cls.env["meta.webhook.endpoint"].create(endpoint_values)

    @classmethod
    def _create_waba(cls, endpoint, waba_id=None, **values):
        owner_values = {
            "name": "WhatsApp Business Account %s" % uuid.uuid4(),
            "endpoint_id": endpoint.id,
            "owner_kind": WHATSAPP_OWNER_KIND,
            "external_page_id": waba_id or cls.WABA_ID,
            "credential_backend": "environment",
            "access_token_ref": cls.WABA_TOKEN_REF,
        }
        owner_values.update(values)
        return cls.env["meta.webhook.page"].create(owner_values)

    @classmethod
    def _subscribe(cls, owner):
        return cls.env["meta.webhook.subscription"].create(
            {
                "page_id": owner.id,
                "consumer_key": WHATSAPP_CLOUD_CONSUMER_KEY,
                "object_type": WHATSAPP_OBJECT_TYPE,
                "field_name": WHATSAPP_WEBHOOK_FIELD,
            }
        )

    @classmethod
    def _create_account(cls, *, team=None, company=None, **values):
        company = company or cls.env.company
        account_values = {
            "name": "WhatsApp Cloud Inbox %s" % uuid.uuid4(),
            "company_id": company.id,
            "platform": "whatsapp",
            "own_external_identity": "%s@s.whatsapp.net" % cls.DISPLAY_PHONE,
            "access_team_ids": [(6, 0, team.ids if team else [])],
        }
        account_values.update(values)
        return cls.env["contact.center.account"].create(account_values)

    @classmethod
    def _create_connection(
        cls, account, asset, phone_id=None, *, active=True, **values
    ):
        connection_values = {
            "name": "WhatsApp Cloud Connection %s" % uuid.uuid4(),
            "account_id": account.id,
            "adapter_key": "whatsapp_cloud",
            "wa_webhook_asset_id": asset.id,
            "wa_phone_number_id": phone_id or cls.PHONE_ID,
            "active": active,
            "role": "primary" if active else "historical",
            "inbound_active": bool(active),
            "outbound_active": False,
        }
        connection_values.update(values)
        return cls.env["contact.center.provider.connection"].create(connection_values)

    @classmethod
    def _make_connected(cls, connection, *, outbound=True):
        connection._apply_health_result(
            {"state": "connected", "reason": "healthy", "identity_matches": True},
            source="health_job",
        )
        if outbound:
            connection.write({"outbound_active": True})
        return connection

    @classmethod
    def _setup_whatsapp_cloud(cls):
        cls._install_network_guard()
        cls._set_environment()
        cls.agent_group = cls.env.ref("contact_center_base.group_contact_center_agent")
        cls.supervisor_group = cls.env.ref(
            "contact_center_base.group_contact_center_supervisor"
        )
        cls.admin_group = cls.env.ref("contact_center_base.group_contact_center_admin")
        cls.agent = cls._create_user("Agent", cls.agent_group)
        cls.supervisor = cls._create_user("Supervisor", cls.supervisor_group)
        cls.admin = cls._create_user("Administrator", cls.admin_group)
        cls.team = cls._create_team(
            "Main", agents=cls.agent, supervisors=cls.supervisor
        )
        cls.app = cls._create_app()
        cls.endpoint = cls._create_endpoint(cls.app)
        cls.waba = cls._create_waba(cls.endpoint)
        cls.waba_asset = cls.waba.asset_ids.ensure_one()
        cls.subscription = cls._subscribe(cls.waba)
        cls.account = cls._create_account(team=cls.team)
        cls.technical_author = cls.env["res.partner"].create(
            {"name": "WhatsApp Cloud Technical Author"}
        )
        cls.account.technical_author_id = cls.technical_author
        cls.connection = cls._create_connection(cls.account, cls.waba_asset)
        cls._make_connected(cls.connection)

    # -- Provider payload builders -------------------------------------------------

    @classmethod
    def now(cls):
        return int(time.time())

    @classmethod
    def wamid(cls, label=None):
        return "wamid.HBgN%s" % (label or uuid.uuid4().hex)

    @classmethod
    def text_message(
        cls,
        body="Olá, quero um orçamento",
        *,
        wamid=None,
        sender=None,
        user_id=None,
        timestamp=None,
        **extra,
    ):
        message = {
            "from": cls.CUSTOMER if sender is None else sender,
            "id": wamid or cls.wamid(),
            "timestamp": str(timestamp or cls.now()),
            "type": "text",
            "text": {"body": body},
        }
        if user_id is not False:
            message["from_user_id"] = user_id or cls.CUSTOMER_BSUID
        if not message["from"]:
            message.pop("from")
        message.update(extra)
        return message

    @classmethod
    def typed_message(cls, message_type, content, **values):
        message = cls.text_message(**values)
        message.pop("text")
        message["type"] = message_type
        if content is not None:
            message[message_type] = content
        return message

    @classmethod
    def contact_block(cls, wa_id=None, user_id=None, name="Cliente Sintético"):
        contact = {"profile": {"name": name}}
        if wa_id is not False:
            contact["wa_id"] = wa_id or cls.CUSTOMER
        if user_id is not False:
            contact["user_id"] = user_id or cls.CUSTOMER_BSUID
        return contact

    @classmethod
    def status(
        cls,
        wamid,
        status="delivered",
        *,
        timestamp=None,
        recipient=None,
        callback=None,
        errors=None,
    ):
        row = {
            "id": wamid,
            "status": status,
            "timestamp": str(timestamp or cls.now()),
            "recipient_id": recipient or cls.CUSTOMER,
            "recipient_type": "individual",
        }
        if callback:
            row["biz_opaque_callback_data"] = callback
        if errors:
            row["errors"] = errors
        return row

    @classmethod
    def value(
        cls, *, messages=(), statuses=(), errors=(), contacts=None, phone_id=None
    ):
        result = {
            "messaging_product": "whatsapp",
            "metadata": {
                "display_phone_number": cls.DISPLAY_PHONE,
                "phone_number_id": phone_id or cls.PHONE_ID,
            },
        }
        if messages:
            result["contacts"] = (
                contacts if contacts is not None else [cls.contact_block()]
            )
            result["messages"] = list(messages)
        if statuses:
            result["statuses"] = list(statuses)
        if errors:
            result["errors"] = list(errors)
        return result

    @classmethod
    def envelope(cls, *values, waba_id=None):
        return {
            "object": WHATSAPP_OBJECT_TYPE,
            "entry": [
                {
                    "id": waba_id or cls.WABA_ID,
                    "changes": [
                        {"field": "messages", "value": copy.deepcopy(value)}
                        for value in values
                    ],
                }
            ],
        }

    @classmethod
    def canonical_body(cls, envelope):
        return json.dumps(
            envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")

    @classmethod
    def signature(cls, body):
        digest = hmac.new(cls.APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return "sha256=%s" % digest

    # -- Pipeline helpers ---------------------------------------------------------

    def create_delivery(self, envelope, *, endpoint=None, body=None):
        """Create only the shared delivery/item evidence, as the controller does."""

        endpoint = endpoint or self.endpoint
        body = body or self.canonical_body(envelope)
        sanitized = sanitized_webhook(envelope)
        delivery = (
            self.env["meta.webhook.delivery"]
            .sudo()
            .with_context(meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN)
            .create(
                {
                    "endpoint_id": endpoint.id,
                    "endpoint_revision": endpoint.revision,
                    "app_revision": endpoint.app_id.revision,
                    "content_sha256": hashlib.sha256(body).hexdigest(),
                    "body_size_bytes": len(body),
                    "object_type": sanitized.object_type,
                    "graph_version": endpoint.app_id.graph_version,
                    "sanitized_envelope_json": sanitized.envelope,
                }
            )
        )
        self.env["meta.webhook.dispatcher"]._ingest_delivery(
            endpoint, delivery, envelope, sanitized
        )
        return delivery

    def fanout(self, delivery):
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        return delivery.dispatch_ids.filtered(
            lambda dispatch: dispatch.consumer_key == WHATSAPP_CLOUD_CONSUMER_KEY
        ).sorted(lambda dispatch: dispatch.item_id.sequence)

    def dispatch_one(self, dispatch):
        with trap_jobs():
            return self.env["meta.webhook.dispatcher"]._dispatch_consumer(dispatch)

    def dispatch_all(self, delivery):
        results = []
        for dispatch in self.fanout(delivery):
            results.append(self.dispatch_one(dispatch))
        return results

    def inbox_from(self, result):
        self.assertTrue(result and result["handled"], result)
        self.assertTrue(result["result_ref"].startswith("contact.center.inbox.event:"))
        return self.env["contact.center.inbox.event"].browse(
            int(result["result_ref"].rsplit(":", 1)[1])
        )

    def process_inbox(self, inbox):
        inbox = inbox.sudo()
        if not inbox.queue_job_uuid:
            inbox.write({"queue_job_uuid": str(uuid.uuid4())})
        with trap_jobs():
            inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
        inbox.invalidate_recordset()
        return inbox

    def deliver(self, envelope, *, process=True):
        """Ingest, dispatch and (optionally) process every inbox event."""

        delivery = self.create_delivery(envelope)
        inboxes = self.env["contact.center.inbox.event"]
        for result in self.dispatch_all(delivery):
            if result and result["result_ref"].startswith(
                "contact.center.inbox.event:"
            ):
                inboxes |= self.inbox_from(result)
        if process:
            for inbox in inboxes:
                self.process_inbox(inbox)
        return delivery, inboxes

    def binding_for(self, wamid, connection=None):
        return self.env["contact.center.message.binding"].search(
            [
                ("provider_connection_id", "=", (connection or self.connection).id),
                ("external_message_id", "=", wamid),
            ]
        )


class WhatsAppCloudCase(WhatsAppCloudFixtureMixin, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_whatsapp_cloud()

    @classmethod
    def tearDownClass(cls):
        cls._restore_environment()
        super().tearDownClass()
