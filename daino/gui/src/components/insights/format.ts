// Small shared formatters for the evidence views.

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return `${d.toLocaleDateString([], { month: "short", day: "2-digit" })} ${d.toLocaleTimeString(
    [],
    { hour: "2-digit", minute: "2-digit" },
  )}`;
}

export function fmtCost(value: number): string {
  if (!value || value <= 0) return "$0.0000";
  if (value < 0.0001) return `$${value.toFixed(8).replace(/0+$/, "")}`;
  return `$${value.toFixed(4)}`;
}

export function fmtTokens(value: number): string {
  return value.toLocaleString();
}

export function fmtDuration(seconds: number): string {
  if (!seconds) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(2)}s`;
  const m = Math.floor(seconds / 60);
  return `${m}m ${Math.round(seconds % 60)}s`;
}

/** Statuses share one CSS vocabulary so a colour means the same thing everywhere. */
export function statusClass(status: string): string {
  const key = status.toLowerCase().replace(/\s+/g, "_");
  if (["passed", "completed", "approved", "ok", "success"].includes(key))
    return "status-pill status-passed";
  if (["running", "in_progress", "started"].includes(key))
    return "status-pill status-running";
  if (["failed", "error", "blocked", "cancelled", "rejected"].includes(key))
    return "status-pill status-failed";
  if (["skipped", "pending"].includes(key)) return "status-pill status-skipped";
  return "status-pill";
}

export function prettyStatus(status: string): string {
  return status.replace(/_/g, " ").toUpperCase();
}
