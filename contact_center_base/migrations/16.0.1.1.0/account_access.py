"""One-time ORM handoff from single owners/teams to cumulative inbox access.

Prepare under the old registry with every application worker stopped. The native
end migration finalizes with the complete new registry. The caller owns commits;
all mutations here are protected by a savepoint and no provider API is called.
"""

import ast
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

VERSION = "16.0.1.1.0"
STATE_KEY = "contact_center_base.account_access_many2many_state"
SNAPSHOT_KEY = "contact_center_base.account_access_many2many_snapshot"
ACCOUNT = "contact.center.account"
OLD_FIELDS = ("owner_user_id", "default_team_id")
NEW_FIELDS = ("access_user_ids", "access_team_ids")
RULE_XMLIDS = (
    "rule_contact_center_team_owner_read",
    "rule_contact_center_account_company",
    "rule_contact_center_connection_company",
    "rule_contact_center_identity_alias_company",
    "rule_contact_center_identity_conflict_company",
    "rule_contact_center_identity_alias_supervisor_company",
    "rule_contact_center_inbox_company",
    "rule_contact_center_outbox_company",
    "rule_contact_center_quick_reply_account",
    "rule_contact_center_quick_reply_team",
)
TEAM_READ_OLD_DOMAIN = (
    "[('company_id', 'in', company_ids), "
    "('account_ids.owner_user_id', '=', user.id)]"
)
TEAM_READ_NEW_DOMAIN = (
    "[('company_id', 'in', company_ids), '|', '|', "
    "('account_ids.access_user_ids', '=', user.id), "
    "('account_ids.access_team_ids.agent_ids', '=', user.id), "
    "('account_ids.access_team_ids.supervisor_ids', '=', user.id)]"
)
TEAM_REPLY_OLD_DOMAIN = (
    "[('company_id', 'in', company_ids), ('scope', '=', 'team'), '|', "
    "('team_id.agent_ids', '=', user.id), ('team_id.supervisor_ids', '=', user.id)]"
)
TEAM_REPLY_NEW_DOMAIN = (
    "[('company_id', 'in', company_ids), ('scope', '=', 'team'), '|', '|', '|', '|', "
    "('team_id.agent_ids', '=', user.id), ('team_id.supervisor_ids', '=', user.id), "
    "('team_id.account_ids.access_user_ids', '=', user.id), "
    "('team_id.account_ids.access_team_ids.agent_ids', '=', user.id), "
    "('team_id.account_ids.access_team_ids.supervisor_ids', '=', user.id)]"
)
AUDITED_RULE_TRANSITIONS = {
    "rule_contact_center_team_owner_read": (TEAM_READ_OLD_DOMAIN, TEAM_READ_NEW_DOMAIN),
    "rule_contact_center_quick_reply_team": (
        TEAM_REPLY_OLD_DOMAIN,
        TEAM_REPLY_NEW_DOMAIN,
    ),
}
RULE_METADATA = (
    "name",
    "model_id",
    "groups",
    "active",
    "perm_read",
    "perm_write",
    "perm_create",
    "perm_unlink",
)
STABLE_FIELDS = {
    ACCOUNT: (
        "name",
        "active",
        "company_id",
        "platform",
        "external_ref",
        "own_external_identity",
        "technical_author_id",
        "auto_assignment_user_id",
        "group_inbound_enabled",
        "group_outbound_enabled",
        "mark_read_enabled",
        "outbound_signature_enabled",
        "show_deleted_message_content",
        "attribution_ui_enabled",
        "default_pipeline_id",
    ),
    "contact.center.team": (
        "name",
        "active",
        "company_id",
        "agent_ids",
        "supervisor_ids",
    ),
    "contact.center.provider.connection": (
        "name",
        "active",
        "account_id",
        "adapter_key",
        "external_ref",
        "state",
        "role",
        "inbound_active",
        "outbound_active",
        "health_detail",
        "capabilities_json",
        "identity_mismatch_latched",
        "wuzapi_base_url",
        "wuzapi_remote_user_id",
        "wuzapi_instance_fingerprint",
        "wuzapi_webhook_key",
        "wuzapi_webhook_url",
        "wuzapi_webhook_event_ids",
        "wuzapi_webhook_sync_state",
        "wuzapi_webhook_sync_revision",
        "wuzapi_hmac_rotation_state",
        "wuzapi_server_id",
        "meta_transport_mode",
        "meta_target_asset_id",
        "meta_webhook_page_id",
        "meta_api_app_id",
        "meta_webhook_asset_id",
    ),
    "contact.center.channel.binding": (
        "account_id",
        "connection_id",
        "channel_id",
        "active",
        "merged_into_id",
    ),
    "mail.channel": (
        "name",
        "active",
        "channel_type",
        "contact_center_company_id",
        "contact_center_responsible_id",
        "contact_center_state",
        "contact_center_last_message_id",
        "contact_center_last_message_at",
        "contact_center_tag_ids",
        "channel_member_ids",
    ),
    "contact.center.case": (
        "channel_id",
        "company_id",
        "team_id",
        "responsible_user_id",
        "pipeline_id",
        "stage_id",
        "active",
    ),
}
UNCHANGED_LEDGER_MODELS = (
    "contact.center.outbox.command",
    "contact.center.inbox.event",
    "contact.center.scheduled.message",
    "contact.center.followup.request",
    "contact.center.message.binding",
    "contact.center.media.binding",
    "contact.center.crm.conversation.link",
    "mail.mail",
    "mail.notification",
    "mail.activity",
    "queue.job",
)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _records(env, model_name):
    model = env[model_name].sudo().with_context(active_test=False)
    domain = (
        [("channel_type", "=", "contact_center")]
        if model_name == "mail.channel"
        else []
    )
    return model.search(domain, order="id")


