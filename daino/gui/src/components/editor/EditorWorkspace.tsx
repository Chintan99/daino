import { Suspense, lazy } from "react";
import { useEditorStore } from "../../store/editorStore";
import { reloadBuffer, saveBufferOverwriting } from "../../lib/saveFile";
import { openStaleDiff } from "../../lib/staleDiff";
import { EditorTabs } from "./EditorTabs";
// Lazy: these are the only things in CODE that need Monaco, and an editor
// with no file open should not have downloaded it.
const MonacoFileEditor = lazy(() =>
  import("./MonacoFileEditor").then((m) => ({ default: m.MonacoFileEditor })),
);
const GitDiffView = lazy(() =>
  import("./GitDiffView").then((m) => ({ default: m.GitDiffView })),
);
const HunkView = lazy(() =>
  import("../scm/HunkView").then((m) => ({ default: m.HunkView })),
);
const ConflictView = lazy(() =>
  import("../scm/ConflictView").then((m) => ({ default: m.ConflictView })),
);

function EmptyState() {
  return (
    <div className="canvas-hint" style={{ position: "static", height: "100%" }}>
      <div className="big">No file open</div>
      <div>
        Pick a file in the Explorer, or select a change in Source Control to
        review its diff.
      </div>
    </div>
  );
}

export function EditorWorkspace() {
  const tabs = useEditorStore((s) => s.tabs);
  const activeTabId = useEditorStore((s) => s.activeTabId);
  const activeTab = tabs.find((t) => t.id === activeTabId) ?? null;
  const conflict = useEditorStore((s) =>
    activeTab?.kind === "file" ? !!s.buffers[activeTab.path]?.conflict : false,
  );
  const hasBuffer = useEditorStore((s) =>
    activeTab?.kind === "file" ? !!s.buffers[activeTab.path] : false,
  );

  return (
    <div className="panel" style={{ background: "var(--bg-0)" }}>
      <EditorTabs />
      {activeTab?.kind === "file" && conflict && (
        <div className="conflict-bar">
          <span>
            ⚠ This file changed on disk since you opened it, and you have unsaved
            edits. Two versions exist — choose which one to keep.
          </span>
          <button
            className="btn subtle"
            title="Discard your edits and take what is on disk"
            onClick={() => void reloadBuffer(activeTab.path)}
          >
            Reload from disk
          </button>
          <button
            className="btn subtle"
            title="Compare your version against what is on disk"
            onClick={() => void openStaleDiff(activeTab.path)}
          >
            Compare
          </button>
          <button
            className="btn danger"
            title="Overwrite the version on disk with yours"
            onClick={() => void saveBufferOverwriting(activeTab.path)}
          >
            Keep mine
          </button>
        </div>
      )}
      <div className="panel-body" style={{ position: "relative", overflow: "hidden" }}>
       <Suspense fallback={<div className="empty" style={{ margin: "auto" }}>Loading editor…</div>}>
        {!activeTab && <EmptyState />}
        {activeTab?.kind === "file" &&
          (hasBuffer ? <MonacoFileEditor path={activeTab.path} /> : <EmptyState />)}
        {activeTab?.kind === "diff" && (
          <GitDiffView
            key={activeTab.id}
            path={activeTab.path}
            staged={activeTab.staged}
          />
        )}
        {activeTab?.kind === "hunks" && (
          <HunkView
            key={activeTab.id}
            path={activeTab.path}
            staged={activeTab.staged}
            onDone={() => useEditorStore.getState().closeTab(activeTab.id)}
          />
        )}
        {activeTab?.kind === "conflict" && (
          <ConflictView key={activeTab.id} path={activeTab.path} />
        )}
       </Suspense>
      </div>
    </div>
  );
}
