from datetime import datetime, timedelta

import pytz

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref("contact_center_base.ir_cron_contact_center_history_retention")
    if cron.interval_number != 1 or cron.interval_type != "minutes":
        return
    zone = pytz.timezone("America/Sao_Paulo")
    now = datetime.now(zone)
    nextcall = now.replace(hour=3, minute=40, second=0, microsecond=0)
    if nextcall <= now:
        nextcall += timedelta(days=1)
    cron.write(
        {
            "interval_number": 1,
            "interval_type": "days",
            "nextcall": nextcall.astimezone(pytz.UTC).replace(tzinfo=None),
        }
    )
