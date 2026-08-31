"""Backward-compatible entry point for the packaged CLI.

New integrations should use ``python -m insta360_converter``.  This shim stays
small so existing scripts and imports keep working.
"""

from insta360_converter.pipeline import *  # noqa: F401,F403
from insta360_converter.pipeline import main


if __name__ == "__main__":
    raise SystemExit(main())
