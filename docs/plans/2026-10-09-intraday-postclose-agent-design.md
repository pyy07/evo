# 盘中分钟决策 + 盘后复盘调度

**目标：** agent-runner 常驻，交易日盘中每分钟观察并可选下单；15:05 后自动跑一轮结算复盘（经验/CR/验收均可为空）；交易与演进请求由 API 按 A 股时段硬拦。

**调度：** `agent-runner serve`；保留 `once --mode intraday|postclose`。时区 `Asia/Shanghai`。盘中 `09:30–11:30`、`13:00–15:00`。始终跟真实/日历交易日（mock 亦按工作日）。

**CR：** `proposed → pending_dev → completed → verified`；拒绝必须理由；验收失败退回 `pending_dev`。盘后可读经验、跟进 CR；盘中只交易分析。
