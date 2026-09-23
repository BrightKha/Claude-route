"""Shared SYNTHETIC sessions for replay tests (generated once per test session)."""

from __future__ import annotations

from pathlib import Path

import pytest

from polymarket_bot.research.synthetic import SynthParams, generate_session


@pytest.fixture(scope="session")
def synthetic_session(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("synth")
    return generate_session(root, SynthParams(windows=8, seed=11, mm_noise=0.6), "base")


@pytest.fixture(scope="session")
def chaos_session(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("chaos")
    params = SynthParams(windows=6, seed=5, mm_noise=0.6, disconnect_prob_per_window=1.0)
    return generate_session(root, params, "chaos")