def _validate_channel_account_coverage(env):
    """Reject missing or ambiguous inbox authority, including inactive history."""
    channels = _records(env, "mail.channel")
    accounts = _records(env, ACCOUNT)
    bindings = _records(env, "contact.center.channel.binding")
    channel_ids = set(channels.ids)
    account_ids = set(accounts.ids)
    by_channel = {}
    invalid_channels = set()
    missing_accounts = set()
    for binding in bindings:
        channel_id = binding.channel_id.id
        account_id = binding.account_id.id
        by_channel.setdefault(channel_id, []).append(account_id)
        if channel_id not in channel_ids:
            invalid_channels.add(binding.id)
        if account_id not in account_ids:
            missing_accounts.add(binding.id)
    problems = {
        "orphan_channel_ids": channel_ids - set(by_channel),
        "ambiguous_channel_ids": {
            channel_id
            for channel_id, linked_accounts in by_channel.items()
            if len(linked_accounts) != 1
        },
        "invalid_channel_binding_ids": invalid_channels,
        "missing_account_binding_ids": missing_accounts,
    }
    invalid = {
        name: {"count": len(ids), "first_ids": sorted(ids)[:20]}
        for name, ids in problems.items()
        if ids
    }
    if invalid:
        raise RuntimeError(
            "Conversation inbox authority requires repair before migration: "
            + json.dumps(invalid, sort_keys=True)
        )
    return {"channels": len(channels), "bindings": len(bindings)}


def _values(record, field_names):
    result = {"id": record.id}
    for name in field_names:
        field = record._fields.get(name)
        if field is None:
            continue
        value = record[name]
        if field.type == "many2one":
            value = value.id or False
        elif field.type in ("many2many", "one2many"):
            value = sorted(value.ids)
        result[name] = value
    return result


def _stable(env):
    stable = {}
    for model, fields in STABLE_FIELDS.items():
        if model in env.registry:
            records = _records(env, model)
            stable[model] = {
                "count": len(records),
                "sha256": digest([_values(record, fields) for record in records]),
            }
    for model in UNCHANGED_LEDGER_MODELS:
        if model in env.registry:
            records = _records(env, model)
            stable[model] = {"count": len(records), "ids_sha256": digest(records.ids)}
    return stable


def _assert_stable(env, before):
    after = _stable(env)
    changed = sorted(
        model
        for model in set(after) | set(before)
        if after.get(model) != before.get(model)
    )
    if changed:
        raise RuntimeError(
            "Business data changed during access migration: " + ", ".join(changed)
        )


def _rule(env, name):
    row = env.ref("contact_center_base." + name, raise_if_not_found=False)
    if not row or row._name != "ir.rule":
        raise RuntimeError("Missing canonical Contact Center access rule: " + name)
    return row.sudo()


def _rules(env):
    return [
        {
            "xmlid": name,
            "id": _rule(env, name).id,
            "domain_force": _rule(env, name).domain_force,
            "metadata_sha256": digest(_values(_rule(env, name), RULE_METADATA)),
        }
        for name in RULE_XMLIDS
    ]


def snapshot(env):
    """Capture old access plus digests of unchanged business data, without secrets."""
    model = env[ACCOUNT]
    if not all(name in model._fields for name in OLD_FIELDS) or any(
        name in model._fields for name in NEW_FIELDS
    ):
        raise RuntimeError("Access migration prepare requires the old registry")
    _validate_channel_account_coverage(env)
    accounts = []
    for account in _records(env, ACCOUNT):
        accounts.append(
            {
                "id": account.id,
                "user_ids": account.owner_user_id.ids,
                "team_ids": account.default_team_id.ids,
                "effective_user_ids": sorted(
                    account._contact_center_effective_users().ids
                ),
            }
        )
    return {
        "schema": 1,
        "target_version": VERSION,
        "database_uuid": env["ir.config_parameter"].sudo().get_param("database.uuid"),
        "accounts": accounts,
        "stable": _stable(env),
        "rules": _rules(env),
    }


