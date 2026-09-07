"""Move pre-release follow-ups from optional cases to canonical conversations.

The release coordinator runs prepare with the OLD registry while every worker
is stopped, then upgrades the new addons and calls finalize with that registry.
All writes use ORM and share the caller's transaction. This is a one-time data
handoff, never an addon runtime compatibility layer.
"""

import hashlib
import json

STATE_KEY = "contact_center_base.followup_conversation_extraction_state"
SOURCE_MODULE = "contact_center_kanban"
TARGET_MODULE = "contact_center_base"
MODEL = "contact.center.followup.request"
MOVED_XMLIDS = {
    "view_contact_center_followup_request_tree": "ir.ui.view",
    "view_contact_center_followup_request_form": "ir.ui.view",
    "action_contact_center_followup_requests": "ir.actions.act_window",
    "menu_contact_center_followup_requests": "ir.ui.menu",
    "access_contact_center_followup_request_admin": "ir.model.access",
    "rule_contact_center_followup_request_admin_company": "ir.rule",
}
_CONTEXT_KEYS = {"case_id", "case_name", "channel_id", "channel_name"}


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _content(value):
    return {
        key: item for key, item in (value or {}).items() if key not in _CONTEXT_KEYS
    }


def _schedule_payload(receipt, *, conversation):
    data = receipt.activity_snapshot_json or {}
    payload = {
        "activity_type_id": (data.get("activity_type") or {}).get("id"),
        "date_deadline": data.get("date_deadline"),
        "note": data.get("note") or "",
        "summary": data.get("summary") or "",
        "user_id": (data.get("assigned_to") or {}).get("id"),
    }
    payload["channel_id" if conversation else "case_id"] = (
        receipt.channel_id.id if conversation else receipt.case_id.id
    )
    return payload


def _metadata(env):
    model = env["ir.model"]._get(MODEL)
    fields = env["ir.model.fields"].search(
        [("model", "=", MODEL), ("name", "!=", "case_id")]
    )
    selections = env["ir.model.fields.selection"].search(
        [("field_id", "in", fields.ids)]
    )
    constraints = env["ir.model.constraint"].search(
        [
            ("model", "=", model.id),
            ("name", "!=", "contact_center_followup_request_case_id_fkey"),
        ]
    )
    domains = [
        ("ir.model", model.ids),
        ("ir.model.fields", fields.ids),
        ("ir.model.fields.selection", selections.ids),
        ("ir.model.constraint", constraints.ids),
    ]
    metadata = env["ir.model.data"]
    for name, ids in domains:
        metadata |= env["ir.model.data"].search(
            [
                ("module", "in", [SOURCE_MODULE, TARGET_MODULE]),
                ("model", "=", name),
                ("res_id", "in", ids),
            ]
        )
    metadata |= env["ir.model.data"].search(
        [
            ("module", "in", [SOURCE_MODULE, TARGET_MODULE]),
            ("name", "in", list(MOVED_XMLIDS)),
        ]
    )
    declared = metadata.filtered(lambda row: row.name in MOVED_XMLIDS)
    if len(declared) != len(MOVED_XMLIDS) or set(declared.mapped("name")) != set(
        MOVED_XMLIDS
    ):
        raise RuntimeError("Follow-up XML-ID inventory is incomplete or duplicated")
    if any(row.model != MOVED_XMLIDS[row.name] for row in declared):
        raise RuntimeError("Follow-up XML-ID model mismatch")
    if len(metadata.mapped("name")) != len(set(metadata.mapped("name"))):
        raise RuntimeError("Follow-up source and target ownership conflict")
    return metadata, constraints


