"""第一期 Capability 种子定义。"""

from __future__ import annotations

from typing import Any

CAPABILITIES: list[dict[str, Any]] = [
    {
        "id": "list_capabilities",
        "name": "列出系统能力",
        "description": "列出当前可用的系统 Capability",
        "category": "system",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "list_capabilities",
    },
    {
        "id": "get_market_snapshot",
        "name": "获取行情快照",
        "description": (
            "获取 ETF/个股/指数快照。codes 应来自持仓、screen_market/list_universe 候选，"
            "或明确的大盘指数（指数请带市场前缀，如 sh000001）；不要传入无依据的固定观察池。"
        ),
        "category": "market",
        "input_schema": {
            "type": "object",
            "properties": {"codes": {"type": "array", "items": {"type": "string"}}},
            "required": ["codes"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_market_snapshot",
    },
    {
        "id": "get_etf_history",
        "name": "获取历史K线",
        "description": (
            "获取个股或 ETF 最近 N 根 OHLCV（默认最近 20 根、最多 40）。"
            "返回 latest + 时间正序 bars（最后一根为最新）；不要把 count 开很大。"
        ),
        "category": "market",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "count": {"type": "integer", "default": 20, "description": "最近 N 根，最大 40"},
                "period": {"type": "string", "default": "day"},
                "adjust": {"type": "string", "default": "qfq"},
            },
            "required": ["code"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_etf_history",
    },
    {
        "id": "get_trading_calendar",
        "name": "获取交易日历",
        "description": "获取指定年月的交易日历",
        "category": "market",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
                "month": {"type": "integer"},
            },
            "required": ["year", "month"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_trading_calendar",
    },
    {
        "id": "get_market_session",
        "name": "获取当前交易时段",
        "description": "返回 A 股交易日/盘中/午休/盘后状态（Asia/Shanghai）",
        "category": "market",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_market_session",
    },
    {
        "id": "list_universe",
        "name": "列出选股宇宙",
        "description": "按 stock_type（stock/etf/convertible_bond）列出可选标的",
        "category": "market",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_type": {
                    "type": "string",
                    "enum": ["stock", "etf", "convertible_bond"],
                },
                "index_code": {"type": "string", "default": "000300"},
            },
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "list_universe",
    },
    {
        "id": "screen_market",
        "name": "全市场筛选",
        "description": "按资产类型筛选候选（ETF/可转债/个股），按涨跌幅绝对值排序",
        "category": "market",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_type": {
                    "type": "string",
                    "enum": ["stock", "etf", "convertible_bond"],
                },
                "index_code": {"type": "string", "default": "000300"},
                "top_n": {"type": "integer", "default": 30},
                "extra_codes": {"type": "array", "items": {"type": "string"}},
            },
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "screen_market",
    },
    {
        "id": "get_market_overview",
        "name": "大盘综合信息",
        "description": (
            "盘中大盘综合快照：主要指数、沪深300估值、行业/概念涨跌、行业资金流向、"
            "全市场涨跌分布。行业等分项失败会降级保留其余字段，不会整包 502。"
            "看大盘时应优先调用本能力。"
        ),
        "category": "market",
        "input_schema": {
            "type": "object",
            "properties": {
                "top_n": {"type": "integer", "default": 8, "description": "行业/概念/资金流各取前 N"},
            },
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_market_overview",
    },
    {
        "id": "get_industry_ranking",
        "name": "行业/概念涨跌排名",
        "description": "行业或概念板块涨跌幅排名（a-stock-data industry_comparison）",
        "category": "market",
        "input_schema": {
            "type": "object",
            "properties": {
                "board": {
                    "type": "string",
                    "enum": ["industry", "concept"],
                    "default": "industry",
                },
                "top_n": {"type": "integer", "default": 10},
            },
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_industry_ranking",
    },
    {
        "id": "get_board_fund_flow",
        "name": "板块资金流向",
        "description": "行业/概念/地域板块主力资金流向（a-stock-data board_fund_flow）",
        "category": "market",
        "input_schema": {
            "type": "object",
            "properties": {
                "board_type": {
                    "type": "string",
                    "enum": ["industry", "concept", "region"],
                    "default": "industry",
                },
                "period": {
                    "type": "string",
                    "enum": ["today", "5d", "10d"],
                    "default": "today",
                },
                "top_n": {"type": "integer", "default": 10},
            },
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_board_fund_flow",
    },
    {
        "id": "get_index_valuation",
        "name": "指数/ETF估值",
        "description": (
            "指数或宽基 ETF 的 PE/PB/PE分位。ETF 快照 pe_ttm/pb 常为 0，"
            "请把 510300 等映射到 000300 后调用本能力，不要用个股字段当指数估值。"
        ),
        "category": "market",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "指数或 ETF 代码，如 000300 / 510300",
                },
            },
            "required": ["code"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_index_valuation",
    },
    {
        "id": "get_market_breadth",
        "name": "全市场涨跌分布",
        "description": "A 股涨跌家数与涨跌幅分布直方图（东财 ZDFenBu + 上证/深证涨跌家数）",
        "category": "market",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_market_breadth",
    },
    {
        "id": "get_market_news",
        "name": "市场/个股资讯摘要",
        "description": (
            "聚合短资讯供 LLM 阅读：财联社电报、华尔街见闻 A 股快讯，"
            "可选东财个股新闻。返回截断后的 time/title/summary/source，勿当全文。"
            "盘中判断情绪/突发时用；个股需传 codes 且 scope=stock|both。"
        ),
        "category": "news",
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["market", "stock", "both"],
                    "default": "market",
                    "description": "market=大盘快讯；stock=个股；both=两者",
                },
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "个股/ETF 代码，scope 含 stock 时使用，最多 8 个",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "最多返回条数（1–50）",
                },
                "sources": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["cls", "wscn", "eastmoney"],
                    },
                    "description": "数据源子集，默认全部",
                },
                "max_chars": {
                    "type": "integer",
                    "default": 300,
                    "description": "每条 summary 最大字符数",
                },
            },
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_market_news",
    },
    {
        "id": "get_announcements",
        "name": "公司公告摘要",
        "description": (
            "巨潮资讯公告标题/类型摘要（无 PDF 正文）。"
            "持仓或候选标的需核对披露事件时调用；必须传 codes。"
            "对 A 股效果较好；部分 ETF/基金代码可能无结果，可改用对应股票或 LOF。"
        ),
        "category": "news",
        "input_schema": {
            "type": "object",
            "properties": {
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "6 位证券代码，最多 8 个",
                },
                "days": {
                    "type": "integer",
                    "default": 30,
                    "description": "回溯天数",
                },
                "limit": {"type": "integer", "default": 20},
                "max_chars": {"type": "integer", "default": 200},
            },
            "required": ["codes"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_announcements",
    },
    {
        "id": "get_macro_digest",
        "name": "宏观日程与联播摘要",
        "description": (
            "华尔街见闻宏观日历（重要性过滤）+ 可选央视新闻联播标题。"
            "盘后/中长期判断用；calendar 含实际/预期/前值，cctv 仅标题摘要。"
        ),
        "category": "news",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "default": 7,
                    "description": "未来天数",
                },
                "days_back": {
                    "type": "integer",
                    "default": 1,
                    "description": "回溯天数",
                },
                "country": {
                    "type": "string",
                    "description": "国家过滤，如 中国 / 美国；默认不过滤",
                },
                "min_importance": {
                    "type": "integer",
                    "default": 2,
                    "description": "最低重要性 1–4",
                },
                "include_cctv": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否附带新闻联播标题",
                },
            },
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_macro_digest",
    },
    {
        "id": "take_portfolio_snapshot",
        "name": "记录组合快照",
        "description": "写入一笔组合估值快照（盘后结算用）",
        "category": "portfolio",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "take_portfolio_snapshot",
    },
    {
        "id": "settle_day",
        "name": "日终清算",
        "description": (
            "盘后清算：按最新行情估值、写入快照，并 upsert 当日 Settlement"
            "（日初/日终权益、当日盈亏、买卖成交额等）；每交易日一条。"
        ),
        "category": "portfolio",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_run_id": {
                    "type": "integer",
                    "description": "关联的盘后 AgentRun（可选）",
                },
            },
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "settle_day",
    },
    {
        "id": "get_day_report",
        "name": "获取当日清算日报",
        "description": "读取当日组合盈亏、持仓、最近快照、清算记录与精简决策/订单摘要（复盘用）",
        "category": "portfolio",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_day_report",
    },
    {
        "id": "list_settlements",
        "name": "列出清算记录",
        "description": "按交易日倒序返回日初/日终权益与当日盈亏等清算记录",
        "category": "portfolio",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 30,
                    "description": "最多返回条数（1–365）",
                },
            },
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "list_settlements",
    },
    {
        "id": "list_today_decisions",
        "name": "列出当日决策",
        "description": "列出当日投资决策摘要（不含完整工具调用明细）",
        "category": "decision",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "list_today_decisions",
    },
    {
        "id": "list_today_orders",
        "name": "列出当日订单成交",
        "description": "列出当日订单与成交记录",
        "category": "execution",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "list_today_orders",
    },
    {
        "id": "submit_observation",
        "name": "提交市场观察",
        "description": "记录一条市场观察（请使用中文）",
        "category": "decision",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "data": {"type": "object"},
                "agent_run_id": {"type": "integer"},
            },
            "required": ["content"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "submit_observation",
    },
    {
        "id": "submit_thesis",
        "name": "提交投资论点",
        "description": "记录投资论点（请使用中文）",
        "category": "decision",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "observation_id": {"type": "integer"},
                "agent_run_id": {"type": "integer"},
            },
            "required": ["content"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "submit_thesis",
    },
    {
        "id": "submit_decision",
        "name": "提交投资决策",
        "description": "记录投资决策并关联观察/论点（摘要与假设请使用中文）；可附带工具使用心得 usage_notes",
        "category": "decision",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "hypothesis": {"type": "string"},
                "action_plan": {"type": "string"},
                "usage_notes": {
                    "type": "array",
                    "description": "工具使用心得：缺工具/报错/需优化等",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "missing_tool",
                                    "tool_error",
                                    "tool_improve",
                                    "other",
                                ],
                            },
                            "capability_id": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["kind", "content"],
                    },
                },
                "observation_id": {"type": "integer"},
                "thesis_id": {"type": "integer"},
                "agent_run_id": {"type": "integer"},
            },
            "required": ["summary"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "submit_decision",
    },
    {
        "id": "submit_order",
        "name": "提交模拟订单",
        "description": "纸面交易下单（模拟成交）",
        "category": "execution",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "quantity": {"type": "number"},
                "decision_id": {"type": "integer"},
                "agent_run_id": {"type": "integer"},
            },
            "required": ["symbol", "side", "quantity"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "submit_order",
    },
    {
        "id": "get_portfolio",
        "name": "查询投资组合",
        "description": "查询现金、持仓与最新快照",
        "category": "portfolio",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "get_portfolio",
    },
    {
        "id": "submit_review",
        "name": "提交复盘",
        "description": "对决策复盘，必须标注问题类型（复盘正文请使用中文）",
        "category": "review",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_type": {
                    "type": "string",
                    "enum": [
                        "NoIssue",
                        "DecisionError",
                        "DataGap",
                        "CapabilityGap",
                        "ToolUsageError",
                    ],
                },
                "content": {"type": "string"},
                "decision_id": {"type": "integer"},
                "agent_run_id": {"type": "integer"},
                "create_change_request": {"type": "boolean"},
                "change_request": {"type": "object"},
                "lessons": {"type": "array"},
            },
            "required": ["issue_type", "content"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "submit_review",
    },
    {
        "id": "create_change_request",
        "name": "创建变更请求",
        "description": "提出系统能力/数据改进建议（正文请使用中文）",
        "category": "evolution",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "problem": {"type": "string"},
                "evidence": {"type": "string"},
                "proposal": {"type": "string"},
                "expected_benefit": {"type": "string"},
                "issue_type": {
                    "type": "string",
                    "enum": ["DataGap", "CapabilityGap"],
                },
            },
            "required": ["title", "problem", "issue_type"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "create_change_request",
    },
    {
        "id": "list_change_requests",
        "name": "列出变更请求",
        "description": "列出系统变更请求",
        "category": "evolution",
        "input_schema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "list_change_requests",
    },
    {
        "id": "list_experiences",
        "name": "列出沉淀经验",
        "description": "读取已沉淀的投资/Agent/系统经验",
        "category": "review",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "list_experiences",
    },
    {
        "id": "verify_change_request",
        "name": "验收变更请求",
        "description": "对已完成开发的 CR 进行测试验收（仅盘后）",
        "category": "evolution",
        "input_schema": {
            "type": "object",
            "properties": {
                "change_request_id": {"type": "integer"},
                "passed": {"type": "boolean"},
                "evidence": {"type": "string"},
            },
            "required": ["change_request_id", "passed"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "verify_change_request",
    },
    {
        "id": "start_agent_run",
        "name": "开始 Agent 运行",
        "description": "创建一条 AgentRun 记录",
        "category": "system",
        "input_schema": {
            "type": "object",
            "properties": {"trigger": {"type": "string"}, "notes": {"type": "string"}},
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "start_agent_run",
    },
    {
        "id": "finish_agent_run",
        "name": "结束 Agent 运行",
        "description": "关闭一条 AgentRun 记录",
        "category": "system",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_run_id": {"type": "integer"},
                "status": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["agent_run_id"],
        },
        "output_schema": {"type": "object"},
        "permission": "agent",
        "implementation": "finish_agent_run",
    },
]
