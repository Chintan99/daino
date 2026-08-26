import { useEffect, useState } from "react";
import { useMapPrompts, useMapTrace } from "../../api/hooks";
import type { TraceStep } from "../../api/types";
import { Metric } from "./Metric";
import {
  fmtCost,
  fmtDateTime,
  fmtDuration,
  fmtTime,
  fmtTokens,
  prettyStatus,
  statusClass,
} from "./format";

const KNOWN_KINDS = ["model", "tool", "tests", "verification", "file"];

function stepClass(step: TraceStep): string {
  const failed = ["failed", "error", "blocked"].includes(
    step.status.toLowerCase(),
  );
  const kind = KNOWN_KINDS.includes(step.kind.toLowerCase())
    ? step.kind.toLowerCase()
    : "other";
  return `map-step kind-${kind}${failed ? " failed" : ""}`;
}

function Step({ step, index }: { step: TraceStep; index: number }) {
  const usage = step.model_usage;
  return (
    <div className={stepClass(step)}>
      <div className="body">
        <div>
          <span className="idx">{String(index).padStart(2, "0")}</span>{" "}
          <span className="title">
            {step.kind.toUpperCase()} {step.title}
          </span>{" "}
          <span className="meta">
            [{step.status.toUpperCase()}] {fmtTime(step.timestamp)}
          </span>
        </div>
        {step.detail && <div className="detail">{step.detail}</div>}
        {step.target && step.target !== step.detail && (
          <div className="detail">target {step.target}</div>
        )}
        <div className="meta">
          {usage
            ? `tokens ${fmtTokens(usage.input_tokens)} in + ${fmtTokens(
                usage.output_tokens,
              )} out = ${fmtTokens(usage.total_tokens)} · ${fmtCost(
                usage.estimated_cost,
              )} · ${(usage.latency_ms / 1000).toFixed(2)}s`
            : `tokens — · ${fmtDuration(step.duration_seconds)}`}
        </div>
      </div>
    </div>
  );
}

export function ExecutionMapView() {
  const { data, isLoading } = useMapPrompts();
  const prompts = data?.prompts ?? [];
  const [selected, setSelected] = useState<string | null>(null);

  // Keep a selection that still exists; default to the newest prompt.
  useEffect(() => {
    if (prompts.length === 0) {
      if (selected) setSelected(null);
      return;
    }
    if (!selected || !prompts.some((p) => p.mission_id === selected))
      setSelected(prompts[0].mission_id);
  }, [prompts, selected]);

  const { data: trace } = useMapTrace(selected);

  return (
    <div className="split">
      <div className="split-left">
        <div className="panel-header">
          Prompts
          <span className="spacer" />
          <span className="muted">{prompts.length}</span>
        </div>
        <div className="scroll-y" style={{ flex: 1 }}>
          {isLoading && <div className="empty">Loading…</div>}
          {!isLoading && prompts.length === 0 && (
            <div className="empty">
              No prompts recorded yet. Send one to the agent and its audited
              execution graph appears here.
            </div>
          )}
          {prompts.length > 0 && (
            <table className="dtable">
              <thead>
                <tr>
                  <th>Started</th>
                  <th>Prompt</th>
                  <th>Tokens</th>
                  <th>Steps</th>
                </tr>
              </thead>
              <tbody>
                {prompts.map((p) => (
                  <tr
                    key={p.mission_id}
                    className={`click ${selected === p.mission_id ? "active" : ""}`}
                    onClick={() => setSelected(p.mission_id)}
                  >
                    <td className="mono" style={{ whiteSpace: "nowrap" }}>
                      {fmtDateTime(p.created_at)}
                    </td>
                    <td className="ellipsis" title={p.request}>
                      {p.title}
                      <div>
                        <span className={statusClass(p.status)}>
                          {prettyStatus(p.status)}
                        </span>
                      </div>
                    </td>
                    <td className="num">{fmtTokens(p.total_tokens)}</td>
                    <td className="num">{p.step_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="split-right">
        {!trace && <div className="empty">Select a prompt to inspect it.</div>}
        {trace && (
          <>
            <div style={{ padding: "12px 14px 0" }}>
              <div style={{ color: "var(--text-0)", fontSize: 13 }}>
                {trace.request}
              </div>
              <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>
                Built from structured audit events — never private chain of
                thought.
              </div>
            </div>
            <div className="metric-row">
              <Metric k="Status" v={prettyStatus(trace.status)} />
              <Metric k="Steps" v={trace.steps.length} />
              <Metric k="Model calls" v={trace.model_call_count} />
              <Metric k="Tools" v={trace.tool_count} />
              <Metric k="Tokens" v={fmtTokens(trace.total_tokens)} />
              <Metric k="Cost" v={fmtCost(trace.estimated_cost)} />
              <Metric
                k="Model time"
                v={`${(trace.total_model_latency_ms / 1000).toFixed(1)}s`}
              />
              <Metric
                k="Tool time"
                v={fmtDuration(trace.total_tool_duration_seconds)}
              />
            </div>
            <div className="scroll-y" style={{ flex: 1 }}>
              <div className="map-graph">
                <div style={{ color: "var(--cyan)", fontWeight: 600 }}>
                  ● PROMPT {fmtTime(trace.created_at)}
                </div>
                {trace.steps.length === 0 && (
                  <div className="muted" style={{ paddingLeft: 14 }}>
                    No execution steps recorded.
                  </div>
                )}
                {trace.steps.map((step, i) => (
                  <Step key={step.id || i} step={step} index={i + 1} />
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
