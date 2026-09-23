#!/usr/bin/env python3
"""Fail if tracked (or staged) files contain secret-looking material.

Checks:
- forbidden files (.env, key files) tracked by git;
- EVM private keys assigned to key-like names, Anthropic keys, PEM private keys;
- non-empty values for secret variables in .env.example.

Exit code 1 on any finding. Findings print file:line and the rule, never the value.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_VARS = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "ANTHROPIC_API_KEY",
    "MCP_HTTP_TOKEN",
)
FORBIDDEN_FILE = re.compile(r"(^|/)(\.env(\.(?!example$)[^/]*)?|.*\.pem|.*\.key|id_rsa.*)$")
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("pem-private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "assigned-hex-private-key",
        re.compile(
            r"(?i)(private[_-]?key|secret|mnemonic|seed)[\"']?\s*[:=]\s*[\"']?(0x)?[0-9a-f]{64}\b"
        ),
    ),
    ("mnemonic-phrase", re.compile(r"(?i)mnemonic[\"']?\s*[:=]\s*[\"']?([a-z]+\s+){11,23}[a-z]+")),
)
ALLOWLIST_FILES = {"scripts/check_secrets.py", "src/polymarket_bot/security/redaction.py"}


def tracked_files(staged: bool) -> list[str]:
    cmd = ["git", "diff", "--cached", "--name-only"] if staged else ["git", "ls-files"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def scan(files: list[str]) -> list[str]:
    findings: list[str] = []
    for rel in files:
        if FORBIDDEN_FILE.search(rel):
            findings.append(f"{rel}: forbidden file type tracked")
            continue
        path = ROOT / rel
        if rel in ALLOWLIST_FILES or not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name, rule in RULES:
                if rule.search(line):
                    findings.append(f"{rel}:{lineno}: {name}")
        if rel.endswith(".env.example"):
            for lineno, line in enumerate(text.splitlines(), start=1):
                key, _, value = line.partition("=")
                if key.strip() in SECRET_VARS and value.strip():
                    findings.append(f"{rel}:{lineno}: .env.example must keep {key.strip()} empty")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true", help="scan staged files only")
    args = parser.parse_args()
    findings = scan(tracked_files(args.staged))
    for finding in findings:
        print(f"SECRET-SCAN: {finding}")
    if findings:
        print(f"{len(findings)} finding(s). Never commit secrets; rotate anything exposed.")
        return 1
    print("secret scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
