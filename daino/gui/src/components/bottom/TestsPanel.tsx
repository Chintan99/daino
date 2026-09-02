import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import { openFileInEditor } from "../../lib/openFile";
import type { TestResult, TestStatus } from "../../api/types";

const MARK: Record<TestStatus, string> = {
  passed: "✓",
  failed: "✗",
  errored: "!",
  skipped: "–",
  xfailed: "–",
  xpassed: "?",
};

/** Only the outcomes worth a colour. Skips are information, not alarm. */
const TONE: Partial<Record<TestStatus, string>> = {
  passed: "passed",
  failed: "failed",
  errored: "errored",
  xpassed: "errored",
};

function duration(seconds: number): string {
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  if (seconds >= 1) return `${seconds.toFixed(2)}s`;
  return `${Math.round(seconds * 1000)}ms`;
}

/**
 * The project's tests: what exists, what ran, and what broke where.
 *
 * Built around one behaviour the old counter could not offer — clicking a
 * failure opens the line it *failed on*, which is frequently not the line the
 * test is defined on. Everything comes from the runner's own machine-readable
 * report, so this panel and the same command in a terminal always agree.
 */
export function TestsPanel() {
  const qc = useQueryClient();
  const [framework, setFramework] = useState("");
  const [coverage, setCoverage] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [onlyFailures, setOnlyFailures] = useState(false);
  const [busy, setBusy] = useState(false);

  const { data: discovery } = useQuery({
    queryKey: ["tests", "frameworks", framework],
    queryFn: () => api.testFrameworks(framework),
    staleTime: 15_000,
  });
  const { data: latest } = useQuery({
    queryKey: ["tests", "latest"],
    queryFn: api.testLatest,
    refetchInterval: (query) => (query.state.data?.running ? 700 : false),
  });

  const run = latest?.run ?? null;
  const running = !!latest?.running;
  const frameworks = discovery?.frameworks ?? [];
  const active =
    frameworks.find((item) => item.id === framework) ??
    frameworks.find((item) => item.available) ??
    null;

  const start = async (options: { failed_only?: boolean } = {}) => {
    setBusy(true);
    try {
      await api.runTests({
        framework: framework || active?.id || "",
        coverage,
        failed_only: options.failed_only,
      });
      await qc.invalidateQueries({ queryKey: ["tests", "latest"] });
    } catch (err) {
      window.alert(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    await api.cancelTests();
    await qc.invalidateQueries({ queryKey: ["tests", "latest"] });
  };

  const failures = run?.results.filter(
    (item) => item.status === "failed" || item.status === "errored",
  ) ?? [];
  const shown = onlyFailures ? failures : (run?.results ?? []);

  if (frameworks.length === 0) {
    return (
      <div className="scroll-y" style={{ height: "100%" }}>
        <div className="empty">
          No test framework detected in this project.
          <div style={{ marginTop: 6, fontSize: "var(--fs-11)" }}>
            pytest, Vitest, Jest, <code>go test</code> and <code>cargo test</code>{" "}
            are recognised automatically.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="panel" style={{ height: "100%" }}>
      <div className="toolbar">
        {frameworks.length > 1 && (
          <select
            className="input sm"
            value={framework || active?.id || ""}
            onChange={(e) => setFramework(e.target.value)}
            style={{ width: 140 }}
          >
            {frameworks.map((item) => (
              <option key={item.id} value={item.id} disabled={!item.available}>
                {item.label}
                {item.test_count >= 0 ? ` (${item.test_count})` : ""}
              </option>
            ))}
          </select>
        )}
        <button
          className="btn primary sm"
          disabled={busy || running || !active?.available}
          onClick={() => void start()}
        >
          {running ? "Running…" : "Run tests"}
        </button>
        <button
          className="btn subtle sm"
          disabled={busy || running || failures.length === 0}
          title="Re-run exactly the tests that failed, by their own ids"
          onClick={() => void start({ failed_only: true })}
        >
          Re-run {failures.length || ""} failed
        </button>
        {running && (
          <button className="btn subtle sm" onClick={() => void cancel()}>
            Stop
          </button>
        )}
        <label className="check sm" title="Collect coverage from the runner">
          <input
            type="checkbox"
            checked={coverage}
            disabled={!active?.supports_coverage}
            onChange={(e) => setCoverage(e.target.checked)}
          />
          Coverage
        </label>
        <span className="grow" />
        {run && <RunSummary run={run} />}
      </div>

      {active && !active.available && (
        <div className="problems-gap">
          <strong>{active.label} cannot run here</strong>
          <div className="muted">{active.detail}</div>
        </div>
      )}
      {active?.available && active.detail && active.test_count === 0 && (
        <div className="problems-gap">
          <strong>{active.label} found no tests</strong>
          <div className="muted mono" style={{ whiteSpace: "pre-wrap" }}>
            {active.detail}
          </div>
        </div>
      )}
      {run?.error && (
        <div className="problems-gap">
          <strong>The run did not complete</strong>
          <div className="muted mono" style={{ whiteSpace: "pre-wrap" }}>
            {run.error}
          </div>
        </div>
      )}

      {run && run.coverage && (
        <div className="pad" style={{ paddingBottom: 0 }}>
          <span className="badge" title={`Measured by ${run.coverage.source}`}>
            coverage{" "}
            {run.coverage.total
              ? `${((100 * run.coverage.covered) / run.coverage.total).toFixed(1)}%`
              : "n/a"}
          </span>{" "}
          <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
            {run.coverage.covered} of {run.coverage.total} lines ·{" "}
            {run.coverage.source}
          </span>
        </div>
      )}

      <div className="scroll-y" style={{ flex: 1 }}>
        {!run && <div className="empty">No test run yet.</div>}
        {run && run.results.length === 0 && !run.error && (
          <div className="empty">
            This runner reported no per-test results.
            {run.command && (
              <div className="mono muted" style={{ marginTop: 6, fontSize: "var(--fs-11)" }}>
                {run.command}
              </div>
            )}
          </div>
        )}
        {shown.length > 0 && (
          <>
            {failures.length > 0 && (
              <label className="check sm" style={{ padding: "6px 12px" }}>
                <input
                  type="checkbox"
                  checked={onlyFailures}
                  onChange={(e) => setOnlyFailures(e.target.checked)}
                />
                Show failures only
              </label>
            )}
            <table className="dtable">
              <thead>
                <tr>
                  <th style={{ width: 26 }} />
                  <th>Test</th>
                  <th style={{ width: 220 }}>File</th>
                  <th style={{ width: 80 }}>Time</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((item) => (
                  <TestRow
                    key={item.id}
                    result={item}
                    expanded={expanded === item.id}
                    onToggle={() =>
                      setExpanded(expanded === item.id ? null : item.id)
                    }
                  />
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}

function RunSummary({ run }: { run: { counts: Record<string, number>; duration_seconds: number } }) {
  const { counts } = run;
  return (
    <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
      <span className="test-tone passed">{counts.passed ?? 0} passed</span>
      {(counts.failed ?? 0) > 0 && (
        <>
          {" · "}
          <span className="test-tone failed">{counts.failed} failed</span>
        </>
      )}
      {(counts.errored ?? 0) > 0 && (
        <>
          {" · "}
          <span className="test-tone errored">{counts.errored} errored</span>
        </>
      )}
      {(counts.skipped ?? 0) > 0 && ` · ${counts.skipped} skipped`}
      {run.duration_seconds > 0 && ` · ${duration(run.duration_seconds)}`}
    </span>
  );
}

function TestRow({
  result,
  expanded,
  onToggle,
}: {
  result: TestResult;
  expanded: boolean;
  onToggle: () => void;
}) {
  // Where it broke, not where it is written — those differ whenever a helper
  // or a fixture is involved, which is most of the time.
  const target = result.failure_file || result.file;
  const line = result.failure_line || result.line || 1;
  const failed = result.status === "failed" || result.status === "errored";

  return (
    <>
      <tr className="click" onClick={onToggle}>
        <td className={`test-tone ${TONE[result.status] ?? ""}`} title={result.status}>
          {MARK[result.status]}
        </td>
        <td title={result.suite ? `${result.suite} › ${result.name}` : result.name}>
          {result.suite && <span className="muted">{result.suite} › </span>}
          {result.name}
        </td>
        <td className="mono ellipsis">
          {target ? (
            <button
              className="ws-link-path"
              title={`Open ${target}:${line}`}
              onClick={(e) => {
                e.stopPropagation();
                void openFileInEditor(target, { line });
              }}
            >
              {target}:{line}
            </button>
          ) : (
            <span className="muted">—</span>
          )}
        </td>
        <td className="num muted">{duration(result.duration_seconds)}</td>
      </tr>
      {expanded && failed && result.message && (
        <tr>
          <td colSpan={4} style={{ padding: 0 }}>
            <pre className="mono test-failure">{result.message}</pre>
          </td>
        </tr>
      )}
    </>
  );
}
