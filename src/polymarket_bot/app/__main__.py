"""``python -m polymarket_bot.app`` entry point."""

from __future__ import annotations

import sys

from polymarket_bot.app.cli import main

sys.exit(main())
