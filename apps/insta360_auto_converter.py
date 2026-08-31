"""Quarantined entry point for the retired Google Drive/Docker workflow.

The supported local SD-card workflow lives in :mod:`insta360_converter`. This
module intentionally performs no imports, configuration reads, network calls,
media processing, uploads, or cleanup when it is imported or executed.
"""

from __future__ import annotations

import sys


LEGACY_DISABLED_MESSAGE = (
    "The legacy Google Drive/Docker controller is disabled because it is no "
    "longer supported. Use `python -m insta360_converter --help` or "
    "`run-from-sd.ps1` for the local SD-card workflow."
)


def main() -> int:
    """Fail closed without starting any legacy service or filesystem action."""

    print(LEGACY_DISABLED_MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
