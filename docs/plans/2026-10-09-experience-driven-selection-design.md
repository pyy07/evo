# 经验驱动选股（大盘→行业→ETF）

## 目标

选股流程沉淀为可演进的投资经验，而不是写死在 Agent prompt。盘后优先用 a-stock-data 已有端点扩展 Capability。

## 行为

1. **Bootstrap** 幂等播种初始经验：选股层次、T+1 止损、能力缺口优先 a-stock-data。
2. **盘中** 读经验 + 大盘指数 + 主题 ETF 观察池 + 持仓（含 `sellable_quantity`）；不再默认全市场 `screen_market`。
3. **执行层** `submit_order` 强制 T+1：当日买入不可卖。
4. **盘后** 提 CR 时优先封装 `industry_comparison` / `board_fund_flow` / `hsgt_realtime` 等。

## 后续演进

经验与 CR 推动接入行业排名、板块资金流后，盘中可真正走完「大盘→行业→ETF」。
