"""Exercise the real 1.0 -> 1.1 Odoo upgrade in a disposable GitHub CI database.

The old checkout must be outside the candidate workspace before oca_install_addons.
Every Odoo process selects and verifies its addon source explicitly; installed
editable packages must not accidentally substitute candidate models for old ones.
No business rows are written through SQL and no HTTP or cron worker is started.
"""

import argparse
import ast
import configparser
import datetime
import json
import logging
import os
import runpy
import subprocess
import sys
import time
import uuid
from pathlib import Path

OLD_COMMIT = "33e662b1d95ba34a879c5f652719911c8902969f"
INSTALLED_COMMIT = "9660986fb530c07de7fd3c44b1007b6a3a12b56f"
MODULES = (
    "contact_center_base",
    "contact_center_ui",
    "contact_center_crm",
    "contact_center_kanban",
    "contact_center_wuzapi",
    "contact_center_meta",
)
GUARD_MESSAGE = "Prepare the 16.0.1.1.0 inbox-access migration with the old registry"
_logger = logging.getLogger(__name__)

# Odoo's namespace may already contain pip-editable addons ahead of addons_path.
# Prefer the selected checkout before the native CLI initializes the registry.
LAUNCHER = """
import sys
from pathlib import Path
import odoo
import odoo.addons
root = str(Path(sys.argv.pop(1)).resolve())
odoo.addons.__path__ = [root] + [path for path in odoo.addons.__path__ if path != root]
from odoo.modules.module import get_module_path
for name in %r:
    assert Path(get_module_path(name)).resolve() == Path(root, name), name
sys.argv[0] = 'odoo'
import odoo.cli
odoo.cli.main()
""" % (
    MODULES,
)


def _write(path, value):
    Path(path).write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
    )


def _read(path):
    return json.loads(Path(path).read_text())


def _helper(candidate):
    return runpy.run_path(
        str(candidate / "contact_center_base/migrations/16.0.1.1.0/account_access.py")
    )


def _fixture_evidence(env, fixture):
    """Preserve message/activity content as hashes and exact native membership."""
    helper = _helper(Path(fixture["candidate_root"]))
    channels = env["mail.channel"].browse(fixture["channel_ids"])
    fields_by_model = {
        "mail.message": (
            fixture["message_ids"],
            ["model", "res_id", "body", "author_id", "author_guest_id", "date"],
        ),
        "mail.activity": (
            fixture["activity_ids"],
            ["res_model", "res_id", "user_id", "date_deadline", "summary", "note"],
        ),
        "contact.center.case": (
            fixture["case_ids"],
            ["channel_id", "team_id", "responsible_user_id", "stage_id", "pipeline_id"],
        ),
    }
    return {
        "content": {
            model: helper["digest"](env[model].browse(ids).read(fields))
            for model, (ids, fields) in fields_by_model.items()
        },
        "members": {
            str(channel.id): sorted(
                [member.id, member.partner_id.id or 0, member.guest_id.id or 0]
                for member in channel.channel_member_ids
            )
            for channel in channels
        },
        "responsible": {
            str(channel.id): channel.contact_center_responsible_id.id
            for channel in channels
        },
    }


