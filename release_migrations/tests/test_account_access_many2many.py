"""Offline regression tests for the migration's handoff and failure boundaries."""

import copy
import importlib.util
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "contact_center_base/migrations/16.0.1.1.0/account_access.py"
SPEC = importlib.util.spec_from_file_location("account_access_migration", PATH)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class _Field:
    __slots__ = ("type",)

    def __init__(self, field_type):
        self.type = field_type


class _Ids:
    def __init__(self, ids=()):
        self.ids = list(ids)

    def __bool__(self):
        return bool(self.ids)


class _Account:
    def __init__(self, record_id):
        self.id = record_id
        self.access_user_ids = _Ids()
        self.access_team_ids = _Ids()
        self.writes = []

    def with_context(self, **_values):
        return self

    def write(self, values):  # pylint: disable=method-required-super
        self.writes.append(copy.deepcopy(values))
        self.access_user_ids = _Ids(values["access_user_ids"][0][2])
        self.access_team_ids = _Ids(values["access_team_ids"][0][2])


class _Accounts(list):
    @property
    def ids(self):
        return [account.id for account in self]


class _Params:
    def __init__(self):
        self.values = {"database.uuid": "isolated-test-database"}

    def sudo(self):
        return self

    def get_param(self, key):
        return self.values.get(key, False)

    def set_param(self, key, value):
        self.values[key] = value


class _Cursor:
    def __init__(self, env):
        self.env = env

    @contextmanager
    def savepoint(self):
        params = copy.deepcopy(self.env.params.values)
        accounts = copy.deepcopy(self.env.accounts)
        try:
            yield
        except BaseException:
            self.env.params.values = params
            self.env.accounts[:] = accounts
            raise


class _Model:
    _fields = {name: _Field("many2many") for name in migration.NEW_FIELDS}


class _Env:
    def __init__(self):
        self.params = _Params()
        self.accounts = _Accounts([_Account(10), _Account(11)])
        self.cr = _Cursor(self)

    def __getitem__(self, name):
        if name == "ir.config_parameter":
            return self.params
        if name == migration.ACCOUNT:
            return _Model()
        raise AssertionError("Unexpected ORM operation: " + name)

    def flush_all(self):
        pass


def _before():
    return {
        "schema": 1,
        "target_version": migration.VERSION,
        "database_uuid": "isolated-test-database",
        "accounts": [
            {"id": 10, "user_ids": [6], "team_ids": [3], "effective_user_ids": [6, 8]},
            {"id": 11, "user_ids": [], "team_ids": [4], "effective_user_ids": [9]},
        ],
        "stable": {migration.ACCOUNT: {"count": 2, "sha256": "old-business-state"}},
        "rules": [{"xmlid": name} for name in migration.RULE_XMLIDS],
    }


def _store(env, before, state="prepared"):
    env.params.set_param(migration.STATE_KEY, state)
    env.params.set_param(
        migration.SNAPSHOT_KEY,
        json.dumps({"snapshot": before, "sha256": migration.digest(before)}),
    )


