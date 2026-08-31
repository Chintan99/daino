// The Design tab's left panel: the canvases on top, the project's files below.
//
// Replaces the old canvas dropdown. A canvas is one click away, and a file can
// be placed on the active canvas (single click) or opened in the code editor
// (double click) without leaving the design surface.
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { qk, useDesigns, useFileTree } from "../../api/hooks";
import { api } from "../../api/client";
import { useDesignStore } from "../../store/designStore";
import { openFileInEditor } from "../../lib/openFile";
import { placeFileOnCanvas } from "../../lib/placeOnCanvas";
import { FileTreeNode, type TreeCallbacks } from "../explorer/FileTreeNode";

export function DesignSidebar({
  onNotice,
}: {
  onNotice: (message: string) => void;
}) {
  const qc = useQueryClient();
  const { data } = useDesigns();
  const { data: tree, isLoading } = useFileTree("");
  const activeId = useDesignStore((s) => s.activeDesignId);
  const setActive = useDesignStore((s) => s.setActiveDesign);
  const [busy, setBusy] = useState(false);

  const designs = data?.designs ?? [];

  const newCanvas = async () => {
    setBusy(true);
    try {
      const name = `Canvas ${designs.length + 1}`;
      const created = await api.createDesign(name, "prototype");
      qc.setQueryData(qk.design(created.id), created);
      await qc.invalidateQueries({ queryKey: qk.designs });
      setActive(created.id);
    } finally {
      setBusy(false);
    }
  };

  // Single click drops the file on the active canvas; double click opens it in
  // the editor. A slight offset keeps successive placements from stacking.
  const placeOnCanvas = async (path: string) => {
    if (!activeId) {
      onNotice("Select or create a canvas first.");
      return;
    }
    const count = qc.getQueryData<{ nodes: unknown[] }>(qk.design(activeId))?.nodes.length ?? 0;
    const offset = (count % 8) * 28;
    const result = await placeFileOnCanvas(activeId, path, {
      x: 80 + offset,
      y: 80 + offset,
    });
    if (!result.ok) {
      onNotice(result.message);
      return;
    }
    qc.setQueryData(qk.design(activeId), result.design);
    void qc.invalidateQueries({ queryKey: qk.designs });
  };

  const cb: TreeCallbacks = {
    onOpen: (p) => void placeOnCanvas(p),
    onFileDoubleClick: (p) => void openFileInEditor(p),
    onContextMenu: (e) => e.preventDefault(),
    gitMap: {},
    activePath: null,
  };

  return (
    <div className="design-sidebar">
      <div className="panel">
        <div className="panel-header">
          <span>Canvases</span>
          <span className="grow" />
          <button
            className="btn icon"
            disabled={busy}
            title="New blank canvas"
            onClick={() => void newCanvas()}
          >
            ＋
          </button>
        </div>
        <div className="canvas-list">
          {designs.length === 0 && (
            <div className="tree-row muted">No canvases yet</div>
          )}
          {designs.map((d) => (
            <button
              key={d.id}
              className={`canvas-row ${d.id === activeId ? "active" : ""}`}
              onClick={() => setActive(d.id)}
              title={d.name}
            >
              <span className="canvas-name">{d.name}</span>
              <span className="canvas-meta">
                {d.node_count}·{d.edge_count}
              </span>
            </button>
          ))}
        </div>
      </div>

      <div className="panel design-files">
        <div className="panel-header">
          <span>Files</span>
          <span className="grow" />
          <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
            click ▸ canvas · dbl ▸ code
          </span>
        </div>
        <div className="panel-body tree">
          {isLoading && <div className="tree-row muted">Loading…</div>}
          {tree?.entries
            ?.slice()
            .sort((a, b) => {
              if (a.type !== b.type) return a.type === "directory" ? -1 : 1;
              return a.name.localeCompare(b.name);
            })
            .map((entry) => (
              <FileTreeNode key={entry.path} entry={entry} depth={0} cb={cb} />
            ))}
        </div>
      </div>
    </div>
  );
}