def _seed_old(env, candidate):
    from odoo import fields

    group = env.ref("contact_center_base.group_contact_center_agent")
    users = (
        env["res.users"]
        .with_context(no_reset_password=True)
        .create(
            [
                {
                    "name": "Synthetic migration agent %s" % index,
                    "login": "cc-upgrade-agent-%s" % index,
                    "company_id": env.company.id,
                    "company_ids": [(6, 0, env.company.ids)],
                    "groups_id": [(6, 0, group.ids)],
                }
                for index in range(3)
            ]
        )
    )
    teams = env["contact.center.team"].create(
        [
            {
                "name": "Synthetic migration team %s" % index,
                "company_id": env.company.id,
                "agent_ids": [(6, 0, user.ids)],
            }
            for index, user in enumerate(users[1:])
        ]
    )
    fixture = {
        "candidate_root": str(candidate),
        "user_ids": users.ids,
        "team_ids": teams.ids,
        "account_ids": [],
        "channel_ids": [],
        "message_ids": [],
        "activity_ids": [],
        "case_ids": [],
    }
    for name, owner, team, responsible in (
        ("owner only", users[0], teams.browse(), users[0]),
        ("team only", users.browse(), teams[0], users[1]),
        ("owner and team", users[0], teams[0], users[0]),
    ):
        account = env["contact.center.account"].create(
            {
                "name": "Synthetic migration inbox " + name,
                "company_id": env.company.id,
                "platform": "whatsapp",
                "owner_user_id": owner.id or False,
                "default_team_id": team.id or False,
                "auto_assignment_user_id": responsible.id,
            }
        )
        guest = env["mail.guest"].create({"name": "Synthetic customer " + name})
        identity = env["contact.center.identity"].create(
            {
                "name": guest.name,
                "company_id": env.company.id,
                "mail_guest_id": guest.id,
            }
        )
        channel = env["mail.channel"]._contact_center_create_channel(
            account=account,
            identity=identity,
            team=team,
            responsible=responsible,
            guest_ids=guest.ids,
        )
        env["contact.center.channel.binding"].create(
            {
                "account_id": account.id,
                "channel_id": channel.id,
                "identity_id": identity.id,
                "conversation_type": "direct",
                "conversation_ref": "synthetic-upgrade-" + uuid.uuid4().hex,
            }
        )
        message = channel._contact_center_post(
            origin="inbound",
            body="Synthetic migration message",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_guest_id=guest.id,
            partner_ids=[],
        )
        env["contact.center.ui.api"].with_user(responsible).schedule_followup(
            channel.id,
            {
                "client_request_id": str(uuid.uuid4()),
                "user_id": responsible.id,
                "date_deadline": fields.Date.to_string(
                    fields.Date.today() + datetime.timedelta(days=1)
                ),
                "summary": "Synthetic follow-up",
                "note": "Retain this activity through the native upgrade.",
            },
        )
        activities = env["mail.activity"].search(
            [("res_model", "=", "mail.channel"), ("res_id", "=", channel.id)]
        )
        cases = channel.contact_center_case_ids.filtered("is_default")
        assert len(activities) == len(cases) == 1
        fixture["account_ids"].append(account.id)
        fixture["channel_ids"].append(channel.id)
        fixture["message_ids"].append(message.id)
        fixture["activity_ids"].extend(activities.ids)
        fixture["case_ids"].extend(cases.ids)
    assert not env["contact.center.provider.connection"].search_count([])
    env.flush_all()
    return fixture


