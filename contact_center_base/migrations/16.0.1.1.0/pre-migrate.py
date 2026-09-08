"""Require the old-registry snapshot before retiring single access fields."""

import runpy
from pathlib import Path

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if version:
        helper = runpy.run_path(str(Path(__file__).with_name("account_access.py")))
        helper["prepared_snapshot"](api.Environment(cr, SUPERUSER_ID, {}))
