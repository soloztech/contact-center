#!/usr/bin/env python3
"""Plan/run native ad-preview QA using cached Odoo/PostgreSQL and fresh local data.

No SSH, Docker, backup, existing database or runtime mutation. Source modules
come from two full immutable Git commits. Only run creates a new cluster and
private Unix socket; it stops only its own child processes and retains evidence.
Python dependencies may be supplied from an already prepared task-owned target.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


INFRA = Path("/home/lucaszotelli/infra-ai-ops")
REPOSITORY = Path(__file__).resolve().parents[1]
MARKETING = REPOSITORY.parent / "marketing-center-ad-origin"
RUNTIME = INFRA / "scans/raw/20260905-centers-greenfield-audit/runtime"
NATIVE = INFRA / "odoo16/scripts/odoo16_contact_center_direct_start_isolated_test.py"
CC_MODULES = (
    "contact_center_base",
    "contact_center_ui",
    "contact_center_wuzapi",
    "contact_center_meta",
)
MARKETING_MODULES = (
    "meta_api_base",
    "meta_webhook_base",
    "marketing_center_base",
    "marketing_center_contact_center",
    "marketing_center_meta",
)
CC_TEST_FILES = (
    "contact_center_base/tests/test_ad_origin_preview.py",
    "contact_center_base/tests/test_attribution.py",
    "contact_center_base/tests/test_transcription.py",
    "contact_center_base/tests/test_media_security.py",
    "contact_center_base/tests/test_retention.py",
    "contact_center_wuzapi/tests/test_ad_origin.py",
    "contact_center_meta/tests/test_ad_origin.py",
)
MARKETING_TEST_FILES = (
    "marketing_center_contact_center/tests/test_ad_preview.py",
    "marketing_center_meta/tests/test_ad_preview.py",
)
TIMEOUT_SECONDS = 1200


def digest(value):
    return hashlib.sha256(value).hexdigest()


def native_library():
    spec = importlib.util.spec_from_file_location("ad_preview_native_helpers", NATIVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pinned_source(repository, commit, modules, test_files):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("A full immutable Git commit is required")
    actual = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "--verify", commit + "^{commit}"],
        text=True,
    ).strip()
    if actual != commit:
        raise ValueError("Git did not resolve the exact requested commit")
    raw = subprocess.check_output(
        ["git", "-C", str(repository), "archive", "--format=tar", commit, *modules]
    )
    files, versions, dependencies, classes = {}, {}, {}, {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] not in modules
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("Unsafe or unexpected archive member")
            if member.isfile():
                if member.name in files:
                    raise ValueError("Duplicate archive member")
                files[member.name] = archive.extractfile(member).read()
    for module in modules:
        manifest = ast.literal_eval(files[module + "/__manifest__.py"].decode())
        versions[module] = manifest["version"]
        dependencies[module] = manifest.get("depends", [])
    for filename in test_files:
        found = False
        for node in ast.parse(files[filename], filename=filename).body:
            if not isinstance(node, ast.ClassDef) or not node.name.startswith("Test"):
                continue
            methods = sorted(
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name.startswith("test_")
            )
            if methods:
                if node.name in classes:
                    raise ValueError("Duplicate selected test class")
                classes[node.name] = {"file": filename, "methods": methods}
                found = True
        if not found:
            raise ValueError("No test methods found in " + filename)
    return files, {
        "commit": commit,
        "archive_sha256": digest(raw),
        "versions": versions,
        "dependencies": dependencies,
        "classes": classes,
        "file_sha256": {name: digest(value) for name, value in files.items()},
    }


def environment(deps_dir):
    paths = [RUNTIME / "native/usr/lib/python3/dist-packages"]
    if deps_dir:
        if (
            not deps_dir.is_dir()
            or INFRA / "scans/raw" not in deps_dir.resolve().parents
        ):
            raise ValueError("Dependency target must already exist under scans/raw")
        paths.insert(0, deps_dir.resolve())
    # Do not inherit a user Odoo configuration or Python import override.
    return {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.pathsep.join(map(str, paths)),
        "LD_LIBRARY_PATH": os.pathsep.join(
            str(RUNTIME / path)
            for path in (
                "native/usr/lib/aarch64-linux-gnu",
                "postgres/usr/lib/aarch64-linux-gnu",
            )
        ),
        "ODOO_QUEUE_JOB_CHANNELS": "root:0",
    }


def preflight(env):
    memory = {
        line.split(":")[0]: int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith(("MemAvailable:", "SwapFree:"))
    }
    program = """import importlib,json,sys
result={'python':sys.version,'imports':{}}
for name in ['psycopg2','lxml','gevent','requests','phonenumbers','PIL','sass','werkzeug','babel']:
    try:
        module=importlib.import_module(name)
        result['imports'][name]={'ok':True,'version':getattr(module,'__version__',None)}
    except ImportError:
        result['imports'][name]={'ok':False}
