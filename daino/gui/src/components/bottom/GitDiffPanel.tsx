import { DiffEditor } from "@monaco-editor/react";
import { useGitDiff } from "../../api/hooks";
import { useUIStore } from "../../store/uiStore";

// Parse a unified diff into "before"/"after" text so Monaco can render it as a
// side-by-side diff. This is an approximation good enough for review.
function splitUnifiedDiff(diff: string): { before: string; after: string } {
  const before: string[] = [];
  const after: string[] = [];
  for (const line of diff.split("\n")) {
    if (
      line.startsWith("diff ") ||
      line.startsWith("index ") ||
      line.startsWith("--- ") ||
      line.startsWith("+++ ") ||
      line.startsWith("@@")
    ) {
      continue;
    }
    if (line.startsWith("+")) after.push(line.slice(1));
    else if (line.startsWith("-")) before.push(line.slice(1));
    else {
      const text = line.startsWith(" ") ? line.slice(1) : line;
      before.push(text);
      after.push(text);
    }
  }
  return { before: before.join("\n"), after: after.join("\n") };
}

export function GitDiffPanel() {
  const path = useUIStore((s) => s.gitDiffPath);
  const staged = useUIStore((s) => s.gitDiffStaged);
  const { data, isLoading } = useGitDiff(path, staged);

  if (!path) {
    return (
      <div className="scroll-y" style={{ height: "100%" }}>
        <div className="empty">
          Select a changed file in Source Control to view its diff.
        </div>
      </div>
    );
  }
  if (isLoading) {
    return <div className="empty">Loading diff…</div>;
  }
  if (!data || !data.diff) {
    return <div className="empty">No diff for {path}.</div>;
  }

  const { before, after } = splitUnifiedDiff(data.diff);

  return (
    <div style={{ height: "100%" }}>
      <div className="panel-header">
        {path} {staged ? "(staged)" : ""}
      </div>
      <div style={{ height: "calc(100% - 34px)" }}>
        <DiffEditor
          original={before}
          modified={after}
          theme="vs-dark"
          options={{
            readOnly: true,
            renderSideBySide: true,
            automaticLayout: true,
            fontSize: 12,
            minimap: { enabled: false },
          }}
        />
      </div>
    </div>
  );
}
