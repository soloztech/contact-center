"""Restore cumulative access after all installed extensions have been loaded."""

import runpy
from pathlib import Path

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if version:
        helper = runpy.run_path(str(Path(__file__).with_name("account_access.py")))
        helper["finalize"](api.Environment(cr, SUPERUSER_ID, {}))
