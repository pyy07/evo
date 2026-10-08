# Evo · 自演进投资系统（第一期）

围绕 **投资决策生命周期** 的纸面交易系统：Agent 只能通过 Capability 调用系统；行情经内部 Market Data（适配 [a-stock-data](https://github.com/simonlin1212/a-stock-data)）；复盘分流后由人类审批 Change Request 并登记新能力。

## 架构

- `apps/api` — FastAPI 核心（Capability / Decision / Paper Portfolio / Review / CR）
- `apps/agent-runner` — 单 Agent 一轮闭环（OpenAI 兼容 API 可选）
- `apps/web` — 监督台（时间线 / 组合 / 审批）
- `services/market-data` — a-stock-data 子集封装 + `MARKET_DATA_MODE=mock|live`
- `config/instruments.yaml` — 品种规则（全量 ETF 可投，按 kind 约束）

## 快速开始

```bash
# 依赖
uv sync

# 启动 API（默认 sqlite + mock 行情）
uv run uvicorn evo_api.main:app --app-dir apps/api/src --reload --port 8000

# 另一终端：Agent 一轮（无 OPENAI_API_KEY 时走规则回退）
uv run agent-runner once --api-base http://127.0.0.1:8000

# Web
cd apps/web && npm install && npm run dev
```

管理台默认 token：`dev-admin-token`（可用环境变量 `ADMIN_TOKEN` 覆盖）。

### 实行情

```bash
export MARKET_DATA_MODE=live
# 依赖：requests pandas（workspace 已包含）
# 实现来自 a-stock-data SKILL 摘录：tencent_quote / tencent_kline / trading_calendar
uv run uvicorn evo_api.main:app --app-dir apps/api/src --port 8000
```

### Docker Compose

```bash
docker compose up --build
# API http://localhost:8000
# Web http://localhost:5173
```

Compose 中 API 默认 `MARKET_DATA_MODE=mock`，数据库为 Postgres。

## 测试

```bash
uv sync --group dev
uv run pytest
```

## Capability 入口

`POST /capabilities/{id}/invoke`，body：`{"input": {...}}`

最小集见计划：`list_capabilities`、行情三项、决策提交、模拟下单、复盘、Change Request 等。

## 演进流程

1. Agent `submit_review`（`CapabilityGap` / `DataGap`）或 `create_change_request`
2. Human `POST /admin/change-requests/{id}/approve`
3. 人工写代码后 `POST /admin/change-requests/{id}/implement` 登记 Capability
4. Agent 下次 `list_capabilities` 可见

## 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `DATABASE_URL` | SQLAlchemy URL | 本地 sqlite 文件 |
| `MARKET_DATA_MODE` | `mock` / `live` | `mock` |
| `ADMIN_TOKEN` | 管理台 Bearer | `dev-admin-token` |
| `INITIAL_CASH` | 纸面初始资金 | `1000000` |
| `OPENAI_API_KEY` | Agent LLM（可选） | 空则规则回退 |
| `OPENAI_BASE_URL` | 兼容网关 | OpenAI |
| `API_BASE` | Runner 用 | `http://127.0.0.1:8000` |

## 许可说明

`services/market-data/src/market_data/asd_core.py` 改编自 simonlin1212/a-stock-data（Apache-2.0）。