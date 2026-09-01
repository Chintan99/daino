import { useState } from "react";
import type { QAFinding, QASeverity } from "../../api/types";
import { SEVERITY_ORDER, findingLocation, severityClass } from "./severity";

/**
 * The findings list, worst first, with the detail folded away until asked for.
 *
 * A pre-push report is read under time pressure, so the collapsed row carries
 * only what decides whether to keep reading: how bad, what it is, and where.
 * Remediation and evidence are one click away rather than three screens down.
 */
export function FindingsTable({ findings }: { findings: QAFinding[] }) {
  const [filter, setFilter] = useState<QASeverity | "all">("all");
  const [query, setQuery] = useState("");

  const needle = query.trim().toLowerCase();
  const shown = findings.filter((f) => {
    if (filter !== "all" && f.severity !== filter) return false;
    if (!needle) return true;
    return `${f.title} ${f.location} ${f.cwe} ${f.source} ${f.reference}`
      .toLowerCase()
      .includes(needle);
  });

  const present = SEVERITY_ORDER.filter((level) =>
    findings.some((f) => f.severity === level),
  );

  return (
    <>
      <div className="findings-bar">
        <div className="segmented">
          <button
            className={filter === "all" ? "active" : ""}
            onClick={() => setFilter("all")}
          >
            ALL {findings.length}
          </button>
          {present.map((level) => (
            <button
              key={level}
              className={filter === level ? "active" : ""}
              onClick={() => setFilter(level)}
            >
              {level.toUpperCase()}{" "}
              {findings.filter((f) => f.severity === level).length}
            </button>
          ))}
        </div>
        <span className="grow" />
        <input
          className="input"
          style={{ maxWidth: 220 }}
          value={query}
          placeholder="Filter by file, CWE, or rule"
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {shown.length === 0 ? (
        <div className="empty">No finding matches this filter.</div>
      ) : (
        <table className="dtable">
          <thead>
            <tr>
              <th style={{ width: 84 }}>Severity</th>
              <th>Finding</th>
              <th style={{ width: 240 }}>Location</th>
              <th style={{ width: 110 }}>CWE</th>
              <th style={{ width: 130 }}>Source</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((finding) => (
              <FindingRow key={finding.id} finding={finding} />
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function FindingRow({ finding }: { finding: QAFinding }) {
  const [open, setOpen] = useState(false);
  const expandable = !!(finding.detail || finding.remediation);
  return (
    <>
      <tr
        className={expandable ? "click" : ""}
        onClick={() => expandable && setOpen(!open)}
      >
        <td>
          <span className={severityClass(finding.severity)}>
            {finding.severity}
          </span>
        </td>
        <td>
          {expandable && (
            <span className="muted" style={{ marginRight: 5 }}>
              {open ? "▾" : "▸"}
            </span>
          )}
          {finding.title}
          {finding.confidence === "low" && (
            <span className="badge" style={{ marginLeft: 6 }} title="Not a blocker on its own">
              low confidence
            </span>
          )}
        </td>
        <td className="mono ellipsis" title={findingLocation(finding)}>
          {findingLocation(finding)}
        </td>
        <td className="mono muted">{finding.cwe || "—"}</td>
        <td className="muted ellipsis" title={`${finding.source} · ${finding.reference}`}>
          {finding.source || "—"}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={5} className="finding-detail">
            {finding.detail && <pre className="mono">{finding.detail}</pre>}
            {finding.remediation && (
              <p>
                <strong>Fix</strong> — {finding.remediation}
              </p>
            )}
            {finding.reference && (
              <p className="muted mono">rule {finding.reference}</p>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
