import json
from contextlib import contextmanager
from unittest import mock

from psycopg2 import IntegrityError

from odoo.exceptions import ValidationError
from odoo.tools import mute_logger

from odoo.addons.meta_webhook_base.services.tokens import META_WEBHOOK_INTERNAL_TOKEN
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.contracts import WHATSAPP_CLOUD_CONSUMER_KEY
from .common import WhatsAppCloudCase


class TestWhatsAppCloudConsumer(WhatsAppCloudCase):
    def _inbox_count(self, connection=None):
        return (
            self.env["contact.center.inbox.event"]
            .sudo()
            .search_count(
                [("provider_connection_id", "=", (connection or self.connection).id)]
            )
        )

    def _failures(self, kind=None):
        domain = [("connection_id", "=", self.connection.id)]
        if kind:
            domain.append(("kind", "=", kind))
        return (
            self.env["contact.center.whatsapp.cloud.delivery.failure"]
            .sudo()
            .search(domain)
        )

    # -- 3: subscription gate, one inbox event per item, only at dispatch -------

    def test_without_active_subscription_nothing_is_claimed_or_projected(self):
        self.subscription.write({"active": False})
        message = self.text_message(body="conteúdo que nunca deve persistir")
        delivery = self.create_delivery(self.envelope(self.value(messages=[message])))
        item = delivery.item_ids.ensure_one()
        self.assertEqual(item.kind, "unknown")
        self.assertEqual(item.payload_json["reason"], "consumer_unavailable")
        self.assertNotIn("nunca deve persistir", json.dumps(item.payload_json))
        self.assertFalse(self.fanout(delivery))
        self.assertEqual(self._inbox_count(), 0)

    def test_inbox_events_are_created_only_at_dispatch_one_per_item(self):
        first = self.text_message(body="Primeira")
        second = self.text_message(body="Segunda")
        delivery = self.create_delivery(
            self.envelope(self.value(messages=[first, second]))
        )
        items = delivery.item_ids.sorted("sequence")
        self.assertEqual(items.mapped("kind"), ["messaging", "messaging"])
        self.assertEqual(
            items.mapped("item_key"),
            ["entry:0:changes:0:messages:0", "entry:0:changes:0:messages:1"],
        )
        self.assertEqual(self._inbox_count(), 0, "ingestion never projects")
        payload = items[0].payload_json
        self.assertEqual(payload["metadata"]["phone_number_id"], self.PHONE_ID)
        self.assertEqual(payload["contact"]["wa_id"], self.CUSTOMER)
        self.assertEqual(payload["message"]["id"], first["id"])
        dispatches = self.fanout(delivery)
        self.assertEqual(len(dispatches), 2)
        self.assertEqual(self._inbox_count(), 0, "fan-out never projects")
        inboxes = self.env["contact.center.inbox.event"]
        for dispatch in dispatches:
            inboxes |= self.inbox_from(self.dispatch_one(dispatch))
        self.assertEqual(len(inboxes), 2)
        self.assertEqual(
            sorted(inbox.raw_envelope_json["message"]["id"] for inbox in inboxes),
            sorted([first["id"], second["id"]]),
        )
        for inbox in inboxes:
            self.assertEqual(
                inbox.metadata_json["technical_ledger"], "meta_webhook_base"
            )
            self.assertEqual(
                inbox.metadata_json["meta_delivery_ref"], delivery.public_ref
            )

    def test_subscription_disabled_after_receipt_prevents_projection(self):
        delivery = self.create_delivery(
            self.envelope(self.value(messages=[self.text_message()]))
        )
        dispatch = self.fanout(delivery).ensure_one()
        self.subscription.write({"active": False})
        self.assertTrue(dispatch.queue_job_uuid)
        self.assertFalse(
            dispatch.with_context(job_uuid=dispatch.queue_job_uuid)._job_process()
        )
        self.assertEqual(dispatch.state, "stale")
        self.assertEqual(dispatch.last_error_class, "RoutingPolicyChanged")
        self.assertEqual(self._inbox_count(), 0)

    def test_batch_with_two_contacts_becomes_independent_events(self):
        first = self.text_message(body="Sou o primeiro")
        second = self.text_message(
            body="Sou o segundo", sender=self.OTHER_CUSTOMER, user_id=self.OTHER_BSUID
        )
        contacts = [
            self.contact_block(name="Primeiro"),
            self.contact_block(
                wa_id=self.OTHER_CUSTOMER, user_id=self.OTHER_BSUID, name="Segundo"
            ),
        ]
        delivery, inboxes = self.deliver(
            self.envelope(self.value(messages=[first, second], contacts=contacts))
        )
        items = delivery.item_ids.sorted("sequence")
        self.assertEqual(
            items[0].payload_json["contact"]["profile"]["name"], "Primeiro"
        )
        self.assertEqual(items[1].payload_json["contact"]["profile"]["name"], "Segundo")
        self.assertNotIn("Segundo", json.dumps(items[0].payload_json))
        self.assertEqual(set(inboxes.mapped("state")), {"done"})
        first_binding = self.binding_for(first["id"])
        second_binding = self.binding_for(second["id"])
        self.assertTrue(first_binding and second_binding)
        self.assertNotEqual(
            first_binding.channel_binding_id, second_binding.channel_binding_id
        )
        self.assertNotEqual(
            first_binding.channel_binding_id.identity_id,
            second_binding.channel_binding_id.identity_id,
        )

    def test_semantic_redelivery_reuses_the_inbox_event(self):
        message = self.text_message(body="Reenviada pela Meta")
        _delivery, first = self.deliver(self.envelope(self.value(messages=[message])))
        envelope = self.envelope(self.value(messages=[message]))
        envelope["entry"][0]["time"] = 1_900_000_000
        second_delivery, second = self.deliver(envelope)
        self.assertEqual(first, second)
        self.assertEqual(self._inbox_count(), 1)
        self.assertEqual(len(self.binding_for(message["id"])), 1)
        self.assertEqual(second_delivery.item_ids.kind, "messaging")

    def test_identical_body_is_a_single_shared_delivery(self):
        envelope = self.envelope(self.value(messages=[self.text_message()]))
        self.create_delivery(envelope)
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            with self.env.cr.savepoint():
                self.create_delivery(envelope)

    # -- 3b: routing ------------------------------------------------------------

    def _second_company_route(self):
        company = self.env["res.company"].create({"name": "WhatsApp Cloud Other Co"})
        self.env.user.company_ids |= company
        app = self._create_app(company=company)
        endpoint = self._create_endpoint(app)
        owner = self._create_waba(endpoint, self.WABA_ID)
        self._subscribe(owner)
        return company, endpoint, owner

    @contextmanager
    def _claimed_as_if_configured(self):
        """Claim as ingestion did while the route still had a connection here.

        That connection is gone, so no owning inbox can be recorded: the claim
        is owner-less, and dispatch must refuse it on its own.
        """

        dispatcher = type(self.env["meta.webhook.dispatcher"])
        with mock.patch.object(
            dispatcher,
            "_wac_configured_item_keys",
            autospec=True,
            side_effect=lambda _model, _endpoint, _specs, eligible: eligible,
        ), mock.patch.object(
            dispatcher,
            "_wac_owned_claims",
            autospec=True,
            side_effect=lambda _model, _endpoint, specs: specs,
        ):
            yield

    def _assert_placeholder(self, delivery, secret):
        item = delivery.item_ids.ensure_one()
        self.assertEqual(item.kind, "unknown")
        self.assertEqual(item.payload_json["reason"], "consumer_unavailable")
        self.assertNotIn(secret, json.dumps(item.payload_json))
        # A placeholder reaches the consumer only to be left unrouted.
        for dispatch in self.fanout(delivery):
            self.assertIsNone(self.dispatch_one(dispatch))

    def test_waba_of_another_company_is_refused(self):
        _company, endpoint, _owner = self._second_company_route()
        # No connection of that company serves the number: nothing is claimed.
        self._assert_placeholder(
            self.create_delivery(
                self.envelope(self.value(messages=[self.text_message("sigilo 1")])),
                endpoint=endpoint,
            ),
            "sigilo 1",
        )
        with self._claimed_as_if_configured():
            delivery = self.create_delivery(
                self.envelope(self.value(messages=[self.text_message()])),
                endpoint=endpoint,
            )
        self.assertEqual(delivery.item_ids.mapped("kind"), ["messaging"])
        dispatch = self.fanout(delivery).ensure_one()
        self.assertEqual(dispatch.company_id, endpoint.company_id)
        self.assertIsNone(self.dispatch_one(dispatch))
        self.assertEqual(self._inbox_count(), 0)

    def test_number_of_another_business_account_is_refused(self):
        other_owner = self._create_waba(self.endpoint, self.OTHER_WABA_ID)
        self._subscribe(other_owner)
        other_account = self._create_account(team=self.team)
        other_connection = self._create_connection(
            other_account, other_owner.asset_ids, self.OTHER_PHONE_ID
        )
        # The item comes from WABA 1 but names WABA 2's phone number.
        self._assert_placeholder(
            self.create_delivery(
                self.envelope(
                    self.value(
                        messages=[self.text_message("sigilo 2")],
                        phone_id=self.OTHER_PHONE_ID,
                    )
                )
            ),
            "sigilo 2",
        )
        with self._claimed_as_if_configured():
            delivery = self.create_delivery(
                self.envelope(
                    self.value(
                        messages=[self.text_message()], phone_id=self.OTHER_PHONE_ID
                    )
                )
            )
        self.assertEqual(delivery.item_ids.mapped("kind"), ["messaging"])
        dispatch = self.fanout(delivery).ensure_one()
        self.assertIsNone(self.dispatch_one(dispatch))
        self.assertEqual(self._inbox_count(other_connection), 0)
        self.assertEqual(self._inbox_count(), 0)

    def test_number_without_a_connection_here_is_never_claimed(self):
        # Meta sends every number of the business account to every subscribed
        # App; another tool's number keeps no content here (L11-ROUTE-01).
        unknown = self.text_message(body="mensagem de outro número")
        receipt = self.status(
            self.wamid("campaign"), "delivered", recipient="5511900001111"
        )
        mine = self.text_message(body="mensagem do número conectado")
        delivery = self.create_delivery(
            self.envelope(
                self.value(
                    messages=[unknown],
                    statuses=[receipt],
                    phone_id=self.OTHER_PHONE_ID,
                ),
                self.value(messages=[mine]),
            )
        )
        items = delivery.item_ids.sorted("sequence")
        self.assertEqual(items.mapped("kind"), ["unknown", "unknown", "messaging"])
        for item in items[:2]:
            self.assertEqual(item.payload_json["reason"], "consumer_unavailable")
            serialized = json.dumps(item.payload_json)
            self.assertNotIn("outro número", serialized)
            self.assertNotIn("5511900001111", serialized)
        results = [self.dispatch_one(dispatch) for dispatch in self.fanout(delivery)]
        self.assertIsNone(results[0])
        self.assertIsNone(results[1])
        self.assertTrue(results[2]["handled"])
        self.assertEqual(self._inbox_count(), 1)

    def test_claimed_number_without_any_connection_is_unrouted_not_retried(self):
        with self._claimed_as_if_configured():
            delivery = self.create_delivery(
                self.envelope(
                    self.value(
                        messages=[self.text_message()], phone_id=self.OTHER_PHONE_ID
                    )
                )
            )
        self.assertEqual(delivery.item_ids.mapped("kind"), ["messaging"])
        dispatch = self.fanout(delivery).ensure_one()
        with trap_jobs():
            self.assertFalse(
                dispatch.with_context(job_uuid=dispatch.queue_job_uuid)._job_process()
            )
        self.assertEqual(dispatch.state, "unrouted")
        self.assertEqual(self._inbox_count(), 0)

    def test_same_business_account_of_another_endpoint_is_refused(self):
        # One company, two Apps: each endpoint owns the same business account.
        app = self._create_app()
        endpoint = self._create_endpoint(app)
        owner = self._create_waba(endpoint, self.WABA_ID)
        self._subscribe(owner)
        # That endpoint served the number once; the live route is endpoint 1's.
        other_account = self._create_account(team=self.team)
        self._create_connection(
            other_account, owner.asset_ids, self.PHONE_ID, active=False
        )
        with self._claimed_as_if_configured():
            delivery = self.create_delivery(
                self.envelope(self.value(messages=[self.text_message()])),
                endpoint=endpoint,
            )
        self.assertEqual(delivery.item_ids.mapped("kind"), ["messaging"])
        dispatch = self.fanout(delivery).ensure_one()
        self.assertEqual(dispatch.page_id, owner)
        self.assertIsNone(self.dispatch_one(dispatch))
        self.assertEqual(self._inbox_count(), 0)

    def test_two_live_connections_for_one_number_are_refused(self):
        other_account = self._create_account(team=self.team)
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            with self.env.cr.savepoint():
                self._create_connection(other_account, self.waba_asset)
                self.env.flush_all()
        # Defense in depth: even without the index the route is ambiguous.
        delivery = self.create_delivery(
            self.envelope(self.value(messages=[self.text_message()]))
        )
        dispatch = self.fanout(delivery).ensure_one()
        self.env.cr.execute("DROP INDEX contact_center_whatsapp_cloud_one_active_phone")
        self._create_connection(other_account, self.waba_asset)
        with self.assertRaises(ValidationError):
            self.dispatch_one(dispatch)
        self.assertEqual(self._inbox_count(), 0)

    def test_inactive_or_non_ingress_connection_is_retried_never_delivered(self):
        delivery = self.create_delivery(
            self.envelope(self.value(messages=[self.text_message()]))
        )
        dispatch = self.fanout(delivery).ensure_one()
        # Concurrent archival between fan-out and dispatch.
        self.connection.write({"active": False})
        with self.assertRaises(RetryableJobError):
            self.dispatch_one(dispatch)
        self.assertEqual(self._inbox_count(), 0)

    def test_standby_connection_without_ingress_is_retried(self):
        delivery = self.create_delivery(
            self.envelope(self.value(messages=[self.text_message()]))
        )
        dispatch = self.fanout(delivery).ensure_one()
        self.connection.write({"outbound_active": False})
        self.connection.action_set_standby()
        self.assertFalse(self.connection.inbound_active)
        with self.assertRaises(RetryableJobError):
            self.dispatch_one(dispatch)
        self.assertEqual(self._inbox_count(), 0)

    def test_route_retry_ceiling_is_classified_and_leaves_no_inbox(self):
        # Another number of the same business account keeps the subscription.
        other_account = self._create_account(team=self.team)
        self._create_connection(other_account, self.waba_asset, self.OTHER_PHONE_ID)
        delivery = self.create_delivery(
            self.envelope(self.value(messages=[self.text_message()]))
        )
        dispatch = self.fanout(delivery).ensure_one()
        self.connection.write({"active": False})
        self.assertTrue(self.subscription.active)
        dispatch.sudo().with_context(
            meta_webhook_internal=META_WEBHOOK_INTERNAL_TOKEN
        ).write({"attempts": 7})
        with trap_jobs():
            self.assertFalse(
                dispatch.with_context(job_uuid=dispatch.queue_job_uuid)._job_process()
            )
        self.assertEqual(dispatch.state, "dead")
        self.assertEqual(dispatch.last_error_class, "RouteNotReady")
        self.assertEqual(self._inbox_count(), 0)

    def test_dispatch_job_projects_through_the_shared_policy(self):
        delivery = self.create_delivery(
            self.envelope(self.value(messages=[self.text_message()]))
        )
        dispatch = self.fanout(delivery).ensure_one()
        with trap_jobs():
            self.assertTrue(
                dispatch.with_context(job_uuid=dispatch.queue_job_uuid)._job_process()
            )
        self.assertEqual(dispatch.state, "done")
        self.assertTrue(dispatch.result_ref.startswith("contact.center.inbox.event:"))
        self.assertEqual(self._inbox_count(), 1)

    # -- 3d: WABA errors are diagnostics, never conversations -------------------

    def test_errors_only_delivery_records_one_row_per_error_without_inbox(self):
        errors = [
            {
                "code": 131000,
                "title": "Something went wrong",
                "message": "texto livre do provedor com dado do cliente",
                "error_data": {"details": "detalhe sensível do cliente"},
            },
            {"code": 131005, "title": "Access denied"},
        ]
        channels_before = self.env["mail.channel"].sudo().search_count([])
        identities_before = self.env["contact.center.identity"].sudo().search_count([])
        delivery = self.create_delivery(self.envelope(self.value(errors=errors)))
        items = delivery.item_ids.sorted("sequence")
        self.assertEqual(items.mapped("kind"), ["messaging", "messaging"])
        self.assertEqual(
            items[0].payload_json["error"],
            {"code": 131000, "title": "Something went wrong"},
        )
        serialized = json.dumps(items.mapped("payload_json"), ensure_ascii=False)
        self.assertNotIn("texto livre", serialized)
        self.assertNotIn("detalhe sensível", serialized)
        results = self.dispatch_all(delivery)
        self.assertTrue(all(result["handled"] for result in results))
        self.assertTrue(
            all(result["result_ref"].startswith("wac-error:") for result in results)
        )
        failures = self._failures("webhook_error")
        self.assertEqual(sorted(failures.mapped("code")), [131000, 131005])
        self.assertEqual(
            {int(result["result_ref"].split(":")[1]) for result in results},
            set(failures.ids),
        )
        for failure in failures:
            self.assertFalse(failure.wamid)
            self.assertFalse(failure.message_binding_id)
            self.assertNotIn("texto livre", failure.title or "")
        self.assertEqual(self._inbox_count(), 0)
        self.assertEqual(
            self.env["mail.channel"].sudo().search_count([]), channels_before
        )
        self.assertEqual(
            self.env["contact.center.identity"].sudo().search_count([]),
            identities_before,
        )
        # Replaying the same dispatches is idempotent.
        again = [self.dispatch_one(dispatch) for dispatch in self.fanout(delivery)]
        self.assertEqual(again, results)
        self.assertEqual(len(self._failures("webhook_error")), 2)

    def test_same_error_in_two_distinct_deliveries_is_two_occurrences(self):
        error = {"code": 131000, "title": "Something went wrong"}
        self.dispatch_all(
            self.create_delivery(self.envelope(self.value(errors=[error])))
        )
        envelope = self.envelope(self.value(errors=[error]))
        envelope["entry"][0]["time"] = 1_900_000_000
        self.dispatch_all(self.create_delivery(envelope))
        self.assertEqual(len(self._failures("webhook_error")), 2)

    def test_message_and_error_in_one_delivery_only_the_message_is_an_event(self):
        message = self.text_message(body="Mensagem real")
        delivery, inboxes = self.deliver(
            self.envelope(
                self.value(
                    messages=[message], errors=[{"code": 131000, "title": "Oops"}]
                )
            )
        )
        self.assertEqual(len(inboxes), 1)
        self.assertEqual(inboxes.raw_envelope_json["message"]["id"], message["id"])
        self.assertEqual(len(self._failures("webhook_error")), 1)
        self.assertEqual(len(delivery.item_ids), 2)

    def test_consumer_key_is_the_registered_subscription_key(self):
        delivery = self.create_delivery(
            self.envelope(self.value(messages=[self.text_message()]))
        )
        dispatch = self.fanout(delivery).ensure_one()
        self.assertEqual(dispatch.consumer_key, WHATSAPP_CLOUD_CONSUMER_KEY)
        self.assertEqual(dispatch.page_id, self.waba)

    def test_unrepresentable_row_stays_an_unclaimed_placeholder(self):
        broken = self.text_message()
        broken.pop("from")
        broken.pop("from_user_id")
        delivery = self.create_delivery(self.envelope(self.value(messages=[broken])))
        item = delivery.item_ids.ensure_one()
        self.assertEqual(item.kind, "unknown")
        self.assertEqual(item.payload_json["reason"], "consumer_unavailable")

    def test_oversized_row_never_blocks_the_rest_of_the_signed_delivery(self):
        # 20,000 four-byte characters exceed the shared 64 KiB item bound.
        oversized = self.text_message(body="😀" * 20_000)
        normal = self.text_message(body="Mensagem normal")
        delivery = self.create_delivery(
            self.envelope(self.value(messages=[oversized, normal]))
        )
        first, second = delivery.item_ids.sorted("sequence")
        self.assertEqual(first.kind, "unknown")
        self.assertEqual(first.payload_json["reason"], "consumer_unavailable")
        self.assertEqual(second.kind, "messaging")
        self.assertEqual(second.payload_json["message"]["id"], normal["id"])

    def test_live_route_without_subscription_is_retried_even_outside_policy(self):
        delivery = self.create_delivery(
            self.envelope(self.value(messages=[self.text_message()]))
        )
        dispatch = self.fanout(delivery).ensure_one()
        # The shared policy would already stop this dispatch; the consumer's own
        # readiness gate must hold on its own as well.
        self.subscription.write({"active": False})
        self.assertTrue(self.connection.active and self.connection.inbound_active)
        with self.assertRaises(RetryableJobError):
            self.dispatch_one(dispatch)
        self.assertEqual(self._inbox_count(), 0)

    # -- CC-WAC-03: the subscription lifecycle follows the completed switch -------

    def _standby(self, asset=None, phone_id=None):
        return self._create_connection(
            self.account,
            asset or self.waba_asset,
            phone_id or self.OTHER_PHONE_ID,
            role="standby",
            inbound_active=False,
        )

    def test_controlled_primary_switch_keeps_the_shared_subscription(self):
        # Primary and standby: two numbers of one business account.
        standby = self._standby()
        standby.action_use_as_primary()
        self.connection.invalidate_recordset()
        self.assertEqual(self.connection.role, "standby")
        self.assertEqual(standby.role, "primary")
        self.assertTrue(self.subscription.active)
        message = self.text_message(body="Depois da troca de primário")
        delivery, inbox = self.deliver(
            self.envelope(self.value(messages=[message], phone_id=self.OTHER_PHONE_ID))
        )
        self.assertEqual(delivery.item_ids.kind, "messaging")
        self.assertEqual(inbox.provider_connection_id, standby)
        self.assertEqual(inbox.state, "done", inbox.last_error_message)
        # The lifecycle never reactivates what an administrator disabled.
        self.subscription.write({"active": False})
        with self.assertRaises(ValidationError):
            self.connection.action_use_as_primary()
        self.assertFalse(self.subscription.active)
        self.assertEqual(standby.role, "primary")

    def test_primary_switch_to_another_owner_retires_only_the_old_route(self):
        other_owner = self._create_waba(self.endpoint, self.OTHER_WABA_ID)
        other_subscription = self._subscribe(other_owner)
        standby = self._standby(other_owner.asset_ids)
        standby.action_use_as_primary()
        # Reconciled on the final topology: the retired owner stops claiming.
        self.assertFalse(self.subscription.active)
        self.assertTrue(other_subscription.active)
        self.assertTrue(standby._wac_inbound_route_is_ready())
