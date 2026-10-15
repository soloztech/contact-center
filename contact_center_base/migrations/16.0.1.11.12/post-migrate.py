"""Retarget thumbnail cleanup and schedule existing temporary-data cleanups daily."""
from datetime import datetime, timedelta

import pytz

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    preview = env.ref(
        "contact_center_base.cron_expire_ad_previews", raise_if_not_found=False
    )
    if preview:
        preview.write(
            {
                "name": "Contact Center: clear expired ad thumbnail locators",
                "model_id": env.ref(
                    "contact_center_base.model_contact_center_ad_preview_locator"
                ).id,
                "code": "model._cron_expire()",
            }
        )
        env["ir.model.data"].search(
            [
                ("module", "=", "contact_center_base"),
                (
                    "name",
                    "in",
                    [
                        "cron_expire_ad_previews",
                        "cron_expire_ad_previews_ir_actions_server",
                    ],
                ),
            ]
        ).write({"noupdate": True})
    now = datetime.now(pytz.timezone("America/Sao_Paulo"))
    for xmlid, minute in [
        ("ir_cron_contact_center_media_upload_cleanup", 20),
        ("cron_expire_ad_previews", 25),
    ]:
        cron = env.ref("contact_center_base." + xmlid, raise_if_not_found=False)
        if not cron or (cron.interval_number, cron.interval_type) != (1, "hours"):
            continue
        nextcall = now.replace(hour=3, minute=minute, second=0, microsecond=0)
        if nextcall <= now:
            nextcall += timedelta(days=1)
        cron.write(
            {
                "interval_number": 1,
                "interval_type": "days",
                "nextcall": nextcall.astimezone(pytz.UTC).replace(tzinfo=None),
            }
        )