def registry_phase(env, phase, source, candidate, output):
    """Invoked inside a native Odoo shell; each phase owns its commit/rollback."""
    from odoo.modules.module import get_module_path
    from odoo.tools import config

    source, candidate, output = map(Path, (source, candidate, output))
    helper = _helper(candidate)
    for name in MODULES:
        assert Path(get_module_path(name)).resolve() == source / name, name
    assert not config["http_enable"] and not config["test_enable"]
    assert config["workers"] == config["max_cron_threads"] == 0
    modules = env["ir.module.module"].search([("name", "in", list(MODULES))])
    expected = ast.literal_eval(
        (source / "contact_center_base/__manifest__.py").read_text()
    )["version"]
    assert len(modules) == len(MODULES)
    versions = {row.name: (row.state, row.latest_version) for row in modules}
    assert all(
        row.state == "installed" and row.latest_version == expected for row in modules
    ), versions
    params = env["ir.config_parameter"].sudo()
    if phase == "seed_old":
        fixture = _seed_old(env, candidate)
        _write(output / "fixture.json", fixture)
        _write(output / "before.json", helper["snapshot"](env))
        _write(output / "fixture-before.json", _fixture_evidence(env, fixture))
        env.cr.commit()
        return

    fixture = _read(output / "fixture.json")
    before = _read(output / "before.json")
    assert _fixture_evidence(env, fixture) == _read(output / "fixture-before.json")
    if phase == "prepare_old":
        assert helper["digest"](helper["snapshot"](env)) == helper["digest"](before)
        assert not params.get_param(helper["STATE_KEY"])
        helper["prepare"](env, before)
        assert params.get_param(helper["STATE_KEY"]) == "prepared"
        env.cr.rollback()
        env.invalidate_all()
        params.clear_caches()
        assert not params.get_param(helper["STATE_KEY"])
        assert not params.get_param(helper["SNAPSHOT_KEY"])
        assert helper["digest"](helper["snapshot"](env)) == helper["digest"](before)
        _write(output / "prepare-dry.json", {"rollback_verified": True})
        helper["prepare"](env, before)
        env.cr.commit()
        _write(output / "prepare-applied.json", {"state": "prepared"})
        return

    assert phase == "verify_new"
    assert params.get_param(helper["STATE_KEY"]) == "done"
    result = helper["validate_transition"](env, before)
    plan = helper["rule_domain_plan"](
        before["rules"],
        candidate / "contact_center_base/security/contact_center_security.xml",
    )
    for row in plan:
        actual = env.ref("contact_center_base." + row["xmlid"])
        assert helper["_domain_ast"](actual.domain_force) == helper["_domain_ast"](
            row["target_domain"]
        )
    assert helper["finalize"](env)["already_done"]
    accounts = env["contact.center.account"].browse(fixture["account_ids"])
    assert all(field not in accounts._fields for field in helper["OLD_FIELDS"])
    assert all(
        accounts._fields[field].type == "many2many" for field in helper["NEW_FIELDS"]
    )
    business_models = ("mail.mail", "contact.center.outbox.command", "queue.job")
    side_effect_counts = {
        model: env[model].search_count([]) for model in business_models
    }

    users = env["res.users"].browse(fixture["user_ids"])
    teams = env["contact.center.team"].browse(fixture["team_ids"])
    account = accounts[2]
    channel = env["mail.channel"].browse(fixture["channel_ids"][2])
    account.write(
        {
            "access_user_ids": [(6, 0, users[:2].ids)],
            "access_team_ids": [(6, 0, teams.ids)],
        }
    )
    assert set(channel.channel_member_ids.partner_id.ids) == set(users.partner_id.ids)
    item = (
        env["contact.center.ui.api"]
        .with_user(users[2])
        .get_conversation(channel.id)["item"]
    )
    # Both verification phases use the same shell entry point. Select the
    # expected contract from the source already verified by get_module_path,
    # never by whether the response happens to contain the new capability.
    restricted_access_projection = source.resolve() == candidate.resolve()
    if restricted_access_projection:
        assert item["capabilities"]["view_inbox_access"] is False
        assert item["access_users"] == item["access_teams"] == []
        assert item["responsible"]["id"] == users[0].id
        for group_name in ("supervisor", "admin"):
            group = env.ref("contact_center_base.group_contact_center_" + group_name)
            users[0].write({"groups_id": [(4, group.id)]})
            manager_item = (
                env["contact.center.ui.api"]
                .with_user(users[0])
                .get_conversation(channel.id)["item"]
            )
            assert manager_item["capabilities"]["view_inbox_access"] is True
            assert {row["id"] for row in manager_item["access_users"]} == set(
                users[:2].ids
            )
            assert {row["id"] for row in manager_item["access_teams"]} == set(teams.ids)
            assert manager_item["responsible"]["id"] == users[0].id
    else:
        assert "view_inbox_access" not in item["capabilities"]
        assert {row["id"] for row in item["access_users"]} == set(users[:2].ids)
        assert {row["id"] for row in item["access_teams"]} == set(teams.ids)
    account.write({"access_user_ids": [(3, users[1].id)]})
    assert users[1].partner_id in channel.channel_member_ids.partner_id
    account.write({"access_team_ids": [(3, teams[0].id)]})
    assert users[1].partner_id not in channel.channel_member_ids.partner_id
    assert users[2].partner_id in channel.channel_member_ids.partner_id
    assert channel.contact_center_responsible_id == users[0]
    assert {
        model: env[model].search_count([]) for model in business_models
    } == side_effect_counts
    # The rehearsal records the proof, then rolls the extra grants back. The
    # upgraded database still represents an exact migration of the old fixtures.
    env.cr.rollback()
    env.invalidate_all()
    env["res.users"].clear_caches()
    assert _fixture_evidence(env, fixture) == _read(output / "fixture-before.json")
    assert not users[0].has_group("contact_center_base.group_contact_center_supervisor")
    assert {
        model: env[model].search_count([]) for model in business_models
    } == side_effect_counts
    _write(
        output
        / ("after.json" if restricted_access_projection else "after-installed.json"),
        dict(
            result,
            state="done",
            rules_verified=len(plan),
            idempotent=True,
            multiple_grants_verified=True,
            grants_rollback_verified=True,
            access_visibility_contract=(
                "role_restricted"
                if restricted_access_projection
                else "legacy_operator_projection"
            ),
            access_visibility_roles_verified=(
                ["operator", "supervisor", "administrator"]
                if restricted_access_projection
                else ["operator"]
            ),
        ),
    )


