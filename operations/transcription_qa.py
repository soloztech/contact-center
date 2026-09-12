#!/usr/bin/env python3
"""Run transcription and media/retention regression tests in a fresh LAB database.

Reuses the approved infra-ai-ops native runner for archive validation, execution,
log sanitization and result parsing. Only ``run`` opens SSH. No backup, database
copy, active-source modification or service restart is performed. The isolated
database and staged source are retained for subsequent QUnit verification.
"""

import argparse
import importlib.util
import json
import re
import shlex
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

INFRA_ROOT = Path("/home/lucaszotelli/infra-ai-ops")
REPOSITORY = Path(__file__).resolve().parents[1]
NATIVE_RUNNER = (
    INFRA_ROOT / "odoo16/scripts/odoo16_contact_center_direct_start_isolated_test.py"
)
MODULES = ("contact_center_base", "contact_center_ui")
TEST_CLASSES = {
    "contact_center_base/tests/test_transcription.py": "TestAudioTranscription",
    "contact_center_base/tests/test_media_security.py": "TestContactCenterMediaSecurity",
    "contact_center_base/tests/test_retention.py": "TestHistoryRetention",
}
DATABASE_PATTERN = r"cc_transcription_qa_[0-9]{8}_[0-9]{6}_[0-9a-f]{10}"
STAGE_PREFIX = "cc-transcription-qa"
QUNIT_FILTER = "contact_center_ui transcription"
QUNIT_EXPECTED_TESTS = 9


