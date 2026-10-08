import React, { useCallback, useEffect, useMemo, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || "";

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

export default function App() {
  const [token, setToken] = useState(
    () => localStorage.getItem("evo_admin_token") || "dev-admin-token",
  );
  const [timeline, setTimeline] = useState(null);
  const [portfolio, setPortfolio] = useState(null);
  const [crs, setCrs] = useState([]);
  const [memory, setMemory] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [implDraft, setImplDraft] = useState({});

  const saveToken = () => {
    localStorage.setItem("evo_admin_token", token);
  };

  const refresh = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const [t, p, c, m] = await Promise.all([
        api("/admin/timeline", { token }),
        api("/admin/portfolio", { token }),
        api("/admin/change-requests", { token }),
        api("/admin/memory", { token }),
      ]);
      setTimeline(t);
      setPortfolio(p);
      setCrs(c.items || []);
      setMemory(m);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }, [token]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const equity = useMemo(() => portfolio?.equity ?? 0, [portfolio]);

  async function approve(id) {
    await api(`/admin/change-requests/${id}/approve`, {
      token,
      method: "POST",
      body: { notes: "监督台审批通过" },
    });
    await refresh();
  }

  async function reject(id) {
    await api(`/admin/change-requests/${id}/reject`, {
      token,
      method: "POST",
      body: { notes: "监督台驳回" },
    });
    await refresh();
  }

  async function implement(id) {
    const draft = implDraft[id] || {};
    const capability_id = draft.capability_id || `cap_${id}`;
    await api(`/admin/change-requests/${id}/implement`, {
      token,
      method: "POST",
      body: {
        capability_id,
        name: draft.name || capability_id,
        description: draft.description || "人工实现后登记的能力",
        implementation: "noop",
        notes: "已通过监督台标记为已实现",
      },
    });
    await refresh();
  }

  return (
    <>
      <header>
        <div>
          <h1>Evo · 自演进投资系统监督台</h1>
          <p>决策时间线 · 模拟组合 · Change Request 审批</p>
        </div>
        <div className="token-row">
          <input
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Admin token"
            style={{ width: 180 }}
          />
          <button onClick={saveToken}>保存 Token</button>
          <button className="primary" onClick={refresh} disabled={busy}>
            {busy ? "刷新中…" : "刷新"}
          </button>
        </div>
      </header>
      <main>
        {error ? <div className="error">{error}</div> : null}
        <div className="grid">
          <section className="card">
            <h2>组合（纸面）</h2>
            {portfolio ? (
              <>
                <p>
                  现金 {portfolio.cash?.toFixed?.(2) ?? portfolio.cash} · 权益{" "}
                  {Number(equity).toFixed(2)} {portfolio.currency}
                </p>
                <div className="list">
                  {(portfolio.positions || []).length === 0 ? (
                    <div className="muted">暂无持仓</div>
                  ) : (
                    portfolio.positions.map((p) => (
                      <div className="item" key={p.symbol}>
                        <strong>{p.symbol}</strong>
                        <span className="muted">
                          qty {p.quantity} · cost {p.avg_cost} · mark {p.mark_price}
                        </span>
                      </div>
                    ))
                  )}
                </div>
              </>
            ) : (
              <div className="muted">加载中…</div>
            )}
          </section>

          <section className="card">
            <h2>Memory 摘要</h2>
            {memory ? (
              <pre className="muted" style={{ whiteSpace: "pre-wrap", margin: 0 }}>
                {JSON.stringify(memory, null, 2)}
              </pre>
            ) : (
              <div className="muted">加载中…</div>
            )}
          </section>
        </div>

        <section className="card">
          <h2>决策时间线</h2>
          <div className="grid">
            <div>
              <h3 className="muted">Agent Runs</h3>
              <div className="list">
                {(timeline?.agent_runs || []).map((r) => (
                  <div className="item" key={r.id}>
                    <strong>
                      #{r.id} <span className="badge">{r.status}</span>
                    </strong>
                    <div className="muted">
                      {r.trigger} · {r.created_at}
                    </div>
                  </div>
                ))}
              </div>
            </div>
            <div>
              <h3 className="muted">Decisions</h3>
              <div className="list">
                {(timeline?.decisions || []).map((d) => (
                  <div className="item" key={d.id}>
                    <strong>#{d.id}</strong>
                    <div>{d.summary}</div>
                    <div className="muted">{d.hypothesis}</div>
                  </div>
                ))}
              </div>
            </div>
            <div>
              <h3 className="muted">Reviews</h3>
              <div className="list">
                {(timeline?.reviews || []).map((r) => (
                  <div className="item" key={r.id}>
                    <strong>
                      #{r.id} <span className="badge warn">{r.issue_type}</span>
                    </strong>
                    <div>{r.content}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>

        <section className="card">
          <h2>Change Requests</h2>
          <div className="list">
            {crs.length === 0 ? (
              <div className="muted">暂无变更请求</div>
            ) : (
              crs.map((cr) => (
                <div className="item" key={cr.id}>
                  <strong>
                    #{cr.id} {cr.title}{" "}
                    <span className={`badge ${cr.status === "implemented" ? "ok" : "warn"}`}>
                      {cr.status}
                    </span>
                  </strong>
                  <div className="muted">{cr.issue_type}</div>
                  <div>{cr.problem}</div>
                  <div className="muted">{cr.proposal}</div>
                  {cr.status === "proposed" ? (
                    <div className="row">
                      <button className="primary" onClick={() => approve(cr.id)}>
                        通过
                      </button>
                      <button onClick={() => reject(cr.id)}>驳回</button>
                    </div>
                  ) : null}
                  {cr.status === "approved" ? (
                    <div className="row">
                      <input
                        placeholder="capability_id"
                        value={implDraft[cr.id]?.capability_id || ""}
                        onChange={(e) =>
                          setImplDraft((s) => ({
                            ...s,
                            [cr.id]: { ...s[cr.id], capability_id: e.target.value },
                          }))
                        }
                      />
                      <input
                        placeholder="name"
                        value={implDraft[cr.id]?.name || ""}
                        onChange={(e) =>
                          setImplDraft((s) => ({
                            ...s,
                            [cr.id]: { ...s[cr.id], name: e.target.value },
                          }))
                        }
                      />
                      <button className="primary" onClick={() => implement(cr.id)}>
                        标记已实现并登记能力
                      </button>
                    </div>
                  ) : null}
                  {cr.implemented_capability_id ? (
                    <div className="muted">capability: {cr.implemented_capability_id}</div>
                  ) : null}
                </div>
              ))
            )}
          </div>
        </section>
      </main>
    </>
  );
}