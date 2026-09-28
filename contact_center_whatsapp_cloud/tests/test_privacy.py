import copy
import json
from contextlib import contextmanager
from unittest import mock

from odoo.exceptions import ValidationError

from .common import WhatsAppCloudCase
from .test_attribution import CDN_IMAGE, _referral_fixture

PERMALINK = "https://www.facebook.com/SolozIndustrial/posts/pfbid02privacy"


class TestWhatsAppCloudPrivacy(WhatsAppCloudCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account.write(
            {"conversation_ignore_enabled": True, "conversation_delete_enabled": True}
        )

    def _api(self):
        return self.env["contact.center.ui.api"].with_user(self.agent)

    def _two_contacts(self):
        first = self.text_message(body="Conteúdo do primeiro contato")
        second = self.text_message(
            body="Conteúdo do segundo contato",
            sender=self.OTHER_CUSTOMER,
            user_id=self.OTHER_BSUID,
        )
        contacts = [
            self.contact_block(),
            self.contact_block(wa_id=self.OTHER_CUSTOMER, user_id=self.OTHER_BSUID),
        ]
        delivery, inboxes = self.deliver(
            self.envelope(self.value(messages=[first, second], contacts=contacts))
        )
        return delivery, inboxes, first, second

    def test_conversation_route_recognizes_cloud_events(self):
        message = self.text_message()
        delivery, inbox = self.deliver(self.envelope(self.value(messages=[message])))
        policy = self.env["contact.center.conversation.ignore"]
        route = policy._route(self.connection, inbox.raw_envelope_json)
        expected_ref = "%s@s.whatsapp.net" % self.CUSTOMER
        self.assertEqual(route["conversation_type"], "direct")
        self.assertEqual(route["conversation_ref"], expected_ref)
        self.assertIn(("whatsapp.pn", expected_ref), route["addresses"])
        self.assertIn(
            ("whatsapp.bsuid", "%s:%s" % (self.WABA_ID, self.CUSTOMER_BSUID)),
            route["addresses"],
        )
        self.assertEqual(
            policy._route(self.connection, delivery.item_ids.payload_json), route
        )
        status_payload = self.create_delivery(
            self.envelope(self.value(statuses=[self.status(message["id"])]))
        ).item_ids.payload_json
        self.assertEqual(
            policy._route(self.connection, status_payload)["conversation_ref"],
            expected_ref,
        )

    def test_ignored_conversation_is_not_claimed_or_projected(self):
        first = self.text_message(body="Antes de ignorar")
        self.deliver(self.envelope(self.value(messages=[first])))
        channel = self.binding_for(first["id"]).channel_binding_id.channel_id
        self._api().set_conversation_ignored(channel.id, True)
        ignored = self.text_message(body="Não deve persistir no ledger")
        delivery, inboxes = self.deliver(self.envelope(self.value(messages=[ignored])))
        item = delivery.item_ids.ensure_one()
        self.assertEqual(item.kind, "unknown")
        self.assertNotIn("Não deve persistir", json.dumps(item.payload_json))
        self.assertFalse(inboxes)
        self.assertFalse(self.binding_for(ignored["id"]))

    def test_delete_removes_only_the_contact_items_events_and_links(self):
        delivery, inboxes, first, second = self._two_contacts()
        items = delivery.item_ids.sorted("sequence")
        digests = items.mapped("event_sha256")
        occurrences = items.mapped("occurrence_ref")
        link_model = self.env["contact.center.whatsapp.cloud.referral.link"]
        first_link = link_model._register_source_link(
            self.connection, first["id"], PERMALINK
        )
        second_link = link_model._register_source_link(
            self.connection, second["id"], PERMALINK
        )
        channel = self.binding_for(first["id"]).channel_binding_id.channel_id
        first_inbox = inboxes.filtered(
            lambda inbox: inbox.raw_envelope_json["message"]["id"] == first["id"]
        )
        second_inbox = inboxes - first_inbox
        self._api().delete_conversation(channel.id)

        items.invalidate_recordset()
        self.assertEqual(items[0].payload_json["reason"], "consumer_content_erased")
        self.assertEqual(
            items[1].payload_json["message"]["text"]["body"],
            "Conteúdo do segundo contato",
        )
        self.assertEqual(items.mapped("event_sha256"), digests)
        self.assertEqual(items.mapped("occurrence_ref"), occurrences)
        first_inbox.invalidate_recordset()
        self.assertTrue(first_inbox.metadata_json.get("content_erased"))
        self.assertNotIn(
            "primeiro contato", json.dumps(first_inbox.sudo().raw_envelope_json)
        )
        self.assertEqual(
            second_inbox.sudo().raw_envelope_json["message"]["id"], second["id"]
        )
        self.assertFalse(link_model.sudo().search([("reference", "=", first_link)]))
        self.assertTrue(link_model.sudo().search([("reference", "=", second_link)]))
        self.assertFalse(self.binding_for(first["id"]))
        self.assertTrue(self.binding_for(second["id"]))

    def test_redelivery_after_deletion_does_not_resurrect_content(self):
        message = self.text_message(body="Conteúdo apagado")
        _delivery, receipt = self.deliver(self.envelope(self.value(messages=[message])))
        channel = self.binding_for(message["id"]).channel_binding_id.channel_id
        self._api().delete_conversation(channel.id)
        # Meta redelivers the same message within its 7-day window.
        envelope = self.envelope(self.value(messages=[message]))
        envelope["entry"][0]["time"] = 1_900_000_000
        delivery, inboxes = self.deliver(envelope)
        # The erased occurrence is not even reclaimed (CC-WAC-01).
        self.assertFalse(inboxes)
        item = delivery.item_ids.ensure_one()
        self.assertEqual(item.kind, "unknown")
        self.assertEqual(item.payload_json["reason"], "consumer_unavailable")
        self.assertNotIn("Conteúdo apagado", json.dumps(item.payload_json))
        receipt.invalidate_recordset()
        self.assertTrue(receipt.metadata_json.get("content_erased"))
        self.assertEqual(receipt.state, "blocked")
        self.assertFalse(self.binding_for(message["id"]))
        self.assertFalse(channel.exists())

    # -- CC-WAC-01: deletion markers of items that had no inbox event yet -------

    def _dispatcher_type(self):
        return type(self.env["meta.webhook.dispatcher"])

    @contextmanager
    def _claimed_as_if_racing_the_deletion(self):
        """Claim as an ingestion whose snapshot predates the deletion did."""

        with mock.patch.object(
            self._dispatcher_type(),
            "_wac_unerased_item_keys",
            autospec=True,
            side_effect=lambda _model, _endpoint, _specs, eligible: eligible,
        ):
            yield

    def _redelivered(self, message, *, time_value):
        """The same message inside another delivery body, with another contact."""

        other = self.text_message(
            body="Outro contato no mesmo lote",
            sender=self.OTHER_CUSTOMER,
            user_id=self.OTHER_BSUID,
        )
        contacts = [
            self.contact_block(),
            self.contact_block(wa_id=self.OTHER_CUSTOMER, user_id=self.OTHER_BSUID),
        ]
        envelope = self.envelope(
            self.value(messages=[message, other], contacts=contacts)
        )
        envelope["entry"][0]["time"] = time_value
        return envelope, other

    def _assert_never_projected(self, message, secret):
        inboxes = self.env["contact.center.inbox.event"].sudo().search([])
        self.assertNotIn(secret, json.dumps(inboxes.mapped("raw_envelope_json")))
        self.assertFalse(
            self.env["contact.center.message.binding"]
            .sudo()
            .search([("external_message_id", "=", message["id"])])
        )
        self.assertFalse(
            self.env["contact.center.channel.binding"]
            .sudo()
            .search(
                [
                    ("account_id", "=", self.account.id),
                    ("conversation_ref", "=", "%s@s.whatsapp.net" % self.CUSTOMER),
                ]
            )
        )
        items = self.env["meta.webhook.item"].sudo().search([])
        self.assertNotIn(secret, json.dumps(items.mapped("payload_json")))

    def _delete_with_an_undispatched_item(self, secret):
        first = self.text_message(body="Mensagem já projetada")
        self.deliver(self.envelope(self.value(messages=[first])))
        channel = self.binding_for(first["id"]).channel_binding_id.channel_id
        pending = self.text_message(body=secret)
        delivery = self.create_delivery(self.envelope(self.value(messages=[pending])))
        item = delivery.item_ids.ensure_one()
        self.assertEqual(item.kind, "messaging")
        self.assertFalse(item.dispatch_ids, "not dispatched before the deletion")
        self._api().delete_conversation(channel.id)
        item.invalidate_recordset()
        self.assertEqual(item.payload_json["reason"], "consumer_content_erased")
        (result,) = self.dispatch_all(delivery)
        self.assertEqual(result["result_ref"], "contact.center.content_erased")
        return pending

    def test_deletion_between_ingestion_and_dispatch_survives_a_redelivery(self):
        secret = "Conteúdo apagado antes do despacho"
        pending = self._delete_with_an_undispatched_item(secret)
        envelope, other = self._redelivered(pending, time_value=1_900_000_000)
        delivery, inboxes = self.deliver(envelope)
        erased, kept = delivery.item_ids.sorted("sequence")
        # The erased occurrence is never reclaimed: a content-free placeholder.
        self.assertEqual(erased.kind, "unknown")
        self.assertEqual(erased.payload_json["reason"], "consumer_unavailable")
        self.assertEqual(kept.kind, "messaging")
        self.assertEqual(len(inboxes), 1)
        self.assertEqual(inboxes.raw_envelope_json["message"]["id"], other["id"])
        self.assertEqual(inboxes.state, "done", inboxes.last_error_message)
        self._assert_never_projected(pending, secret)

    def test_claim_racing_a_deletion_is_erased_at_dispatch(self):
        secret = "Conteúdo reivindicado durante a exclusão"
        pending = self._delete_with_an_undispatched_item(secret)
        envelope, _other = self._redelivered(pending, time_value=1_900_000_001)
        with self._claimed_as_if_racing_the_deletion():
            delivery = self.create_delivery(envelope)
        raced = delivery.item_ids.sorted("sequence")[0]
        self.assertEqual(raced.payload_json["message"]["id"], pending["id"])
        dispatch = (
            self.fanout(delivery)
            .filtered(lambda row: row.item_id == raced)
            .ensure_one()
        )
        self.assertEqual(
            self.dispatch_one(dispatch)["result_ref"],
            "contact.center.content_erased",
        )
        raced.invalidate_recordset()
        self.assertEqual(raced.payload_json["reason"], "consumer_content_erased")
        self._assert_never_projected(pending, secret)

    # -- CC-WAC-02: receipts of a replaced connection of the same route --------

    def _replace_connection(self):
        self.connection.write({"outbound_active": False})
        self.connection.write({"active": False})
        replacement = self._create_connection(self.account, self.waba_asset)
        replacement.action_whatsapp_cloud_configure_webhook()
        return replacement

    def test_replacement_reuses_the_route_receipt_of_a_redelivery(self):
        message = self.text_message(body="Recebida antes da troca")
        _delivery, (inbox,) = self.deliver(
            self.envelope(self.value(messages=[message]))
        )
        replacement = self._replace_connection()
        envelope, other = self._redelivered(message, time_value=1_900_000_002)
        _delivery, inboxes = self.deliver(envelope)
        # Only the other contact's message is new; the redelivery reuses the
        # replaced connection's receipt instead of projecting a duplicate.
        self.assertIn(inbox, inboxes)
        new = inboxes - inbox
        self.assertEqual(new.provider_connection_id, replacement)
        self.assertEqual(new.raw_envelope_json["message"]["id"], other["id"])
        self.assertEqual(
            len(
                self.env["contact.center.message.binding"]
                .sudo()
                .search([("external_message_id", "=", message["id"])])
            ),
            1,
        )

    def test_deleted_message_redelivered_after_replacement_stays_erased(self):
        secret = "Apagada antes da troca de conexão"
        message = self.text_message(body=secret)
        self.deliver(self.envelope(self.value(messages=[message])))
        channel = self.binding_for(message["id"]).channel_binding_id.channel_id
        self._api().delete_conversation(channel.id)
        replacement = self._replace_connection()
        envelope, _other = self._redelivered(message, time_value=1_900_000_003)
        self.deliver(envelope)
        self._assert_never_projected(message, secret)
        # Even without the item markers, the erased receipt of the replaced
        # connection of this route keeps the content out.
        envelope, _other = self._redelivered(message, time_value=1_900_000_004)
        with mock.patch.object(
            self._dispatcher_type(),
            "_wac_erased_occurrences",
            autospec=True,
            return_value=set(),
        ):
            _delivery, inboxes = self.deliver(envelope)
        receipt = inboxes.filtered(
            lambda inbox: inbox.provider_connection_id != replacement
        ).ensure_one()
        self.assertTrue(receipt.metadata_json.get("content_erased"))
        self._assert_never_projected(message, secret)

    # -- CC-WAC-06: ownership of claimed items outlives the subscription ------

    def _claimed_before_fan_out(self, secret):
        first = self.text_message(body="Mensagem já projetada")
        self.deliver(self.envelope(self.value(messages=[first])))
        channel = self.binding_for(first["id"]).channel_binding_id.channel_id
        pending = self.text_message(body=secret)
        delivery = self.create_delivery(self.envelope(self.value(messages=[pending])))
        item = delivery.item_ids.ensure_one()
        self.assertEqual(item.kind, "messaging")
        self.assertFalse(item.dispatch_ids, "no fan-out before the deletion")
        return channel, delivery, item, pending

    def _assert_erased_for_good(self, delivery, item, pending, secret, time_value):
        item.invalidate_recordset()
        self.assertEqual(item.payload_json.get("reason"), "consumer_content_erased")
        # Fan-out and dispatch after re-activation, then a redelivery.
        (result,) = self.dispatch_all(delivery)
        self.assertEqual(result["result_ref"], "contact.center.content_erased")
        envelope, _other = self._redelivered(pending, time_value=time_value)
        self.deliver(envelope)
        self._assert_never_projected(pending, secret)

    def test_deletion_with_the_subscription_disabled_erases_claimed_items(self):
        secret = "Reivindicada antes de desativar a assinatura"
        channel, delivery, item, pending = self._claimed_before_fan_out(secret)
        self.subscription.write({"active": False})
        self._api().delete_conversation(channel.id)
        self.assertFalse(channel.exists())
        self.connection.action_whatsapp_cloud_configure_webhook()
        self.assertTrue(self.subscription.active)
        self._assert_erased_for_good(delivery, item, pending, secret, 1_900_000_005)

    def test_deletion_after_the_last_route_retired_erases_claimed_items(self):
        secret = "Reivindicada antes de arquivar a conexão"
        channel, delivery, item, pending = self._claimed_before_fan_out(secret)
        # Archiving the last live route auto-archives the subscription.
        self.connection.write({"outbound_active": False})
        self.connection.write({"active": False})
        self.assertFalse(self.subscription.active)
        self._api().delete_conversation(channel.id)
        replacement = self._create_connection(self.account, self.waba_asset)
        replacement.action_whatsapp_cloud_configure_webhook()
        self._assert_erased_for_good(delivery, item, pending, secret, 1_900_000_006)

    def test_deletion_never_silently_keeps_content_it_cannot_erase(self):
        secret = "Compartilhada com outro consumidor"
        # Another consumer of the same business account field shares the item.
        self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.waba.id,
                "consumer_key": "another.consumer",
                "object_type": "whatsapp_business_account",
                "field_name": "messages",
            }
        )
        channel, _delivery, item, _pending = self._claimed_before_fan_out(secret)
        with self.assertRaises(ValidationError):
            self._api().delete_conversation(channel.id)
        self.assertTrue(channel.exists())
        item.invalidate_recordset()
        self.assertEqual(item.payload_json["message"]["text"]["body"], secret)

    # -- CC-WAC-09: a demoted number keeps its privacy admission --------------

    def test_ignored_conversation_on_a_demoted_route_is_never_claimed(self):
        # Another number of the business account keeps the subscription live.
        other_account = self._create_account(team=self.team)
        self._create_connection(other_account, self.waba_asset, self.OTHER_PHONE_ID)
        first = self.text_message(body="Antes de ignorar")
        self.deliver(self.envelope(self.value(messages=[first])))
        channel = self.binding_for(first["id"]).channel_binding_id.channel_id
        self._api().set_conversation_ignored(channel.id, True)
        self.connection.write({"outbound_active": False})
        self.connection.action_set_standby()
        self.assertEqual(self.connection.role, "standby")
        self.assertTrue(self.subscription.active)
        ignored = self.text_message(body="Contato ignorado no número rebaixado")
        other = self.text_message(
            body="Outro contato no número rebaixado",
            sender=self.OTHER_CUSTOMER,
            user_id=self.OTHER_BSUID,
        )
        contacts = [
            self.contact_block(),
            self.contact_block(wa_id=self.OTHER_CUSTOMER, user_id=self.OTHER_BSUID),
        ]
        delivery = self.create_delivery(
            self.envelope(self.value(messages=[ignored, other], contacts=contacts))
        )
        dropped, claimed = delivery.item_ids.sorted("sequence")
        self.assertEqual(dropped.kind, "unknown")
        self.assertEqual(dropped.payload_json["reason"], "consumer_unavailable")
        self.assertNotIn("Contato ignorado", json.dumps(dropped.payload_json))
        # The demoted number still claims everyone else's messages.
        self.assertEqual(claimed.kind, "messaging")
        self.assertEqual(claimed.payload_json["message"]["id"], other["id"])

    # -- CC-WAC-10: every owner is known before anything is erased ------------

    def test_deletion_counts_a_consumer_archived_before_fan_out(self):
        secret = "Recebida enquanto outro consumidor assinava"
        first = self.text_message(body="Mensagem já projetada")
        first_delivery, _inbox = self.deliver(
            self.envelope(self.value(messages=[first]))
        )
        channel = self.binding_for(first["id"]).channel_binding_id.channel_id
        foreign = self.env["meta.webhook.subscription"].create(
            {
                "page_id": self.waba.id,
                "consumer_key": "another.consumer",
                "object_type": "whatsapp_business_account",
                "field_name": "messages",
            }
        )
        pending = self.text_message(body=secret)
        delivery = self.create_delivery(self.envelope(self.value(messages=[pending])))
        item = delivery.item_ids.ensure_one()
        self.assertFalse(item.dispatch_ids, "no fan-out before the deletion")
        # Archived before fan-out: reactivating it would still deliver the item.
        foreign.write({"active": False})
        with self.assertRaises(ValidationError):
            self._api().delete_conversation(channel.id)
        self.assertTrue(channel.exists())
        self.assertTrue(self.binding_for(first["id"]))
        item.invalidate_recordset()
        self.assertEqual(item.payload_json["message"]["text"]["body"], secret)
        first_item = first_delivery.item_ids.ensure_one()
        first_item.invalidate_recordset()
        self.assertEqual(first_item.payload_json["message"]["id"], first["id"])

    # -- CC-WAC-13: a claim belongs to the inbox its number served -----------

    def _move_number_to_another_inbox(self):
        """Archive inbox A's route and serve the same number from inbox B."""

        agent_b = self._create_user("Agent B", self.agent_group)
        account_b = self._create_account(team=self._create_team("B", agents=agent_b))
        self.connection.write({"outbound_active": False})
        self.connection.write({"active": False})
        connection_b = self._create_connection(account_b, self.waba_asset)
        connection_b.action_whatsapp_cloud_configure_webhook()
        return account_b, connection_b

    def test_number_moved_to_another_inbox_keeps_each_inbox_claims(self):
        first = self.text_message(body="Conversa da caixa A")
        self.deliver(self.envelope(self.value(messages=[first])))
        channel_a = self.binding_for(first["id"]).channel_binding_id.channel_id
        left_in_a = self.text_message(body="Reivindicada para a caixa A")
        delivery_a = self.create_delivery(
            self.envelope(self.value(messages=[left_in_a]))
        )
        item_a = delivery_a.item_ids.ensure_one()
        self.assertEqual(item_a.payload_json["claim"], {"account_id": self.account.id})
        account_b, connection_b = self._move_number_to_another_inbox()
        # A's claim never reaches the inbox the number now serves.
        (result,) = self.dispatch_all(delivery_a)
        self.assertIsNone(result)
        self.assertFalse(self.binding_for(left_in_a["id"], connection_b))
        dispatched = self.text_message(body="Projetada na caixa B")
        delivery_b1, inbox_b = self.deliver(
            self.envelope(self.value(messages=[dispatched]))
        )
        self.assertEqual(inbox_b.provider_connection_id, connection_b)
        self.assertEqual(inbox_b.state, "done", inbox_b.last_error_message)
        pending = self.text_message(body="Pendente na caixa B")
        delivery_b2 = self.create_delivery(
            self.envelope(self.value(messages=[pending]))
        )
        items_b = delivery_b1.item_ids | delivery_b2.item_ids
        self.assertEqual(
            [item.payload_json["claim"] for item in items_b],
            [{"account_id": account_b.id}] * 2,
        )
        # An agent of inbox A deletes A's conversation: only A's claims go.
        self._api().delete_conversation(channel_a.id)
        item_a.invalidate_recordset()
        self.assertEqual(item_a.payload_json["reason"], "consumer_content_erased")
        items_b.invalidate_recordset()
        self.assertEqual(
            sorted(item.payload_json["message"]["id"] for item in items_b),
            sorted([dispatched["id"], pending["id"]]),
        )
        self.assertTrue(self.binding_for(dispatched["id"], connection_b))
        # B's pending claim is still projected into inbox B.
        (result,) = self.dispatch_all(delivery_b2)
        inbox = self.process_inbox(self.inbox_from(result))
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        self.assertTrue(self.binding_for(pending["id"], connection_b))

    # -- CC-WAC-16: an occurrence stays with the inbox that first claimed it --

    def test_redelivery_after_a_transfer_stays_with_the_first_inbox(self):
        projected = self.text_message(body="Projetada na caixa A")
        self.deliver(self.envelope(self.value(messages=[projected])))
        pending = self.text_message(body="Pendente na caixa A")
        delivery_a = self.create_delivery(self.envelope(self.value(messages=[pending])))
        account_b, connection_b = self._move_number_to_another_inbox()
        # Meta repeats both inside another body, next to a new message.
        fresh = self.text_message(
            body="Nova na caixa B",
            sender=self.OTHER_CUSTOMER,
            user_id=self.OTHER_BSUID,
        )
        contacts = [
            self.contact_block(),
            self.contact_block(wa_id=self.OTHER_CUSTOMER, user_id=self.OTHER_BSUID),
        ]
        envelope = self.envelope(
            self.value(messages=[projected, pending, fresh], contacts=contacts)
        )
        envelope["entry"][0]["time"] = 1_900_000_010
        delivery, inboxes = self.deliver(envelope)
        repeated_projected, repeated_pending, new = delivery.item_ids.sorted("sequence")
        for repeated in (repeated_projected, repeated_pending):
            self.assertEqual(repeated.kind, "unknown")
            self.assertEqual(repeated.payload_json["reason"], "consumer_unavailable")
            self.assertNotIn("caixa A", json.dumps(repeated.payload_json))
        self.assertEqual(new.payload_json["claim"], {"account_id": account_b.id})
        self.assertEqual(inboxes.ensure_one().provider_connection_id, connection_b)
        self.assertEqual(inboxes.raw_envelope_json["message"]["id"], fresh["id"])
        self.assertFalse(self.binding_for(projected["id"], connection_b))
        self.assertFalse(self.binding_for(pending["id"], connection_b))
        # A's own pending claim stays A's: unrouted while the number serves B.
        (result,) = self.dispatch_all(delivery_a)
        self.assertIsNone(result)
        self.assertFalse(self.binding_for(pending["id"], connection_b))

    # -- CC-WAC-18: deletion drops the private thumbnail URLs of the conversation

    def _ad_message(self, **values):
        message = self.text_message(**values)
        message["referral"] = copy.deepcopy(_referral_fixture()["referral"])
        return message

    def _locators_for(self, wamid):
        return (
            self.env["contact.center.ad.preview.locator"]
            .sudo()
            .search([("source_key", "=", wamid)], order="id")
        )

    def _assert_consumed(self, locator):
        locator.invalidate_recordset()
        self.assertTrue(locator.consumed)
        self.assertFalse(locator.download_url)
        self.assertTrue(locator.exists(), "the content-free marker remains")

    def _assert_intact(self, locator):
        locator.invalidate_recordset()
        self.assertFalse(locator.consumed)
        self.assertEqual(locator.download_url, CDN_IMAGE)

    def test_deletion_consumes_the_conversation_thumbnail_locators(self):
        first = self.text_message(body="Conversa existente")
        self.deliver(self.envelope(self.value(messages=[first])))
        channel = self.binding_for(first["id"]).channel_binding_id.channel_id
        # Before dispatch: ingested only.
        before_dispatch = self._ad_message()
        self.create_delivery(self.envelope(self.value(messages=[before_dispatch])))
        # Before the worker: dispatched, its inbox event not processed yet.
        before_worker = self._ad_message()
        _delivery, inbox = self.deliver(
            self.envelope(self.value(messages=[before_worker])), process=False
        )
        self.assertEqual(inbox.state, "pending")
        # Another contact of this inbox keeps its own.
        other_contact = self._ad_message(
            sender=self.OTHER_CUSTOMER, user_id=self.OTHER_BSUID
        )
        self.create_delivery(
            self.envelope(
                self.value(
                    messages=[other_contact],
                    contacts=[
                        self.contact_block(
                            wa_id=self.OTHER_CUSTOMER, user_id=self.OTHER_BSUID
                        )
                    ],
                )
            )
        )
        # Another inbox's locator for the same message ID is never touched.
        other_inbox = self._create_connection(
            self._create_account(team=self.team), self.waba_asset, self.OTHER_PHONE_ID
        )
        self.env["contact.center.ad.preview.locator"]._register_thumbnail_locator(
            other_inbox, before_dispatch["id"], CDN_IMAGE
        )
        own_pending, foreign = self._locators_for(before_dispatch["id"])
        self.assertEqual(own_pending.connection_id, self.connection)
        self.assertEqual(foreign.connection_id, other_inbox)
        own_dispatched = self._locators_for(before_worker["id"]).ensure_one()
        other_contacts = self._locators_for(other_contact["id"]).ensure_one()
        for locator in (own_pending, own_dispatched, other_contacts, foreign):
            self._assert_intact(locator)

        self._api().delete_conversation(channel.id)

        self._assert_consumed(own_pending)
        self._assert_consumed(own_dispatched)
        self._assert_intact(other_contacts)
        self._assert_intact(foreign)

    def test_deletion_consumes_a_locator_rebound_for_a_replacement(self):
        first = self.text_message(body="Conversa existente")
        self.deliver(self.envelope(self.value(messages=[first])))
        channel = self.binding_for(first["id"]).channel_binding_id.channel_id
        message = self._ad_message()
        delivery = self.create_delivery(self.envelope(self.value(messages=[message])))
        original = self._locators_for(message["id"]).ensure_one()
        self.connection.write({"outbound_active": False})
        self.connection.write({"active": False})
        replacement = self._create_connection(self.account, self.waba_asset)
        replacement.action_whatsapp_cloud_configure_webhook()
        self.inbox_from(self.dispatch_all(delivery)[0])
        rebound = self._locators_for(message["id"]) - original
        self.assertEqual(rebound.ensure_one().connection_id, replacement)
        self._assert_consumed(original)
        self._assert_intact(rebound)
        # Deleted before the worker ran: the rebound URL goes too.
        self._api().delete_conversation(channel.id)
        self._assert_consumed(rebound)
