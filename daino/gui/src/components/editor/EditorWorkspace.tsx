import { useEditorStore } from "../../store/editorStore";
import { reloadBuffer } from "../../lib/saveFile";
import { EditorTabs } from "./EditorTabs";
import { MonacoFileEditor } from "./MonacoFileEditor";
import { GitDiffView } from "./GitDiffView";

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
            ⚠ This file changed on disk since you opened it. Saving may overwrite
            those changes.
          </span>
          <button
            className="btn danger"
            onClick={() => void reloadBuffer(activeTab.path)}
          >
            Reload from disk
          </button>
        </div>
      )}
      <div className="panel-body" style={{ position: "relative", overflow: "hidden" }}>
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
      </div>
    </div>
  );
}
