import { useEditorStore } from "../../store/editorStore";

export function EditorTabs() {
  const tabs = useEditorStore((s) => s.tabs);
  const buffers = useEditorStore((s) => s.buffers);
  const activeTabId = useEditorStore((s) => s.activeTabId);
  const setActiveTab = useEditorStore((s) => s.setActiveTab);
  const closeTab = useEditorStore((s) => s.closeTab);

  if (tabs.length === 0) return null;

  return (
    <div className="editor-tabs">
      {tabs.map((tab) => {
        const buf = tab.kind === "file" ? buffers[tab.path] : undefined;
        const dirty = !!buf?.dirty;
        return (
          <div
            key={tab.id}
            className={`editor-tab ${activeTabId === tab.id ? "active" : ""}`}
            onClick={() => setActiveTab(tab.id)}
            onAuxClick={(e) => {
              if (e.button === 1) closeTab(tab.id);
            }}
            title={
              tab.kind === "diff"
                ? `${tab.path} — ${tab.staged ? "staged changes" : "working tree"}`
                : tab.path
            }
          >
            <span>{tab.name}</span>
            {tab.kind === "diff" && (
              <span className="kind">
                {tab.staged ? "STAGED ⇄" : "DIFF ⇄"}
              </span>
            )}
            {dirty ? (
              <span className="dirty" title="Unsaved changes" />
            ) : (
              <span style={{ width: 7 }} />
            )}
            <span
              className="close"
              title="Close"
              onClick={(e) => {
                e.stopPropagation();
                if (
                  !dirty ||
                  window.confirm(`Discard unsaved changes to ${tab.name}?`)
                )
                  closeTab(tab.id);
              }}
            >
              ✕
            </span>
          </div>
        );
      })}
    </div>
  );
}