print(json.dumps(result))
"""
    versions = {}
    for binary in ("postgres", "initdb"):
        versions[binary] = subprocess.check_output(
            [str(RUNTIME / "postgres/usr/lib/postgresql/16/bin" / binary), "--version"],
            env=env,
            text=True,
        ).strip()
    imports = json.loads(
        subprocess.check_output(
            [str(RUNTIME / "venv/bin/python"), "-c", program],
            env=env,
            text=True,
        )
    )
    return {
        "memory_kib": memory,
        "disk_free_bytes": shutil.disk_usage(INFRA).free,
        "runtime": versions,
        **imports,
        "missing_dependencies": sorted(
            name for name, item in imports["imports"].items() if not item["ok"]
        ),
        "unshare_available": shutil.which("unshare") is not None,
        "runtime_sha256": {
            name: digest((RUNTIME / name).read_bytes())
            for name in (
                "OCB/odoo-bin",
                "OCB/odoo/release.py",
                "queue/queue_job/__manifest__.py",
                "OCB/addons/web/static/lib/owl/owl.js",
            )
        },
    }


def own_process(command, env, log_path, timeout):
    with log_path.open("x", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=timeout)
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise


def stage(files, directory):
    directory.mkdir(mode=0o700)
    for filename, content in files.items():
        path = directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600)


def execute(args, native, evidence, result, trees, env):
    checks = result["preflight"]
    if checks["missing_dependencies"]:
        raise RuntimeError(
            "Missing Python dependencies: " + ", ".join(checks["missing_dependencies"])
        )
    if checks["disk_free_bytes"] < 2 * 1024**3:
        raise RuntimeError("Local QA requires at least 2 GiB free disk")
    if checks["memory_kib"]["MemAvailable"] < 1024**2:
        raise RuntimeError("Local native QA requires at least 1 GiB available RAM")
    if not checks["unshare_available"]:
        raise RuntimeError("Network namespace isolation is unavailable")
    subprocess.run(
        ["unshare", "--user", "--map-current-user", "--keep-caps", "--net", "true"],
        check=True,
        timeout=10,
    )
    local = evidence / "runtime"
    local.mkdir(mode=0o700)
    for name, files in trees.items():
        stage(files, local / name)
    data = local / "odoo-data"
    data.mkdir(mode=0o700)
    cluster = local / "pgdata"
    socket = Path(result["socket_path"])
    socket.mkdir(mode=0o700)
    pg_bin = RUNTIME / "postgres/usr/lib/postgresql/16/bin"
    database = result["database"]
    addons = [
        local / "cc",
        local / "marketing",
        RUNTIME / "OCB/addons",
        RUNTIME / "OCB/odoo/addons",
        RUNTIME / "queue",
    ]
    config = local / "odoo.conf"
    native.write_private(
        config,
        "\n".join(
            [
                "[options]",
                "admin_passwd = synthetic-ad-preview-qa-only",
                "db_host = " + str(socket),
                "db_port = 55485",
                "db_user = ad_preview_qa",
                "db_password = False",
                "db_name = " + database,
                "dbfilter = ^" + database + "$",
                "data_dir = " + str(data),
                "addons_path = " + ",".join(map(str, addons)),
                "list_db = False",
                "proxy_mode = False",
                "workers = 0",
                "max_cron_threads = 0",
                "server_wide_modules = base,web",
                "",
                "[queue_job]",
                "channels = root:0",
                "",
            ]
        ),
    )
    init_status = own_process(
        [
            str(pg_bin / "initdb"),
            "-D",
            str(cluster),
            "--username=ad_preview_qa",
            "--auth-local=trust",
            "--auth-host=reject",
            "--encoding=UTF8",
            "--no-locale",
        ],
        env,
        evidence / "initdb.log",
        60,
    )
    if init_status:
        raise RuntimeError("Fresh PostgreSQL cluster initialization failed")
    pg_log = (evidence / "postgres.log").open("x", encoding="utf-8")
    process = None
    try:
        process = subprocess.Popen(
            [
                str(pg_bin / "postgres"),
                "-D",
                str(cluster),
                "-k",
                str(socket),
                "-p",
                "55485",
                "-c",
                "listen_addresses=",
                "-c",
                "shared_buffers=32MB",
                "-c",
                "max_connections=20",
                "-c",
                "unix_socket_permissions=0700",
            ],
            env=env,
            stdout=pg_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        result["postgres_pid"] = process.pid
        for _attempt in range(100):
            if process.poll() is not None:
                raise RuntimeError(
                    "Task PostgreSQL process exited before becoming ready"
                )
            ready = subprocess.run(
                [
                    str(pg_bin / "pg_isready"),
                    "-h",
                    str(socket),
                    "-p",
                    "55485",
                    "-U",
                    "ad_preview_qa",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Fresh PostgreSQL cluster did not become ready")
        command = [
            "unshare",
            "--user",
            "--map-current-user",
            "--keep-caps",
            "--net",
            str(RUNTIME / "venv/bin/python"),
            str(RUNTIME / "OCB/odoo-bin"),
            "-c",
            str(config),
            "-d",
            database,
            "--db-filter=^" + database + "$",
            "--init=" + ",".join(CC_MODULES + MARKETING_MODULES),
            "--without-demo=all",
            "--test-enable",
            "--test-tags=" + result["source"]["test_tags"],
            "--stop-after-init",
            "--no-http",
            "--http-interface=127.0.0.1",
            "--http-port=0",
            "--workers=0",
            "--max-cron-threads=0",
            "--load=base,web",
            "--log-level=test",
            "--logfile=" + str(evidence / "tests.log"),
        ]
        native.write_json(evidence / "started.json", result)
        status = own_process(command, env, evidence / "runner.stdout", TIMEOUT_SECONDS)
        test_log = (
            (evidence / "tests.log").read_text()
            if (evidence / "tests.log").exists()
            else ""
        )
        result["test_result"] = native.parse_test_result(
            test_log,
            "QA_EXIT_STATUS=" + str(status),
            result["source"],
        )
        result["passed"] = result["test_result"]["passed"]
    finally:
        if process is not None and process.poll() is None:
            # SIGINT requests PostgreSQL fast shutdown and targets our Popen PID only.
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=15)
        result["postgres_stopped"] = process is None or process.poll() is not None
        pg_log.close()
        if socket.exists() and not any(socket.iterdir()):
            socket.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run"))
    parser.add_argument("--cc-commit", required=True)
    parser.add_argument("--marketing-commit", required=True)
    parser.add_argument("--marketing-repository", type=Path, default=MARKETING)
    parser.add_argument("--deps-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    native = native_library()
    cc, cc_proof = pinned_source(REPOSITORY, args.cc_commit, CC_MODULES, CC_TEST_FILES)
    marketing, marketing_proof = pinned_source(
        args.marketing_repository,
        args.marketing_commit,
        MARKETING_MODULES,
        MARKETING_TEST_FILES,
    )
    if set(cc_proof["classes"]) & set(marketing_proof["classes"]):
        raise ValueError("Duplicate native test class across repositories")
    classes = {**cc_proof["classes"], **marketing_proof["classes"]}
    env = environment(args.deps_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    identity = uuid.uuid4().hex[:10]
    result = {
        "started_at": native.utc_now(),
        "passed": False,
        "database": "cc_ad_preview_qa_" + stamp + "_" + identity,
        "socket_path": "/tmp/cc-ad-preview-qa-" + identity,
        "runner_sha256": digest(Path(__file__).read_bytes()),
        "source": {
            "cc": cc_proof,
            "marketing": marketing_proof,
            "classes": classes,
            "expected_tests": sum(len(item["methods"]) for item in classes.values()),
            "test_tags": ",".join(
                "/" + item["file"].split("/")[0] + ":" + name
                for name, item in classes.items()
            ),
        },
        "preflight": preflight(env),
        "backup_created": False,
        "existing_databases_opened": False,
        "remote_accessed": False,
        "existing_services_modified": False,
        "retained_for_review": True,
        "limits": {
            "workers": 0,
            "cron_threads": 0,
            "queue_channels": "root:0",
            "no_http": True,
            "pg_shared_buffers": "32MB",
            "pg_max_connections": 20,
            "network_namespace": True,
            "timeout_seconds": TIMEOUT_SECONDS,
        },
    }
    evidence = native.prepare_evidence(args.output_dir)
    native.write_json(evidence / "plan.json", result)
    print(
        json.dumps(
            {
                "command": args.command,
                "evidence": str(evidence),
                "expected_tests": result["source"]["expected_tests"],
                "preflight": result["preflight"],
            }
        ),
        flush=True,
    )
    if args.command == "plan":
        return 0
    started = time.monotonic()
    previous_umask = os.umask(0o077)
    try:
        execute(args, native, evidence, result, {"cc": cc, "marketing": marketing}, env)
    except Exception as error:
        result["error_type"] = type(error).__name__
        result["error"] = str(error)
        result["passed"] = False
    finally:
        os.umask(previous_umask)
        result["finished_at"] = native.utc_now()
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        native.write_json(evidence / "summary.json", result)
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "evidence": str(evidence),
                "test_result": result.get("test_result"),
                "error": result.get("error"),
            }
        ),
        flush=True,
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