def _validate_snapshot(value):
    if (
        not isinstance(value, dict)
        or value.get("schema") != 1
        or value.get("target_version") != VERSION
    ):
        raise RuntimeError("Unexpected inbox-access migration snapshot version")
    rows = value.get("accounts")
    if not isinstance(rows, list) or len({row.get("id") for row in rows}) != len(rows):
        raise RuntimeError("Duplicate or invalid inbox migration scope")
    for row in rows:
        if type(row.get("id")) is not int or row["id"] <= 0:
            raise RuntimeError("Invalid inbox migration ID")
        for name in ("user_ids", "team_ids", "effective_user_ids"):
            ids = row.get(name)
            if (
                not isinstance(ids, list)
                or any(type(item) is not int or item <= 0 for item in ids)
                or len(ids) != len(set(ids))
            ):
                raise RuntimeError("Invalid inbox access membership snapshot")
        if len(row["user_ids"]) > 1 or len(row["team_ids"]) > 1:
            raise RuntimeError(
                "The source snapshot must use the old single access fields"
            )
    rules = value.get("rules", [])
    if len(rules) != len(RULE_XMLIDS) or {row.get("xmlid") for row in rules} != set(
        RULE_XMLIDS
    ):
        raise RuntimeError("Unexpected inbox-access rule migration scope")
    if not isinstance(value.get("stable"), dict) or ACCOUNT not in value["stable"]:
        raise RuntimeError("Missing business-state preservation evidence")
    return value


