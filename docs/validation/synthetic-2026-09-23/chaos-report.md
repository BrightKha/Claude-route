# Backtest report

> **SYNTHETIC DATA: simulated prices, books and outcomes; results validate the pipeline only and say nothing about real profitability.**

- session: `<scratch>/sessions/chaos48` (synthetic=True, 64661 messages)
- strategy `btc5m-0.1.0`, model `twap-gauss-1.0.0`, policy `a30a141b63ebbc46`
- Claude: not called in replay (with-vs-without-Claude comparison NOT AVAILABLE)

## PnL

| metric | value |
|---|---|
| n_trades | 3 |
| net_pnl_usd | -25.7951 |
| gross_pnl_usd | -24.0759 |
| fees_usd | 1.71917 |
| expectancy_usd | -8.59836 |
| hit_rate | 0 |
| net_pnl_ci95 | (-29.4918, -23.6242) |
| max_drawdown_usd | 26.2985 |
| max_drawdown_pct | 0.131162 |
| final_equity_usd | 174.205 |
| open_positions_at_end | 0 |

## Execution

| metric | value |
|---|---|
| entry_orders | 27 |
| entry_orders_filled | 3 |
| entry_fill_ratio | 0.111111 |
| exit_orders | 14 |
| exit_orders_filled | 3 |
| mean_entry_slippage_vs_planned_vwap | -0.0133333 |
| max_entry_slippage_vs_planned_vwap | 0 |
| fees_usd | 1.71917 |

## Calibration (entries, scored at settlement)

| metric | value |
|---|---|
| n | 3 |
| brier_model | 0.333147 |
| brier_market_implied | 0.168967 |
| brier_skill_vs_market | -0.971671 |
| log_loss_model | 0.86999 |
| log_loss_market_implied | 0.499681 |

| bin | n | mean predicted | observed |
|---|---|---|---|
| [0.3,0.4) | 1 | 0.316 | 0.000 |
| [0.6,0.7) | 1 | 0.626 | 0.000 |
| [0.7,0.8) | 1 | 0.713 | 0.000 |

## PnL by edge bucket

| bucket | n | pnl | mean |
|---|---|---|---|
| [0.03,0.05) | 3 | -25.7951 | -8.5984 |

## PnL by time to expiry at entry

| bucket | n | pnl | mean |
|---|---|---|---|
| >=240s | 1 | -7.8747 | -7.8747 |
| [180,240)s | 2 | -17.9203 | -8.9602 |

## PnL by vol regime bps

| bucket | n | pnl | mean |
|---|---|---|---|
| [1,1.5) | 3 | -25.7951 | -8.5984 |

## PnL by hour utc

| bucket | n | pnl | mean |
|---|---|---|---|
| 01h | 3 | -25.7951 | -8.5984 |

## PnL by exit kind

| bucket | n | pnl | mean |
|---|---|---|---|
| sell | 3 | -25.7951 | -8.5984 |

## PnL by signal source

| bucket | n | pnl | mean |
|---|---|---|---|
| strategy | 3 | -25.7951 | -8.5984 |

## Safety

| metric | value |
|---|---|
| final_state | KILL_SWITCH |
| final_state_reason | kill switch: loss limit: daily loss 0.1037 >= 0.1 of equity |
| kill_switch_engaged | True |
| execution_violations | [] |
| reconciliation_failures | 0 |
| incidents | 8 |
