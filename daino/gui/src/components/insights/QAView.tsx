import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import { useQAHistory, useQALatest, qk } from "../../api/hooks";
import { api } from "../../api/client";
import type { QACheck, QAReport, QASpecialist } from "../../api/types";
import { Metric } from "./Metric";
import { fmtDateTime, fmtDuration, prettyStatus, statusClass } from "./format";

function failureCount(report: QAReport): number {
  return (
    report.checks.filter((c) => c.status === "failed").length +
    report.specialists.filter((s) => s.status === "failed").length
  );
}

function CheckRow({ check }: { check: QACheck }) {
  const [open, setOpen] = useState(false);
  const hasOutput = !!check.output.trim();
  return (
    <>
      <tr
        className={hasOutput ? "click" : ""}
        onClick={() => hasOutput && setOpen(!open)}
      >
        <td>
          {hasOutput && (
            <span className="muted" style={{ marginRight: 5 }}>
              {open ? "▾" : "▸"}
            </span>
          )}
          {check.label}
          {check.command && (
            <div className="mono muted" style={{ fontSize: 11 }}>
              {check.command}
            </div>
          )}
        </td>
        <td className="muted">{check.category}</td>
        <td>
          <span className={statusClass(check.status)}>
            {prettyStatus(check.status)}
          </span>
        </td>
        <td className="ellipsis" title={check.summary}>
          {check.summary || "—"}
        </td>
        <td className="num">{fmtDuration(check.duration_seconds)}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={5} style={{ padding: 0 }}>
            <pre
              className="mono"
              style={{
                margin: 0,
                padding: "10px 14px",
                background: "var(--bg-0)",
                fontSize: 11.5,
                maxHeight: 300,
                overflow: "auto",
                whiteSpace: "pre-wrap",
                color: "var(--text-2)",
              }}
            >
              {check.output}
            </pre>
          </td>
        </tr>
      )}
    </>
  );
}

function SpecialistRow({ item }: { item: QASpecialist }) {
  return (
    <tr>
      <td>
        {item.label}
        <div className="muted" style={{ fontSize: 11 }}>
          {item.objective}
        </div>
      </td>
      <td className="muted">{item.role}</td>
      <td>
        <span className={statusClass(item.status)}>
          {prettyStatus(item.status)}
        </span>
      </td>
      <td className="ellipsis" title={item.error || item.summary}>
        {item.error || item.summary.split("\n")[0] || "—"}
      </td>
      <td className="num">{item.steps}</td>
    </tr>
  );
}

export function QAView() {
  const qc = useQueryClient();
  const { data: latest, isLoading } = useQALatest();
  const { data: history } = useQAHistory();
  const [viewing, setViewing] = useState<QAReport | null>(null);
  const [busy, setBusy] = useState(false);

  const running = !!latest?.running;
  // A saved scan the user picked wins until the next live run starts.
  const report = running ? (latest?.report ?? null) : (viewing ?? latest?.report ?? null);

  const start = async () => {
    setBusy(true);
    try {
      setViewing(null);
      await api.qaRun();
      await qc.invalidateQueries({ queryKey: qk.qaLatest });
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    await api.qaCancel();
    await qc.invalidateQueries({ queryKey: qk.qaLatest });
  };

  const openSaved = async (id: string) => {
    const res = await api.qaReport(id);
    setViewing(res.report);
  };

  const failed = report ? failureCount(report) : 0;
  const skipped = report
    ? report.checks.filter((c) => c.status === "skipped").length
    : 0;

  return (
    <div className="split">
      <div className="split-left" style={{ width: 320 }}>
        <div className="panel-header">
          Saved scans
          <span className="spacer" />
          <button
            className="btn icon"
            title="Refresh"
            onClick={() => void qc.invalidateQueries({ queryKey: qk.qaHistory })}
          >
            ⟳
          </button>
        </div>
        <div className="scroll-y" style={{ flex: 1 }}>
          {(history?.reports.length ?? 0) === 0 && (
            <div className="empty">No saved scans for this repository.</div>
          )}
          {history && history.reports.length > 0 && (
            <table className="dtable">
              <thead>
                <tr>
                  <th>Started</th>
                  <th>Status</th>
                  <th>Fail</th>
                </tr>
              </thead>
              <tbody>
                {history.reports.map((r) => (
                  <tr
                    key={r.id}
                    className={`click ${report?.id === r.id ? "active" : ""}`}
                    onClick={() => void openSaved(r.id)}
                    title={r.project_profile.join(", ") || "general"}
                  >
                    <td className="mono" style={{ whiteSpace: "nowrap" }}>
                      {fmtDateTime(r.started_at)}
                    </td>
                    <td>
                      <span className={statusClass(r.status)}>
                        {prettyStatus(r.status)}
                      </span>
                    </td>
                    <td className="num">{failureCount(r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="split-right">
        <div className="toolbar">
          <button
            className="btn primary"
            disabled={running || busy}
            onClick={() => void start()}
          >
            {running ? "QA running…" : "Run QA"}
          </button>
          {running && (
            <button className="btn danger" onClick={() => void cancel()}>
              Cancel
            </button>
          )}
          <span className="muted" style={{ fontSize: 11 }}>
            Parallel read-only reviewers, tests, browser checks, and dependency
            audits. Scans needing network approval are skipped, not granted.
          </span>
        </div>

        {isLoading && <div className="empty">Loading…</div>}
        {!isLoading && !report && (
          <div className="empty">
            No QA report yet. Run a scan to inspect this project.
          </div>
        )}

        {report && (
          <div className="scroll-y" style={{ flex: 1 }}>
            <div className="metric-row">
              <Metric k="Report" v={report.id} />
              <Metric k="Status" v={prettyStatus(report.status)} />
              <Metric k="Started" v={fmtDateTime(report.started_at)} />
              <Metric k="Failed" v={failed} />
              <Metric k="Skipped" v={skipped} />
              <Metric
                k="Profile"
                v={report.project_profile.join(", ") || "general"}
              />
            </div>

            <div className="section-title">Specialists</div>
            {report.specialists.length === 0 ? (
              <div className="empty">No specialists ran for this scan.</div>
            ) : (
              <table className="dtable">
                <thead>
                  <tr>
                    <th>Specialist</th>
                    <th>Role</th>
                    <th>Status</th>
                    <th>Result</th>
                    <th>Steps</th>
                  </tr>
                </thead>
                <tbody>
                  {report.specialists.map((s) => (
                    <SpecialistRow key={s.id} item={s} />
                  ))}
                </tbody>
              </table>
            )}

            <div className="section-title">Automated evidence</div>
            {report.checks.length === 0 ? (
              <div className="empty">No automated checks were discovered.</div>
            ) : (
              <table className="dtable">
                <thead>
                  <tr>
                    <th>Check</th>
                    <th>Category</th>
                    <th>Status</th>
                    <th>Result</th>
                    <th>Time</th>
                  </tr>
                </thead>
                <tbody>
                  {report.checks.map((c) => (
                    <CheckRow key={c.id} check={c} />
                  ))}
                </tbody>
              </table>
            )}

            <div className="section-title">Consolidated report</div>
            <div className="md-block">
              {report.summary ? (
                <ReactMarkdown>{report.summary}</ReactMarkdown>
              ) : (
                <span className="muted">
                  The consolidated write-up appears when the scan finishes.
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
