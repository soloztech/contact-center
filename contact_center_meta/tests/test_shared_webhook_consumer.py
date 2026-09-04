from odoo import fields
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.meta_webhook_base.services.tokens import META_WEBHOOK_INTERNAL_TOKEN
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.contracts import META_TRANSPORT_CONTRACTS
from ..services.shared_webhook import (
    META_MESSAGING_CONSUMER_KEY,
    route_contract,
    subscription_contract,
)
from .common import MetaCase


class TestMetaWebhookConsumer(MetaCase):
    def _envelope(
        self,
        *,
        mid="m_meta_greenfield_1",
        text="Olá",
        object_type="page",
        asset_id=None,
        attachment_url=None,
    ):
        asset_id = asset_id or (
            self.INSTAGRAM_ID if object_type == "instagram" else self.ACTIVE_PAGE_ID
        )
        message = {"mid": mid, "text": text}
        if attachment_url:
            message = {
                "mid": mid,
                "attachments": [{"type": "image", "payload": {"url": attachment_url}}],
            }
        return {
            "object": object_type,
            "entry": [
                {
                    "id": asset_id,
                    "time": 1_800_000_000,
                    "messaging": [
                        {
                            "sender": {"id": "900000000000001"},
                            "recipient": {"id": asset_id},
                            "timestamp": 1_800_000_000_000,
                            "message": message,
                        }
                    ],
                }
            ],
        }

    def _fanout(self, delivery):
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        return delivery.dispatch_ids.ensure_one()

    def _dispatch(self, delivery):
        dispatch = self._fanout(delivery)
        result = self.env["meta.webhook.dispatcher"]._dispatch_consumer(dispatch)
        self.assertTrue(result and result["handled"])
        inbox_id = int(result["result_ref"].rsplit(":", 1)[1])
        return dispatch, self.env["contact.center.inbox.event"].browse(inbox_id)

    def test_messaging_item_is_claimed_only_by_the_registered_consumer(self):
        delivery = self.create_delivery(self._envelope())
        item = delivery.item_ids.ensure_one()

        self.assertEqual(item.kind, "messaging")
        self.assertEqual(item.event_field, "messages")
        self.assertEqual(item.target_asset_id, self.ACTIVE_PAGE_ID)
        dispatch = self._fanout(delivery)
        self.assertEqual(dispatch.consumer_key, META_MESSAGING_CONSUMER_KEY)

    def test_private_media_url_exists_only_in_the_short_lived_vault(self):
        signed_url = "https://lookaside.fbsbx.com/media?token=private"
        delivery = self.create_delivery(
            self._envelope(attachment_url=signed_url, mid="m_meta_media")
        )
        item = delivery.item_ids.ensure_one()
        locator = (
            self.env["contact.center.meta.media.locator"]
            .sudo()
            .search([("meta_delivery_id", "=", delivery.id)])
        )

        self.assertEqual(len(locator), 1)
        self.assertEqual(locator.meta_item_key, item.item_key)
        self.assertEqual(locator.download_url, signed_url)
        self.assertNotIn(signed_url, str(delivery.sanitized_envelope_json))
        self.assertNotIn(signed_url, str(item.payload_json))
        self.assertEqual(
            item.payload_json["messaging"]["message"]["attachments"][0]["payload"],
            {"private_locator_ref": locator.reference},
        )
        _dispatch, inbox = self._dispatch(delivery)
        locator.invalidate_recordset(["provider_connection_id", "inbox_event_id"])
        self.assertEqual(locator.provider_connection_id, self.connection)
        self.assertEqual(locator.inbox_event_id, inbox)
        self.assertNotIn(signed_url, str(inbox.raw_envelope_json))
        self.assertNotIn(signed_url, str(inbox.metadata_json))

    def test_dispatch_projects_one_inbox_with_canonical_ledger_metadata(self):
        dispatch, inbox = self._dispatch(
            self.create_delivery(self._envelope(mid="m_meta_dispatch"))
        )

        self.assertEqual(inbox.provider_connection_id, self.connection)
        self.assertEqual(inbox.metadata_json["technical_ledger"], "meta_webhook_base")
        self.assertEqual(
            inbox.metadata_json["meta_delivery_ref"], dispatch.delivery_id.public_ref
        )
        self.assertEqual(
            inbox.metadata_json["meta_item_key"], dispatch.item_id.item_key
        )

    def test_owner_only_account_projects_and_enqueues_the_new_webhook(self):
        self.account.write(
            {
                "owner_user_id": self.agent.id,
                "default_team_id": False,
            }
        )
        delivery = self.create_delivery(self._envelope(mid="m_meta_owner_only"))
        dispatch = self._fanout(delivery)

        with trap_jobs() as trap:
            result = self.env["meta.webhook.dispatcher"]._dispatch_consumer(dispatch)
            trap.assert_jobs_count(1)

        self.assertTrue(result and result["handled"])
        inbox_id = int(result["result_ref"].rsplit(":", 1)[1])
        inbox = self.env["contact.center.inbox.event"].browse(inbox_id)
        self.assertEqual(inbox.state, "pending")
        self.assertTrue(inbox.queue_job_uuid)
        self.assertNotIn("blocked_reason", inbox.metadata_json)

    def test_live_route_cannot_lose_its_last_account_scope(self):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.account.write(
                {
                    "owner_user_id": False,
                    "default_team_id": False,
                }
            )

        self.account.invalidate_recordset(["owner_user_id", "default_team_id"])
        self.assertTrue(self.account._contact_center_access_is_ready())
        self.assertTrue(self.connection._meta_inbound_route_is_ready())

    def test_consumer_route_uses_the_public_transport_contract(self):
        delivery = self.create_delivery(self._envelope(mid="m_meta_transport_contract"))
        dispatch = self._fanout(delivery)
        contract = route_contract(dispatch.item_id.payload_json)

        self.assertEqual(contract["transport_mode"], "messenger_page")
        expected = META_TRANSPORT_CONTRACTS[contract["transport_mode"]]
        asset = self.env["meta.webhook.dispatcher"]._contact_center_meta_routing_asset(
            dispatch, contract
        )

        self.assertEqual(asset, self.page_asset)
        self.assertEqual(asset.platform, expected["asset_platform"])
        self.assertEqual(asset.object_type, expected["object_type"])
        self.assertEqual(asset.transport, expected["transport"])

    def test_subscription_contracts_cannot_drift_from_transport_contracts(self):
        for mode, transport in META_TRANSPORT_CONTRACTS.items():
            with self.subTest(mode=mode):
                subscription = subscription_contract(mode)
                self.assertIsNotNone(subscription)
                self.assertEqual(subscription["object_type"], transport["object_type"])
                self.assertEqual(subscription["fields"], transport["fields"])

    def test_semantic_redelivery_reuses_the_same_inbox(self):
        first = self.create_delivery(self._envelope(mid="m_meta_redelivery"))
        _first_dispatch, first_inbox = self._dispatch(first)
        second_envelope = self._envelope(mid="m_meta_redelivery")
        second_envelope["entry"][0]["time"] += 1
        second = self.create_delivery(second_envelope)
        _second_dispatch, second_inbox = self._dispatch(second)

        self.assertEqual(first_inbox, second_inbox)
        self.assertEqual(
            self.env["contact.center.inbox.event"].search_count(
                [
                    ("provider_connection_id", "=", self.connection.id),
                    ("inbox_dedupe_key", "=", first_inbox.inbox_dedupe_key),
                ]
            ),
            1,
        )

    def test_unknown_carrier_remains_unclaimed_technical_evidence(self):
        envelope = self._envelope()
        envelope["entry"][0]["messaging"][0].pop("message")
        envelope["entry"][0]["messaging"][0]["optin"] = {"ref": "synthetic"}
        delivery = self.create_delivery(envelope)

        item = delivery.item_ids.ensure_one()
        self.assertEqual(item.kind, "unknown")
        with trap_jobs():
            delivery.sudo().with_context(
                meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
            )._fanout_once()
        dispatch = delivery.dispatch_ids.ensure_one()
        self.assertEqual(dispatch.consumer_key, META_MESSAGING_CONSUMER_KEY)
        self.assertEqual(dispatch.item_id, item)
        self.assertIsNone(
            self.env["meta.webhook.dispatcher"]._dispatch_consumer(dispatch)
        )
        self.assertFalse(
            self.env["contact.center.inbox.event"]
            .sudo()
            .search([("provider_connection_id", "=", self.connection.id)])
        )

    def test_unsubscribed_message_is_not_claimed_and_creates_no_private_locator(self):
        self.subscriptions.filtered(
            lambda item: item.object_type == "page" and item.field_name == "messages"
        ).write({"active": False})
        delivery = self.create_delivery(
            self._envelope(
                mid="m_meta_unsubscribed",
                attachment_url="https://lookaside.fbsbx.com/media?token=ignored",
            )
        )

        self.assertEqual(delivery.item_ids.ensure_one().kind, "unknown")
        self.assertFalse(
            self.env["contact.center.meta.media.locator"]
            .sudo()
            .search([("meta_delivery_id", "=", delivery.id)])
        )

    def test_configure_webhook_is_idempotent_and_system_only(self):
        with trap_jobs():
            self.assertTrue(self.connection.action_meta_configure_webhook())
            self.assertTrue(self.connection.action_meta_configure_webhook())
        subscriptions = self.env["meta.webhook.subscription"].search(
            [
                ("page_id", "=", self.page.id),
                ("consumer_key", "=", META_MESSAGING_CONSUMER_KEY),
                ("object_type", "=", "page"),
                ("active", "=", True),
            ]
        )
        self.assertTrue(subscriptions)
        with self.assertRaises(AccessError):
            self.connection.with_user(self.admin).action_meta_configure_webhook()

    def test_route_readiness_requires_fresh_provider_readback(self):
        self.assertTrue(self.connection._meta_inbound_route_is_ready())
        self.page.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        ).write({"subscription_state": "drift"})
        self.assertFalse(self.connection._meta_inbound_route_is_ready())

    def test_temporarily_unready_canonical_route_retries_then_recovers(self):
        delivery = self.create_delivery(self._envelope(mid="m_meta_reconcile_recovery"))
        dispatch = self._fanout(delivery)
        internal_page = self.page.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal_page.write({"subscription_state": "drift"})

        dispatcher = self.env["meta.webhook.dispatcher"]
        with self.assertRaises(RetryableJobError):
            dispatcher._dispatch_consumer(dispatch)
        self.assertFalse(
            self.env["contact.center.inbox.event"]
            .sudo()
            .search([("provider_connection_id", "=", self.connection.id)])
        )

        # Project the successful provider readback produced by reconciliation.
        internal_page.write(
            {
                "subscription_state": "in_sync",
                "verified_at": fields.Datetime.now(),
            }
        )
        result = dispatcher._dispatch_consumer(dispatch)

        self.assertTrue(result and result["handled"])
        inbox_id = int(result["result_ref"].rsplit(":", 1)[1])
        self.assertEqual(
            self.env["contact.center.inbox.event"]
            .browse(inbox_id)
            .provider_connection_id,
            self.connection,
        )

    def test_reconcile_revives_route_retry_after_terminal_ceiling(self):
        delivery = self.create_delivery(
            self._envelope(mid="m_meta_reconcile_terminal_recovery")
        )
        dispatch = self._fanout(delivery)
        internal_page = self.page.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        )
        internal_page.write({"subscription_state": "drift"})
        dispatch.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        ).write(
            {
                "state": "pending",
                "attempts": 7,
            }
        )

        self.assertTrue(dispatch.queue_job_uuid)
        self.assertFalse(
            dispatch.with_context(job_uuid=dispatch.queue_job_uuid)._job_process()
        )
        self.assertEqual(dispatch.state, "dead")
        self.assertEqual(dispatch.last_error_class, "SubscriptionNotReady")

        internal_page.write(
            {
                "subscription_state": "in_sync",
                "verified_at": fields.Datetime.now(),
            }
        )
        with trap_jobs() as trap:
            self.env["meta.webhook.dispatcher"]._after_subscription_reconcile(
                self.endpoint
            )
            trap.assert_jobs_count(1)
            dispatch.invalidate_recordset(
                ["state", "attempts", "queue_job_uuid", "last_error_class"]
            )
            self.assertEqual(dispatch.state, "pending")
            self.assertEqual(dispatch.attempts, 0)
            self.assertFalse(dispatch.last_error_class)
            self.assertTrue(dispatch.queue_job_uuid)
            self.assertTrue(
                dispatch.with_context(job_uuid=dispatch.queue_job_uuid)._job_process()
            )

        dispatch.invalidate_recordset(["state", "result_ref"])
        self.assertEqual(dispatch.state, "done")
        self.assertTrue(dispatch.result_ref)

    def test_instagram_route_uses_the_same_page_and_its_own_asset(self):
        account = self._create_account("instagram", self.INSTAGRAM_ID, team=self.team)
        connection = self._create_connection(account, self.instagram_asset)
        delivery = self.create_delivery(
            self._envelope(
                object_type="instagram",
                asset_id=self.INSTAGRAM_ID,
                mid="m_meta_instagram",
            )
        )
        dispatch, inbox = self._dispatch(delivery)

        self.assertEqual(dispatch.page_id, self.page)
        self.assertEqual(inbox.provider_connection_id, connection)
        self.assertEqual(connection.meta_api_app_id, self.app)

    def test_asset_binding_is_immutable(self):
        other_page = self._create_page(self.endpoint, self.INACTIVE_PAGE_ID)
        with self.assertRaises(ValidationError):
            self.connection.write(
                {"meta_webhook_asset_id": other_page.asset_ids.ensure_one().id}
            )

    def test_retiring_last_route_archives_only_its_consumer_object(self):
        foreign = self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.page.id,
                "consumer_key": "another.consumer",
                "object_type": "page",
                "field_name": "messages",
            }
        )
        with trap_jobs():
            self.connection.write({"active": False})

        page_subscriptions = self.subscriptions.filtered(
            lambda item: item.object_type == "page"
        )
        instagram_subscriptions = self.subscriptions.filtered(
            lambda item: item.object_type == "instagram"
        )
        self.assertFalse(any(page_subscriptions.mapped("active")))
        self.assertTrue(all(instagram_subscriptions.mapped("active")))
        self.assertTrue(foreign.active)
