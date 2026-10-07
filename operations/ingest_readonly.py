#!/usr/bin/env python3
"""Reproducible, read-only inbox diagnostics. Never persist message/error text."""
import argparse
import importlib.util
import json
import re
import socket
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

FIELDS = {
    "contact.center.inbox.event": {
        "account_id",
        "company_id",
        "state",
        "attempts",
        "last_error_class",
        "queue_job_uuid",
        "create_date",
        "processed_at",
        "metadata_json",
    },
    "queue.job": {
        "company_id",
        "model_name",
        "method_name",
        "state",
        "retry",
        "exc_name",
        "date_created",
        "eta",
        "uuid",
    },
}
METHODS = {"search_read", "search_count", "read_group"}


def checked_read(rpc, company, model, method, domain, fields=(), groupby=(), **options):
    if model not in FIELDS or method not in METHODS:
        raise ValueError("Read-only model/method allowlist")
    allowed = FIELDS[model]
    if not set(fields) <= allowed or any(
        g.split(":")[0] not in allowed for g in groupby
    ):
        raise ValueError("Field allowlist")
    if any(
        not isinstance(term, (list, tuple))
        or len(term) != 3
        or term[0] not in allowed
        or term[1] not in {"=", "in", ">=", "<"}
        for term in domain
    ):
        raise ValueError("Domain allowlist")
    if not set(options) <= {"limit", "offset", "order", "lazy"}:
        raise ValueError("Option allowlist")
    if "order" in options and options["order"] not in {"id", "date_created"}:
        raise ValueError("Order allowlist")
    scoped = [("company_id", "=", company)] + domain
    kwargs = dict(
        context={"allowed_company_ids": [company], "active_test": False, "tz": "UTC"},
        **options
    )
    args = [scoped]
    if method == "search_read":
        kwargs["fields"] = list(fields)
    if method == "read_group":
        args += [list(fields), list(groupby)]
    return rpc.call(model, method, args, kwargs)


def label(value):
    if value is None or value is False or value == "":
        return "(none)"
    return (
        value
        if isinstance(value, str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,79}", value)
        else "(unknown)"
    )


def collect(rpc, company, start, end):
    event = "contact.center.inbox.event"
    job = "queue.job"
    domain = [("create_date", ">=", start), ("create_date", "<", end)]

    def read(model, method, terms, **kw):
        return checked_read(rpc, company, model, method, terms, **kw)

    total = read(event, "search_count", domain)
    groups = read(
        event,
        "read_group",
        domain,
        fields=["account_id", "state", "last_error_class", "create_date"],
        groupby=["account_id", "state", "last_error_class", "create_date:day"],
        lazy=False,
    )
    states = []
    for r in groups:
        account = r.get("account_id")
        states.append(
            {
                "account_id": account[0] if account else False,
                "state": label(r.get("state")),
                "error_class": label(r.get("last_error_class")),
                "day_utc": ((r.get("__range") or {}).get("create_date:day") or {}).get(
                    "from", r.get("create_date:day")
                ),
                "count": r["__count"],
            }
        )
    pending_domain = [
        ("model_name", "=", event),
        ("method_name", "=", "_job_process"),
        ("state", "in", ["pending", "enqueued", "started", "wait_dependencies"]),
    ]
    pending_total = read(job, "search_count", pending_domain)
    jobs = read(
        job,
        "search_read",
        pending_domain,
        fields=["state", "retry", "exc_name", "uuid"],
        order="date_created",
        limit=500,
    )
    uuids = [r["uuid"] for r in jobs]
    events = (
        read(
            event,
            "search_read",
            [("queue_job_uuid", "in", uuids)],
            fields=[
                "account_id",
                "state",
                "attempts",
                "last_error_class",
                "metadata_json",
            ],
            order="id",
            limit=500,
        )
        if uuids
        else []
    )
    sanitized = []
    for r in events:
        metadata = r.get("metadata_json")
        sanitized.append(
            {
                "account_id": r["account_id"][0] if r.get("account_id") else False,
                "state": label(r["state"]),
                "attempts": r["attempts"],
                "error_class": label(r.get("last_error_class")),
                "event_type": label(metadata.get("event_type"))
                if isinstance(metadata, dict)
                else ("(none)" if not metadata else "(unknown)"),
            }
        )
    return {
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "company_id": company,
        "window_utc": {"from_inclusive": start, "until_exclusive": end},
        "event_denominator": total,
        "event_groups": states,
        "pending_job_denominator": pending_total,
        "pending_job_sample_count": len(jobs),
        "pending_event_sample_count": len(events),
        "pending_job_states": dict(Counter(label(r["state"]) for r in jobs)),
        "job_retry_histogram": dict(Counter(r["retry"] for r in jobs)),
        "job_exception_classes": dict(Counter(label(r.get("exc_name")) for r in jobs)),
        "pending_events": sanitized,
        "limitations": [
            "(none) means no class/value was recorded; (unknown) means a "
            "non-empty value was discarded by sanitization.",
            "Current states of creation cohorts; not historical snapshots.",
            "Retries belong to jobs; durable event attempts can lag "
            "after rolled-back retries.",
            "Pending samples capped at 500; denominators shown separately.",
            "Terminal events do not establish loss of a message or a lead.",
            "No envelope, message, phone, exception text or metadata "
            "beyond event_type persisted.",
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--rpc-helper",
        required=True,
        type=Path,
        help="Trusted local Odoo helper exposing Odoo().call; "
        "credentials remain in that helper.",
    )
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--company", type=int, default=1)
    p.add_argument("--since", default="2026-10-05 22:23:42")
    p.add_argument(
        "--until", default=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    )
    a = p.parse_args()
    if a.company <= 0 or a.output.exists():
        p.error("Positive company and a new output path are required")
    start, end = [datetime.strptime(v, "%Y-%m-%d %H:%M:%S") for v in (a.since, a.until)]
    if end <= start:
        p.error("until must be after since")
    socket.setdefaulttimeout(25)
    spec = importlib.util.spec_from_file_location("trusted_rpc_helper", a.rpc_helper)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    result = collect(helper.Odoo(), a.company, a.since, a.until)
    with a.output.open("x") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")
    # This standalone CLI returns only a sanitized aggregate summary on stdout.
    # pylint: disable-next=print-used
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "event_denominator",
                    "pending_job_denominator",
                    "pending_job_sample_count",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
