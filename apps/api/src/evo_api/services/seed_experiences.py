"""Initial experiences — investment stays durable; tool/system tips are usage-kind."""

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
            "1) 大盘：优先看指数、估值、行业/概念强弱、资金流与涨跌分布；\n"
            "2) 行业：需要更细时再下钻行业排名/板块资金/指数估值/宽度；\n"
            "3) ETF：在确认方向后，用少数主题/宽基 ETF 对照持仓落地。\n"
            "持仓是对照对象，不是唯一研究对象。交易服务于中长期配置，而非捕捉日内波动。"
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
        "kind": "agent",
        "fingerprint": "[数据] 大盘综合 get_market_overview",
        "content": (
            "[数据] 大盘综合 get_market_overview\n"
            "需要大盘综合信息时调用 get_market_overview，不要只拼几个固定指数代码。"
            "该能力聚合指数行情、指数估值、行业/概念涨跌、资金流向与涨跌分布；"
            "单分项失败会降级。ETF 估值请用 get_index_valuation，K 线看 latest。"
            "（使用经验：随能力演进可覆盖更新。）"
        ),
    },
    {
        "kind": "system",
        "fingerprint": "[演进] 能力缺口优先 a-stock-data",
        "content": (
            "[演进] 能力缺口优先 a-stock-data\n"
            "选股缺数据或工具时，盘中只记录缺口、不硬凑；"
            "盘后复盘提 CapabilityGap/DataGap 时，优先把 a-stock-data 已有端点做成系统 Capability，"
            "例如 industry_comparison、board_fund_flow、hsgt、指数估值、ETF 份额/IOPV 等；"
            "proposal 写明端点名，避免无数据源的空接口。"
            "（系统经验：随能力演进可覆盖更新。）"
        ),
    },
]


def seed_experiences(db: Session) -> int:
    """Insert missing seed experiences. Returns number of rows added."""
    existing = {
        (e.kind.value, e.fingerprint)
        for e in db.query(Experience).all()
        if e.fingerprint
    }
    # also match legacy rows that only have fingerprint inside content
    legacy_fps = {
        item["fingerprint"]
        for e in db.query(Experience).all()
        for item in SEED_EXPERIENCES
        if item["fingerprint"] in (e.content or "")
    }
    added = 0
    for item in SEED_EXPERIENCES:
        fp = item["fingerprint"]
        kind = item["kind"]
        if (kind, fp) in existing or fp in legacy_fps:
            # 若旧种子曾标成 investment，但现应为 agent/system，原地纠正 kind
            if kind in ("agent", "system"):
                row = (
                    db.query(Experience)
                    .filter(Experience.fingerprint == fp)
                    .order_by(Experience.id.desc())
                    .first()
                )
                if row is None:
                    row = (
                        db.query(Experience)
                        .filter(Experience.content.contains(fp))
                        .order_by(Experience.id.desc())
                        .first()
                    )
                if row and row.kind.value != kind:
                    row.kind = ExperienceKind(kind)
                    row.fingerprint = fp
                    row.content = item["content"]
            continue
        db.add(
            Experience(
                kind=ExperienceKind(kind),
                fingerprint=fp,
                content=item["content"],
                source_review_id=None,
                agent_run_id=None,
            )
        )
        added += 1
    if added:
        db.flush()
    return added
