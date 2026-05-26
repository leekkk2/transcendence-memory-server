"""Standard exit codes used across `tm` subcommands.

The values are stable contract — shell scripts may branch on them.
"""

from __future__ import annotations

OK = 0
USAGE = 1
AUTH = 2
CONNECTION = 3
SERVER = 4
CONFIG_MISSING = 5
