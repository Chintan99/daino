import { useEffect, useState } from "react";
import { useMissionDetails, useMissions } from "../../api/hooks";
import { Metric } from "./Metric";
import { fmtDateTime, prettyStatus, statusClass } from "./format";

const TASK_MARK: Record<string, string> = {
  completed: "✓",
  running: "→",
  failed: "✗",
};

export function MissionsView() {
  const { data, isLoading } = useMissions();
  const missions = data?.missions ?? [];
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    if (missions.length === 0) {
      if (selected) setSelected(null);
      return;
    }
    if (!selected || !missions.some((m) => m.id === selected))
      setSelected(missions[0].id);
  }, [missions, selected]);

  const { data: details } = useMissionDetails(selected);
  const mission = details?.mission as Record<string, unknown> | undefined;

  return (
    <div className="split">
      <div className="split-left" style={{ width: 380 }}>
        <div className="panel-header">
          Missions
          <span className="spacer" />
          <span className="muted">{missions.length}</span>
        </div>
        <div className="scroll-y" style={{ flex: 1 }}>
          {isLoading && <div className="empty">Loading…</div>}
          {!isLoading && missions.length === 0 && (
            <div className="empty">
              No missions yet. Missions are recorded whenever the agent plans and
              executes work.
            </div>
          )}
          {missions.length > 0 && (
            <table className="dtable">
              <thead>
                <tr>
                  <th>Updated</th>
                  <th>Title</th>
                  <th>Mode</th>
                </tr>
              </thead>
              <tbody>
                {missions.map((m) => (
                  <tr
                    key={m.id}
                    className={`click ${selected === m.id ? "active" : ""}`}
                    onClick={() => setSelected(m.id)}
                  >
                    <td className="mono" style={{ whiteSpace: "nowrap" }}>
                      {fmtDateTime(m.updated_at)}
                    </td>
                    <td className="ellipsis" title={m.title}>
                      {m.title}
                      <div>
                        <span className={statusClass(m.status)}>
                          {prettyStatus(m.status)}
                        </span>
                      </div>
                    </td>
                    <td className="muted">{m.mode}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="split-right">
        {!details && <div className="empty">Select a mission to inspect it.</div>}
        {details && mission && (
          <div className="scroll-y" style={{ flex: 1 }}>
            <div style={{ padding: "12px 14px 0", color: "var(--text-0)" }}>
              {String(mission.request ?? "")}
            </div>
            <div className="metric-row">
              <Metric k="Status" v={prettyStatus(String(mission.status ?? ""))} />
              <Metric k="Branch" v={String(mission.branch || "not created")} />
              <Metric k="Tasks" v={details.tasks.length} />
              <Metric k="Tools" v={details.tools.length} />
              <Metric k="Test runs" v={details.tests.length} />
              <Metric k="Reviews" v={details.reviews.length} />
              <Metric k="Approvals" v={details.approvals.length} />
              <Metric k="Checkpoints" v={details.checkpoints.length} />
            </div>

            <div className="section-title">Tasks</div>
            {details.tasks.length === 0 ? (
              <div className="empty">No tasks persisted.</div>
            ) : (
              <table className="dtable">
                <thead>
                  <tr>
                    <th style={{ width: 30 }} />
                    <th>Task</th>
                    <th>Status</th>
                    <th>Risk</th>
                  </tr>
                </thead>
                <tbody>
                  {details.tasks.map((t) => (
                    <tr key={t.id}>
                      <td
                        style={{
                          color:
                            t.status === "completed"
                              ? "var(--green)"
                              : t.status === "failed"
                                ? "var(--red)"
                                : "var(--text-3)",
                        }}
                      >
                        {TASK_MARK[t.status] ?? "○"}
                      </td>
                      <td>{t.title}</td>
                      <td>
                        <span className={statusClass(t.status)}>
                          {prettyStatus(t.status)}
                        </span>
                      </td>
                      <td className="muted">{t.risk_level}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <div className="section-title">Tool calls</div>
            {details.tools.length === 0 ? (
              <div className="empty">No tool calls recorded.</div>
            ) : (
              <table className="dtable">
                <thead>
                  <tr>
                    <th>Tool</th>
                    <th>Summary</th>
                    <th>Result</th>
                    <th>Time</th>
                  </tr>
                </thead>
                <tbody>
                  {details.tools.map((t, i) => (
                    <tr key={i}>
                      <td className="mono" style={{ color: "var(--cyan)" }}>
                        {t.tool}
                      </td>
                      <td className="ellipsis" title={t.summary}>
                        {t.summary}
                      </td>
                      <td>
                        <span
                          className={statusClass(t.success ? "passed" : "failed")}
                        >
                          {t.success ? "OK" : "FAILED"}
                        </span>
                      </td>
                      <td className="num">{(t.duration ?? 0).toFixed(2)}s</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <div className="section-title">Workspace</div>
            <div className="md-block mono" style={{ fontSize: 12 }}>
              {String(mission.workspace_path || "not created")}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
