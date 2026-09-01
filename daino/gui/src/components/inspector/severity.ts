// One vocabulary for severity and verdict, so a colour means the same thing in
// the banner, the pills, and the findings table.
import type { QAFinding, QASeverity, QAVerdict } from "../../api/types";

export const SEVERITY_ORDER: QASeverity[] = [
  "critical",
  "high",
  "medium",
  "low",
  "info",
];

export function severityClass(severity: QASeverity): string {
  return `sev sev-${severity}`;
}

export function countBySeverity(
  findings: QAFinding[],
): Record<QASeverity, number> {
  const counts = {
    critical: 0,
    high: 0,
    medium: 0,
    low: 0,
    info: 0,
  } as Record<QASeverity, number>;
  for (const finding of findings) counts[finding.severity] += 1;
  return counts;
}

export interface VerdictLook {
  label: string;
  hint: string;
  tone: "ok" | "warn" | "bad" | "info";
}

export const VERDICT: Record<QAVerdict, VerdictLook> = {
  pass: {
    label: "SAFE TO PUSH",
    hint: "No critical or high finding, and nothing the project's own checks reject.",
    tone: "ok",
  },
  warn: {
    label: "REVIEW BEFORE PUSH",
    hint: "Findings that need a decision, but none of them blocks on its own.",
    tone: "warn",
  },
  blocked: {
    label: "DO NOT PUSH",
    hint: "At least one release blocker is confirmed by evidence below.",
    tone: "bad",
  },
  unknown: {
    label: "NO VERDICT",
    hint: "The inspection has not finished, so nothing has been cleared.",
    tone: "info",
  },
};

/** Where a finding came from, rendered for the table's Source column. */
export function findingLocation(finding: QAFinding): string {
  if (!finding.location) return "—";
  return finding.line ? `${finding.location}:${finding.line}` : finding.location;
}