def native_library():
    spec = importlib.util.spec_from_file_location(
        "transcription_native_qa", NATIVE_RUNNER
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("The approved native LAB runner is unavailable")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    native.REPOSITORY = REPOSITORY
    native.MODULES = MODULES
    native.TEST_CLASSES = TEST_CLASSES
    native.TEST_TAGS = ",".join(
        f"/{path.split('/')[0]}:{name}" for path, name in TEST_CLASSES.items()
    )
    return native


def validate_database(database):
    if not re.fullmatch(DATABASE_PATTERN, database):
        raise ValueError("Only a fresh transcription QA database is allowed")
    return database


def active_services_state(remote, native):
    result = {"odoo16": native.active_service_state(remote)}
    rows = (
        remote.run(
            "docker inspect --format '{{.Id}}|{{.State.StartedAt}}|"
            "{{.RestartCount}}|{{.State.Running}}|{{.State.Pid}}' odoo16_dbmanager\n"
            "docker exec odoo16_dbmanager sha256sum /etc/odoo/odoo.conf",
            timeout=30,
        )
        .strip()
        .splitlines()
    )
    if len(rows) != 2:
        raise ValueError("Unexpected LAB dbmanager fingerprint")
    parts = rows[0].split("|")
    config_digest = rows[1].split()[0]
    if (
        len(parts) != 5
        or parts[3] != "true"
        or not re.fullmatch(r"[0-9a-f]{64}", parts[0])
        or not re.fullmatch(r"[0-9a-f]{64}", config_digest)
    ):
        raise ValueError("The LAB dbmanager is not running as expected")
    result["odoo16_dbmanager"] = {
        "container_id": parts[0],
        "started_at": parts[1],
        "restart_count": int(parts[2]),
        "running": True,
        "config_sha256": config_digest,
        "main_pid": int(parts[4]),
    }
    result["odoo16"]["main_pid"] = int(
        remote.run(
            "docker inspect --format '{{.State.Pid}}' odoo16", timeout=30
        ).strip()
    )
    return result


def prepare_remote(remote, native, archive, source, database, progress):
    validate_database(database)
    quoted = shlex.quote
    container_dir = native.validate_temp_path(
        remote.run(
            "docker exec odoo16 sh -lc "
            + quoted(f"umask 077; mktemp -d /tmp/{STAGE_PREFIX}.XXXXXXXX"),
            timeout=30,
        ),
        STAGE_PREFIX,
    )
    progress["container_dir"] = container_dir
    host_dir = native.validate_temp_path(
        remote.run(
            f"umask 077\nmktemp -d /tmp/{STAGE_PREFIX}-upload.XXXXXXXX", timeout=30
        ),
        STAGE_PREFIX + "-upload",
    )
    progress["host_dir"] = host_dir
    with tempfile.TemporaryDirectory(prefix="cc-transcription-source-") as local_dir:
        archive_path = Path(local_dir) / "source.tar"
        archive_path.write_bytes(archive)
        archive_path.chmod(0o600)
        remote.upload(archive_path, host_dir + "/source.tar")
    stage = f"""set -euo pipefail
test "$(hostname)" = servidor05
docker cp {quoted(host_dir + '/source.tar')} odoo16:{quoted(container_dir + '/source.tar')}
docker exec --user 0 odoo16 chown 1000:1000 {quoted(container_dir + '/source.tar')}
docker exec odoo16 sha256sum {quoted(container_dir + '/source.tar')}
docker exec odoo16 tar -xf {quoted(container_dir + '/source.tar')} -C {quoted(container_dir)}
"""
    staged = remote.run(stage, timeout=60).strip()
    if not staged.startswith(source["sha256"] + " "):
        raise ValueError("The remote archive hash changed")
    paths = ",".join((container_dir, *native.NATIVE_ADDONS))
    # Ask the Odoo maintenance connection only for database names. The main
    # database is never opened or copied into this new test database.
    program = f"""import json, sys
sys.path.insert(0, '/opt/odoo')
import phonenumbers, requests, odoo
from odoo.service import db
odoo.tools.config.parse_config(['-c', {native.ODOO_CONFIG!r}, '-d', {database!r},
    '--db-filter', {('^' + database + '$')!r}, '--addons-path', {paths!r},
    '--no-http', '--workers=0', '--max-cron-threads=0', '--load=base,web'])
if {database!r} in db.list_dbs(force=True):
    raise RuntimeError('The disposable database already exists')
print(json.dumps({{'database_absent': True, 'phonenumbers_version': phonenumbers.__version__,
    'requests_version': requests.__version__}}))
"""
    command = (
        "docker exec odoo16 sh -lc 'command -v timeout >/dev/null'\n"
        + "docker exec odoo16 "
        + quoted(native.ODOO_PYTHON)
        + " -c "
        + quoted(program)
    )
    preflight = json.loads(remote.run(command, timeout=60).strip().splitlines()[-1])
    if preflight.get("database_absent") is not True:
        raise ValueError("The isolated database absence check did not pass")
    return {
        "container_dir": container_dir,
        "host_dir": host_dir,
        "addons_path": paths,
        "preflight": preflight,
    }


def native_command(native, database, staged):
    validate_database(database)
    command = native.native_command(database, staged)
    if command.count("--no-http") != 1:
        raise ValueError("The inherited native command changed its HTTP guard")
    return command.replace(
        "--no-http", "--no-http --http-interface=127.0.0.1 --http-port=0", 1
    )


def run(args):
    native = native_library()
    archive, source = native.pinned_archive(args.commit)
    evidence = native.prepare_evidence(args.output_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    database = validate_database(
        "cc_transcription_qa_" + stamp + "_" + uuid.uuid4().hex[:10]
    )
    result = {
        "started_at": native.utc_now(),
        "commit": args.commit,
        "database": database,
        "source": source,
        "runner_sha256": native.digest(Path(__file__).read_bytes()),
        "backup_required": False,
        "backup_reason": "User explicitly requested no backup; fresh isolated test DB.",
        "main_database_opened": False,
        "active_source_changed": False,
        "services_restarted": False,
        "retained_for_review": True,
        "production_accessed": False,
        "provider_messages_sent": False,
        "passed": False,
        "limits": {
            "no_http": True,
            "workers": 0,
            "max_cron_threads": 0,
            "queue_channels": "root:0",
            "timeout_seconds": native.TEST_TIMEOUT_SECONDS,
        },
        "qunit": {"filter": QUNIT_FILTER, "expected_tests": QUNIT_EXPECTED_TESTS},
    }
    native.write_json(evidence / "plan.json", result)
    if args.command == "plan":
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    remote, before = None, None
    started = time.monotonic()
    try:
        remote = native.remote_helper().Remote()
        before = active_services_state(remote, native)
        native.write_json(evidence / "before.json", before)
        result["staged"] = {}
        staged = prepare_remote(
            remote, native, archive, source, database, result["staged"]
        )
        result["staged"] = staged
        native.write_json(evidence / "started.json", result)
        print(
            json.dumps(
                {"database": database, "commit": args.commit, "evidence": str(evidence)}
            ),
            flush=True,
        )
        output = remote.run(
            native_command(native, database, staged),
            timeout=native.TEST_TIMEOUT_SECONDS + 60,
        )
        native.write_private(
            evidence / "runner-output.log", native.sanitize_log(output)
        )
        logs = {}
        for name in ("tests.log", "runner.stdout"):
            path = staged["container_dir"] + "/" + name
            logs[name] = remote.run(
                "docker exec odoo16 sh -lc "
                + shlex.quote(
                    f"if [ -f {shlex.quote(path)} ]; then cat {shlex.quote(path)}; fi"
                ),
                timeout=60,
            )
            native.write_private(
                evidence / (name + ".sanitized.log"), native.sanitize_log(logs[name])
            )
        result["test_result"] = native.parse_test_result(
            logs["tests.log"], output, source
        )
        result["passed"] = result["test_result"]["passed"]
    except Exception as error:
        result["error_type"] = type(error).__name__
        result["passed"] = False
    finally:
        if remote is not None:
            try:
                after = active_services_state(remote, native)
                native.write_json(evidence / "after.json", after)
                result["active_services_preserved"] = (
                    before is not None and before == after
                )
                result["passed"] = (
                    result["passed"] and result["active_services_preserved"]
                )
            except Exception as error:
                result["after_error_type"] = type(error).__name__
                result["passed"] = False
            remote.close()
        result["finished_at"] = native.utc_now()
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        native.write_json(evidence / "summary.json", result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run"))
    parser.add_argument(
        "--commit", required=True, help="Immutable 40-character commit SHA"
    )
    parser.add_argument(
        "--output-dir", required=True, type=Path, help="New scans/raw folder"
    )
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
