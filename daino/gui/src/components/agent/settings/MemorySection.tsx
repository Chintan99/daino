import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../../api/client";
import { qk, useMemory } from "../../../api/hooks";
import type { AgentConfig } from "../../../api/types";

/**
 * Durable memory — the browser's `/memory`.
 *
 * A fact you state yourself is stored as an authoritative `user` memory, which
 * is the one class the agent's own extraction cannot grant itself. Everything
 * else here is inspection: what is remembered, where it came from, and whether
 * it still matches its source.
 */
const TYPES = ["", "user", "semantic", "decision", "failure", "episode", "procedural"];

export function MemorySection({
  config,
  sessionId,
}: {
  config: AgentConfig;
  sessionId: string;
}) {
  const qc = useQueryClient();
  const [query, setQuery] = useState("");
  const [applied, setApplied] = useState("");
  const [type, setType] = useState("");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const { data, isFetching } = useMemory({ q: applied, type });
  const items = data?.items ?? [];

  const refresh = async () => {
    await qc.invalidateQueries({ queryKey: ["agent", "memory"] });
    await qc.invalidateQueries({ queryKey: qk.agentConfig(sessionId) });
  };

  const run = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label);
    setError("");
    try {
      await action();
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="cfg-section">
      <div className="section-title">Remember something</div>
      <div className="muted field-hint">
        Stated by you, so it is stored as authoritative and needs no review.
      </div>
      <textarea
        className="input"
        rows={3}
        value={draft}
        placeholder="Deploys go out on Thursdays. The staging DB is reset nightly."
        onChange={(e) => setDraft(e.target.value)}
      />
      <div className="provider-actions">
        <span className="grow" />
        <button
          className="btn primary"
          disabled={busy !== "" || !draft.trim()}
          onClick={() =>
            void run("add", async () => {
              await api.remember({ content: draft.trim() });
              setDraft("");
            })
          }
        >
          {busy === "add" ? "Saving…" : "Remember"}
        </button>
      </div>

      <div className="section-title">
        Stored memory
        {config.memory.total > 0 ? ` · ${config.memory.total}` : ""}
        {!config.memory.enabled && " · disabled in config"}
      </div>
      <div className="row" style={{ gap: 6 }}>
        <input
          className="input"
          value={query}
          placeholder="Search memory…  (Enter)"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") setApplied(query.trim());
          }}
        />
        <select
          className="input"
          style={{ width: 120 }}
          value={type}
          onChange={(e) => setType(e.target.value)}
        >
          {TYPES.map((item) => (
            <option key={item || "all"} value={item}>
              {item || "all types"}
            </option>
          ))}
        </select>
      </div>

      {isFetching && <div className="empty">Loading…</div>}
      {!isFetching && items.length === 0 && (
        <div className="empty">
          {applied ? "Nothing matches that." : "Nothing remembered yet."}
        </div>
      )}

      {items.map((item) => (
        <div className="mem-row" key={item.id}>
          <div className="row" style={{ gap: 6 }}>
            <span className="badge info">{item.type}</span>
            <span className="badge">{item.scope}</span>
            {item.status !== "active" && <span className="badge warn">{item.status}</span>}
            <span className="grow" />
            <span className="muted mono">{item.confidence.toFixed(2)}</span>
          </div>
          <div className="mem-content">{item.summary || item.content}</div>
          <div className="mem-meta mono">
            {item.source || item.source_type}
            {item.created_at ? ` · ${new Date(item.created_at).toLocaleDateString()}` : ""}
          </div>
          <div className="row" style={{ gap: 6 }}>
            <button
              className="btn subtle sm"
              disabled={busy !== ""}
              title="Re-check this against its current source"
              onClick={() => void run(item.id, () => api.verifyMemory(item.id))}
            >
              Verify
            </button>
            <button
              className="btn subtle sm"
              disabled={busy !== ""}
              onClick={() => {
                if (window.confirm("Forget this memory?"))
                  void run(item.id, () => api.forgetMemory(item.id));
              }}
            >
              Forget
            </button>
          </div>
        </div>
      ))}

      <div className="provider-actions">
        <button
          className="btn danger sm"
          disabled={busy !== ""}
          onClick={() => {
            if (window.confirm("Clear every project memory? This cannot be undone."))
              void run("clear", async () => {
                const answer = await api.clearMemory("project");
                window.alert(`Cleared ${answer.cleared} project memory item(s).`);
              });
          }}
        >
          Clear project memory
        </button>
      </div>

      {error && <div className="test-result bad">{error}</div>}
    </div>
  );
}
