import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../../api/client";
import { qk } from "../../../api/hooks";
import type { AgentConfig } from "../../../api/types";

/**
 * Session policy: autonomy, reasoning effort, progress detail.
 *
 * These are the browser's `/mode`, `/effort`, and `/verbose`. They are session
 * state, not project configuration — the same conversation carries them into the
 * terminal client, and a new session starts from the project default.
 */
export function SessionSection({
  config,
  sessionId,
}: {
  config: AgentConfig;
  sessionId: string;
}) {
  const qc = useQueryClient();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const refresh = () => qc.invalidateQueries({ queryKey: qk.agentConfig(sessionId) });

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
      <div className="section-title">Autonomy</div>
      <div className="muted field-hint">
        How much D[Ai]NO may do on its own in this conversation.
      </div>
      <div className="choice-list">
        {config.autonomy.options.map((option) => (
          <button
            key={option.id}
            className={`choice ${config.autonomy.mode === option.id ? "active" : ""}`}
            disabled={busy !== ""}
            onClick={() => void run("mode", () => api.setAutonomy(sessionId, option.id))}
          >
            <span className="mark">{config.autonomy.mode === option.id ? "●" : "○"}</span>
            <span className="grow">
              <span className="name">{option.label}</span>
              <span className="detail">{option.hint}</span>
            </span>
          </button>
        ))}
      </div>

      <div className="section-title">Reasoning effort</div>
      <label className="field">
        <select
          className="input"
          value={config.effort.value}
          disabled={busy !== ""}
          onChange={(e) => void run("effort", () => api.setEffort(sessionId, e.target.value))}
        >
          {config.effort.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </label>
      <div className="muted field-hint">
        Applies to the selected model profile for this session. Not every provider
        supports every level — an unsupported one is refused rather than ignored.
      </div>

      <div className="section-title">Progress</div>
      <label className="row check">
        <input
          type="checkbox"
          checked={config.verbose}
          disabled={busy !== ""}
          onChange={(e) =>
            void run("verbose", () => api.setVerbose(sessionId, e.target.checked))
          }
        />
        <span>Detailed live progress</span>
      </label>
      <div className="muted field-hint">
        The server-side `/verbose` setting: how much of a running turn the agent
        reports. Settings ▸ Diagnostics has a separate browser-only event log.
      </div>

      {error && <div className="test-result bad">{error}</div>}
    </div>
  );
}