def _run_odoo(source, config_path, mode, log_path, shell_body=None):
    configuration = configparser.ConfigParser(interpolation=None)
    configuration.read(config_path)
    addon_paths = [str(source)] + configuration.get(
        "options", "addons_path", fallback=""
    ).split(",")
    command = [sys.executable, "-c", LAUNCHER, str(source)]
    if mode == "shell":
        command.append("shell")
    command.extend(
        [
            "--config",
            str(config_path),
            "--addons-path=" + ",".join(path for path in addon_paths if path),
            "--load=base,web",
            "--no-http",
        ]
    )
    if mode != "shell":
        command.extend(
            ["--stop-after-init", "--without-demo=all", mode, ",".join(MODULES)]
        )
    with Path(log_path).open("w") as log:
        result = subprocess.run(
            command,
            input=shell_body,
            text=True,
            cwd=source,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=1200,
            check=False,
        )
    return result.returncode


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("PGHOST") != "postgres"
    ):
        parser.error("This rehearsal only runs against the GitHub CI postgres service.")
    old, candidate, output = (
        path.resolve() for path in (args.old_root, args.candidate_root, args.output)
    )
    old_commit = subprocess.check_output(
        ["git", "-C", str(old), "rev-parse", "HEAD"], text=True
    ).strip()
    candidate_commit = subprocess.check_output(
        ["git", "-C", str(candidate), "rev-parse", "HEAD"], text=True
    ).strip()
    installed = args.installed_root.resolve()
    installed_commit = subprocess.check_output(
        ["git", "-C", str(installed), "rev-parse", "HEAD"], text=True
    ).strip()
    if installed_commit != INSTALLED_COMMIT or any(
        installed == root or installed in root.parents or root in installed.parents
        for root in (old, candidate)
    ):
        parser.error("The installed 1.1 checkout must be pinned and isolated.")
    if (
        old_commit != OLD_COMMIT
        or old == candidate
        or candidate in old.parents
        or old in candidate.parents
    ):
        parser.error(
            "The pinned old checkout must be separate from the candidate workspace."
        )
    output.mkdir(parents=True, exist_ok=True)
    runtime = output / "runtime"
    runtime.mkdir(exist_ok=True)
    database = "cc_access_upgrade_" + uuid.uuid4().hex[:12]
    configuration = configparser.ConfigParser(interpolation=None)
    configuration.read(os.environ.get("ODOO_RC", "/dev/null"))
    if not configuration.has_section("options"):
        configuration.add_section("options")
    options = configuration["options"]
    options.update(
        db_name=database,
        workers="0",
        max_cron_threads="0",
        http_enable="False",
        test_enable="False",
        test_tags="",
        server_wide_modules="base,web",
        logfile="False",
        data_dir=str(runtime / "data"),
    )
    for option, variable in (
        ("db_host", "PGHOST"),
        ("db_user", "PGUSER"),
        ("db_password", "PGPASSWORD"),
        ("db_port", "PGPORT"),
    ):
        if os.environ.get(variable):
            options[option] = os.environ[variable]
    config_path = runtime / "odoo.conf"
    with config_path.open("w") as stream:
        configuration.write(stream)
    config_path.chmod(0o600)
    script = str(Path(__file__).resolve())
    summary = {
        "state": "running",
        "old_commit": old_commit,
        "candidate_commit": candidate_commit,
        "installed_commit": installed_commit,
        "modules": list(MODULES),
        "database": database,
        "http_enabled": False,
        "cron_workers": 0,
        "provider_connections": 0,
        "timings_seconds": {},
    }
    created = False
    error = None

    def phase(name, source, mode="shell", expected_failure=False, shell_phase=None):
        _logger.info("Native access upgrade rehearsal: %s", name)
        body = None
        if mode == "shell":
            body = (
                "import runpy\nrunpy.run_path(%r)['registry_phase'](env, %r, %r, %r, %r)\n"
                % (
                    script,
                    shell_phase or name,
                    str(source),
                    str(candidate),
                    str(output),
                )
            )
        log_path = output / (name + ".log")
        started = time.monotonic()
        try:
            code = _run_odoo(source, config_path, mode, log_path, body)
        finally:
            summary["timings_seconds"][name] = round(time.monotonic() - started, 3)
        if expected_failure:
            if code == 0 or GUARD_MESSAGE not in log_path.read_text():
                raise RuntimeError(
                    "Unprepared native upgrade did not fail at its guard"
                )
            summary["unprepared_upgrade_rejected"] = True
        elif code:
            raise RuntimeError("Odoo phase %s failed; see %s" % (name, log_path.name))

    try:
        subprocess.run(["createdb", "--template=template0", database], check=True)
        created = True
        phase("install_old", old, "-i")
        phase("seed_old", old)
        phase("reject_unprepared", candidate, "-u", expected_failure=True)
        phase("prepare_old", old)
        phase("upgrade_installed", installed, "-u")
        phase("verify_installed", installed, shell_phase="verify_new")
        phase("upgrade_new", candidate, "-u")
        phase("verify_new", candidate)
        summary.update(
            state="passed",
            evidence=_read(output / "after.json"),
            installed_evidence=_read(output / "after-installed.json"),
            prepare_rollback=_read(output / "prepare-dry.json")["rollback_verified"],
        )
    except Exception as caught:
        error = caught
        summary.update(state="failed", error=str(caught))
    finally:
        if created:
            result = subprocess.run(["dropdb", "--if-exists", database], check=False)
            summary["database_removed"] = result.returncode == 0
            if result.returncode and error is None:
                error = RuntimeError("Could not remove the disposable CI database")
                summary.update(state="failed", error=str(error))
        _write(output / "summary.json", summary)
    if error:
        raise RuntimeError(summary["error"]) from error
    _logger.info("Native access upgrade rehearsal passed; disposable database removed.")


if __name__ == "__main__":
    main()
