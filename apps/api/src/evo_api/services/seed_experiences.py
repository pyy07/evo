"""Initial investment experiences — evolve via postclose lessons, not hard-coded prompts."""

from __future__ import annotations

from sqlalchemy.orm import Session

from evo_api.models.entities import Experience, ExperienceKind

# Fingerprints keep seeding idempotent across restarts.
SEED_EXPERIENCES: list[dict[str, str]] = [
    {
        "kind": "investment",
        "fingerprint": "[风格] 中长期价值配置",
        "content": (
            "[风格] 中长期价值配置\n"
            "本 Agent 不是短线/日内交易者：以中长期配置与价值逻辑为主，"
            "关注行业前景、估值性价比、组合结构与风险预算；"
            "避免追分时涨跌、无逻辑频繁调仓。盘中多数时候观察与更新判断，"
            "仅在中长期逻辑或风险显著变化时建仓、换仓或止损。"
        ),
    },
    {
        "kind": "investment",
        "fingerprint": "[选股流程] 大盘→行业→ETF",
        "content": (
            "[选股流程] 大盘→行业→ETF\n"
            "盘中选股按层次推进，不做无脑全市场扫描：\n"
            "1) 大盘：优先调用 get_market_overview 看指数、估值、行业/概念强弱、资金流与涨跌分布；\n"
            "2) 行业：需要更细时可再调 get_industry_ranking / get_board_fund_flow / get_index_valuation / get_market_breadth；\n"
            "3) ETF：在确认方向后，用 screen_market 或候选代码拉少数主题/宽基 ETF，对照持仓。\n"
            "持仓是对照对象，不是唯一研究对象。交易服务于中长期配置，而非捕捉日内波动。"
        ),
    },
    {
        "kind": "investment",
        "fingerprint": "[数据] 大盘综合 get_market_overview",
        "content": (
            "[数据] 大盘综合 get_market_overview\n"
            "需要大盘综合信息时调用 get_market_overview，不要只拼几个固定指数代码。"
            "该能力聚合指数行情、指数估值、行业/概念涨跌、资金流向与涨跌分布；"
            "单分项失败会降级。ETF 估值请用 get_index_valuation，K 线看 latest。"
        ),
    },
    {
        "kind": "investment",
        "fingerprint": "[风控] 弱势止损遵守T+1",
        "content": (
            "[风控] 弱势止损遵守T+1\n"
            "当中长期逻辑被证伪或风险显著上升时，允许止损/减仓卖出；"
            "但只能卖出系统组合中 sellable_quantity 允许的数量（A股/ETF T+1）。"
            "当日买入锁定部分不可卖；不要编造可卖数量。勿因分时噪音频繁进出。"
        ),
    },
    {
        "kind": "investment",
        "fingerprint": "[演进] 能力缺口优先 a-stock-data",
        "content": (
            "[演进] 能力缺口优先 a-stock-data\n"
            "选股缺数据或工具时，盘中只记录缺口、不硬凑；"
            "盘后复盘提 CapabilityGap/DataGap 时，优先把 a-stock-data 已有端点做成系统 Capability，"
            "例如 industry_comparison（行业涨跌排名）、board_fund_flow（板块资金流）、"
            "hsgt_realtime（北向）、指数估值等；proposal 写明端点名，避免无数据源的空接口。"
        ),
    },
]


def seed_experiences(db: Session) -> int:
    """Insert missing seed experiences. Returns number of rows added."""
    existing_fps = {
        item["fingerprint"]
        for e in db.query(Experience).all()
        for item in SEED_EXPERIENCES
        if item["fingerprint"] in (e.content or "")
    }
    added = 0
    for item in SEED_EXPERIENCES:
        if item["fingerprint"] in existing_fps:
            continue
        db.add(
            Experience(
                kind=ExperienceKind(item["kind"]),
                content=item["content"],
                source_review_id=None,
                agent_run_id=None,
            )
        )
        added += 1
    if added:
        db.flush()
    return added
