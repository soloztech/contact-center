import threading
import uuid
from functools import partial
from unittest import mock

from psycopg2.errors import DeadlockDetected, LockNotAvailable, SerializationFailure

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.queue_job.exception import RetryableJobError

from ..services.adapter import adapter_registry
from ..services.dto import AdapterResult, EventDTO
from ..services.tokens import CONTACT_CENTER_MEMBERSHIP_TOKEN, CONTACT_CENTER_POST_TOKEN


@tagged("-at_install", "post_install")
class TestPhase1Concurrency(TransactionCase):
    """Exercise the cold-start race with real, independent transactions."""

    WORKER_TIMEOUT_SECONDS = 12

    def _setup_committed_fixture(self, token):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            company = env.company
            agent_group = env.ref("contact_center_base.group_contact_center_agent")
            agent = (
                env["res.users"]
                .with_context(no_reset_password=True)
                .create(
                    {
                        "name": "Concurrency Agent %s" % token,
                        "login": "cc-concurrency-%s" % token,
                        "email": "cc-concurrency-%s@example.invalid" % token,
                        "company_id": company.id,
                        "company_ids": [(6, 0, company.ids)],
                        "groups_id": [(6, 0, [agent_group.id])],
                    }
                )
            )
            team = env["contact.center.team"].create(
                {
                    "name": "Concurrency Team %s" % token,
                    "company_id": company.id,
                    "agent_ids": [(6, 0, agent.ids)],
                }
            )
            account = env["contact.center.account"].create(
                {
                    "name": "Concurrency Account %s" % token,
                    "company_id": company.id,
                    "platform": "whatsapp",
                    "external_ref": "concurrency-account-%s" % token,
                    "default_team_id": team.id,
                }
            )
            connection = env["contact.center.provider.connection"].create(
                {
                    "name": "Concurrency Connection %s" % token,
                    "account_id": account.id,
                    "adapter_key": "test.fake",
                    "external_ref": "concurrency-connection-%s" % token,
                    "provider_schema_version": "fixture-v1",
                    "state": "connected",
                    "active": True,
                    "role": "primary",
                    "inbound_active": True,
                    "outbound_active": True,
                    "capabilities_json": {"send_message": True},
                    "last_state_observed_at": fields.Datetime.now(),
                }
            )
            pn = "55%s@s.whatsapp.net" % token[:16]
            lid = "%s@lid" % token
            external_message_id = "concurrent-message-%s" % token
            actor_name = "Concurrent Sender %s" % token
            inboxes = env["contact.center.inbox.event"]
            for index in range(2):
                event = EventDTO.from_dict(
                    {
                        "schema_version": 1,
                        "provider_schema_version": "fixture-v1",
                        "event_id": "concurrent-event-%s-%s" % (token, index),
                        "event_type": "message.created",
                        "occurred_at": "2026-08-21T13:00:00Z",
                        "account_ref": account.external_ref,
                        "connection_ref": connection.external_ref,
                        "conversation_ref": "concurrent-conversation-%s" % token,
                        "platform": "whatsapp",
                        "direction": "inbound",
                        "is_from_me": False,
                        "origin": "provider",
                        "actor": {
                            "display_name": actor_name,
                            "addresses": [
                                {
                                    "namespace": "whatsapp.pn",
                                    "value": pn,
                                    "value_normalized": pn,
                                    "role": "primary",
                                },
                                {
                                    "namespace": "whatsapp.lid",
                                    "value": lid,
                                    "value_normalized": lid,
                                    "role": "alternate",
                                },
                            ],
                        },
                        "conversation": {
                            "conversation_type": "direct",
                            "addresses": [
                                {
                                    "namespace": "whatsapp.pn",
                                    "value": pn,
                                    "value_normalized": pn,
                                    "role": "primary",
                                },
                                {
                                    "namespace": "whatsapp.lid",
                                    "value": lid,
                                    "value_normalized": lid,
                                    "role": "alternate",
                                },
                            ],
                        },
                        "message": {
                            "external_message_id": external_message_id,
                            "content_type": "text",
                            "text": "Concurrent inbound text",
                        },
                    }
                )
                inboxes |= (
                    env["contact.center.inbox.event"]
                    .sudo()
                    .with_context(contact_center_skip_enqueue=True)
                    .create(
                        {
                            "provider_connection_id": connection.id,
                            "inbox_dedupe_key": event.event_id,
                            "provider_schema_version": "fixture-v1",
                            "raw_envelope_json": event.to_dict(),
                        }
                    )
                )
            # queue_job persists ownership before a worker starts. Keep that
            # boundary faithful instead of writing the UUID inside each worker.
            for inbox in inboxes:
                inbox.write({"queue_job_uuid": str(uuid.uuid4())})
            # Independent worker cursors must observe the shared race fixture.
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "account_id": account.id,
                "agent_id": agent.id,
                "connection_id": connection.id,
                "inbox_ids": inboxes.ids,
                "pn": pn,
                "lid": lid,
                "actor_name": actor_name,
                "external_message_id": external_message_id,
            }

    def _process_inbox_transaction(self, inbox_id):
        try:
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '5s'")
                cr.execute("SET LOCAL statement_timeout = '10s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                inbox = env["contact.center.inbox.event"].sudo().browse(inbox_id)
                inbox.invalidate_recordset(["queue_job_uuid"])
                job_uuid = inbox.queue_job_uuid
                if not job_uuid:
                    raise RuntimeError("concurrency fixture has no durable job owner")
                inbox.with_context(job_uuid=job_uuid)._job_process()
                env.flush_all()
                return {"outcome": "done", "cause": None}
        except RetryableJobError as error:
            cause = error.__cause__
            return {
                "outcome": "retry",
                "cause": cause.__class__.__name__ if cause else None,
                "is_serialization_failure": isinstance(cause, SerializationFailure),
                "is_deadlock": isinstance(cause, DeadlockDetected),
            }

    def _create_followup_inbox(self, fixture, token):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            account = env["contact.center.account"].browse(fixture["account_id"])
            connection = env["contact.center.provider.connection"].browse(
                fixture["connection_id"]
            )
            binding = (
                env["contact.center.channel.binding"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", account.id),
                        ("conversation_type", "=", "direct"),
                        ("active", "=", True),
                    ],
                    limit=1,
                )
            )
            self.assertTrue(binding)
            external_message_id = "concurrent-followup-%s" % token
            event = EventDTO.from_dict(
                {
                    "schema_version": 1,
                    "provider_schema_version": "fixture-v1",
                    "event_id": "concurrent-followup-event-%s" % token,
                    "event_type": "message.created",
                    "occurred_at": "2026-08-25T13:00:00Z",
                    "account_ref": account.external_ref,
                    "connection_ref": connection.external_ref,
                    "conversation_ref": "concurrent-conversation-%s" % token,
                    "platform": "whatsapp",
                    "direction": "inbound",
                    "is_from_me": False,
                    "origin": "provider",
                    "actor": {
                        "display_name": fixture["actor_name"],
                        "addresses": [
                            {
                                "namespace": "whatsapp.pn",
                                "value": fixture["pn"],
                                "value_normalized": fixture["pn"],
                                "role": "primary",
                            },
                            {
                                "namespace": "whatsapp.lid",
                                "value": fixture["lid"],
                                "value_normalized": fixture["lid"],
                                "role": "alternate",
                            },
                        ],
                    },
                    "conversation": {
                        "conversation_type": "direct",
                        "addresses": [
                            {
                                "namespace": "whatsapp.pn",
                                "value": fixture["pn"],
                                "value_normalized": fixture["pn"],
                                "role": "primary",
                            },
                            {
                                "namespace": "whatsapp.lid",
                                "value": fixture["lid"],
                                "value_normalized": fixture["lid"],
                                "role": "alternate",
                            },
                        ],
                    },
                    "message": {
                        "external_message_id": external_message_id,
                        "content_type": "text",
                        "text": "Concurrent follow-up inbound text",
                    },
                }
            )
            inbox = (
                env["contact.center.inbox.event"]
                .sudo()
                .with_context(contact_center_skip_enqueue=True)
                .create(
                    {
                        "provider_connection_id": connection.id,
                        "inbox_dedupe_key": event.event_id,
                        "provider_schema_version": "fixture-v1",
                        "raw_envelope_json": event.to_dict(),
                    }
                )
            )
            inbox.write({"queue_job_uuid": str(uuid.uuid4())})
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "channel_id": binding.channel_id.id,
                "inbox_id": inbox.id,
                "external_message_id": external_message_id,
            }

    def _create_committed_outbox(self, fixture, token):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, fixture["agent_id"], {})
            binding = (
                env["contact.center.channel.binding"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", fixture["account_id"]),
                        ("conversation_type", "=", "direct"),
                        ("active", "=", True),
                    ],
                    limit=1,
                )
            )
            self.assertTrue(binding)
            result = (
                env["contact.center.ui.api"]
                .with_context(contact_center_skip_enqueue=True)
                .send_message(
                    binding.channel_id.id,
                    "Durable outbox boundary %s" % token,
                    client_request_id=str(uuid.uuid4()),
                )
            )
            # Outbox ledgers are intentionally hidden from agents.  The normal
            # application service creates and enqueues them as sudo; this helper
            # disabled auto-enqueue to expose the durable boundary explicitly, so
            # it must resume through that same technical environment.
            outbox = (
                env["contact.center.outbox.command"]
                .sudo()
                .browse(result["outbox_command_id"])
            )
            outbox._enqueue()
            self.assertTrue(outbox.queue_job_uuid)
            values = {
                "outbox_id": outbox.id,
                "queue_job_uuid": outbox.queue_job_uuid,
                "message_id": result["message_id"],
            }
            cr.commit()  # pylint: disable=invalid-commit
            return values

    def _simulate_outbox_worker_death_after_boundary(self, outbox_id, job_uuid):
        with self.registry.cursor() as cr:
            cr.execute("SET LOCAL statement_timeout = '10s'")
            env = api.Environment(cr, SUPERUSER_ID, {})
            outbox = env["contact.center.outbox.command"].sudo().browse(outbox_id)
            outbox_model_class = type(outbox)
            with mock.patch.object(
                outbox_model_class,
                "_execute_adapter",
                autospec=True,
                return_value=AdapterResult.success(
                    external_message_id="provider-accepted-before-worker-death"
                ),
            ) as execute_adapter, mock.patch.object(
                outbox_model_class,
                "_finalize_dispatch_success",
                autospec=True,
                side_effect=SystemExit(
                    "simulated worker death after provider accepted the command"
                ),
            ):
                with self.assertRaises(SystemExit):
                    outbox.with_context(job_uuid=job_uuid)._job_process()
            return execute_adapter.call_count

    def _resume_outbox_after_worker_death(self, outbox_id, job_uuid):
        with self.registry.cursor() as cr:
            cr.execute("SET LOCAL statement_timeout = '10s'")
            env = api.Environment(cr, SUPERUSER_ID, {})
            outbox = env["contact.center.outbox.command"].sudo().browse(outbox_id)
            outbox_model_class = type(outbox)
            with mock.patch.object(
                outbox_model_class,
                "_execute_adapter",
                autospec=True,
            ) as execute_adapter:
                result = outbox.with_context(job_uuid=job_uuid)._job_process()
            return result, execute_adapter.call_count

    def _run_concurrent_throttled_outboxes(
        self, durables, leader_at_adapter, follower_finished
    ):
        results = {}
        errors = {}

        def worker(index, durable):
            try:
                with self.registry.cursor() as cr:
                    cr.execute("SET LOCAL lock_timeout = '5s'")
                    cr.execute("SET LOCAL statement_timeout = '10s'")
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    outbox = (
                        env["contact.center.outbox.command"]
                        .sudo()
                        .browse(durable["outbox_id"])
                    )
                    if index and not leader_at_adapter.wait(timeout=5):
                        raise RuntimeError(
                            "leader did not reach the provider adapter boundary"
                        )
                    try:
                        result = outbox.with_context(
                            job_uuid=durable["queue_job_uuid"]
                        )._job_process()
                    except RetryableJobError as error:
                        results[durable["outbox_id"]] = {
                            "outcome": "retry",
                            "cause": type(error.__cause__).__name__,
                            "seconds": error.seconds,
                            "ignore_retry": error.ignore_retry,
                        }
                    else:
                        results[durable["outbox_id"]] = {
                            "outcome": "done",
                            "result": result,
                        }
            except Exception as error:  # surface failures in the main test thread
                errors[durable["outbox_id"]] = error
            finally:
                if index:
                    follower_finished.set()

        threads = [
            threading.Thread(
                target=worker,
                args=(index, durable),
                name="cc-throttle-%s" % durable["outbox_id"],
                daemon=True,
            )
            for index, durable in enumerate(durables)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=self.WORKER_TIMEOUT_SECONDS)
        live_threads = [thread.name for thread in threads if thread.is_alive()]
        if live_threads:
            follower_finished.set()
            for thread in threads:
                thread.join(timeout=2)
            self.fail("Concurrent throttle workers did not finish: %s" % live_threads)
        if errors:
            first_id = sorted(errors)[0]
            raise errors[first_id]
        return results

    def _observe_send_inbound_lock(self, observed_order, observed_order_lock, marker):
        with observed_order_lock:
            observed_order.append(marker)

    def _coordinate_send_inbound_conversation_lock(
        self,
        recordset,
        original_lock,
        inbound_has_conversation_locks,
        outbound_at_topology_lock,
        observed_order,
        observed_order_lock,
    ):
        worker_name = threading.current_thread().name
        if worker_name == "cc-lock-order-outbound":
            self._observe_send_inbound_lock(
                observed_order,
                observed_order_lock,
                "outbound_conversation_attempted",
            )
        locked = original_lock(recordset)
        if worker_name == "cc-lock-order-inbound":
            self._observe_send_inbound_lock(
                observed_order,
                observed_order_lock,
                "inbound_conversation_locked",
            )
            inbound_has_conversation_locks.set()
            if not outbound_at_topology_lock.wait(timeout=5):
                raise RuntimeError("outbound did not reach the topology lock")
        elif worker_name == "cc-lock-order-outbound":
            self._observe_send_inbound_lock(
                observed_order,
                observed_order_lock,
                "outbound_conversation_locked",
            )
        return locked

    def _send_concurrent_outbound_transaction(self, fixture, channel_id, request_id):
        with self.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = '5s'")
            cr.execute("SET LOCAL statement_timeout = '10s'")
            env = api.Environment(cr, fixture["agent_id"], {})
            result = (
                env["contact.center.ui.api"]
                .with_context(contact_center_skip_enqueue=True)
                .send_message(
                    channel_id,
                    "Concurrent outbound text",
                    client_request_id=request_id,
                )
            )
            env.flush_all()
            cr.commit()  # pylint: disable=invalid-commit
            return result

    def _coordinate_send_inbound_topology_lock(
        self,
        recordset,
        account_ids,
        original_lock,
        outbound_at_topology_lock,
        observed_order,
        observed_order_lock,
    ):
        is_outbound = threading.current_thread().name == "cc-lock-order-outbound"
        if is_outbound:
            self._observe_send_inbound_lock(
                observed_order,
                observed_order_lock,
                "outbound_topology_attempted",
            )
            outbound_at_topology_lock.set()
        locked = original_lock(recordset, account_ids)
        if is_outbound:
            self._observe_send_inbound_lock(
                observed_order,
                observed_order_lock,
                "outbound_topology_locked",
            )
        return locked

    def _run_concurrent_send_and_inbound(self, fixture, followup):
        inbound_has_conversation_locks = threading.Event()
        outbound_at_topology_lock = threading.Event()
        observed_order = []
        observed_order_lock = threading.Lock()
        results = {}
        errors = {}
        outbound_request_id = str(uuid.uuid4())
        binding_model_class = type(self.env["contact.center.channel.binding"])
        provider_model_class = type(self.env["contact.center.provider.connection"])
        coordinated_conversation_lock = partial(
            self._coordinate_send_inbound_conversation_lock,
            original_lock=(
                binding_model_class._contact_center_lock_channel_then_binding
            ),
            inbound_has_conversation_locks=inbound_has_conversation_locks,
            outbound_at_topology_lock=outbound_at_topology_lock,
            observed_order=observed_order,
            observed_order_lock=observed_order_lock,
        )
        coordinated_topology_lock = partial(
            self._coordinate_send_inbound_topology_lock,
            original_lock=(
                provider_model_class._contact_center_lock_operational_admission
            ),
            outbound_at_topology_lock=outbound_at_topology_lock,
            observed_order=observed_order,
            observed_order_lock=observed_order_lock,
        )

        def coordinated_conversation_lock_method(recordset):
            return coordinated_conversation_lock(recordset)

        def coordinated_topology_lock_method(recordset, account_ids):
            return coordinated_topology_lock(recordset, account_ids)

        def inbound_worker():
            try:
                results["inbound"] = self._process_inbox_transaction(
                    followup["inbox_id"]
                )
            except Exception as error:  # surface failures in the main test thread
                errors["inbound"] = error
                inbound_has_conversation_locks.set()

        def outbound_worker():
            try:
                if not inbound_has_conversation_locks.wait(timeout=5):
                    raise RuntimeError("inbound did not acquire conversation locks")
                results["outbound"] = self._send_concurrent_outbound_transaction(
                    fixture,
                    followup["channel_id"],
                    outbound_request_id,
                )
            except SerializationFailure as error:
                results["outbound_contention"] = error.__class__.__name__
            except Exception as error:  # surface failures in the main test thread
                errors["outbound"] = error
                outbound_at_topology_lock.set()

        threads = [
            threading.Thread(
                target=inbound_worker,
                name="cc-lock-order-inbound",
                daemon=True,
            ),
            threading.Thread(
                target=outbound_worker,
                name="cc-lock-order-outbound",
                daemon=True,
            ),
        ]
        with mock.patch.object(
            binding_model_class,
            "_contact_center_lock_channel_then_binding",
            new=coordinated_conversation_lock_method,
        ), mock.patch.object(
            provider_model_class,
            "_contact_center_lock_operational_admission",
            new=coordinated_topology_lock_method,
        ):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=self.WORKER_TIMEOUT_SECONDS)
        live_threads = [thread.name for thread in threads if thread.is_alive()]
        if live_threads:
            inbound_has_conversation_locks.set()
            outbound_at_topology_lock.set()
            for thread in threads:
                thread.join(timeout=2)
            self.fail("Send/inbound workers did not finish: %s" % live_threads)
        if errors:
            first_name = sorted(errors)[0]
            raise errors[first_name]
        return results, observed_order, outbound_request_id

    def _create_concurrent_edit_mutations(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            target = (
                env["contact.center.message.binding"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", fixture["account_id"]),
                        (
                            "external_message_id",
                            "=",
                            fixture["external_message_id"],
                        ),
                    ],
                    limit=1,
                )
            )
            self.assertTrue(target)
            mutation_model = env["contact.center.message.mutation"].sudo()
            common = {
                "target_message_binding_id": target.id,
                "provider_connection_id": fixture["connection_id"],
                "mutation_type": "edit",
                "direction": "inbound",
                "actor_guest_id": target.message_id.author_guest_id.id,
            }
            older = mutation_model.create(
                {
                    **common,
                    "external_event_id": "concurrent-edit-older-%s"
                    % fixture["account_id"],
                    "occurred_at": "2026-08-23 18:01:00",
                    "new_text": "Older concurrent body",
                }
            )
            newer = mutation_model.create(
                {
                    **common,
                    "external_event_id": "concurrent-edit-newer-%s"
                    % fixture["account_id"],
                    "occurred_at": "2026-08-23 18:02:00",
                    "new_text": "Newest concurrent body",
                }
            )
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "target_id": target.id,
                "older_id": older.id,
                "newer_id": newer.id,
            }

    def _create_concurrent_reaction_mutations(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            target = (
                env["contact.center.message.binding"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", fixture["account_id"]),
                        (
                            "external_message_id",
                            "=",
                            fixture["external_message_id"],
                        ),
                    ],
                    limit=1,
                )
            )
            self.assertTrue(target)
            actor_guest = target.message_id.author_guest_id
            self.assertTrue(actor_guest)
            mutation_model = env["contact.center.message.mutation"].sudo()
            common = {
                "target_message_binding_id": target.id,
                "provider_connection_id": fixture["connection_id"],
                "mutation_type": "react",
                "direction": "inbound",
                "actor_guest_id": actor_guest.id,
                "reaction_operation": "add",
            }
            older = mutation_model.create(
                {
                    **common,
                    "external_event_id": "concurrent-react-older-%s"
                    % fixture["account_id"],
                    "occurred_at": "2026-08-23 18:01:00",
                    "reaction_emoji": "❤️",
                }
            )
            newer = mutation_model.create(
                {
                    **common,
                    "external_event_id": "concurrent-react-newer-%s"
                    % fixture["account_id"],
                    "occurred_at": "2026-08-23 18:02:00",
                    "reaction_emoji": "👍",
                }
            )
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "target_id": target.id,
                "older_id": older.id,
                "newer_id": newer.id,
            }

    def _run_concurrent_mutation_projection(self, mutation_fixture):
        lock_acquired = threading.Event()
        older_started = threading.Event()
        errors = {}

        def newer_worker():
            try:
                with self.registry.cursor() as cr:
                    cr.execute("SET LOCAL lock_timeout = '5s'")
                    cr.execute("SET LOCAL statement_timeout = '10s'")
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    cr.execute(
                        "SELECT id FROM contact_center_message_binding "
                        "WHERE id = %s FOR UPDATE",
                        [mutation_fixture["target_id"]],
                    )
                    lock_acquired.set()
                    if not older_started.wait(timeout=5):
                        raise RuntimeError("older mutation worker did not start")
                    env["contact.center.message.mutation"].sudo().browse(
                        mutation_fixture["newer_id"]
                    )._apply_projection()
                    cr.commit()  # pylint: disable=invalid-commit
            except Exception as error:  # surface failures in the main test thread
                errors["newer"] = error
                lock_acquired.set()

        def older_worker():
            try:
                if not lock_acquired.wait(timeout=5):
                    raise RuntimeError("newer mutation worker did not lock the target")
                with self.registry.cursor() as cr:
                    cr.execute("SET LOCAL lock_timeout = '5s'")
                    cr.execute("SET LOCAL statement_timeout = '10s'")
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    mutation = (
                        env["contact.center.message.mutation"]
                        .sudo()
                        .browse(mutation_fixture["older_id"])
                    )
                    older_started.set()
                    mutation._apply_projection()
                    cr.commit()  # pylint: disable=invalid-commit
            except Exception as error:  # surface failures in the main test thread
                errors["older"] = error
                older_started.set()

        threads = [
            threading.Thread(
                target=newer_worker, name="cc-mutation-newer", daemon=True
            ),
            threading.Thread(
                target=older_worker, name="cc-mutation-older", daemon=True
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=self.WORKER_TIMEOUT_SECONDS)
        live_threads = [thread.name for thread in threads if thread.is_alive()]
        if live_threads:
            self.fail("Concurrent mutation workers did not finish: %s" % live_threads)
        unexpected = {
            name: error
            for name, error in errors.items()
            if not isinstance(error, SerializationFailure)
        }
        if unexpected:
            first_name = sorted(unexpected)[0]
            raise unexpected[first_name]
        return errors

    def _retry_mutation_projection(self, mutation_id):
        with self.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = '5s'")
            cr.execute("SET LOCAL statement_timeout = '10s'")
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["contact.center.message.mutation"].sudo().browse(
                mutation_id
            )._apply_projection()
            cr.commit()  # pylint: disable=invalid-commit

    def _run_concurrent_first_attempts(self, fixture):
        barrier = threading.Barrier(2)
        worker_names = {
            "cc-concurrency-a-%s" % fixture["account_id"],
            "cc-concurrency-b-%s" % fixture["account_id"],
        }
        waited_workers = set()
        waited_lock = threading.Lock()
        observed_prelock_states = {}
        results = {}
        errors = {}
        application_model_class = type(self.env["contact.center.application"])
        original_lock = application_model_class._lock_inbound_account_scope

        def synchronized_lock(recordset, account):
            worker_name = threading.current_thread().name
            should_wait = False
            if worker_name in worker_names and account.id == fixture["account_id"]:
                inbox_index = sorted(worker_names).index(worker_name)
                inbox = (
                    recordset.env["contact.center.inbox.event"]
                    .sudo()
                    .browse(fixture["inbox_ids"][inbox_index])
                )
                inbox.invalidate_recordset(["state", "attempts"])
                prelock_state = (inbox.state, inbox.attempts)
                with waited_lock:
                    if worker_name not in waited_workers:
                        waited_workers.add(worker_name)
                        observed_prelock_states[worker_name] = prelock_state
                        should_wait = True
            if should_wait:
                barrier.wait(timeout=5)
            return original_lock(recordset, account)

        def worker(worker_name, inbox_id):
            try:
                results[worker_name] = self._process_inbox_transaction(inbox_id)
            except Exception as error:  # surface failures in the main test thread
                errors[worker_name] = error

        threads = [
            threading.Thread(
                target=worker,
                args=(worker_name, inbox_id),
                name=worker_name,
                daemon=True,
            )
            for worker_name, inbox_id in (
                (worker_name, fixture["inbox_ids"][index])
                for index, worker_name in enumerate(sorted(worker_names))
            )
        ]
        with mock.patch.object(
            application_model_class,
            "_lock_inbound_account_scope",
            new=synchronized_lock,
        ):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=self.WORKER_TIMEOUT_SECONDS)

        live_threads = [thread.name for thread in threads if thread.is_alive()]
        if live_threads:
            barrier.abort()
            for thread in threads:
                thread.join(timeout=2)
            self.fail("Concurrent workers did not finish: %s" % live_threads)
        if errors:
            first_name = sorted(errors)[0]
            raise errors[first_name]
        self.assertEqual(waited_workers, worker_names)
        self.assertEqual(
            set(observed_prelock_states.values()),
            {("pending", 0)},
            "The account aggregate must be locked before the inbox ledger is written.",
        )
        return results

    def _snapshot(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            account = env["contact.center.account"].browse(fixture["account_id"])
            aliases = (
                env["contact.center.identity.alias"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", account.id),
                        ("namespace", "in", ["whatsapp.pn", "whatsapp.lid"]),
                        (
                            "value_normalized",
                            "in",
                            [fixture["pn"], fixture["lid"]],
                        ),
                    ]
                )
            )
            identities = (
                env["contact.center.identity"]
                .sudo()
                .search(
                    [
                        ("company_id", "=", account.company_id.id),
                        ("name", "=", fixture["actor_name"]),
                    ]
                )
            )
            guests = (
                env["mail.guest"].sudo().search([("name", "=", fixture["actor_name"])])
            )
            bindings = (
                env["contact.center.channel.binding"]
                .sudo()
                .search([("account_id", "=", account.id)])
            )
            channel_aliases = (
                env["contact.center.channel.alias"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", account.id),
                        ("namespace", "in", ["whatsapp.pn", "whatsapp.lid"]),
                        (
                            "value_normalized",
                            "in",
                            [fixture["pn"], fixture["lid"]],
                        ),
                    ]
                )
            )
            message_bindings = (
                env["contact.center.message.binding"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", account.id),
                        (
                            "external_message_id",
                            "=",
                            fixture["external_message_id"],
                        ),
                    ]
                )
            )
            inboxes = (
                env["contact.center.inbox.event"].sudo().browse(fixture["inbox_ids"])
            )
            external_comments = (
                env["mail.message"]
                .sudo()
                .search(
                    [
                        ("model", "=", "mail.channel"),
                        ("res_id", "in", bindings.channel_id.ids),
                        ("message_type", "=", "comment"),
                        ("subtype_id", "=", env.ref("mail.mt_comment").id),
                    ]
                )
            )
            return {
                "inbox_states": sorted(inboxes.mapped("state")),
                "identity_ids": identities.ids,
                "guest_ids": guests.ids,
                "alias_count": len(aliases),
                "alias_identity_ids": aliases.identity_id.ids,
                "binding_ids": bindings.ids,
                "channel_ids": bindings.channel_id.ids,
                "binding_identity_ids": bindings.identity_id.ids,
                "channel_alias_count": len(channel_aliases),
                "channel_alias_binding_ids": channel_aliases.channel_binding_id.ids,
                "message_binding_ids": message_bindings.ids,
                "message_ids": message_bindings.message_id.ids,
                "message_author_guest_ids": (
                    message_bindings.message_id.author_guest_id.ids
                ),
                "channel_guest_ids": (
                    bindings.channel_id.channel_member_ids.guest_id.ids
                ),
                "external_comment_ids": external_comments.ids,
                "conflict_count": (
                    env["contact.center.identity.conflict"]
                    .sudo()
                    .search_count([("account_id", "=", account.id)])
                ),
            }

    def _setup_committed_group_fixture(self, token):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            company = env.company
            agent_group = env.ref("contact_center_base.group_contact_center_agent")
            agent = (
                env["res.users"]
                .with_context(no_reset_password=True)
                .create(
                    {
                        "name": "Concurrency Agent %s" % token,
                        "login": "cc-concurrency-%s" % token,
                        "email": "cc-concurrency-%s@example.invalid" % token,
                        "company_id": company.id,
                        "company_ids": [(6, 0, company.ids)],
                        "groups_id": [(6, 0, [agent_group.id])],
                    }
                )
            )
            team = env["contact.center.team"].create(
                {
                    "name": "Concurrency Team %s" % token,
                    "company_id": company.id,
                    "agent_ids": [(6, 0, agent.ids)],
                }
            )
            account = env["contact.center.account"].create(
                {
                    "name": "Concurrency Account %s" % token,
                    "company_id": company.id,
                    "platform": "whatsapp",
                    "external_ref": "concurrency-account-%s" % token,
                    "default_team_id": team.id,
                    "group_inbound_enabled": True,
                }
            )
            connection = env["contact.center.provider.connection"].create(
                {
                    "name": "Concurrency Connection %s" % token,
                    "account_id": account.id,
                    "adapter_key": "test.fake",
                    "external_ref": "concurrency-connection-%s" % token,
                    "provider_schema_version": "fixture-v1",
                    "state": "connected",
                    "active": True,
                    "role": "primary",
                    "inbound_active": True,
                    "outbound_active": False,
                }
            )
            group_ref = "120363%s@g.us" % token[:12]
            participants = (
                {
                    "name": "Concurrent Group A %s" % token,
                    "lid": "%sa@lid" % token,
                    "pn": "5511%sa@s.whatsapp.net" % token[:12],
                    "message_id": "concurrent-group-a-%s" % token,
                },
                {
                    "name": "Concurrent Group B %s" % token,
                    "lid": "%sb@lid" % token,
                    "pn": "5511%sb@s.whatsapp.net" % token[:12],
                    "message_id": "concurrent-group-b-%s" % token,
                },
            )
            inboxes = env["contact.center.inbox.event"]
            for index, participant in enumerate(participants):
                event = EventDTO.from_dict(
                    {
                        "schema_version": 1,
                        "provider_schema_version": "fixture-v1",
                        "event_id": "concurrent-group-event-%s-%s" % (token, index),
                        "event_type": "message.created",
                        "occurred_at": "2026-08-24T13:00:00Z",
                        "account_ref": account.external_ref,
                        "connection_ref": connection.external_ref,
                        "conversation_ref": group_ref,
                        "platform": "whatsapp",
                        "direction": "inbound",
                        "is_from_me": False,
                        "origin": "provider",
                        "actor": {
                            "display_name": participant["name"],
                            "addresses": [
                                {
                                    "namespace": "whatsapp.lid",
                                    "value": participant["lid"],
                                    "value_normalized": participant["lid"],
                                    "role": "sender",
                                    "confidence": "protocol",
                                },
                                {
                                    "namespace": "whatsapp.pn",
                                    "value": participant["pn"],
                                    "value_normalized": participant["pn"],
                                    "role": "alternate",
                                },
                            ],
                        },
                        "conversation": {
                            "conversation_type": "group",
                            "addresses": [
                                {
                                    "namespace": "whatsapp.group",
                                    "value": group_ref,
                                    "value_normalized": group_ref,
                                    "role": "group",
                                }
                            ],
                        },
                        "message": {
                            "external_message_id": participant["message_id"],
                            "content_type": "text",
                            "text": "Concurrent group inbound %s" % index,
                        },
                    }
                )
                inboxes |= (
                    env["contact.center.inbox.event"]
                    .sudo()
                    .with_context(contact_center_skip_enqueue=True)
                    .create(
                        {
                            "provider_connection_id": connection.id,
                            "inbox_dedupe_key": event.event_id,
                            "provider_schema_version": "fixture-v1",
                            "raw_envelope_json": event.to_dict(),
                        }
                    )
                )
            for inbox in inboxes:
                inbox.write({"queue_job_uuid": str(uuid.uuid4())})
            cr.commit()  # pylint: disable=invalid-commit
            return {
                "account_id": account.id,
                "inbox_ids": inboxes.ids,
                "group_ref": group_ref,
                "participant_names": [item["name"] for item in participants],
                "actor_values": [
                    value
                    for participant in participants
                    for value in (participant["lid"], participant["pn"])
                ],
                "message_ids": [item["message_id"] for item in participants],
            }

    def _run_concurrent_group_first_attempts(self, fixture):
        barrier = threading.Barrier(2)
        worker_names = {
            "cc-group-a-%s" % fixture["account_id"],
            "cc-group-b-%s" % fixture["account_id"],
        }
        waited_workers = set()
        waited_lock = threading.Lock()
        observed_prelock_states = {}
        results = {}
        errors = {}
        application_model_class = type(self.env["contact.center.application"])
        original_lock = application_model_class._lock_inbound_account_scope

        def synchronized_lock(recordset, account):
            worker_name = threading.current_thread().name
            should_wait = False
            if worker_name in worker_names and account.id == fixture["account_id"]:
                inbox_index = sorted(worker_names).index(worker_name)
                inbox = (
                    recordset.env["contact.center.inbox.event"]
                    .sudo()
                    .browse(fixture["inbox_ids"][inbox_index])
                )
                inbox.invalidate_recordset(["state", "attempts"])
                prelock_state = (inbox.state, inbox.attempts)
                with waited_lock:
                    if worker_name not in waited_workers:
                        waited_workers.add(worker_name)
                        observed_prelock_states[worker_name] = prelock_state
                        should_wait = True
            if should_wait:
                barrier.wait(timeout=5)
            return original_lock(recordset, account)

        def worker(worker_name, inbox_id):
            try:
                results[inbox_id] = self._process_inbox_transaction(inbox_id)
            except Exception as error:  # surface failures in the main test thread
                errors[worker_name] = error

        threads = [
            threading.Thread(
                target=worker,
                args=(worker_name, inbox_id),
                name=worker_name,
                daemon=True,
            )
            for worker_name, inbox_id in zip(sorted(worker_names), fixture["inbox_ids"])
        ]
        with mock.patch.object(
            application_model_class,
            "_lock_inbound_account_scope",
            new=synchronized_lock,
        ):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=self.WORKER_TIMEOUT_SECONDS)

        live_threads = [thread.name for thread in threads if thread.is_alive()]
        if live_threads:
            barrier.abort()
            for thread in threads:
                thread.join(timeout=2)
            self.fail("Concurrent group workers did not finish: %s" % live_threads)
        if errors:
            first_name = sorted(errors)[0]
            raise errors[first_name]
        self.assertEqual(waited_workers, worker_names)
        self.assertEqual(
            set(observed_prelock_states.values()),
            {("pending", 0)},
            "The account aggregate must be locked before the inbox ledger is written.",
        )
        return results

    def _snapshot_group_fixture(self, fixture):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            bindings = (
                env["contact.center.channel.binding"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", fixture["account_id"]),
                        ("conversation_type", "=", "group"),
                        ("conversation_ref", "=", fixture["group_ref"]),
                    ]
                )
            )
            aliases = (
                env["contact.center.identity.alias"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", fixture["account_id"]),
                        ("value_normalized", "in", fixture["actor_values"]),
                    ]
                )
            )
            message_bindings = (
                env["contact.center.message.binding"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", fixture["account_id"]),
                        ("external_message_id", "in", fixture["message_ids"]),
                    ]
                )
            )
            channel_aliases = (
                env["contact.center.channel.alias"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", fixture["account_id"]),
                        ("value_normalized", "=", fixture["group_ref"]),
                    ]
                )
            )
            inboxes = (
                env["contact.center.inbox.event"].sudo().browse(fixture["inbox_ids"])
            )
            return {
                "inbox_states": sorted(inboxes.mapped("state")),
                "binding_ids": bindings.ids,
                "binding_identity_ids": bindings.identity_id.ids,
                "channel_ids": bindings.channel_id.ids,
                "group_profile_ids": bindings.group_profile_ids.ids,
                "identity_ids": aliases.identity_id.ids,
                "guest_ids": aliases.identity_id.mail_guest_id.ids,
                "identity_alias_ids": aliases.ids,
                "channel_alias_ids": channel_aliases.ids,
                "channel_alias_binding_ids": channel_aliases.channel_binding_id.ids,
                "message_binding_ids": message_bindings.ids,
                "message_channel_ids": message_bindings.channel_binding_id.ids,
                "message_author_guest_ids": (
                    message_bindings.message_id.author_guest_id.ids
                ),
                "channel_guest_ids": (
                    bindings.channel_id.channel_member_ids.guest_id.ids
                ),
                "conflict_count": (
                    env["contact.center.identity.conflict"]
                    .sudo()
                    .search_count([("account_id", "=", fixture["account_id"])])
                ),
            }

    def _cleanup_marketing_fixture(self, env, channel_bindings, message_bindings):
        """Remove optional bridge evidence owned by this committed test fixture.

        Marketing ledgers intentionally forbid runtime deletion. These scoped
        test-only statements clean child rows before the Contact Center parents;
        no production unlink capability or foreign-key policy is weakened.
        """

        if "marketing.contact.center.response.signal" not in env:
            return
        record_ids_by_model = {
            "contact.center.channel.binding": set(channel_bindings.ids),
            "contact.center.message.binding": set(message_bindings.ids),
        }
        env["queue.job"].sudo().search(
            [("model_name", "in", list(record_ids_by_model))]
        ).filtered(
            lambda job: bool(
                set(job.record_ids or []) & record_ids_by_model[job.model_name]
            )
        ).unlink()
        env.cr.execute(
            "DELETE FROM marketing_contact_center_response_cursor "
            "WHERE channel_binding_id = ANY(%s)",
            [channel_bindings.ids],
        )
        env.cr.execute(
            "DELETE FROM marketing_contact_center_response "
            "WHERE episode_id IN (SELECT id FROM "
            "marketing_contact_center_response_episode "
            "WHERE channel_binding_id = ANY(%s))",
            [channel_bindings.ids],
        )
        env.cr.execute(
            "DELETE FROM marketing_contact_center_response_episode "
            "WHERE channel_binding_id = ANY(%s)",
            [channel_bindings.ids],
        )
        env.cr.execute(
            "DELETE FROM marketing_contact_center_response_signal "
            "WHERE channel_binding_id = ANY(%s)",
            [channel_bindings.ids],
        )
        events = (
            env["marketing.business.event"]
            .sudo()
            .search(
                [
                    ("source_system", "=", "contact_center"),
                    ("source_model", "=", "mail.channel"),
                    ("source_res_id", "in", channel_bindings.channel_id.ids),
                ]
            )
        )
        if events:
            if "marketing.business.event.crm.link" in env:
                env.cr.execute(
                    "DELETE FROM marketing_business_event_crm_link "
                    "WHERE event_id = ANY(%s)",
                    [events.ids],
                )
            env.cr.execute(
                "DELETE FROM marketing_business_event_observation "
                "WHERE event_id = ANY(%s)",
                [events.ids],
            )
            env.cr.execute(
                "DELETE FROM marketing_business_event WHERE id = ANY(%s)",
                [events.ids],
            )

    def _cleanup_committed_fixture(self, token):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            accounts = (
                env["contact.center.account"]
                .sudo()
                .with_context(active_test=False)
                .search([("external_ref", "=", "concurrency-account-%s" % token)])
            )
            if accounts:
                inboxes = (
                    env["contact.center.inbox.event"]
                    .sudo()
                    .search([("account_id", "in", accounts.ids)])
                )
                outboxes = (
                    env["contact.center.outbox.command"]
                    .sudo()
                    .search([("account_id", "in", accounts.ids)])
                )
                job_uuids = [
                    value
                    for value in (
                        inboxes.mapped("queue_job_uuid")
                        + outboxes.mapped("queue_job_uuid")
                    )
                    if value
                ]
                if job_uuids:
                    env["queue.job"].sudo().search([("uuid", "in", job_uuids)]).unlink()
                outboxes.unlink()
                env["contact.center.identity.conflict"].sudo().search(
                    [("account_id", "in", accounts.ids)]
                ).unlink()
                channel_bindings = (
                    env["contact.center.channel.binding"]
                    .sudo()
                    .with_context(active_test=False)
                    .search([("account_id", "in", accounts.ids)])
                )
                channels = channel_bindings.channel_id
                message_bindings = (
                    env["contact.center.message.binding"]
                    .sudo()
                    .search([("account_id", "in", accounts.ids)])
                )
                self._cleanup_marketing_fixture(env, channel_bindings, message_bindings)
                message_bindings.unlink()
                # The diagnostic source link deliberately restricts deletion of
                # ledger evidence while a projected message still references it.
                inboxes.unlink()
                env["mail.message"].sudo().search(
                    [
                        ("model", "=", "mail.channel"),
                        ("res_id", "in", channels.ids),
                    ]
                ).with_context(
                    contact_center_post_token=CONTACT_CENTER_POST_TOKEN
                ).unlink()
                channel_bindings.unlink()
                channels.with_context(
                    contact_center_membership_token=(CONTACT_CENTER_MEMBERSHIP_TOKEN),
                    contact_center_post_token=CONTACT_CENTER_POST_TOKEN,
                ).unlink()
                identity_aliases = (
                    env["contact.center.identity.alias"]
                    .sudo()
                    .search([("account_id", "in", accounts.ids)])
                )
                identities = identity_aliases.identity_id | (
                    env["contact.center.identity"]
                    .sudo()
                    .search([("name", "=", "Concurrent Sender %s" % token)])
                )
                guests = identities.mail_guest_id | env["mail.guest"].sudo().search(
                    [("name", "=", "Concurrent Sender %s" % token)]
                )
                identity_aliases.unlink()
                identities.unlink()
                guests.with_context(
                    contact_center_membership_token=(CONTACT_CENTER_MEMBERSHIP_TOKEN)
                ).unlink()
                env["contact.center.provider.connection"].sudo().search(
                    [("account_id", "in", accounts.ids)]
                ).unlink()
                accounts.unlink()

            env["contact.center.team"].sudo().with_context(active_test=False).search(
                [("name", "=", "Concurrency Team %s" % token)]
            ).unlink()
            users = (
                env["res.users"]
                .sudo()
                .with_context(active_test=False)
                .search([("login", "=", "cc-concurrency-%s" % token)])
            )
            partners = users.partner_id
            users.unlink()
            partners.exists().unlink()
            # Persist cleanup performed through this independent test cursor.
            cr.commit()  # pylint: disable=invalid-commit

    def test_concurrent_duplicate_events_converge_after_retry(self):
        token = uuid.uuid4().hex
        fixture = None
        try:
            fixture = self._setup_committed_fixture(token)
            results = self._run_concurrent_first_attempts(fixture)

            self.assertEqual(
                sorted(result["outcome"] for result in results.values()),
                ["done", "retry"],
            )
            retry_result = next(
                result for result in results.values() if result["outcome"] == "retry"
            )
            self.assertTrue(retry_result["is_serialization_failure"])
            self.assertFalse(retry_result["is_deadlock"])

            retry_inbox_id = next(
                inbox_id
                for worker_name, inbox_id in (
                    (worker_name, fixture["inbox_ids"][index])
                    for index, worker_name in enumerate(sorted(results))
                )
                if results[worker_name]["outcome"] == "retry"
            )
            self.assertEqual(
                self._process_inbox_transaction(retry_inbox_id)["outcome"],
                "done",
            )

            snapshot = self._snapshot(fixture)
            self.assertEqual(snapshot["inbox_states"], ["done", "done"])
            self.assertEqual(len(snapshot["identity_ids"]), 1)
            self.assertEqual(len(snapshot["guest_ids"]), 1)
            self.assertEqual(snapshot["alias_count"], 2)
            self.assertEqual(
                set(snapshot["alias_identity_ids"]),
                set(snapshot["identity_ids"]),
            )
            self.assertEqual(len(snapshot["binding_ids"]), 1)
            self.assertEqual(len(snapshot["channel_ids"]), 1)
            self.assertEqual(
                set(snapshot["binding_identity_ids"]),
                set(snapshot["identity_ids"]),
            )
            self.assertEqual(snapshot["channel_alias_count"], 2)
            self.assertEqual(
                set(snapshot["channel_alias_binding_ids"]),
                set(snapshot["binding_ids"]),
            )
            self.assertEqual(len(snapshot["message_binding_ids"]), 1)
            self.assertEqual(len(snapshot["message_ids"]), 1)
            self.assertEqual(
                set(snapshot["external_comment_ids"]),
                set(snapshot["message_ids"]),
            )
            self.assertEqual(
                set(snapshot["message_author_guest_ids"]),
                set(snapshot["guest_ids"]),
            )
            self.assertEqual(
                set(snapshot["channel_guest_ids"]),
                set(snapshot["guest_ids"]),
            )
            self.assertEqual(snapshot["conflict_count"], 0)
        finally:
            self._cleanup_committed_fixture(token)

    def test_send_and_inbound_use_channel_then_binding_lock_order(self):
        token = uuid.uuid4().hex
        fixture = None
        try:
            fixture = self._setup_committed_fixture(token)
            self.assertEqual(
                self._process_inbox_transaction(fixture["inbox_ids"][0])["outcome"],
                "done",
            )
            followup = self._create_followup_inbox(fixture, token)

            (
                results,
                observed_order,
                outbound_request_id,
            ) = self._run_concurrent_send_and_inbound(fixture, followup)

            self.assertIn(results["inbound"]["outcome"], ("done", "retry"))
            self.assertLess(
                observed_order.index("inbound_conversation_locked"),
                observed_order.index("outbound_topology_attempted"),
            )
            self.assertLess(
                observed_order.index("outbound_topology_attempted"),
                observed_order.index("outbound_topology_locked"),
            )
            self.assertLess(
                observed_order.index("outbound_topology_locked"),
                observed_order.index("outbound_conversation_attempted"),
            )
            if "outbound" in results:
                self.assertLess(
                    observed_order.index("outbound_conversation_attempted"),
                    observed_order.index("outbound_conversation_locked"),
                )
            else:
                self.assertEqual(
                    results.get("outbound_contention"), "SerializationFailure"
                )
            if results["inbound"]["outcome"] == "retry":
                self.assertEqual(
                    self._process_inbox_transaction(followup["inbox_id"])["outcome"],
                    "done",
                )
            committed_outbound = self._send_concurrent_outbound_transaction(
                fixture,
                followup["channel_id"],
                outbound_request_id,
            )
            self.assertIn("message_id", committed_outbound)
            if "outbound" in results:
                self.assertEqual(
                    committed_outbound["message_id"],
                    results["outbound"]["message_id"],
                )
                self.assertEqual(
                    committed_outbound["outbox_command_id"],
                    results["outbound"]["outbox_command_id"],
                )
            idempotent_replay = self._send_concurrent_outbound_transaction(
                fixture,
                followup["channel_id"],
                outbound_request_id,
            )
            self.assertEqual(
                idempotent_replay["message_id"], committed_outbound["message_id"]
            )
            self.assertEqual(
                idempotent_replay["outbox_command_id"],
                committed_outbound["outbox_command_id"],
            )
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                inbox = (
                    env["contact.center.inbox.event"]
                    .sudo()
                    .browse(followup["inbox_id"])
                )
                self.assertEqual(inbox.state, "done")
                self.assertEqual(
                    env["contact.center.message.binding"]
                    .sudo()
                    .search_count(
                        [
                            ("account_id", "=", fixture["account_id"]),
                            (
                                "external_message_id",
                                "=",
                                followup["external_message_id"],
                            ),
                        ]
                    ),
                    1,
                )
                self.assertEqual(
                    env["contact.center.outbox.command"]
                    .sudo()
                    .search_count(
                        [
                            ("account_id", "=", fixture["account_id"]),
                            ("command_type", "=", "send_message"),
                        ]
                    ),
                    1,
                )
        finally:
            if fixture:
                self._cleanup_committed_fixture(token)

    def _setup_outbound_cutover_candidate(self, fixture, token):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            binding = (
                env["contact.center.channel.binding"]
                .sudo()
                .search(
                    [
                        ("account_id", "=", fixture["account_id"]),
                        ("conversation_type", "=", "direct"),
                        ("active", "=", True),
                    ],
                    limit=1,
                )
            )
            self.assertTrue(binding)
            replacement = env["contact.center.provider.connection"].create(
                {
                    "name": "Concurrency Cutover %s" % token,
                    "account_id": fixture["account_id"],
                    "adapter_key": "test.fake",
                    "external_ref": "concurrency-cutover-%s" % token,
                    "provider_schema_version": "fixture-v1",
                    "state": "connected",
                    "active": True,
                    "role": "standby",
                    "inbound_active": False,
                    "outbound_active": False,
                    "capabilities_json": {"send_message": True},
                    "last_state_observed_at": fields.Datetime.now(),
                }
            )
            result = binding.channel_id.id, replacement.id
            cr.commit()  # pylint: disable=invalid-commit
        return result

    def _coordinate_outbound_admission(
        self,
        recordset,
        original_admission,
        admission_written,
        cutover_observed_account_lock,
    ):
        revision = original_admission(recordset)
        if threading.current_thread().name != "cc-admission-send":
            return revision
        admission_written.set()
        if not cutover_observed_account_lock.wait(timeout=5):
            raise RuntimeError("cutover did not observe the sender's topology lock")
        return revision

    def _coordinate_cutover_topology_lock(
        self,
        recordset,
        account_ids,
        original_topology_lock,
        fixture,
        cutover_observed_account_lock,
    ):
        if threading.current_thread().name != "cc-admission-cutover":
            return original_topology_lock(recordset, account_ids)
        # Prove real lock contention and establish the REPEATABLE READ snapshot
        # while the admission revision bump is still uncommitted.
        try:
            with recordset.env.cr.savepoint():
                recordset.env.cr.execute(
                    "SELECT id FROM contact_center_account "
                    "WHERE id = %s FOR UPDATE NOWAIT",
                    [fixture["account_id"]],
                )
        except LockNotAvailable:
            cutover_observed_account_lock.set()
        else:
            raise RuntimeError("sender did not hold the account topology lock")
        return original_topology_lock(recordset, account_ids)

    def _outbound_admission_send_worker(
        self, fixture, channel_id, request_id, results, errors, admission_written
    ):
        try:
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '5s'")
                cr.execute("SET LOCAL statement_timeout = '10s'")
                env = api.Environment(cr, fixture["agent_id"], {})
                results["send"] = (
                    env["contact.center.ui.api"]
                    .with_context(contact_center_skip_enqueue=True)
                    .send_message(
                        channel_id,
                        "Admitted before provider cutover",
                        client_request_id=request_id,
                    )
                )
                env.flush_all()
                cr.commit()  # pylint: disable=invalid-commit
        except Exception as error:  # surface in the main test thread
            errors["send"] = error
            admission_written.set()

    def _primary_cutover_worker(
        self,
        replacement_id,
        admission_written,
        cutover_observed_account_lock,
        results,
        errors,
    ):
        try:
            if not admission_written.wait(timeout=5):
                raise RuntimeError("sender did not write the admission barrier")
            with self.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '5s'")
                cr.execute("SET LOCAL statement_timeout = '10s'")
                env = api.Environment(cr, SUPERUSER_ID, {})
                env["contact.center.provider.connection"].browse(
                    replacement_id
                ).action_use_as_primary()
                cr.commit()  # pylint: disable=invalid-commit
                results["cutover"] = "committed"
        except Exception as error:  # surface in the main test thread
            errors["cutover"] = error
            cutover_observed_account_lock.set()

    def _join_outbound_cutover_workers(
        self, threads, admission_written, cutover_observed_account_lock
    ):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=self.WORKER_TIMEOUT_SECONDS)
        live_threads = [thread.name for thread in threads if thread.is_alive()]
        if not live_threads:
            return
        admission_written.set()
        cutover_observed_account_lock.set()
        for thread in threads:
            thread.join(timeout=2)
        self.fail("Admission/cutover workers did not finish: %s" % live_threads)

    def _assert_outbound_cutover_result(
        self, fixture, replacement_id, request_id, results, errors
    ):
        if "send" in errors:
            raise errors["send"]
        self.assertIn("send", results)
        self.assertNotIn("cutover", results)
        self.assertIsInstance(errors.get("cutover"), SerializationFailure)

        # A new transaction sees the admitted outbox and rejects the role switch.
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            with self.assertRaises(ValidationError):
                env["contact.center.provider.connection"].browse(
                    replacement_id
                ).action_use_as_primary()

        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            primary = env["contact.center.provider.connection"].browse(
                fixture["connection_id"]
            )
            replacement = env["contact.center.provider.connection"].browse(
                replacement_id
            )
            outbox = (
                env["contact.center.outbox.command"]
                .sudo()
                .search([("ui_request_id", "=", request_id)])
            )
            self.assertEqual(len(outbox), 1)
            self.assertEqual(outbox.state, "pending")
            self.assertEqual(outbox.provider_connection_id, primary)
            self.assertEqual(primary.role, "primary")
            self.assertEqual(replacement.role, "standby")
            self.assertEqual(primary.outbound_admission_revision, 1)

    def test_outbound_admission_revision_serializes_send_before_primary_cutover(self):
        token = uuid.uuid4().hex
        fixture = None
        admission_written = threading.Event()
        cutover_observed_account_lock = threading.Event()
        results = {}
        errors = {}
        try:
            fixture = self._setup_committed_fixture(token)
            self.assertEqual(
                self._process_inbox_transaction(fixture["inbox_ids"][0])["outcome"],
                "done",
            )
            channel_id, replacement_id = self._setup_outbound_cutover_candidate(
                fixture, token
            )

            request_id = str(uuid.uuid4())
            provider_model_class = type(self.env["contact.center.provider.connection"])
            original_admission = (
                provider_model_class._contact_center_record_outbound_admission
            )
            original_topology_lock = provider_model_class._contact_center_lock_topology

            coordinated_admission = partial(
                self._coordinate_outbound_admission,
                original_admission=original_admission,
                admission_written=admission_written,
                cutover_observed_account_lock=cutover_observed_account_lock,
            )
            coordinated_topology_lock = partial(
                self._coordinate_cutover_topology_lock,
                original_topology_lock=original_topology_lock,
                fixture=fixture,
                cutover_observed_account_lock=cutover_observed_account_lock,
            )

            def coordinated_admission_method(recordset):
                return coordinated_admission(recordset)

            def coordinated_topology_lock_method(recordset, account_ids):
                return coordinated_topology_lock(recordset, account_ids)

            threads = [
                threading.Thread(
                    target=partial(
                        self._outbound_admission_send_worker,
                        fixture,
                        channel_id,
                        request_id,
                        results,
                        errors,
                        admission_written,
                    ),
                    name="cc-admission-send",
                    daemon=True,
                ),
                threading.Thread(
                    target=partial(
                        self._primary_cutover_worker,
                        replacement_id,
                        admission_written,
                        cutover_observed_account_lock,
                        results,
                        errors,
                    ),
                    name="cc-admission-cutover",
                    daemon=True,
                ),
            ]
            with mock.patch.object(
                provider_model_class,
                "_contact_center_record_outbound_admission",
                new=coordinated_admission_method,
            ), mock.patch.object(
                provider_model_class,
                "_contact_center_lock_topology",
                new=coordinated_topology_lock_method,
            ):
                self._join_outbound_cutover_workers(
                    threads, admission_written, cutover_observed_account_lock
                )

            self._assert_outbound_cutover_result(
                fixture, replacement_id, request_id, results, errors
            )
        finally:
            admission_written.set()
            cutover_observed_account_lock.set()
            if fixture:
                self._cleanup_committed_fixture(token)

    def test_worker_death_after_durable_outbox_boundary_never_redispatches(self):
        token = uuid.uuid4().hex
        fixture = None
        try:
            fixture = self._setup_committed_fixture(token)
            self.assertEqual(
                self._process_inbox_transaction(fixture["inbox_ids"][0])["outcome"],
                "done",
            )
            durable = self._create_committed_outbox(fixture, token)

            first_dispatch_count = self._simulate_outbox_worker_death_after_boundary(
                durable["outbox_id"], durable["queue_job_uuid"]
            )
            self.assertEqual(first_dispatch_count, 1)
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                outbox = (
                    env["contact.center.outbox.command"]
                    .sudo()
                    .browse(durable["outbox_id"])
                )
                self.assertEqual(outbox.state, "processing")
                self.assertEqual(outbox.dispatch_job_uuid, durable["queue_job_uuid"])
                self.assertTrue(outbox.dispatch_started_at)

            result, dispatch_count = self._resume_outbox_after_worker_death(
                durable["outbox_id"], durable["queue_job_uuid"]
            )

            self.assertFalse(result)
            self.assertEqual(dispatch_count, 0)
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                outbox = (
                    env["contact.center.outbox.command"]
                    .sudo()
                    .browse(durable["outbox_id"])
                )
                self.assertEqual(outbox.state, "uncertain")
                self.assertEqual(outbox.last_error_class, "RuntimeError")
                self.assertEqual(
                    outbox.message_binding_id.message_id.id,
                    durable["message_id"],
                )
                self.assertEqual(
                    outbox.message_binding_id.delivery_state,
                    "queued",
                )
        finally:
            if fixture:
                self._cleanup_committed_fixture(token)

    def test_concurrent_outbox_jobs_share_connection_throttle_before_boundary(self):
        token = uuid.uuid4().hex
        fixture = None
        try:
            fixture = self._setup_committed_fixture(token)
            self.assertEqual(
                self._process_inbox_transaction(fixture["inbox_ids"][0])["outcome"],
                "done",
            )
            durables = [
                self._create_committed_outbox(fixture, "%s-%s" % (token, index))
                for index in range(2)
            ]
            crossings = []
            crossings_lock = threading.Lock()
            leader_at_adapter = threading.Event()
            follower_finished = threading.Event()

            def execute_adapter(recordset, _adapter, _connection, _command_dto):
                with crossings_lock:
                    crossings.append(recordset.id)
                leader_at_adapter.set()
                if not follower_finished.wait(timeout=5):
                    raise RuntimeError("follower did not observe the shared throttle")
                return AdapterResult.success(
                    external_message_id="provider-throttled-%s" % recordset.id,
                    provider_response={"accepted": True},
                )

            outbox_model_class = type(self.env["contact.center.outbox.command"])
            adapter_class = adapter_registry.get("test.fake")
            with mock.patch.object(
                adapter_class,
                "outbound_min_interval_seconds",
                autospec=True,
                return_value=60,
            ), mock.patch.object(
                outbox_model_class,
                "_execute_adapter",
                autospec=True,
                side_effect=execute_adapter,
            ):
                results = self._run_concurrent_throttled_outboxes(
                    durables,
                    leader_at_adapter,
                    follower_finished,
                )

            self.assertEqual(
                sorted(item["outcome"] for item in results.values()),
                ["done", "retry"],
            )
            self.assertEqual(len(crossings), 1)
            done_id = next(
                outbox_id
                for outbox_id, result in results.items()
                if result["outcome"] == "done"
            )
            retry_id = next(
                outbox_id
                for outbox_id, result in results.items()
                if result["outcome"] == "retry"
            )
            self.assertEqual(crossings, [done_id])
            self.assertTrue(results[done_id]["result"])
            self.assertEqual(results[retry_id]["cause"], "OutboundThrottleError")
            self.assertTrue(results[retry_id]["ignore_retry"])
            self.assertGreaterEqual(results[retry_id]["seconds"], 1)
            self.assertLessEqual(results[retry_id]["seconds"], 61)

            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                done = env["contact.center.outbox.command"].sudo().browse(done_id)
                retry = env["contact.center.outbox.command"].sudo().browse(retry_id)
                self.assertEqual(done.state, "done")
                self.assertEqual(done.attempts, 1)
                self.assertEqual(done.message_binding_id.delivery_state, "sent")
                self.assertEqual(retry.state, "pending")
                self.assertEqual(retry.attempts, 0)
                self.assertFalse(retry.dispatch_job_uuid)
                self.assertFalse(retry.dispatch_started_at)
                self.assertFalse(retry.provider_request_json)
                self.assertEqual(retry.last_error_class, "OutboundThrottleError")
                self.assertEqual(retry.message_binding_id.delivery_state, "queued")
        finally:
            if fixture:
                self._cleanup_committed_fixture(token)

    def test_concurrent_first_group_channel_converges_after_retry(self):
        token = uuid.uuid4().hex
        fixture = None
        try:
            fixture = self._setup_committed_group_fixture(token)
            results = self._run_concurrent_group_first_attempts(fixture)

            outcomes = sorted(result["outcome"] for result in results.values())
            self.assertIn(
                outcomes,
                (["done", "done"], ["done", "retry"]),
            )
            if "retry" in outcomes:
                retry_inbox_id = next(
                    inbox_id
                    for inbox_id, result in results.items()
                    if result["outcome"] == "retry"
                )
                retry_result = results[retry_inbox_id]
                self.assertTrue(retry_result["is_serialization_failure"])
                self.assertFalse(retry_result["is_deadlock"])
                self.assertEqual(
                    self._process_inbox_transaction(retry_inbox_id)["outcome"],
                    "done",
                )

            # Both PostgreSQL schedules are valid here: one worker may be forced
            # through queue retry, or the second worker may observe the first
            # committed group and converge in its initial job.  The durable
            # cardinality assertions below are the actual safety property.
            snapshot = self._snapshot_group_fixture(fixture)
            self.assertEqual(snapshot["inbox_states"], ["done", "done"])
            self.assertEqual(len(snapshot["binding_ids"]), 1)
            self.assertEqual(snapshot["binding_identity_ids"], [])
            self.assertEqual(len(snapshot["channel_ids"]), 1)
            self.assertEqual(len(snapshot["group_profile_ids"]), 1)
            self.assertEqual(len(snapshot["identity_ids"]), 2)
            self.assertEqual(len(snapshot["guest_ids"]), 2)
            self.assertEqual(len(snapshot["identity_alias_ids"]), 4)
            self.assertEqual(len(snapshot["channel_alias_ids"]), 1)
            self.assertEqual(
                set(snapshot["channel_alias_binding_ids"]),
                set(snapshot["binding_ids"]),
            )
            self.assertEqual(len(snapshot["message_binding_ids"]), 2)
            self.assertEqual(
                set(snapshot["message_channel_ids"]),
                set(snapshot["binding_ids"]),
            )
            self.assertEqual(
                set(snapshot["message_author_guest_ids"]),
                set(snapshot["guest_ids"]),
            )
            self.assertEqual(
                set(snapshot["channel_guest_ids"]),
                set(snapshot["guest_ids"]),
            )
            self.assertEqual(snapshot["conflict_count"], 0)
        finally:
            self._cleanup_committed_fixture(token)

    def test_concurrent_edits_converge_after_repeatable_read_retry(self):
        token = uuid.uuid4().hex
        fixture = None
        try:
            fixture = self._setup_committed_fixture(token)
            result = self._process_inbox_transaction(fixture["inbox_ids"][0])
            self.assertEqual(result["outcome"], "done")
            mutation_fixture = self._create_concurrent_edit_mutations(fixture)

            errors = self._run_concurrent_mutation_projection(mutation_fixture)

            # Odoo runs PostgreSQL transactions at REPEATABLE READ.  The older
            # worker established its snapshot before the newer worker committed
            # the binding update, so PostgreSQL correctly aborts that whole
            # transaction instead of serving a stale snapshot.  Production
            # inbox jobs turn this exception into RetryableJobError; model code
            # must not attempt an unsafe in-transaction retry.
            self.assertEqual(set(errors), {"older"})
            self.assertIsInstance(errors["older"], SerializationFailure)
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                mutations = (
                    env["contact.center.message.mutation"]
                    .sudo()
                    .browse(
                        [
                            mutation_fixture["older_id"],
                            mutation_fixture["newer_id"],
                        ]
                    )
                )
                self.assertEqual(mutations[0].state, "pending")
                self.assertEqual(mutations[1].state, "applied")
            self._retry_mutation_projection(mutation_fixture["older_id"])

            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                target = (
                    env["contact.center.message.binding"]
                    .sudo()
                    .browse(mutation_fixture["target_id"])
                )
                older = (
                    env["contact.center.message.mutation"]
                    .sudo()
                    .browse(mutation_fixture["older_id"])
                )
                self.assertEqual(
                    env["contact.center.ui.api"]._body_text(target.message_id.body),
                    "Newest concurrent body",
                )
                self.assertEqual(target.message_state, "edited")
                self.assertEqual(older.state, "applied")
                self.assertEqual(
                    older.details_json["projection"]["reason"], "superseded"
                )
        finally:
            if fixture:
                self._cleanup_committed_fixture(token)

    def test_concurrent_reactions_converge_after_repeatable_read_retry(self):
        token = uuid.uuid4().hex
        fixture = None
        try:
            fixture = self._setup_committed_fixture(token)
            result = self._process_inbox_transaction(fixture["inbox_ids"][0])
            self.assertEqual(result["outcome"], "done")
            mutation_fixture = self._create_concurrent_reaction_mutations(fixture)

            errors = self._run_concurrent_mutation_projection(mutation_fixture)

            self.assertEqual(set(errors), {"older"})
            self.assertIsInstance(errors["older"], SerializationFailure)
            self._retry_mutation_projection(mutation_fixture["older_id"])

            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                target = (
                    env["contact.center.message.binding"]
                    .sudo()
                    .browse(mutation_fixture["target_id"])
                )
                older = (
                    env["contact.center.message.mutation"]
                    .sudo()
                    .browse(mutation_fixture["older_id"])
                )
                self.assertEqual(target.message_id.sudo().reaction_ids.content, "👍")
                self.assertGreaterEqual(target.mutation_projection_revision, 1)
                self.assertEqual(older.state, "applied")
                self.assertEqual(
                    older.details_json["projection"]["reason"], "superseded"
                )
        finally:
            if fixture:
                self._cleanup_committed_fixture(token)
