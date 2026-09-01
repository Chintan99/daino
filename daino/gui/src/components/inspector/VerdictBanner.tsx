import type { QAReport } from "../../api/types";
import { SEVERITY_ORDER, VERDICT, countBySeverity } from "./severity";

/**
 * The release gate's answer, stated before any evidence.
 *
 * The whole point of the Inspector is to replace "the scan finished" with a
 * decision, so the verdict is the first thing on screen and every reason behind
 * it is listed rather than summarised — a gate a team cannot predict is a gate
 * they will start ignoring.
 */
export function VerdictBanner({
  report,
  running,
}: {
  report: QAReport;
  running: boolean;
}) {
  const look = VERDICT[running ? "unknown" : report.verdict];
  const counts = countBySeverity(report.findings);

  return (
    <div className={`verdict verdict-${running ? "running" : look.tone}`}>
      <div className="verdict-head">
        <span className="verdict-label">
          {running ? "INSPECTING…" : look.label}
        </span>
        <span className="verdict-hint">
          {running
            ? "Evidence appears as each stage lands; the verdict is set when the scan ends."
            : look.hint}
        </span>
      </div>

      <div className="verdict-counts">
        {SEVERITY_ORDER.map((level) => (
          <span key={level} className={`sev sev-${level} ${counts[level] ? "" : "zero"}`}>
            {counts[level]} {level}
          </span>
        ))}
      </div>

      {report.gate_reasons.length > 0 && !running && (
        <ul className="verdict-reasons">
          {report.gate_reasons.map((reason, index) => (
            <li key={index}>{reason}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
