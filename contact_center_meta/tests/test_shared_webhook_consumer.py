from unittest import mock

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
from .test_media import FakeMediaResponse


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

    def test_rejected_attachment_slots_preserve_text_and_valid_private_media(self):
        mid = "m_meta_mixed_attachment_slots"
        envelope = self._envelope(mid=mid, text="Arquivos do pedido")
        message = envelope["entry"][0]["messaging"][0]["message"]
        message["attachments"] = [
            None,
            {
                "type": "image",
                "payload": {"url": "https://lookaside.fbsbx.com/image?token=private"},
            },
            "invalid-attachment",
            {
                "type": "audio",
                "payload": {"url": "https://lookaside.fbsbx.com/audio?token=private"},
            },
        ]
        delivery = self.create_delivery(envelope)
        item = delivery.item_ids.ensure_one()
        attachments = item.payload_json["messaging"]["message"]["attachments"]
        self.assertEqual(len(attachments), 4)
        self.assertEqual(attachments[0], {})
        self.assertEqual(attachments[2], {})
        locators = (
            self.env["contact.center.meta.media.locator"]
            .sudo()
            .search([("meta_delivery_id", "=", delivery.id)])
        )
        self.assertEqual(set(locators.mapped("slot")), {"attachment:1", "attachment:3"})

        _dispatch, inbox = self._dispatch(delivery)
        with trap_jobs():
            self.assertTrue(
                inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
            )

        inbox.invalidate_recordset()
        locators.invalidate_recordset(["provider_connection_id", "inbox_event_id"])
        self.assertEqual(inbox.state, "done")
        self.assertEqual(locators.mapped("inbox_event_id"), inbox)
        self.assertEqual(locators.mapped("provider_connection_id"), self.connection)
        binding = (
            self.env["contact.center.message.binding"]
            .search(
                [
                    ("provider_connection_id", "=", self.connection.id),
                    ("external_message_id", "=", mid),
                ]
            )
            .ensure_one()
        )
        self.assertIn("Arquivos do pedido", str(binding.message_id.body))
        self.assertEqual(set(binding.media_ids.mapped("kind")), {"image", "audio"})
        self.assertEqual(set(binding.media_ids.mapped("state")), {"pending"})
        self.assertEqual(
            set(binding.media_ids.mapped("external_media_id")),
            {"%s:1" % mid, "%s:3" % mid},
        )
        self.assertEqual(
            {
                media.remote_locator_json["private_locator_ref"]
                for media in binding.media_ids
            },
            set(locators.mapped("reference")),
        )
        for payload in (
            delivery.sanitized_envelope_json,
            item.payload_json,
            inbox.raw_envelope_json,
            inbox.normalized_dto_json,
        ):
            self.assertNotIn("token=private", str(payload))

    def test_rejected_story_context_preserves_text_and_reply_validation(self):
        account = self._create_account("instagram", self.INSTAGRAM_ID, team=self.team)
        connection = self._create_connection(account, self.instagram_asset)
        cases = (
            ("rejected", {"id": []}, False, "done"),
            ("valid", {"id": "story-123"}, False, "done"),
            ("unmarked_empty", {}, False, "unsupported"),
            ("self_reply", {"id": []}, True, "dead"),
        )
        for suffix, story, self_reply, expected_state in cases:
            with self.subTest(suffix=suffix):
                mid = "m_meta_story_context_%s" % suffix
                envelope = self._envelope(
                    object_type="instagram", mid=mid, text="Sobre este produto"
                )
                message = envelope["entry"][0]["messaging"][0]["message"]
                message["reply_to"] = {"story": story}
                if self_reply:
                    message["reply_to"]["mid"] = mid
                _dispatch, inbox = self._dispatch(self.create_delivery(envelope))
                with trap_jobs():
                    self.assertTrue(
                        inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
                    )

                inbox.invalidate_recordset()
                self.assertEqual(inbox.state, expected_state)
                binding = self.env["contact.center.message.binding"].search(
                    [
                        ("provider_connection_id", "=", connection.id),
                        ("external_message_id", "=", mid),
                    ]
                )
                if expected_state != "done":
                    self.assertFalse(binding)
                    continue
                self.assertIn("Sobre este produto", str(binding.message_id.body))
                self.assertFalse(binding.reply_to_binding_id)
                if suffix == "valid":
                    self.assertEqual(
                        binding.protocol_snapshot_json["story_reply"], story
                    )
                else:
                    self.assertNotIn("story_reply", binding.protocol_snapshot_json)
                    self.assertEqual(
                        inbox.normalized_dto_json["extensions"]["provider.meta"][
                            "unsupported_content"
                        ],
                        "content",
                    )

    def test_shared_cards_keep_public_permalink_and_private_media_through_ingress(self):
        account = self._create_account("instagram", self.INSTAGRAM_ID, team=self.team)
        connection = self._create_connection(account, self.instagram_asset)
        envelope = self._envelope(
            object_type="instagram", mid="m_shared_cards", text="Confira estes produtos"
        )
        envelope["entry"][0]["messaging"][0]["message"]["attachments"] = [
            None,
            {
                "type": "ig_post",
                "payload": {
                    "url": "https://www.instagram.com/p/PublicPost/?access_token=private",
                    "title": "Produto público",
                },
            },
            {
                "type": "ig_reel",
                "payload": {
                    "url": "https://video.cdninstagram.com/demo.mp4?signature=private"
                },
            },
            {
                "type": "image",
                "payload": {
                    "url": "https://lookaside.fbsbx.com/photo.png?token=private"
                },
            },
            {
                "type": "story_mention",
                "payload": {"url": "https://lookaside.fbsbx.com/opaque?token=private"},
            },
        ]
        delivery = self.create_delivery(envelope)
        _dispatch, inbox = self._dispatch(delivery)
        with trap_jobs():
            self.assertTrue(
                inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
            )
        self.assertEqual(inbox.state, "done")
        binding = (
            self.env["contact.center.message.binding"]
            .search(
                [
                    ("provider_connection_id", "=", connection.id),
                    ("external_message_id", "=", "m_shared_cards"),
                ]
            )
            .ensure_one()
        )
        content = binding.structured_content_json
        self.assertEqual(content["type"], "shared")
        self.assertEqual(
            [item["kind"] for item in content["items"]], ["post", "reel", "story"]
        )
        self.assertEqual(
            content["items"][0]["url"], "https://www.instagram.com/p/PublicPost/"
        )
        self.assertEqual(content["items"][0]["title"], "Produto público")
        self.assertNotIn("url", content["items"][1])
        self.assertNotIn("url", content["items"][2])
        self.assertEqual(
            set(binding.media_ids.mapped("external_media_id")),
            {"m_shared_cards:2", "m_shared_cards:3"},
        )
        self.assertEqual(set(binding.media_ids.mapped("kind")), {"image", "video"})
        ui = (
            self.env["contact.center.ui.api"]
            .with_user(self.agent)
            ._serialize_message(binding.message_id, binding)
        )
        self.assertEqual(ui["structured_content"], content)
        for value in (
            delivery.sanitized_envelope_json,
            delivery.item_ids.payload_json,
            inbox.raw_envelope_json,
            inbox.normalized_dto_json,
            ui,
        ):
            self.assertNotIn("token=private", str(value))
            self.assertNotIn("signature=private", str(value))
        # A repeated provider delivery binds to the same domain message/cards.
        envelope["entry"][0]["time"] += 1
        _dispatch, replay = self._dispatch(self.create_delivery(envelope))
        self.assertEqual(replay, inbox)

    def test_story_reply_media_download_retains_text_and_enforces_private_slot_ownership(
        self,
    ):
        account = self._create_account("instagram", self.INSTAGRAM_ID, team=self.team)
        connection = self._create_connection(account, self.instagram_asset)
        envelope = self._envelope(
            object_type="instagram", mid="m_story_card", text="Qual o preço?"
        )
        private_url = "https://lookaside.fbsbx.com/story.png?token=private"
        envelope["entry"][0]["messaging"][0]["message"]["reply_to"] = {
            "story": {"id": "123456789", "url": private_url}
        }
        delivery = self.create_delivery(envelope)
        _dispatch, inbox = self._dispatch(delivery)
        with trap_jobs():
            self.assertTrue(
                inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
            )
        self.assertEqual(inbox.state, "done")
        binding = (
            self.env["contact.center.message.binding"]
            .search(
                [
                    ("provider_connection_id", "=", connection.id),
                    ("external_message_id", "=", "m_story_card"),
                ]
            )
            .ensure_one()
        )
        self.assertIn("Qual o preço?", str(binding.message_id.body))
        self.assertEqual(binding.structured_content_json["items"][0]["kind"], "story")
        media = binding.media_ids.ensure_one()
        self.assertEqual(media.external_media_id, "m_story_card:story")
        response = FakeMediaResponse(
            content=b"story-private-bytes", headers={"Content-Type": "image/png"}
        )
        with mock.patch(
            "odoo.addons.contact_center_meta.services.media.requests.request",
            return_value=response,
        ) as request:
            downloaded = connection.get_adapter().download_media(
                connection, media._as_dto()
            )
            self.assertEqual(downloaded.content, b"story-private-bytes")
            self.assertEqual(request.call_args.args[1], private_url)
        self.assertTrue(response.closed)
        self.assertNotIn(private_url, str(inbox.normalized_dto_json))
        self.assertNotIn(private_url, str(binding.structured_content_json))

    def test_social_card_rejects_nonpublic_urls_without_erasing_the_shared_label(self):
        urls = [
            "https://cdninstagram.com/private.jpg?access_token=secret",
            "https://www.instagram.com.evil.invalid/p/Public/",
            "https://user:secret@www.instagram.com/p/Public/",
            "https://www.instagram.com/direct/t/private/",
        ]
        for index, url in enumerate(urls):
            envelope = self._envelope(mid="m_public_link_guard_%s" % index, text="")
            envelope["entry"][0]["messaging"][0]["message"]["attachments"] = [
                {"type": "share", "payload": {"url": url}}
            ]
            _dispatch, inbox = self._dispatch(self.create_delivery(envelope))
            with trap_jobs():
                inbox.with_context(job_uuid=inbox.queue_job_uuid)._job_process()
            if inbox.state == "unsupported":
                # A URL rejected at the authenticated ingress is quarantined.
                continue
            self.assertEqual(inbox.state, "done")
            content = inbox.normalized_dto_json["message"]["structured_content"]
            self.assertTrue(content["items"][0]["title"])
            self.assertNotIn("url", content["items"][0])

    def test_owner_only_account_projects_and_enqueues_the_new_webhook(self):
        self.account.write(
            {
                "access_user_ids": [(6, 0, self.agent.ids)],
                "access_team_ids": [(6, 0, [])],
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
                    "access_user_ids": [(6, 0, [])],
                    "access_team_ids": [(6, 0, [])],
                }
            )

        self.account.invalidate_recordset(["access_user_ids", "access_team_ids"])
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