class TestAccountAccessHandoff(unittest.TestCase):
    def test_prepare_is_idempotent_and_does_not_change_accounts(self):
        env = _Env()
        before = _before()
        with patch.object(migration, "snapshot", return_value=before):
            self.assertEqual(migration.prepare(env, before), before)
            original = copy.deepcopy(env.params.values)
            self.assertEqual(migration.prepare(env, before), before)
        self.assertEqual(env.params.values, original)
        self.assertTrue(all(not account.writes for account in env.accounts))

    def test_prepare_rollback_restores_absent_handoff(self):
        env = _Env()
        original = copy.deepcopy(env.params.values)
        with self.assertRaisesRegex(RuntimeError, "operator dry run"):
            with env.cr.savepoint(), patch.object(
                migration, "snapshot", return_value=_before()
            ):
                migration.prepare(env)
                raise RuntimeError("operator dry run")
        self.assertEqual(env.params.values, original)

    def test_prepare_refuses_changed_reviewed_grants(self):
        env = _Env()
        current = _before()
        current["accounts"][0]["user_ids"] = [12]
        with patch.object(migration, "snapshot", return_value=current):
            with self.assertRaisesRegex(RuntimeError, "reviewed snapshot"):
                migration.prepare(env, _before())
        self.assertFalse(env.params.get_param(migration.STATE_KEY))

    def test_native_guard_requires_preparation(self):
        with self.assertRaisesRegex(RuntimeError, "old registry"):
            migration.prepared_snapshot(_Env())

    def test_native_guard_rejects_tampered_snapshot(self):
        env = _Env()
        _store(env, _before())
        envelope = json.loads(env.params.get_param(migration.SNAPSHOT_KEY))
        envelope["snapshot"]["accounts"][0]["user_ids"] = [12]
        env.params.set_param(migration.SNAPSHOT_KEY, json.dumps(envelope))
        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            migration.prepared_snapshot(env)

    def test_native_guard_rejects_another_database(self):
        env = _Env()
        _store(env, _before())
        env.params.values["database.uuid"] = "wrong-database"
        with self.assertRaisesRegex(RuntimeError, "another database"):
            migration.prepared_snapshot(env)

    def test_snapshot_refuses_duplicate_accounts_and_non_singleton_source(self):
        before = _before()
        before["accounts"][1]["id"] = before["accounts"][0]["id"]
        with self.assertRaisesRegex(RuntimeError, "Duplicate"):
            migration._validate_snapshot(before)
        before = _before()
        before["accounts"][0]["user_ids"] = [6, 7]
        with self.assertRaisesRegex(RuntimeError, "single access fields"):
            migration._validate_snapshot(before)

    def _finalize(self, env, validation=None):
        with patch.object(
            migration, "_stable", return_value=_before()["stable"]
        ), patch.object(migration, "rule_domain_plan", return_value=[]), patch.object(
            migration, "_records", return_value=env.accounts
        ), patch.object(
            migration,
            "validate_transition",
            side_effect=validation or (lambda *_: {"accounts": 2}),
        ):
            return migration.finalize(env)

    def test_finalize_preserves_singletons_and_empty_direct_grants(self):
        env = _Env()
        _store(env, _before())
        result = self._finalize(env)
        self.assertEqual(result["state"], "done")
        self.assertEqual(env.accounts[0].access_user_ids.ids, [6])
        self.assertEqual(env.accounts[0].access_team_ids.ids, [3])
        self.assertEqual(env.accounts[1].access_user_ids.ids, [])
        self.assertEqual(env.accounts[1].access_team_ids.ids, [4])
        self.assertEqual(env.params.get_param(migration.STATE_KEY), "done")

    def test_failed_validation_rolls_back_every_access_write(self):
        env = _Env()
        _store(env, _before())
        original = copy.deepcopy(env.params.values)
        with self.assertRaisesRegex(RuntimeError, "assignment changed"):
            self._finalize(env, RuntimeError("assignment changed"))
        self.assertEqual(env.params.values, original)
        self.assertTrue(all(not account.writes for account in env.accounts))
        self.assertTrue(all(not account.access_user_ids for account in env.accounts))

    def test_existing_target_grants_are_not_overwritten(self):
        env = _Env()
        _store(env, _before())
        env.accounts[1].access_user_ids = _Ids([19])
        with self.assertRaisesRegex(RuntimeError, "already exist"):
            self._finalize(env)
        self.assertFalse(env.accounts[0].writes)
        self.assertEqual(env.accounts[1].access_user_ids.ids, [19])

    def test_completed_migration_never_overwrites_subsequent_user_edits(self):
        env = _Env()
        _store(env, _before(), state="done")
        env.accounts[0].access_user_ids = _Ids([6, 20])
        result = migration.finalize(env)
        self.assertTrue(result["already_done"])
        self.assertEqual(env.accounts[0].access_user_ids.ids, [6, 20])
        self.assertTrue(all(not account.writes for account in env.accounts))


class TestCanonicalRuleMigration(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.xml = ROOT / "contact_center_base/security/contact_center_security.xml"
        tree = migration.ElementTree.parse(self.xml)
        self.before = []
        for row in tree.getroot().findall(".//record[@model='ir.rule']"):
            if row.get("id") not in migration.RULE_XMLIDS:
                continue
            domain = row.find("field[@name='domain_force']").text.strip()
            domain = domain.replace("access_user_ids", "owner_user_id").replace(
                "access_team_ids", "default_team_id"
            )
            if row.get("id") in migration.AUDITED_RULE_TRANSITIONS:
                domain = migration.AUDITED_RULE_TRANSITIONS[row.get("id")][0]
            self.before.append({"xmlid": row.get("id"), "domain_force": domain})

    def test_exact_ten_domains_are_updated_from_canonical_xml(self):
        plan = migration.rule_domain_plan(self.before, self.xml)
        self.assertEqual({row["xmlid"] for row in plan}, set(migration.RULE_XMLIDS))
        for row in plan:
            self.assertNotIn("owner_user_id", row["target_domain"])
            self.assertNotIn("default_team_id", row["target_domain"])

    def test_custom_existing_domain_stops_migration(self):
        self.before[1]["domain_force"] = "[(1, '=', 1)]"
        with self.assertRaisesRegex(RuntimeError, "Customized"):
            migration.rule_domain_plan(self.before, self.xml)

    def test_custom_team_domain_is_not_accepted_as_a_rename(self):
        self.before[0]["domain_force"] = "[('company_id', 'in', company_ids)]"
        with self.assertRaisesRegex(RuntimeError, "Customized"):
            migration.rule_domain_plan(self.before, self.xml)

    def test_duplicate_xml_rule_is_rejected(self):
        tree = migration.ElementTree.parse(self.xml)
        matching = next(
            row
            for row in tree.getroot().findall(".//record[@model='ir.rule']")
            if row.get("id") in migration.RULE_XMLIDS
        )
        tree.getroot().append(copy.deepcopy(matching))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "security.xml"
            tree.write(path)
            with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
                migration.rule_domain_plan(self.before, path)


if __name__ == "__main__":
    unittest.main()
