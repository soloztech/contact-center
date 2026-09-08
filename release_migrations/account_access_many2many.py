"""Standalone old-registry entry point for the cumulative-access migration.

Load with runpy.run_path and call snapshot(env), prepare(env, reviewed_snapshot)
or finalize(env). The implementation is packaged with the native versioned
migration so the end hook and the explicit operator share the same code.
"""

import runpy
from pathlib import Path

_helper = runpy.run_path(
    str(
        Path(__file__).resolve().parents[1]
        / "contact_center_base/migrations/16.0.1.1.0/account_access.py"
    )
)
snapshot = _helper["snapshot"]
prepare = _helper["prepare"]
prepared_snapshot = _helper["prepared_snapshot"]
finalize = _helper["finalize"]
validate_transition = _helper["validate_transition"]
