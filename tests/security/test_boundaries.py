"""Static architecture boundaries (import rules) that keep the blast radius small."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "polymarket_bot"


def _modules() -> dict[str, ast.Module]:
    return {
        str(p.relative_to(ROOT)): ast.parse(p.read_text(encoding="utf-8"))
        for p in SRC.rglob("*.py")
    }


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_only_live_adapter_imports_polymarket_sdk():
    offenders = [
        path
        for path, tree in _modules().items()
        if any(n == "polymarket" or n.startswith("polymarket.") for n in _imports(tree))
        and not path.endswith("adapters/polymarket_live.py")
    ]
    assert offenders == []


def test_only_claude_client_imports_anthropic():
    offenders = [
        path
        for path, tree in _modules().items()
        if any(n == "anthropic" or n.startswith("anthropic.") for n in _imports(tree))
        and not path.endswith("llm/claude_client.py")
    ]
    assert offenders == []


def test_secret_loaders_used_only_by_authorized_modules():
    allowed = {
        "load_polymarket_credentials": {"adapters/polymarket_live.py", "security/secrets.py"},
        "load_anthropic_api_key": {"llm/claude_client.py", "security/secrets.py"},
    }
    for path, tree in _modules().items():
        for node in ast.walk(tree):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else None
            )
            if name in allowed:
                assert any(path.endswith(a) for a in allowed[name]), f"{path} uses {name}"


def test_secret_env_vars_read_only_in_secrets_module():
    secret_names = (
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
        "ANTHROPIC_API_KEY",
    )
    for path in SRC.rglob("*.py"):
        rel = str(path.relative_to(ROOT))
        text = path.read_text(encoding="utf-8")
        if rel.endswith("security/secrets.py"):
            continue
        for name in secret_names:
            assert f'"{name}"' not in text, f"{rel} references {name}"


def test_live_authorization_mint_token_only_in_live_lock():
    for path in SRC.rglob("*.py"):
        rel = str(path.relative_to(ROOT))
        if "_MINT_TOKEN" in path.read_text(encoding="utf-8"):
            assert rel.endswith(("promotion/live_lock.py", "lifecycle/state_machine.py")), rel


def test_production_never_imports_research():
    offenders = [
        path
        for path, tree in _modules().items()
        if "/research/" not in path
        and not path.endswith(("app/cli.py", "app/__main__.py"))
        and any(n.startswith("polymarket_bot.research") for n in _imports(tree))
    ]
    assert offenders == []


def test_risk_engine_is_pure():
    risk_tree = ast.parse((SRC / "risk" / "engine.py").read_text(encoding="utf-8"))
    imports = _imports(risk_tree)
    for banned in ("httpx", "websockets", "anthropic", "polymarket", "sqlite3", "asyncio", "os"):
        assert not any(i == banned or i.startswith(banned + ".") for i in imports), banned
    assert not any(i.startswith("polymarket_bot.llm") for i in imports)


def test_gitignore_protects_secrets():
    text = (ROOT / ".gitignore").read_text()
    for pattern in (".env", "*.pem", "*.key", "data/*", "*.sqlite", "KILL_SWITCH"):
        assert pattern in text


def test_env_example_contains_only_placeholders():
    secret_vars = {
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
        "ANTHROPIC_API_KEY",
        "MCP_HTTP_TOKEN",
    }
    for line in (ROOT / ".env.example").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in secret_vars:
            assert value.strip() == "", key
        if sep and key.strip() == "TRADING_MODE":
            assert value.strip() == "disabled"
        if sep and key.strip() == "LIVE_TRADING_ENABLED":
            assert value.strip() == "false"


def test_repository_secret_scan_is_clean():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_secrets.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
