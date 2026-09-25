"""Every Makefile target that calls the CLI must be accepted by the real parser.

Regression: global options (``--config`` …) were placed after the subcommand,
which argparse rejects ("unrecognized arguments"). Parsing only — nothing runs.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from polymarket_bot.app.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]
CLI = "$(PY) -m polymarket_bot.app"


def _variables(text: str) -> dict[str, str]:
    return dict(re.findall(r"^([A-Z_]+) \?= (.+)$", text, flags=re.MULTILINE))


def _cli_calls() -> list[tuple[str, str]]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    variables = _variables(text)
    calls: list[tuple[str, str]] = []
    target = ""
    for line in text.splitlines():
        if m := re.match(r"^([a-z][a-z-]*):", line):
            target = m.group(1)
        elif line.startswith("\t") and CLI in line:
            command = line.strip()[len(CLI) :]
            for name, value in variables.items():
                command = command.replace(f"$({name})", value)
            calls.append((target, command))
    return calls


CALLS = _cli_calls()


def test_all_cli_targets_are_covered() -> None:
    targets = {t for t, _ in CALLS}
    expected = {"synth", "replay", "backtest", "walk-forward", "paper", "record", "status"}
    assert expected | {"live-readiness", "kill-switch", "mcp"} <= targets


@pytest.mark.parametrize(("target", "command"), CALLS, ids=[t for t, _ in CALLS])
def test_makefile_command_parses(target: str, command: str) -> None:
    args = build_parser().parse_args(shlex.split(command))
    assert args.command is not None, target
    assert "$(" not in command, f"unexpanded variable in {target}: {command}"
