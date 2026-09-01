import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import {
  usePreviewStatus,
  useQAHistory,
  useQALatest,
  qk,
} from "../../api/hooks";
import { api, ApiError } from "../../api/client";
import type {
  QACheck,
  QAReport,
  QAScanProfile,
  QASpecialist,
} from "../../api/types";
import { Metric } from "../insights/Metric";
import {
  fmtDateTime,
  fmtDuration,
  prettyStatus,
  statusClass,
} from "../insights/format";
import { FindingsTable } from "./FindingsTable";
import { VerdictBanner } from "./VerdictBanner";
import { VERDICT } from "./severity";

const PROFILES: { id: QAScanProfile; label: string; hint: string }[] = [
  {
    id: "full",
    label: "FULL",
    hint: "Quality, tests, and the vulnerability assessment in one pass",
  },
  {
    id: "quality",
    label: "QUALITY",
    hint: "Lint, types, tests, and the code-quality reviewers only",
  },
  {
    id: "security",
    label: "SECURITY",
    hint: "Secrets, SAST, dependencies, threat model, and the live probe only",
  },
];

/**
 * Whether a target is somewhere the probe may go without being told twice.
 *
 * The server decides this authoritatively (and resolves hostnames to do it);
 * this copy exists only so the confirmation appears as the URL is typed rather
 * than after a rejected request.
 */
function looksLocal(url: string): boolean {
  let host: string;
  try {
    host = new URL(url.includes("://") ? url : `http://${url}`).hostname;
  } catch {
    return true; // not a URL yet — do not nag mid-typing
  }
  if (["localhost", "127.0.0.1", "::1", "0.0.0.0", ""].includes(host)) return true;
  if (host.endsWith(".local") || host.endsWith(".localhost")) return true;
  if (/^10\./.test(host) || /^192\.168\./.test(host)) return true;
  return /^172\.(1[6-9]|2\d|3[01])\./.test(host);
}