def snapshot(env):
    """Return IDs and content digests, without activity text or credentials."""
    receipts = env[MODEL].sudo().search([], order="id")
    channels = (
        env["mail.channel"]
        .sudo()
        .with_context(active_test=False)
        .search(
            [
                ("channel_type", "=", "contact_center"),
            ]
        )
    )
    activities = (
        env["mail.activity"]
        .sudo()
        .search(
            [
                "|",
                ("res_model", "=", "contact.center.case"),
                "&",
                ("res_model", "=", "mail.channel"),
                ("res_id", "in", channels.ids),
            ],
            order="id",
        )
    )
    case_model = env["contact.center.case"].sudo().with_context(active_test=False)
    activity_rows = []
    for activity in activities:
        case = (
            case_model.browse(activity.res_id).exists()
            if activity.res_model == "contact.center.case"
            else case_model
        )
        if activity.res_model == "contact.center.case" and not case:
            raise RuntimeError("A follow-up targets a missing optional case")
        channel = (
            case.channel_id
            if case
            else env["mail.channel"].browse(activity.res_id).exists()
        )
        if not channel or channel.channel_type != "contact_center":
            raise RuntimeError(
                "An activity has no canonical Contact Center conversation"
            )
        payload = {
            "id": activity.id,
            "channel_id": channel.id,
            "user_id": activity.user_id.id,
            "activity_type_id": activity.activity_type_id.id,
            "date_deadline": str(activity.date_deadline),
            "summary_sha256": _digest(activity.summary or ""),
            "note_sha256": _digest(activity.note or ""),
        }
        activity_rows.append(
            {
                "stable": payload,
                "res_model": activity.res_model,
                "res_id": activity.res_id,
            }
        )
    receipt_rows = []
    old_registry = "case_id" in env[MODEL]._fields
    for receipt in receipts:
        stable = {
            "id": receipt.id,
            "channel_id": receipt.channel_id.id,
            "activity_id": receipt.activity_id.id,
            "activity_record_id": receipt.activity_record_id,
            "requested_by_id": receipt.requested_by_id.id,
            "ui_request_id": receipt.ui_request_id,
            "state": receipt.state,
            "completion_request_id": receipt.completion_request_id,
            "completion_payload_sha256": receipt.completion_payload_sha256,
            "completed_by_id": receipt.completed_by_id.id,
            "completed_at": str(receipt.completed_at),
            "closed_at": str(receipt.closed_at),
            "activity_content_sha256": _digest(
                _content(receipt.activity_snapshot_json)
            ),
            "completion_content_sha256": _digest(
                _content(receipt.completion_snapshot_json)
            ),
        }
        receipt_rows.append(
            {
                "stable": stable,
                "case_id": receipt.case_id.id if old_registry else False,
                "schedule_payload_sha256": receipt.schedule_payload_sha256,
                "candidate_schedule_sha256": _digest(
                    _schedule_payload(receipt, conversation=True)
                )
                if receipt.schedule_payload_sha256
                else False,
                "old_schedule_reconstructible": (
                    _digest(_schedule_payload(receipt, conversation=False))
                    == receipt.schedule_payload_sha256
                    if old_registry and receipt.schedule_payload_sha256
                    else None
                ),
            }
        )
    metadata, constraints = _metadata(env)
    return {
        "state": env["ir.config_parameter"].sudo().get_param(STATE_KEY, False),
        "old_registry": old_registry,
        "activities": activity_rows,
        "receipts": receipt_rows,
        "metadata": [
            {
                "id": row.id,
                "name": row.name,
                "model": row.model,
                "res_id": row.res_id,
                "module": row.module,
            }
            for row in metadata.sorted("id")
        ],
        "constraints": [
            {"id": row.id, "module": row.module.name}
            for row in constraints.sorted("id")
        ],
    }


def validate_transition(env, before_snapshot, *, prepared=True):
    after = snapshot(env)
    for key in ("activities", "receipts"):
        if [row["stable"] for row in before_snapshot[key]] != [
            row["stable"] for row in after[key]
        ]:
            raise RuntimeError(
                "Follow-up IDs, assignees, deadlines or content changed: " + key
            )
    if any(
        row["res_model"] != "mail.channel"
        or row["res_id"] != row["stable"]["channel_id"]
        for row in after["activities"]
    ):
        raise RuntimeError("A follow-up still targets an optional case")
    expected_hashes = {
        row["stable"]["id"]: row["candidate_schedule_sha256"]
        for row in before_snapshot["receipts"]
    }
    if any(
        row["schedule_payload_sha256"] != expected_hashes[row["stable"]["id"]]
        for row in after["receipts"]
    ):
        raise RuntimeError("Follow-up replay hash was not migrated")
    before_metadata = [
        {key: value for key, value in row.items() if key != "module"}
        for row in before_snapshot["metadata"]
    ]
    after_metadata = [
        {key: value for key, value in row.items() if key != "module"}
        for row in after["metadata"]
    ]
    if before_metadata != after_metadata or any(
        row["module"] != TARGET_MODULE for row in after["metadata"]
    ):
        raise RuntimeError(
            "Follow-up metadata identity or ownership changed unexpectedly"
        )
    if [row["id"] for row in after["constraints"]] != [
        row["id"] for row in before_snapshot["constraints"]
    ] or any(row["module"] != TARGET_MODULE for row in after["constraints"]):
        raise RuntimeError("Follow-up constraint ownership is inconsistent")
    if not prepared and after["old_registry"]:
        raise RuntimeError("The new follow-up registry still has a case field")
    return after


