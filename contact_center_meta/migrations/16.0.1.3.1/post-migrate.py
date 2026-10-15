"""Move the existing default media-locator cleanup to the daily maintenance window."""
from datetime import datetime, timedelta

import pytz

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref(
        "contact_center_meta.ir_cron_contact_center_meta_media_locator_cleanup",
        raise_if_not_found=False,
    )
    if not cron or (cron.interval_number, cron.interval_type) != (15, "minutes"):
        return
    now = datetime.now(pytz.timezone("America/Sao_Paulo"))
    nextcall = now.replace(hour=3, minute=30, second=0, microsecond=0)
    if nextcall <= now:
        nextcall += timedelta(days=1)
    cron.write(
        {
            "interval_number": 1,
            "interval_type": "days",
            "nextcall": nextcall.astimezone(pytz.UTC).replace(tzinfo=None),
        }
    )
