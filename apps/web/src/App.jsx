import React, { useCallback, useEffect, useMemo, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || "";

const STATUS_LABEL = {
  proposed: "待审批",
  pending_dev: "待开发",
  rejected: "不采纳",
  completed: "已完成",
  verified: "验收通过",
  approved: "待开发",
  implemented: "已完成",
};

const SIDE_LABEL = { buy: "买入", sell: "卖出" };

const ISSUE_LABEL = {
  NoIssue: "无问题",
  CapabilityGap: "能力缺口",
  DataGap: "数据缺口",
  RuleViolation: "规则违反",
  ProcessIssue: "流程问题",
};

function money(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  return v.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function pct(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : "";
  const digits = Math.abs(v) < 0.01 && v !== 0 ? 4 : 2;
  return `${sign}${v.toFixed(digits)}%`;
}

function clip(text, n = 120) {
  const s = String(text || "").trim();
  if (s.length <= n) return s;
  return `${s.slice(0, n)}…`;
}

function pnlClass(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v === 0) return "flat";
  return v > 0 ? "up" : "down";
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function api(path, { token, method = "GET", body } = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text}`);
  }
  return res.json();
}

function Pnl({ value, withPct }) {
  return (
    <span className={`pnl ${pnlClass(value)}`}>
      {Number(value) > 0 ? "+" : ""}
      {money(value)}
      {withPct != null ? <small> {pct(withPct)}</small> : null}
    </span>
  );
}

function prettyJson(value) {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return String(value);
  }
}

function InvocationList({ invocations }) {
  const [openId, setOpenId] = useState(null);
  if (!invocations?.length) {
    return <div className="empty">本轮未记录到接口调用。</div>;
  }
  return (
    <div className="inv-list">
      {invocations.map((call, idx) => {
        const key = call.id ?? idx;
        const expanded = openId === key;
        return (
          <div className={`inv-item ${call.success === false ? "fail" : ""}`} key={key}>
            <button
              type="button"
              className="inv-head"
              onClick={() => setOpenId(expanded ? null : key)}
            >
              <span className="inv-cap">
                {idx + 1}. {call.capability_id || "unknown"}
              </span>
              <span className="muted">
                {call.success === false ? "失败" : "成功"} · {when(call.created_at)} ·{" "}
                {expanded ? "收起" : "看输入输出"}
              </span>
            </button>
            {expanded ? (
              <div className="inv-body">
                {call.error ? <p className="inv-error">{call.error}</p> : null}
                <div className="inv-io">
                  <div>
                    <h4>输入</h4>
                    <pre>{prettyJson(call.input)}</pre>
                  </div>
                  <div>
                    <h4>输出</h4>
                    <pre>{prettyJson(call.output)}</pre>
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function TradeTable({ trades, emptyText }) {
  if (!trades?.length) {
    return <div className="empty">{emptyText}</div>;
  }
  return (
    <table className="pos-table trade-table">
      <thead>
        <tr>
          <th>时间</th>
          <th>方向</th>
          <th>代码</th>
          <th>名称</th>
          <th>数量</th>
          <th>成交价</th>
          <th>成交额</th>
          <th>佣金</th>
          <th>订单</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t) => (
          <tr key={t.id}>
            <td>{when(t.created_at)}</td>
            <td className={t.side === "buy" ? "up" : "down"}>
              {SIDE_LABEL[t.side] || t.side}
            </td>
            <td className="sym">{t.symbol}</td>
            <td>{t.name || "—"}</td>
            <td>{t.quantity}</td>
            <td>{money(t.price)}</td>
            <td>{money(t.amount ?? Number(t.price) * Number(t.quantity))}</td>
            <td>{money(t.commission)}</td>
            <td>#{t.order_id}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SettlementTable({ settlements, todayDate }) {
  if (!settlements?.length) {
    return (
      <div className="empty">
        还没有清算记录。交易日 15:05 后由盘后 Agent 调用 settle_day 写入（每交易日一条）。
      </div>
    );
  }
  return (
    <table className="pos-table settle-table">
      <thead>
        <tr>
          <th>日期</th>
          <th>日初权益</th>
          <th>日终权益</th>
          <th>当日盈亏</th>
          <th>日初现金</th>
          <th>日终现金</th>
          <th>买入额</th>
          <th>卖出额</th>
          <th>成交</th>
          <th>决策</th>
          <th>浮动盈亏</th>
        </tr>
      </thead>
      <tbody>
        {settlements.map((s) => (
          <tr key={s.id} className={s.trade_date === todayDate ? "today-row" : ""}>
            <td className="sym">{s.trade_date}</td>
            <td>{money(s.open_equity)}</td>
            <td>{money(s.close_equity)}</td>
            <td>
              <Pnl value={s.day_pnl} withPct={s.day_pnl_pct} />
            </td>
            <td>{money(s.open_cash)}</td>
            <td>{money(s.close_cash)}</td>
            <td>{money(s.buy_amount)}</td>
            <td>{money(s.sell_amount)}</td>
            <td>{s.trade_count}</td>
            <td>{s.decision_count}</td>
            <td>
              <Pnl value={s.unrealized_pnl} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DeskPage({ desk, busy, onRefresh, onOpenDecision }) {
  const p = desk?.portfolio;
  const today = desk?.today;
  const settlements = desk?.settlements || [];
  const settlementToday = desk?.settlement_today;
  const tradesToday = today?.trades || [];
  const tradesHistory = desk?.trades_history || [];
  const openCrHint = desk?.open_cr_count;
  const [ledgerTab, setLedgerTab] = useState("today");

  const feed = useMemo(() => {
    const decisions = (today?.decisions || []).map((d) => {
      const notes = d.usage_notes || [];
      const noteHint =
        notes.length > 0
          ? `心得 ${notes.length}：` +
            notes
              .slice(0, 2)
              .map((n) => n.content || n.kind)
              .join("；")
          : "";
      return {
        kind: "decision",
        decisionId: d.id,
        id: `d-${d.id}`,
        at: d.created_at,
        title: `决策 #${d.id}`,
        body: d.summary,
        meta: [d.action_plan ? clip(d.action_plan, 90) : "", noteHint ? clip(noteHint, 120) : ""]
          .filter(Boolean)
          .join(" · "),
        usageNotes: notes,
        agentRunId: d.agent_run_id,
        invocationCount: (d.invocations || []).length,
      };
    });
    const orders = (today?.orders || []).map((o) => ({
      kind: "order",
      id: `o-${o.id}`,
      at: o.created_at,
      title: `${SIDE_LABEL[o.side] || o.side} ${o.symbol}${o.name ? ` ${o.name}` : ""}`,
      body: `${o.quantity} 份 · ${o.status}${o.price != null ? ` · ${money(o.price)}` : ""}`,
      meta: o.reason || (o.decision_id ? `关联决策 #${o.decision_id}` : ""),
      decisionId: o.decision_id || null,
    }));
    const trades = (today?.trades || []).map((t) => ({
      kind: "trade",
      id: `t-${t.id}`,
      at: t.created_at,
      title: `成交 ${SIDE_LABEL[t.side] || t.side} ${t.symbol}${t.name ? ` ${t.name}` : ""}`,
      body: `${t.quantity} 份 · ${money(t.price)} · 额 ${money(t.amount ?? t.price * t.quantity)}`,
      meta: `佣金 ${money(t.commission)} · 订单 #${t.order_id}`,
    }));
    return [...decisions, ...orders, ...trades].sort((a, b) => String(b.at).localeCompare(String(a.at)));
  }, [today]);

  const ledgerMeta =
    ledgerTab === "today"
      ? `${tradesToday.length} 笔 · ${today?.date || ""}`
      : ledgerTab === "history"
        ? `${tradesHistory.length} 笔`
        : `${settlements.length} 条${
            settlementToday
              ? ` · 今日已清算（日盈亏 ${money(settlementToday.day_pnl)}）`
              : " · 今日尚未清算"
          }`;

  return (
    <>
      <section className="hero-stats">
        <div className="hero-card main">
          <label>总权益</label>
          <div className="hero-num">{money(p?.equity)}</div>
          <div className="hero-sub">现金 {money(p?.cash)} · 初始 {money(p?.initial_cash)}</div>
        </div>
        <div className="hero-card">
          <label>总盈亏</label>
          <div className="hero-num">
            <Pnl value={p?.total_pnl} withPct={p?.total_pnl_pct} />
          </div>
          <div className="hero-sub">相对初始资金</div>
        </div>
        <div className="hero-card">
          <label>今日盈亏</label>
          <div className="hero-num">
            <Pnl value={p?.day_pnl} withPct={p?.day_pnl_pct} />
          </div>
          <div className="hero-sub">相对日初权益 {money(p?.day_start_equity)}</div>
        </div>
        <div className="hero-card">
          <label>浮动盈亏</label>
          <div className="hero-num">
            <Pnl value={p?.unrealized_pnl} />
          </div>
          <div className="hero-sub">持仓市值相对成本</div>
        </div>
      </section>

      <div className="desk-grid">
        <section className="panel">
          <h2>
            今日决策 / 订单 / 成交
            <span>
              {feed.length} 条 ·{" "}
              <button type="button" className="linkish" onClick={onRefresh} disabled={busy}>
                {busy ? "刷新中" : "刷新"}
              </button>
            </span>
          </h2>
          {feed.length === 0 ? (
            <div className="empty">今天还没有决策或成交。</div>
          ) : (
            <div className="feed">
              {feed.map((item) => {
                const clickable = item.kind === "decision" && item.decisionId;
                return (
                  <article
                    className={`feed-item ${item.kind}${clickable ? " clickable" : ""}`}
                    key={item.id}
                    onClick={
                      clickable
                        ? () => onOpenDecision?.(item.decisionId)
                        : undefined
                    }
                    onKeyDown={
                      clickable
                        ? (e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              onOpenDecision?.(item.decisionId);
                            }
                          }
                        : undefined
                    }
                    role={clickable ? "button" : undefined}
                    tabIndex={clickable ? 0 : undefined}
                  >
                    <div className="feed-top">
                      <strong>{item.title}</strong>
                      <time>{when(item.at)}</time>
                    </div>
                    <p>{item.body}</p>
                    {item.meta ? <p className="muted">{item.meta}</p> : null}
                    {item.kind === "decision" ? (
                      <p className="muted go-detail">
                        查看详情
                        {item.invocationCount
                          ? ` · ${item.invocationCount} 次接口调用`
                          : ""}
                        {item.agentRunId ? ` · run #${item.agentRunId}` : ""} →
                      </p>
                    ) : null}
                    {item.kind === "order" && item.decisionId ? (
                      <button
                        type="button"
                        className="linkish inv-toggle"
                        onClick={(e) => {
                          e.stopPropagation();
                          onOpenDecision?.(item.decisionId);
                        }}
                      >
                        查看关联决策 #{item.decisionId} →
                      </button>
                    ) : null}
                  </article>
                );
              })}
            </div>
          )}
          {openCrHint > 0 ? (
            <p className="muted tip">有 {openCrHint} 条待处理变更单，可在「演进」页查看。</p>
          ) : null}
        </section>

        <section className="panel">
          <h2>
            持仓
            <span>
              {(p?.positions || []).length} 只 · {today?.date || ""}
            </span>
          </h2>
          {(p?.positions || []).length === 0 ? (
            <div className="empty">暂无持仓，全现金。</div>
          ) : (
            <table className="pos-table">
              <thead>
                <tr>
                  <th>代码</th>
                  <th>名称</th>
                  <th>数量</th>
                  <th>可卖</th>
                  <th>成本</th>
                  <th>现价</th>
                  <th>市值</th>
                  <th>盈亏</th>
                </tr>
              </thead>
              <tbody>
                {p.positions.map((row) => (
                  <tr key={row.symbol}>
                    <td className="sym">{row.symbol}</td>
                    <td>{row.name || "—"}</td>
                    <td>{row.quantity}</td>
                    <td>{row.sellable_quantity ?? "—"}</td>
                    <td>{money(row.avg_cost)}</td>
                    <td>{money(row.mark_price)}</td>
                    <td>{money(row.market_value)}</td>
                    <td>
                      <Pnl value={row.unrealized_pnl} withPct={row.unrealized_pnl_pct} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      <section className="panel ledger-panel">
        <h2>
          成交与清算
          <span>
            {ledgerMeta}
            {" · "}
            <button type="button" className="linkish" onClick={onRefresh} disabled={busy}>
              {busy ? "刷新中" : "刷新"}
            </button>
          </span>
        </h2>
        <div className="tabs" role="tablist">
          {[
            { id: "today", label: "当日成交" },
            { id: "history", label: "历史成交" },
            { id: "settlements", label: "每日清算" },
          ].map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={ledgerTab === tab.id}
              className={`tab ${ledgerTab === tab.id ? "active" : ""}`}
              onClick={() => setLedgerTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>
        <div className="tab-body">
          {ledgerTab === "today" ? (
            <TradeTable trades={tradesToday} emptyText="今天还没有成交。" />
          ) : null}
          {ledgerTab === "history" ? (
            <TradeTable trades={tradesHistory} emptyText="暂无历史成交。" />
          ) : null}
          {ledgerTab === "settlements" ? (
            <SettlementTable settlements={settlements} todayDate={today?.date} />
          ) : null}
        </div>
      </section>
    </>
  );
}

function BackBar({ label, onBack }) {
  return (
    <div className="back-bar">
      <button type="button" className="linkish" onClick={onBack}>
        ← {label}
      </button>
    </div>
  );
}

function DecisionsListPage({ token, onOpen, onBack }) {
  const [items, setItems] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const data = await api("/admin/decisions?limit=200", { token });
      setItems(data.items || []);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }, [token]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="panel detail-panel">
      <BackBar label="返回演进" onBack={onBack} />
      <h2>
        全部决策
        <span>
          {items.length} 条 ·{" "}
          <button type="button" className="linkish" onClick={load} disabled={busy}>
            {busy ? "刷新中" : "刷新"}
          </button>
        </span>
      </h2>
      {error ? <div className="flash">{error}</div> : null}
      {items.length === 0 && !busy ? (
        <div className="empty">还没有决策记录。</div>
      ) : (
        <div className="record-list">
          {items.map((d) => (
            <button
              type="button"
              className="record-row"
              key={d.id}
              onClick={() => onOpen(d.id)}
            >
              <div className="record-top">
                <strong>决策 #{d.id}</strong>
                <time>
                  {d.trade_date || ""} · {when(d.created_at)}
                </time>
              </div>
              <p>{clip(d.summary || d.preview, 160)}</p>
              <p className="muted">
                {d.usage_note_count ? `心得 ${d.usage_note_count} · ` : ""}
                {d.agent_run_id ? `run #${d.agent_run_id} · ` : ""}
                查看详情 →
              </p>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

function DecisionDetailPage({ token, decisionId, onBack }) {
  const [row, setRow] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const data = await api(`/admin/decisions/${decisionId}`, { token });
      setRow(data);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }, [token, decisionId]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="panel detail-panel">
      <BackBar label="返回决策列表" onBack={onBack} />
      {error ? <div className="flash">{error}</div> : null}
      {busy && !row ? <div className="empty">加载中…</div> : null}
      {row ? (
        <>
          <h2>
            决策 #{row.id}
            <span>
              {row.trade_date || ""} · {when(row.created_at)}
              {row.agent_run_id ? ` · run #${row.agent_run_id}` : ""}
            </span>
          </h2>
          <div className="detail-block">
            <h3>摘要</h3>
            <p className="prose">{row.summary || "—"}</p>
          </div>
          {row.hypothesis ? (
            <div className="detail-block">
              <h3>假设</h3>
              <p className="prose">{row.hypothesis}</p>
            </div>
          ) : null}
          {row.action_plan ? (
            <div className="detail-block">
              <h3>行动计划</h3>
              <p className="prose">{row.action_plan}</p>
            </div>
          ) : null}
          <div className="detail-block">
            <h3>使用心得（{row.usage_notes?.length || 0}）</h3>
            {(row.usage_notes || []).length === 0 ? (
              <div className="empty">本轮无 usage_notes。</div>
            ) : (
              <ul className="usage-notes">
                {(row.usage_notes || []).map((note, i) => (
                  <li key={`note-${i}`}>
                    <strong>
                      [{note.kind || "other"}
                      {note.capability_id ? ` · ${note.capability_id}` : ""}]
                    </strong>{" "}
                    {note.content}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="detail-block">
            <h3>关联订单（{row.orders?.length || 0}）</h3>
            {(row.orders || []).length === 0 ? (
              <div className="empty">本决策未下单。</div>
            ) : (
              <table className="pos-table trade-table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>方向</th>
                    <th>代码</th>
                    <th>数量</th>
                    <th>状态</th>
                    <th>成交价</th>
                  </tr>
                </thead>
                <tbody>
                  {row.orders.map((o) => (
                    <tr key={o.id}>
                      <td>{when(o.created_at)}</td>
                      <td className={o.side === "buy" ? "up" : "down"}>
                        {SIDE_LABEL[o.side] || o.side}
                      </td>
                      <td className="sym">{o.symbol}</td>
                      <td>{o.quantity}</td>
                      <td>{o.status}</td>
                      <td>{o.price != null ? money(o.price) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          <div className="detail-block">
            <h3>接口调用（{row.invocations?.length || 0}）</h3>
            <InvocationList invocations={row.invocations || []} />
          </div>
        </>
      ) : null}
    </section>
  );
}

function ReviewsListPage({ token, onOpen, onBack }) {
  const [items, setItems] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const data = await api("/admin/reviews?limit=200", { token });
      setItems(data.items || []);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }, [token]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="panel detail-panel">
      <BackBar label="返回演进" onBack={onBack} />
      <h2>
        全部复盘
        <span>
          {items.length} 条 ·{" "}
          <button type="button" className="linkish" onClick={load} disabled={busy}>
            {busy ? "刷新中" : "刷新"}
          </button>
        </span>
      </h2>
      {error ? <div className="flash">{error}</div> : null}
      {items.length === 0 && !busy ? (
        <div className="empty">还没有复盘记录。</div>
      ) : (
        <div className="record-list">
          {items.map((r) => (
            <button
              type="button"
              className="record-row"
              key={r.id}
              onClick={() => onOpen(r.id)}
            >
              <div className="record-top">
                <strong>
                  复盘 #{r.id}
                  <span className="badge soft">
                    {ISSUE_LABEL[r.issue_type] || r.issue_type}
                  </span>
                </strong>
                <time>
                  {r.trade_date || ""} · {when(r.created_at)}
                </time>
              </div>
              <p>{clip(r.preview || r.content, 180)}</p>
              <p className="muted">
                {r.change_request_id ? `CR #${r.change_request_id} · ` : ""}
                查看全文 →
              </p>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

function ReviewDetailPage({ token, reviewId, onBack, onOpenDecision, onOpenCr }) {
  const [row, setRow] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const data = await api(`/admin/reviews/${reviewId}`, { token });
      setRow(data);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }, [token, reviewId]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="panel detail-panel">
      <BackBar label="返回复盘列表" onBack={onBack} />
      {error ? <div className="flash">{error}</div> : null}
      {busy && !row ? <div className="empty">加载中…</div> : null}
      {row ? (
        <>
          <h2>
            复盘 #{row.id}
            <span>
              {ISSUE_LABEL[row.issue_type] || row.issue_type} · {row.trade_date || ""} ·{" "}
              {when(row.created_at)}
            </span>
          </h2>
          <div className="detail-meta">
            {row.decision_id ? (
              <button
                type="button"
                className="linkish"
                onClick={() => onOpenDecision?.(row.decision_id)}
              >
                关联决策 #{row.decision_id}
              </button>
            ) : null}
            {row.change_request_id ? (
              <button
                type="button"
                className="linkish"
                onClick={() => onOpenCr?.()}
              >
                关联变更单 #{row.change_request_id}
              </button>
            ) : null}
            {row.agent_run_id ? (
              <span className="muted">run #{row.agent_run_id}</span>
            ) : null}
          </div>
          <div className="detail-block">
            <h3>复盘正文</h3>
            <pre className="review-body">{row.content || "—"}</pre>
          </div>
        </>
      ) : null}
    </section>
  );
}

function EvolutionPage({ token, crs, memory, onRefresh, onOpenDecisions, onOpenReviews }) {
  const [rejectingId, setRejectingId] = useState(null);
  const [rejectReason, setRejectReason] = useState("");
  const [error, setError] = useState("");
  const inv = memory?.investment_memory || {};
  const lessons = inv.recent_experiences || [];
  const openCount = crs.filter((c) =>
    ["proposed", "pending_dev", "completed", "approved"].includes(c.status),
  ).length;

  async function run(fn) {
    setError("");
    try {
      await fn();
      await onRefresh();
    } catch (e) {
      setError(String(e.message || e));
    }
  }

  return (
    <div className="evo-page">
      {error && !rejectingId ? <div className="flash">{error}</div> : null}
      <div className="sub-grid">
      <section className="panel">
        <h2>
          变更单
          <span>{openCount} 待跟进 / {crs.length} 全部</span>
        </h2>
        {crs.length === 0 ? (
          <div className="empty">还没有变更请求。</div>
        ) : (
          crs.map((cr) => (
            <article className="cr" key={cr.id}>
              <div className="cr-head">
                <h3>
                  #{cr.id} {cr.title}
                </h3>
                <span className={`badge ${cr.status}`}>{STATUS_LABEL[cr.status] || cr.status}</span>
              </div>
              <p className="muted">{cr.issue_type}</p>
              <p>{cr.problem}</p>
              {cr.proposal ? <p className="muted">建议：{cr.proposal}</p> : null}
              {cr.review_notes ? <p className="muted">备注：{cr.review_notes}</p> : null}
              {cr.verification_notes ? <p className="muted">验收：{cr.verification_notes}</p> : null}

              {cr.status === "proposed" ? (
                <div className="actions">
                  <button
                    type="button"
                    className="primary"
                    onClick={() =>
                      run(() =>
                        api(`/admin/change-requests/${cr.id}/approve`, {
                          token,
                          method: "POST",
                          body: { notes: "监督台审批通过" },
                        }),
                      )
                    }
                  >
                    通过，进入待开发
                  </button>
                  <button
                    type="button"
                    className="ghost-danger"
                    onClick={() => {
                      setError("");
                      setRejectingId(cr.id);
                    }}
                  >
                    不做
                  </button>
                </div>
              ) : null}

              {cr.status === "pending_dev" ? (
                <div className="actions">
                  <button
                    type="button"
                    className="primary"
                    onClick={() =>
                      run(() =>
                        api(`/admin/change-requests/${cr.id}/implement`, {
                          token,
                          method: "POST",
                          body: { notes: "监督台标记开发完成" },
                        }),
                      )
                    }
                  >
                    开发完成
                  </button>
                  <button
                    type="button"
                    className="ghost-danger"
                    onClick={() => {
                      setError("");
                      setRejectingId(cr.id);
                    }}
                  >
                    不做
                  </button>
                </div>
              ) : null}

              {rejectingId === cr.id ? (
                <div className="reject-box">
                  <label className="muted" htmlFor={`reject-${cr.id}`}>
                    不做理由（可选）
                  </label>
                  <textarea
                    id={`reject-${cr.id}`}
                    value={rejectReason}
                    onChange={(e) => setRejectReason(e.target.value)}
                    placeholder="可选：例如本期优先级不够，先稳住交易闭环。"
                  />
                  <div className="actions">
                    <button
                      type="button"
                      className="primary"
                      onClick={() => {
                        run(() =>
                          api(`/admin/change-requests/${cr.id}/reject`, {
                            token,
                            method: "POST",
                            body: { notes: rejectReason.trim() || null },
                          }),
                        ).then(() => {
                          setRejectingId(null);
                          setRejectReason("");
                        });
                      }}
                    >
                      确认不做
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setRejectingId(null);
                        setError("");
                      }}
                    >
                      取消
                    </button>
                  </div>
                </div>
              ) : null}
            </article>
          ))
        )}
      </section>

      <section className="panel">
        <h2>记忆与经验</h2>
        <div className="mem-grid">
          <button type="button" className="mem-cell clickable" onClick={onOpenDecisions}>
            <span className="muted">决策</span>
            <b>{inv.decision_count ?? 0}</b>
            <span className="mem-go">查看全部 →</span>
          </button>
          <button type="button" className="mem-cell clickable" onClick={onOpenReviews}>
            <span className="muted">复盘</span>
            <b>{inv.review_count ?? 0}</b>
            <span className="mem-go">查看全部 →</span>
          </button>
        </div>
        <h3 className="muted">近期经验</h3>
        {lessons.length === 0 ? (
          <div className="empty">暂无沉淀经验</div>
        ) : (
          <ul className="lessons">
            {lessons.map((e) => (
              <li key={e.id}>{e.content}</li>
            ))}
          </ul>
        )}
      </section>
      </div>
    </div>
  );
}

export default function App() {
  // route: desk | evolution | decisions | decision | reviews | review
  const [route, setRoute] = useState({ name: "desk" });
  const [token, setToken] = useState(
    () => localStorage.getItem("evo_admin_token") || "dev-admin-token",
  );
  const [desk, setDesk] = useState(null);
  const [crs, setCrs] = useState([]);
  const [memory, setMemory] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const saveToken = () => localStorage.setItem("evo_admin_token", token);
  const pageName = route.name;
  const navActive =
    pageName === "desk"
      ? "desk"
      : pageName === "decisions" || (pageName === "decision" && route.from !== "desk")
        ? "decisions"
        : pageName === "decision" && route.from === "desk"
          ? "desk"
          : pageName === "reviews" || pageName === "review"
            ? "reviews"
            : pageName === "evolution"
              ? "evolution"
              : "desk";

  const refresh = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      if (pageName === "desk") {
        const d = await api("/admin/desk", { token });
        const c = await api("/admin/change-requests", { token });
        const open = (c.items || []).filter((x) =>
          ["proposed", "pending_dev", "completed", "approved"].includes(x.status),
        ).length;
        setDesk({ ...d, open_cr_count: open });
        setCrs(c.items || []);
      } else if (pageName === "evolution") {
        const [c, m] = await Promise.all([
          api("/admin/change-requests", { token }),
          api("/admin/memory", { token }),
        ]);
        setCrs(c.items || []);
        setMemory(m);
      }
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }, [token, pageName]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const goDesk = () => setRoute({ name: "desk" });
  const goEvolution = () => setRoute({ name: "evolution" });
  const goDecisions = () => setRoute({ name: "decisions" });
  const goReviews = () => setRoute({ name: "reviews" });
  const openDecision = (id) => setRoute({ name: "decision", id, from: pageName });
  const openReview = (id) => setRoute({ name: "review", id, from: pageName });

  let body = null;
  if (pageName === "desk") {
    body = (
      <DeskPage
        desk={desk}
        busy={busy}
        onRefresh={refresh}
        onOpenDecision={openDecision}
      />
    );
  } else if (pageName === "evolution") {
    body = (
      <EvolutionPage
        token={token}
        crs={crs}
        memory={memory}
        onRefresh={refresh}
        onOpenDecisions={goDecisions}
        onOpenReviews={goReviews}
      />
    );
  } else if (pageName === "decisions") {
    body = (
      <DecisionsListPage
        token={token}
        onOpen={openDecision}
        onBack={goEvolution}
      />
    );
  } else if (pageName === "decision") {
    body = (
      <DecisionDetailPage
        token={token}
        decisionId={route.id}
        onBack={() =>
          setRoute({
            name: route.from === "desk" ? "desk" : "decisions",
          })
        }
      />
    );
  } else if (pageName === "reviews") {
    body = (
      <ReviewsListPage token={token} onOpen={openReview} onBack={goEvolution} />
    );
  } else if (pageName === "review") {
    body = (
      <ReviewDetailPage
        token={token}
        reviewId={route.id}
        onBack={() => setRoute({ name: "reviews" })}
        onOpenDecision={openDecision}
        onOpenCr={goEvolution}
      />
    );
  }

  return (
    <div className="shell">
      <header className="masthead">
        <div>
          <p className="kicker">Paper Desk</p>
          <h1>Evo 监督台</h1>
          <p className="sub">持仓 · 盈亏 · 决策与复盘</p>
        </div>
        <nav className="nav">
          <button
            type="button"
            className={navActive === "desk" ? "nav-active" : ""}
            onClick={goDesk}
          >
            台面
          </button>
          <button
            type="button"
            className={navActive === "decisions" ? "nav-active" : ""}
            onClick={goDecisions}
          >
            决策
          </button>
          <button
            type="button"
            className={navActive === "reviews" ? "nav-active" : ""}
            onClick={goReviews}
          >
            复盘
          </button>
          <button
            type="button"
            className={navActive === "evolution" ? "nav-active" : ""}
            onClick={goEvolution}
          >
            演进
          </button>
        </nav>
        <div className="tools">
          <input
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Admin token"
            aria-label="Admin token"
          />
          <button type="button" onClick={saveToken}>
            保存
          </button>
          <button type="button" className="primary" onClick={refresh} disabled={busy}>
            {busy ? "…" : "刷新"}
          </button>
        </div>
      </header>

      {error ? <div className="flash">{error}</div> : null}
      {body}
    </div>
  );
}