export function ScanView() {
  const qc = useQueryClient();
  const { data: latest, isLoading } = useQALatest();
  const { data: history } = useQAHistory();
  const { data: preview } = usePreviewStatus(4000);
  const [viewing, setViewing] = useState<QAReport | null>(null);
  const [profile, setProfile] = useState<QAScanProfile>("full");
  const [target, setTarget] = useState("");
  const [authorized, setAuthorized] = useState(false);
  const [busy, setBusy] = useState(false);

  const running = !!latest?.running;
  // A saved scan the user picked wins until the next live run starts.
  const report = running
    ? (latest?.report ?? null)
    : (viewing ?? latest?.report ?? null);

  // The app started in the Live view is the obvious thing to probe, so it
  // prefills the target rather than making the user retype the URL.
  useEffect(() => {
    if (!target && preview?.running && preview.url) setTarget(preview.url);
  }, [preview?.running, preview?.url, target]);

  // The saved list has no reason to poll, but a finished run adds a row to it.
  useEffect(() => {
    if (!running) void qc.invalidateQueries({ queryKey: qk.qaHistory });
  }, [running, qc]);

  const remote = !!target.trim() && !looksLocal(target);

  const start = async () => {
    setBusy(true);
    try {
      setViewing(null);
      await api.qaRun({
        profile,
        target_url: target.trim(),
        authorize_remote_target: authorized,
      });
      await qc.invalidateQueries({ queryKey: qk.qaLatest });
    } catch (err) {
      window.alert(
        err instanceof ApiError && err.status === 403
          ? err.message
          : `Could not start the inspection: ${
              err instanceof Error ? err.message : String(err)
            }`,
      );
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

  return (
    <div className="split">
      <div className="split-left" style={{ width: 300 }}>
        <div className="panel-header">
          Saved inspections
          <span className="spacer" />
          <button
            className="btn icon"
            title="Refresh"
            onClick={() =>
              void qc.invalidateQueries({ queryKey: qk.qaHistory })
            }
          >
            ⟳
          </button>
        </div>
        <div className="scroll-y" style={{ flex: 1 }}>
          {(history?.reports.length ?? 0) === 0 && (
            <div className="empty">No inspection has been saved yet.</div>
          )}
          {history && history.reports.length > 0 && (
            <table className="dtable">
              <thead>
                <tr>
                  <th>Started</th>
                  <th>Verdict</th>
                  <th>Findings</th>
                </tr>
              </thead>
              <tbody>
                {history.reports.map((r) => (
                  <tr
                    key={r.id}
                    className={`click ${report?.id === r.id ? "active" : ""}`}
                    onClick={() => void openSaved(r.id)}
                    title={`${r.scan_profile} · ${
                      r.project_profile.join(", ") || "general"
                    }`}
                  >
                    <td className="mono" style={{ whiteSpace: "nowrap" }}>
                      {fmtDateTime(r.started_at)}
                    </td>
                    <td>
                      <span className={`verdict-pill v-${r.verdict}`}>
                        {VERDICT[r.verdict].label}
                      </span>
                    </td>
                    <td className="num">{r.findings.length}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="split-right">
        <div className="toolbar">
          <div className="segmented">
            {PROFILES.map((p) => (
              <button
                key={p.id}
                className={profile === p.id ? "active" : ""}
                onClick={() => setProfile(p.id)}
                title={p.hint}
                disabled={running}
              >
                {p.label}
              </button>
            ))}
          </div>
          <input
            className="input"
            style={{ maxWidth: 230 }}
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            placeholder="Live target (optional)"
            title={
              "A running app to probe with GET/HEAD/OPTIONS: headers, cookie " +
              "flags, exposed paths, error handling, CORS. Start one in LIVE APP " +
              "and it is filled in for you."
            }
            disabled={running}
          />
          <button
            className="btn primary"
            disabled={running || busy || (remote && !authorized)}
            onClick={() => void start()}
          >
            {running ? "Inspecting…" : "Run inspection"}
          </button>
          {running && (
            <button className="btn danger" onClick={() => void cancel()}>
              Cancel
            </button>
          )}
          <span className="grow" />
          <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
            {PROFILES.find((p) => p.id === profile)?.hint}
          </span>
        </div>

        {remote && (
          <label className="authorize-bar">
            <input
              type="checkbox"
              checked={authorized}
              onChange={(e) => setAuthorized(e.target.checked)}
            />
            <span>
              <strong>{target}</strong> is not a loopback or private address.
              Confirm you own this host, or are authorised to test it, before
              the probe runs. The confirmation is recorded in the audit log.
            </span>
          </label>
        )}

        {isLoading && <div className="empty">Loading…</div>}
        {!isLoading && !report && (
          <div className="empty">
            No inspection yet. Run one to get a pre-push verdict for this
            repository.
          </div>
        )}

        {report && (
          <div className="scroll-y" style={{ flex: 1 }}>
            <VerdictBanner report={report} running={running} />

            <div className="metric-row">
              <Metric k="Report" v={report.id} />
              <Metric k="Scan" v={report.scan_profile} />
              <Metric k="Status" v={prettyStatus(report.status)} />
              <Metric k="Started" v={fmtDateTime(report.started_at)} />
              <Metric k="Findings" v={report.findings.length} />
              <Metric
                k="Failed checks"
                v={report.checks.filter((c) => c.status === "failed").length}
              />
              <Metric k="Live target" v={report.target_url || "none"} />
              <Metric
                k="Profile"
                v={report.project_profile.join(", ") || "general"}
              />
            </div>

            <div className="section-title">Findings</div>
            {report.findings.length === 0 ? (
              <div className="empty">
                {running
                  ? "Collecting evidence…"
                  : "No finding was produced by the scanners, the built-in audit, or the live probe."}
              </div>
            ) : (
              <FindingsTable findings={report.findings} />
            )}

            <div className="section-title">Specialists</div>
            {report.specialists.length === 0 ? (
              <div className="empty">No specialist ran for this scan.</div>
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

            <div className="section-title">Evidence</div>
            {report.checks.length === 0 ? (
              <div className="empty">No automated check was applicable.</div>
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
            <div className="mono muted" style={{ fontSize: "var(--fs-11)" }}>
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
            <pre className="check-output mono">{check.output}</pre>
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
        <div className="muted" style={{ fontSize: "var(--fs-11)" }}>
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
