import { useGitStatus } from "../../api/hooks";
import { useUIStore } from "../../store/uiStore";
import { openFileInEditor } from "../../lib/openFile";
import type { GitEntry } from "../../api/types";

function Section({
  title,
  entries,
  staged,
  activePath,
  onDiff,
}: {
  title: string;
  entries: GitEntry[];
  staged: boolean;
  activePath: string | null;
  onDiff: (path: string, staged: boolean) => void;
}) {
  if (entries.length === 0) return null;
  return (
    <div className="scm-section">
      <div className="scm-title">
        {title} · {entries.length}
      </div>
      {entries.map((e) => (
        <div
          key={`${title}:${e.path}`}
          className={`scm-row ${activePath === e.path ? "active" : ""}`}
          onClick={() => onDiff(e.path, staged)}
          onDoubleClick={() => void openFileInEditor(e.path)}
          title={`${e.path} — click for diff, double-click to open`}
        >
          <span className={`git-mark git-${(e.status || title[0])[0]}`}>
            {(e.status || title[0])[0]}
          </span>
          <span className="tree-name">{e.path}</span>
        </div>
      ))}
    </div>
  );
}

export function SourceControlPanel() {
  const { data: git, isLoading } = useGitStatus();
  const openGitDiff = useUIStore((s) => s.openGitDiff);
  const activePath = useUIStore((s) => s.gitDiffPath);

  return (
    <div className="panel">
      <div className="panel-header">Source Control</div>
      <div className="panel-body">
        {isLoading && <div className="empty">Loading…</div>}
        {git && !git.repository && (
          <div className="empty">Not a git repository</div>
        )}
        {git?.repository && (
          <>
            <div style={{ padding: "8px 10px", borderBottom: "1px solid var(--border)" }}>
              <span className="badge">⑃ {git.branch || "detached"}</span>
            </div>
            {git.staged.length === 0 &&
              git.modified.length === 0 &&
              git.untracked.length === 0 && (
                <div className="empty">No changes</div>
              )}
            <Section
              title="Staged"
              entries={git.staged}
              staged
              activePath={activePath}
              onDiff={openGitDiff}
            />
            <Section
              title="Modified"
              entries={git.modified}
              staged={false}
              activePath={activePath}
              onDiff={openGitDiff}
            />
            <Section
              title="Untracked"
              entries={git.untracked.map((u) => ({ ...u, status: "?" }))}
              staged={false}
              activePath={activePath}
              onDiff={openGitDiff}
            />
          </>
        )}
      </div>
    </div>
  );
}
