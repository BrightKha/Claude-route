# Backtest report

> **SYNTHETIC DATA: simulated prices, books and outcomes; results validate the pipeline only and say nothing about real profitability.**

- session: `<scratch>/sessions/day1` (synthetic=True, 383916 messages)
- strategy `btc5m-0.1.0`, model `twap-gauss-1.0.0`, policy `a30a141b63ebbc46`
- Claude: not called in replay (with-vs-without-Claude comparison NOT AVAILABLE)

## PnL

| metric | value |
|---|---|
| n_trades | 27 |
| net_pnl_usd | 64.6325 |
| gross_pnl_usd | 81.573 |
| fees_usd | 16.9405 |
| expectancy_usd | 2.3938 |
| hit_rate | 0.703704 |
| net_pnl_ci95 | (-9.02363, 137.95) |
| max_drawdown_usd | 47.6805 |
| max_drawdown_pct | 0.206826 |
| final_equity_usd | 264.632 |
| open_positions_at_end | 0 |

## Execution

| metric | value |
|---|---|
| entry_orders | 120 |
| entry_orders_filled | 27 |
| entry_fill_ratio | 0.225 |
| exit_orders | 40 |
| exit_orders_filled | 27 |
| mean_entry_slippage_vs_planned_vwap | -0.0164066 |
| max_entry_slippage_vs_planned_vwap | 0 |
| fees_usd | 16.9405 |

## Calibration (entries, scored at settlement)

| metric | value |
|---|---|
| n | 27 |
| brier_model | 0.138011 |
| brier_market_implied | 0.211971 |
| brier_skill_vs_market | 0.348916 |
| log_loss_model | 0.436519 |
| log_loss_market_implied | 0.603824 |

| bin | n | mean predicted | observed |
|---|---|---|---|
| [0.2,0.3) | 1 | 0.249 | 0.000 |
| [0.3,0.4) | 1 | 0.324 | 0.000 |
| [0.4,0.5) | 3 | 0.445 | 0.333 |
| [0.5,0.6) | 4 | 0.549 | 0.500 |
| [0.6,0.7) | 7 | 0.629 | 0.714 |
| [0.7,0.8) | 3 | 0.751 | 1.000 |
| [0.8,0.9) | 6 | 0.856 | 1.000 |
| [0.9,1.0) | 2 | 0.937 | 1.000 |

## PnL by edge bucket

| bucket | n | pnl | mean |
|---|---|---|---|
| >=0.12 | 4 | 46.1246 | 11.5312 |
| [0.03,0.05) | 14 | 23.7162 | 1.6940 |
| [0.05,0.08) | 7 | -11.4836 | -1.6405 |
| [0.08,0.12) | 2 | 6.2752 | 3.1376 |

## PnL by time to expiry at entry

| bucket | n | pnl | mean |
|---|---|---|---|
| >=240s | 15 | 28.6185 | 1.9079 |
| [120,180)s | 4 | 16.5576 | 4.1394 |
| [180,240)s | 7 | -3.8925 | -0.5561 |
| [60,120)s | 1 | 23.3489 | 23.3489 |

## PnL by vol regime bps

| bucket | n | pnl | mean |
|---|---|---|---|
| [0.7,1) | 5 | 8.8340 | 1.7668 |
| [1,1.5) | 20 | 45.1212 | 2.2561 |
| [1.5,2.5) | 2 | 10.6773 | 5.3386 |

## PnL by hour utc

| bucket | n | pnl | mean |
|---|---|---|---|
| 00h | 1 | -4.2953 | -4.2953 |
| 01h | 1 | -7.3596 | -7.3596 |
| 02h | 3 | 26.3455 | 8.7818 |
| 03h | 2 | 7.3671 | 3.6836 |
| 04h | 2 | 4.9914 | 2.4957 |
| 05h | 2 | -12.5452 | -6.2726 |
| 06h | 3 | 7.2915 | 2.4305 |
| 07h | 4 | 2.2080 | 0.5520 |
| 08h | 2 | 11.4066 | 5.7033 |
| 09h | 4 | 17.1498 | 4.2874 |
| 10h | 2 | 4.3782 | 2.1891 |
| 11h | 1 | 7.6945 | 7.6945 |

## PnL by exit kind

| bucket | n | pnl | mean |
|---|---|---|---|
| sell | 24 | 64.3196 | 2.6800 |
| settlement | 3 | 0.3129 | 0.1043 |

## PnL by signal source

| bucket | n | pnl | mean |
|---|---|---|---|
| strategy | 27 | 64.6325 | 2.3938 |

## Safety

| metric | value |
|---|---|
| final_state | HALTED |
| final_state_reason | watchdog: market_stream_silent(last=1789516799093) |
| kill_switch_engaged | False |
| execution_violations | [] |
| reconciliation_failures | 0 |
| incidents | 1 |
