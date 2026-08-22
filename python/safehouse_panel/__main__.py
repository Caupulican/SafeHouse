"""`python -m safehouse_panel` - see server.main for the commands and flags."""

from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