def prepared_snapshot(env):
    """Guard native upgrades before any retired field can be removed."""
    params = env["ir.config_parameter"].sudo()
    if params.get_param(STATE_KEY) not in ("prepared", "done"):
        raise RuntimeError(
            "Prepare the 16.0.1.1.0 inbox-access migration with the old registry "
            "before upgrading Contact Center; see operations/account-access-upgrade.md"
        )
    try:
        envelope = json.loads(params.get_param(SNAPSHOT_KEY) or "{}")
        value = _validate_snapshot(envelope["snapshot"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            "Missing or invalid inbox-access handoff snapshot"
        ) from error
    if envelope.get("sha256") != digest(value):
        raise RuntimeError("Inbox-access handoff snapshot digest mismatch")
    if value["database_uuid"] != params.get_param("database.uuid"):
        raise RuntimeError("Inbox-access handoff belongs to another database")
    return value


def prepare(env, before_snapshot=None):
    """Persist a frozen old-registry snapshot in the caller's transaction."""
    with env.cr.savepoint():
        params = env["ir.config_parameter"].sudo()
        current = snapshot(env)
        if before_snapshot is not None and digest(current) != digest(before_snapshot):
            raise RuntimeError("Inbox access changed after the reviewed snapshot")
        if params.get_param(STATE_KEY):
            original = prepared_snapshot(env)
            if digest(current) != digest(original):
                raise RuntimeError(
                    "Prepared inbox migration no longer matches the old registry"
                )
            return original
        _validate_snapshot(current)
        params.set_param(
            SNAPSHOT_KEY,
            json.dumps(
                {"snapshot": current, "sha256": digest(current)},
                sort_keys=True,
                default=str,
            ),
        )
        params.set_param(STATE_KEY, "prepared")
        return current


def _domain_ast(source):
    return ast.dump(ast.parse(source.strip(), mode="eval"), include_attributes=False)


def rule_domain_plan(before_rules, security_xml_path):
    """Replace only the ten canonical noupdate domains; reject customizations."""
    root = ElementTree.parse(security_xml_path).getroot()
    targets = {}
    for row in root.findall(".//record[@model='ir.rule']"):
        name = row.get("id")
        if name not in RULE_XMLIDS:
            continue
        fields = row.findall("field[@name='domain_force']")
        if name in targets or len(fields) != 1 or fields[0].get("eval") is not None:
            raise RuntimeError("Ambiguous canonical access-rule domain: " + name)
        targets[name] = (fields[0].text or "").strip()
    if set(targets) != set(RULE_XMLIDS):
        raise RuntimeError("Candidate XML does not contain the exact access-rule scope")
    if len(before_rules) != len(RULE_XMLIDS) or {
        row["xmlid"] for row in before_rules
    } != set(RULE_XMLIDS):
        raise RuntimeError("Unexpected access-rule snapshot scope")
    plan = []
    for row in before_rules:
        target = targets[row["xmlid"]]
        transformed = (
            row["domain_force"]
            .replace("owner_user_id", "access_user_ids")
            .replace("default_team_id", "access_team_ids")
        )
        if row["xmlid"] in AUDITED_RULE_TRANSITIONS:
            # Shared inbox operators can read every roster participating in the
            # same inbox. This audited read-only expansion is intentionally more
            # than a field rename; never treat a customized old rule as canonical.
            old_domain, new_domain = AUDITED_RULE_TRANSITIONS[row["xmlid"]]
            if _domain_ast(row["domain_force"]) != _domain_ast(old_domain):
                raise RuntimeError("Customized team access rule requires review")
            transformed = new_domain
        if _domain_ast(transformed) != _domain_ast(target):
            raise RuntimeError(
                "Customized access rule requires review: " + row["xmlid"]
            )
        if "owner_user_id" in target or "default_team_id" in target:
            raise RuntimeError("Candidate access rule still references retired fields")
        plan.append(dict(row, target_domain=target))
    return plan


def validate_transition(env, before):
    """Prove singleton access, effective scope and unchanged business assignments."""
    _validate_snapshot(before)
    _validate_channel_account_coverage(env)
    accounts = _records(env, ACCOUNT)
    if accounts.ids != [row["id"] for row in before["accounts"]]:
        raise RuntimeError("Inbox inventory changed across the access migration")
    for account, row in zip(accounts, before["accounts"]):
        if (
            sorted(account.access_user_ids.ids) != row["user_ids"]
            or sorted(account.access_team_ids.ids) != row["team_ids"]
        ):
            raise RuntimeError("Migrated inbox access differs from its original grants")
        if (
            sorted(account._contact_center_effective_users().ids)
            != row["effective_user_ids"]
        ):
            raise RuntimeError("Effective inbox permissions changed during migration")
        bindings = (
            env["contact.center.channel.binding"]
            .sudo()
            .with_context(active_test=False)
            .search([("account_id", "=", account.id)])
        )
        for channel in bindings.mapped("channel_id"):
            if (
                sorted(channel.contact_center_access_user_ids.ids) != row["user_ids"]
                or sorted(channel.contact_center_access_team_ids.ids) != row["team_ids"]
            ):
                raise RuntimeError("Conversation access projection was not reconciled")
    _assert_stable(env, before["stable"])
    return {
        "accounts": len(accounts),
        "stable": before["stable"],
        "snapshot_sha256": digest(before),
    }


def finalize(env, before_snapshot=None, security_xml_path=None):
    """Complete in the native end phase, or explicitly after a reviewed rollback."""
    with env.cr.savepoint():
        before = prepared_snapshot(env)
        if before_snapshot is not None and digest(before_snapshot) != digest(before):
            raise RuntimeError("Caller snapshot does not match the prepared migration")
        params = env["ir.config_parameter"].sudo()
        if params.get_param(STATE_KEY) == "done":
            return {
                "state": "done",
                "already_done": True,
                "snapshot_sha256": digest(before),
            }
        model = env[ACCOUNT]
        if any(name in model._fields for name in OLD_FIELDS) or any(
            name not in model._fields or model._fields[name].type != "many2many"
            for name in NEW_FIELDS
        ):
            raise RuntimeError("Access migration finalize requires the new registry")
        _validate_channel_account_coverage(env)
        _assert_stable(env, before["stable"])
        xml_path = (
            security_xml_path
            or Path(__file__).resolve().parents[2]
            / "security/contact_center_security.xml"
        )
        plan = rule_domain_plan(before["rules"], xml_path)
        for row in plan:
            rule = _rule(env, row["xmlid"])
            if (
                rule.id != row["id"]
                or digest(_values(rule, RULE_METADATA)) != row["metadata_sha256"]
            ):
                raise RuntimeError(
                    "Access-rule identity or permissions changed during upgrade"
                )
            if _domain_ast(rule.domain_force) not in (
                _domain_ast(row["domain_force"]),
                _domain_ast(row["target_domain"]),
            ):
                raise RuntimeError("Access-rule domain changed after preparation")
            if rule.domain_force != row["target_domain"]:
                rule.write({"domain_force": row["target_domain"]})
        accounts = _records(env, ACCOUNT)
        if accounts.ids != [row["id"] for row in before["accounts"]]:
            raise RuntimeError("Inbox inventory changed before access finalization")
        for account, row in zip(accounts, before["accounts"]):
            if account.access_user_ids or account.access_team_ids:
                raise RuntimeError(
                    "New access grants already exist; inspect before finalizing"
                )
            account.with_context(tracking_disable=True, mail_notrack=True).write(
                {
                    "access_user_ids": [(6, 0, row["user_ids"])],
                    "access_team_ids": [(6, 0, row["team_ids"])],
                }
            )
        env.flush_all()
        result = validate_transition(env, before)
        params.set_param(STATE_KEY, "done")
        return dict(result, state="done", rules_updated=len(plan), already_done=False)
