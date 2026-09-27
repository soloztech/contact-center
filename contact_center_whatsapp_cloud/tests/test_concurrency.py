import threading
import uuid
from unittest import mock

from psycopg2.errors import DeadlockDetected, LockNotAvailable, SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from .common import WhatsAppCloudFixtureMixin

_CONTENTION = (DeadlockDetected, LockNotAvailable, SerializationFailure)


@tagged("post_install", "-at_install")
class TestWhatsAppCloudLifecycleLockOrder(WhatsAppCloudFixtureMixin, TransactionCase):
    """Run subscription lifecycle decisions in real, independent transactions.

    A dispatch holds the endpoint (shared policy) and then locks the inbox
    topology; retiring the last route holds the topology and then archives the
    subscription under endpoint locks (CC-WAC-07). Two inboxes retiring the last
    two routes of one business account hold disjoint topologies (CC-WAC-11).
    Only committed rows can be locked by another transaction, so the routes are
    committed and removed again.
    """

    WORKER_TIMEOUT_SECONDS = 15

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._install_network_guard()
        cls._set_environment()

    @classmethod
    def tearDownClass(cls):
        cls._restore_environment()
        super().tearDownClass()

    # -- Committed fixture -------------------------------------------------------

    def _committed_route(self, inboxes=1, spare_inboxes=0):
        """One business account owner with one live route per inbox.

        Spare inboxes have no connection yet: a worker may create one.
        """

        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            cr.execute("SELECT COALESCE(MAX(id), 0) FROM queue_job")
            first_job_id = cr.fetchone()[0]
            builder = type("CommittedRoute", (WhatsAppCloudFixtureMixin,), {"env": env})
            agent = builder._create_user(
                "Concurrency", env.ref("contact_center_base.group_contact_center_agent")
            )
            team = builder._create_team("Concurrency", agents=agent)
            app = builder._create_app()
            endpoint = builder._create_endpoint(app)
            owner = builder._create_waba(endpoint, builder._numeric_id())
            subscription = builder._subscribe(owner)
            accounts = env["contact.center.account"]
            connections = env["contact.center.provider.connection"]
            for _index in range(inboxes):
                account = builder._create_account(team=team)
                accounts |= account
                connections |= builder._create_connection(
                    account, owner.asset_ids.ensure_one(), builder._numeric_id()
                )
            spares = env["contact.center.account"]
            for _index in range(spare_inboxes):
                spares |= builder._create_account(team=team)
            env.flush_all()
            route = {
                "first_job_id": first_job_id,
                "user_id": agent.id,
                "partner_id": agent.partner_id.id,
                "team_id": team.id,
                "app_id": app.id,
                "endpoint_id": endpoint.id,
                "page_id": owner.id,
                "subscription_id": subscription.id,
                "asset_id": owner.asset_ids.id,
                "account_ids": (accounts | spares).ids,
                "spare_account_ids": spares.ids,
                "connection_ids": connections.ids,
            }
            # Independent worker cursors must observe the committed route.
            cr.commit()  # pylint: disable=invalid-commit
            return route

    @staticmethod
    def _fixture_connections(env, route):
        return (
            env["contact.center.provider.connection"]
            .sudo()
            .with_context(active_test=False)
            .search([("account_id", "in", route["account_ids"])], order="id")
        )

    def _fixture_jobs(self, env, route):
        """Only the queue jobs whose records belong to this committed route."""

        owned = {
            "contact.center.provider.connection": set(
                self._fixture_connections(env, route).ids
            ),
            "contact.center.account": set(route["account_ids"]),
            "meta.webhook.endpoint": {route["endpoint_id"]},
            "meta.webhook.page": {route["page_id"]},
            "meta.webhook.subscription": {route["subscription_id"]},
        }
        candidates = (
            env["queue.job"].sudo().search([("id", ">", route["first_job_id"])])
        )
        return candidates.filtered(
            lambda job: owned.get(job.model_name, set()).intersection(job.records.ids)
        )

    def _remove_committed_route(self, route):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            self._fixture_jobs(env, route).unlink()
            self._fixture_connections(env, route).unlink()
            env["contact.center.account"].sudo().with_context(active_test=False).browse(
                route["account_ids"]
            ).unlink()
            env["contact.center.team"].sudo().with_context(active_test=False).browse(
                route["team_id"]
            ).unlink()
            env["res.users"].sudo().with_context(active_test=False).browse(
                route["user_id"]
            ).unlink()
            env["res.partner"].sudo().browse(route["partner_id"]).exists().unlink()
            # The shared Meta configuration can only be archived through the ORM;
            # this test-owned route is removed directly, children first.
            cr.execute(
                "DELETE FROM meta_webhook_subscription WHERE page_id = %s",
                [route["page_id"]],
            )
            cr.execute(
                "DELETE FROM meta_webhook_asset WHERE page_id = %s", [route["page_id"]]
            )
            cr.execute(
                "DELETE FROM meta_webhook_page WHERE id = %s", [route["page_id"]]
            )
            cr.execute(
                "DELETE FROM meta_webhook_endpoint WHERE id = %s",
                [route["endpoint_id"]],
            )
            cr.execute("DELETE FROM meta_api_app WHERE id = %s", [route["app_id"]])
            # Persist cleanup performed through this independent test cursor.
            cr.commit()  # pylint: disable=invalid-commit

    # -- Workers -----------------------------------------------------------------

    @staticmethod
    def _limit_waits(cr):
        cr.execute("SET LOCAL lock_timeout = '5s'")
        cr.execute("SET LOCAL statement_timeout = '10s'")

    def _retire_route(self, connection_id):
        with self.registry.cursor() as cr:
            self._limit_waits(cr)
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["contact.center.provider.connection"].browse(
                connection_id
            ).action_set_historical()

    def _dispatch_like(self, route, endpoint_held, topology_held):
        """Lock exactly as a WhatsApp dispatch does: policy, then topology."""

        with self.registry.cursor() as cr:
            self._limit_waits(cr)
            env = api.Environment(cr, SUPERUSER_ID, {})
            endpoint = env["meta.webhook.endpoint"].sudo().browse(route["endpoint_id"])
            self.assertTrue(endpoint._lock_active_policy())
            endpoint_held.set()
            if not topology_held.wait(timeout=5):
                raise RuntimeError("the lifecycle never held the topology")
            env[
                "contact.center.provider.connection"
            ].sudo()._contact_center_lock_topology(route["account_ids"])

    @staticmethod
    def _worker_outcome(worker, *args, on_error=None):
        try:
            worker(*args)
            return "done"
        except _CONTENTION as error:
            return error.__class__.__name__
        except Exception as error:  # surface failures in the main test thread
            if on_error:
                on_error()
            return repr(error)

    def _run_threads(self, threads, release):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=self.WORKER_TIMEOUT_SECONDS)
        live = [thread.name for thread in threads if thread.is_alive()]
        if live:
            release()
            for thread in threads:
                thread.join(timeout=2)
            self.fail("Lifecycle workers did not finish: %s" % live)

    def _connection_class(self):
        return type(self.env["contact.center.provider.connection"])

    def _run_retirement_against_dispatch(self, route):
        endpoint_held = threading.Event()
        topology_held = threading.Event()
        results = {}
        reconcile = self._connection_class()._wac_reconcile_subscription_lifecycle

        def release():
            endpoint_held.set()
            topology_held.set()

        def coordinated_reconcile(recordset, page_ids):
            # The core switch already holds the inbox and connection locks here.
            topology_held.set()
            return reconcile(recordset, page_ids)

        def dispatch_worker():
            results["dispatch"] = self._worker_outcome(
                self._dispatch_like,
                route,
                endpoint_held,
                topology_held,
                on_error=release,
            )

        def lifecycle_worker():
            if not endpoint_held.wait(timeout=5):
                results["lifecycle"] = "the dispatch never held the endpoint"
                return
            results["lifecycle"] = self._worker_outcome(
                self._retire_route, route["connection_ids"][0], on_error=release
            )

        with mock.patch.object(
            self._connection_class(),
            "_wac_reconcile_subscription_lifecycle",
            new=coordinated_reconcile,
        ):
            self._run_threads(
                [
                    threading.Thread(
                        target=dispatch_worker, name="wac-dispatch", daemon=True
                    ),
                    threading.Thread(
                        target=lifecycle_worker, name="wac-archive", daemon=True
                    ),
                ],
                release,
            )
        return results

    def _run_simultaneous_retirements(self, route):
        both_decide = threading.Barrier(2, timeout=5)
        results = {}
        reconcile = self._connection_class()._wac_reconcile_subscription_lifecycle

        def coordinated_reconcile(recordset, page_ids):
            # Both inboxes hold their own topology and snapshot before either
            # decides: each still sees the other's route as live.
            both_decide.wait()
            return reconcile(recordset, page_ids)

        def retirement_worker(connection_id):
            results[connection_id] = self._worker_outcome(
                self._retire_route, connection_id, on_error=both_decide.abort
            )

        with mock.patch.object(
            self._connection_class(),
            "_wac_reconcile_subscription_lifecycle",
            new=coordinated_reconcile,
        ):
            self._run_threads(
                [
                    threading.Thread(
                        target=retirement_worker,
                        args=(connection_id,),
                        name="wac-retire-%s" % connection_id,
                        daemon=True,
                    )
                    for connection_id in route["connection_ids"]
                ],
                both_decide.abort,
            )
        return results

    def _create_live_route(self, route, account_id):
        with self.registry.cursor() as cr:
            self._limit_waits(cr)
            env = api.Environment(cr, SUPERUSER_ID, {})
            builder = type("CommittedRoute", (WhatsAppCloudFixtureMixin,), {"env": env})
            builder._create_connection(
                env["contact.center.account"].browse(account_id),
                env["meta.webhook.asset"].browse(route["asset_id"]),
                builder._numeric_id(),
            )

    def _run_retirement_around_creation(self, route):
        retirement_holds_snapshot = threading.Event()
        creation_committed = threading.Event()
        results = {}
        reconcile = self._connection_class()._wac_reconcile_subscription_lifecycle

        def release():
            retirement_holds_snapshot.set()
            creation_committed.set()

        def coordinated_reconcile(recordset, page_ids):
            if threading.current_thread().name == "wac-retire":
                # The retirement's snapshot still shows its route as the last
                # one; another inbox creates and commits a live route now.
                retirement_holds_snapshot.set()
                if not creation_committed.wait(timeout=5):
                    raise RuntimeError("the new route was never committed")
            return reconcile(recordset, page_ids)

        def retirement_worker():
            results["retirement"] = self._worker_outcome(
                self._retire_route, route["connection_ids"][0], on_error=release
            )

        def creation_worker():
            try:
                if not retirement_holds_snapshot.wait(timeout=5):
                    results["creation"] = "the retirement never took its snapshot"
                    return
                results["creation"] = self._worker_outcome(
                    self._create_live_route, route, route["spare_account_ids"][0]
                )
            finally:
                creation_committed.set()

        with mock.patch.object(
            self._connection_class(),
            "_wac_reconcile_subscription_lifecycle",
            new=coordinated_reconcile,
        ):
            self._run_threads(
                [
                    threading.Thread(
                        target=retirement_worker, name="wac-retire", daemon=True
                    ),
                    threading.Thread(
                        target=creation_worker, name="wac-create", daemon=True
                    ),
                ],
                release,
            )
        return results

    def _observed(self, route):
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT active FROM meta_webhook_subscription WHERE id = %s",
                [route["subscription_id"]],
            )
            subscription_active = cr.fetchone()[0]
            cr.execute(
                "SELECT role, active FROM contact_center_provider_connection "
                "WHERE account_id IN %s ORDER BY id",
                [tuple(route["account_ids"])],
            )
            return subscription_active, cr.fetchall()

    # -- Tests -------------------------------------------------------------------

    def test_retiring_the_last_route_never_deadlocks_with_a_dispatch(self):
        route = self._committed_route()
        try:
            results = self._run_retirement_against_dispatch(route)
            # The archive never waits for the endpoint while it holds the
            # topology: it gives way at once and the dispatch completes.
            self.assertEqual(
                results,
                {"dispatch": "done", "lifecycle": "LockNotAvailable"},
            )
            self.assertEqual(self._observed(route), (True, [("primary", True)]))
            # The retried transaction decides again and archives the route.
            self._retire_route(route["connection_ids"][0])
            self.assertEqual(self._observed(route), (False, [("historical", False)]))
        finally:
            self._remove_committed_route(route)

    def test_two_inboxes_retiring_the_last_routes_archive_the_subscription(self):
        route = self._committed_route(inboxes=2)
        try:
            results = self._run_simultaneous_retirements(route)
            # One decision wins; the other is refused before it can keep the
            # subscription on a stale view, and never waits while it holds its
            # topology.
            outcomes = sorted(results.values())
            self.assertEqual(len(outcomes), 2, results)
            self.assertEqual(outcomes.count("done"), 1, results)
            (refused,) = [value for value in outcomes if value != "done"]
            self.assertIn(refused, ("LockNotAvailable", "SerializationFailure"))
            # The retried transaction sees the other route retired.
            (retry_id,) = [key for key, value in results.items() if value != "done"]
            self._retire_route(retry_id)
            self.assertEqual(
                self._observed(route),
                (False, [("historical", False), ("historical", False)]),
            )
        finally:
            self._remove_committed_route(route)

    def test_route_created_after_a_retirement_snapshot_keeps_the_subscription(self):
        route = self._committed_route(inboxes=1, spare_inboxes=1)
        try:
            results = self._run_retirement_around_creation(route)
            # The new live route versioned the shared subscription after the
            # retirement's snapshot: the retirement cannot archive it on that
            # stale view.
            self.assertEqual(
                results, {"creation": "done", "retirement": "SerializationFailure"}
            )
            self.assertEqual(
                self._observed(route), (True, [("primary", True), ("primary", True)])
            )
            # Retried on a new snapshot, it sees the new route and keeps it.
            self._retire_route(route["connection_ids"][0])
            self.assertEqual(
                self._observed(route),
                (True, [("historical", False), ("primary", True)]),
            )
        finally:
            self._remove_committed_route(route)

    def test_cleanup_removes_only_the_fixture_jobs(self):
        route = self._committed_route()
        unrelated_uuid = None
        try:
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                connection = env["contact.center.provider.connection"].browse(
                    route["connection_ids"]
                )
                fixture_job = connection.with_delay(
                    description="Fixture-owned job %s" % uuid.uuid4()
                )._job_check_health()
                unrelated_job = (
                    env.ref("base.main_partner")
                    .with_delay(description="Unrelated job %s" % uuid.uuid4())
                    .exists()
                )
                fixture_uuid, unrelated_uuid = fixture_job.uuid, unrelated_job.uuid
                cr.commit()  # pylint: disable=invalid-commit
            self._remove_committed_route(route)
            route = None
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                jobs = (
                    env["queue.job"]
                    .sudo()
                    .search([("uuid", "in", [fixture_uuid, unrelated_uuid])])
                )
                self.assertEqual(jobs.mapped("uuid"), [unrelated_uuid])
        finally:
            if route:
                self._remove_committed_route(route)
            if unrelated_uuid:
                with self.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    env["queue.job"].sudo().search(
                        [("uuid", "=", unrelated_uuid)]
                    ).unlink()
                    cr.commit()  # pylint: disable=invalid-commit
