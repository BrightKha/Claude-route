# research/

Notebooks and one-off studies live here. They may import `polymarket_bot`, but
production code must never import research code (enforced by
`tests/security/test_boundaries.py`). Reusable research tooling (SYNTHETIC
generator, backtest report, walk-forward calibration) lives in
`src/polymarket_bot/research/` under the same rule.

Promotion of anything found here (a model, a threshold, a calibrator) to
production is a manual, reviewed change with evidence recorded through the
promotion pipeline (docs/risk.md). Results on SYNTHETIC data never count.