def prepare(env, before_snapshot=None):
    """Run with the old registry. The caller owns rollback/commit and isolation."""
    params = env["ir.config_parameter"].sudo()
    if params.get_param(STATE_KEY) in ("pending", "done"):
        if not before_snapshot:
            raise RuntimeError(
                "Resuming follow-up migration requires the original snapshot"
            )
        return validate_transition(env, before_snapshot)
    before = snapshot(env)
    if before_snapshot is not None and before != before_snapshot:
        raise RuntimeError("Follow-up state changed after the frozen snapshot")
    if not before["old_registry"] or any(
        row["module"] != SOURCE_MODULE for row in before["metadata"]
    ):
        raise RuntimeError("Follow-up migration requires the old Kanban-owned registry")
    if any(row["old_schedule_reconstructible"] is False for row in before["receipts"]):
        raise RuntimeError(
            "An edited follow-up cannot safely reconstruct its original request"
        )
    if (
        env["mail.activity.type"]
        .sudo()
        .search_count([("res_model", "=", "contact.center.case")])
    ):
        raise RuntimeError(
            "Case-specific activity types require explicit migration review"
        )
    from odoo.addons.contact_center_kanban.models.productivity import (
        _PRODUCTIVITY_NOTIFICATION_SUPPRESSION_TOKEN as suppression_token,
        _PRODUCTIVITY_SERVICE_TOKEN,
    )

    model_id = env["ir.model"]._get_id("mail.channel")
    for row in before["activities"]:
        env["mail.activity"].sudo().browse(row["stable"]["id"]).with_context(
            contact_center_productivity_notification_suppression=suppression_token,
        ).write({"res_model_id": model_id, "res_id": row["stable"]["channel_id"]})
    for row in before["receipts"]:
        receipt = env[MODEL].sudo().browse(row["stable"]["id"])
        values = {"schedule_payload_sha256": row["candidate_schedule_sha256"]}
        for field in ("activity_snapshot_json", "completion_snapshot_json"):
            data = receipt[field]
            if not data:
                continue
            data = dict(
                _content(data),
                channel_id=receipt.channel_id.id,
                channel_name=receipt.channel_id.display_name,
            )
            values[field] = data
        receipt.with_context(
            contact_center_productivity_service_token=_PRODUCTIVITY_SERVICE_TOKEN
        ).write(values)
    metadata, constraints = _metadata(env)
    metadata.write({"module": TARGET_MODULE})
    target = (
        env["ir.module.module"].sudo().search([("name", "=", TARGET_MODULE)], limit=1)
    )
    constraints.write({"module": target.id})
    # case_id deliberately keeps its old XML-ID. The normal Kanban upgrade
    # removes this retired field and its column through Odoo's own ORM cleanup.
    params.set_param(STATE_KEY, "pending")
    return validate_transition(env, before)


def finalize(env, before_snapshot):
    """Validate the new registry and close the one-time handoff transaction."""
    if env["ir.config_parameter"].sudo().get_param(STATE_KEY) not in (
        "pending",
        "done",
    ):
        raise RuntimeError("Follow-up migration was not prepared")
    after = validate_transition(env, before_snapshot, prepared=False)
    if (
        env["ir.model.fields"]
        .sudo()
        .search_count([("model", "=", MODEL), ("name", "=", "case_id")])
    ):
        raise RuntimeError("Retired case field metadata survived the upgrade")
    env["ir.config_parameter"].sudo().set_param(STATE_KEY, "done")
    return dict(after, state="done")
