import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { openFileInEditor } from "../../lib/openFile";
import { useEditorStore } from "../../store/editorStore";
import {
  coverageGaps,
  mergedProblems,
  useProblemsStore,
} from "../../store/problemsStore";

const MARK: Record<string, string> = {
  error: "✗",
  warning: "⚠",
  info: "ℹ",
  hint: "·",
};

/**
 * Problems in the files you have open, and what did or did not look at them.
 *
 * Two sources feed this: a real language server per language (run by the
 * backend) and Monaco's own workers for syntax and self-contained languages.
 * Both are honest about their limits, and this panel is built around the state
 * neither reports on its own — a file *nothing* analysed. An empty list there
 * means no evidence, not no problems, so the gap is shown with the install
 * command that would close it.
 */
export function ProblemsPanel() {
  const byPath = useProblemsStore((s) => s.byPath);
  const editorByPath = useProblemsStore((s) => s.editorByPath);
  const openFiles = useEditorStore((s) =>
    s.tabs.filter((tab) => tab.kind === "file"),
  );
  // Cheap and rarely changing, so a long stale time keeps this off the wire
  // while still refreshing when someone installs a server and comes back.
  const { data: servers } = useQuery({
    queryKey: ["lsp", "servers"],
    queryFn: api.languageServers,
    staleTime: 30_000,
  });

  const problems = mergedProblems(byPath, editorByPath);
  const gaps = coverageGaps(byPath);
  const missing = (servers?.servers ?? []).filter((item) => !item.available);

  return (
    <div className="scroll-y" style={{ height: "100%" }}>
      {gaps.length > 0 && (
        <div className="problems-gap">
          <strong>
            {gaps.length === 1
              ? "1 open file was not analysed"
              : `${gaps.length} open files were not analysed`}
          </strong>
          <div className="muted">
            No problems reported for {gaps.map((item) => item.path).join(", ")}{" "}
            because nothing looked at {gaps.length === 1 ? "it" : "them"}.
          </div>
          {missing.length > 0 && (
            <ul className="problems-install">
              {missing.slice(0, 4).map((item) => (
                <li key={item.id}>
                  <span className="muted">{item.languages.join(", ")}:</span>{" "}
                  <code>{item.install}</code>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {problems.length === 0 ? (
        <div className="empty">
          {openFiles.length === 0
            ? "Nothing to analyse — open a file."
            : gaps.length > 0
              ? "Nothing reported by the analysers that did run."
              : "No problems in the open files."}
          <div style={{ marginTop: 6, fontSize: "var(--fs-11)" }}>
            Covers the files you have open. For the whole project — the
            project's own lint, type and test commands — run the Inspector.
          </div>
        </div>
      ) : (
        <table className="dtable">
          <thead>
            <tr>
              <th style={{ width: 26 }} />
              <th>Problem</th>
              <th style={{ width: 260 }}>File</th>
              <th style={{ width: 110 }}>Source</th>
            </tr>
          </thead>
          <tbody>
            {problems.map((problem, index) => (
              <tr
                key={`${problem.path}:${problem.line}:${problem.column}:${index}`}
                className="click"
                onClick={() =>
                  void openFileInEditor(problem.path, {
                    line: problem.line,
                    column: problem.column,
                  })
                }
              >
                <td
                  className={`problem-mark ${problem.severity}`}
                  title={problem.severity}
                >
                  {MARK[problem.severity] ?? "·"}
                </td>
                <td title={problem.message}>{problem.message}</td>
                <td className="mono ellipsis" title={problem.path}>
                  {problem.path}:{problem.line}
                </td>
                <td className="muted">{problem.source || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
