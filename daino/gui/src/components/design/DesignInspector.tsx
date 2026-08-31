import { useEffect, useState } from "react";
import Editor from "@monaco-editor/react";
import { useDesign, useDesignMutations } from "../../api/hooks";
import { api, ApiError } from "../../api/client";
import { useDesignStore } from "../../store/designStore";
import { promptFor } from "../../store/dialogStore";
import { useAgentStore } from "../../store/agentStore";
import { sendChatMessage } from "../../lib/agent";
import { exportArtifact } from "../../lib/exportDesign";
import { useEditorOptions, useMonacoTheme } from "../../lib/editorPrefs";
import { BRAND } from "../../lib/branding";

const LANGUAGE: Record<string, string> = {
  html: "html",
  svg: "xml",
  markdown: "markdown",
  text: "plaintext",
};

export function DesignInspector({
  designId,
  onNotice,
}: {
  designId: string;
  onNotice: (message: string) => void;
}) {
  const { data: design } = useDesign(designId);
  const monacoTheme = useMonacoTheme();
  const sourceOptions = useEditorOptions({ lineNumbers: "off" });
  const m = useDesignMutations(designId);
  const selectedNodeIds = useDesignStore((s) => s.selectedNodeIds);
  const sourceNodeId = useDesignStore((s) => s.sourceNodeId);
  const setSourceNode = useDesignStore((s) => s.setSourceNode);
  const setViewerNode = useDesignStore((s) => s.setViewerNode);
  const addChip = useAgentStore((s) => s.addChip);
  const removeChip = useAgentStore((s) => s.removeChip);

  const selected =
    design && selectedNodeIds.length === 1
      ? design.nodes.find((n) => n.id === selectedNodeIds[0])
      : undefined;
  const data = (selected?.data ?? {}) as Record<string, unknown>;
  const kind = String(data.kind ?? "");
  const isArtifact = !!kind;

  const [label, setLabel] = useState("");
  const [source, setSource] = useState("");
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    setLabel(selected?.label ?? "");
  }, [selected?.id, selected?.label]);

  useEffect(() => {
    setSource(String((selected?.data as Record<string, unknown>)?.content ?? ""));
    setDirty(false);
  }, [selected?.id, selected?.data]);

  // Selecting node(s) adds/updates a design-node context chip.
  useEffect(() => {
    if (selectedNodeIds.length > 0) {
      addChip({
        id: "design_node",
        kind: "design_node",
        label: `design: ${selectedNodeIds.join(", ")}`,
        payload: {
          workspace: "design",
          design_id: designId,
          selected_nodes: selectedNodeIds,
        },
      });
    } else {
      removeChip("design_node");
    }
  }, [selectedNodeIds, designId, addChip, removeChip]);

  const editing = !!selected && sourceNodeId === selected.id && kind !== "image";

  const saveSource = () => {
    if (!selected) return;
    m.patchNode.mutate({
      nodeId: selected.id,
      body: { data: { ...data, content: source } },
    });
    setDirty(false);
  };

  /** Write the artifact back into the repository so the agent can work on it. */
  const saveToProject = async () => {
    if (!selected) return;
    const suggested = String(data.filename || `${selected.label || "artifact"}.html`);
    const path = await promptFor({
      title: "Save to project",
      hint: "Path relative to the project root",
      initial: suggested,
      confirmLabel: "Save",
    });
    if (!path?.trim()) return;
    try {
      try {
        await api.createFile(path.trim(), false);
      } catch (err) {
        // Already exists is fine; anything else is a real failure.
        if (!(err instanceof ApiError && err.status === 409)) throw err;
      }
      const current = await api.readFile(path.trim());
      await api.writeFile(path.trim(), source, current.hash);
      onNotice(`Saved ${path.trim()}.`);
    } catch (err) {
      onNotice(
        `Could not save: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  };

  return (
    <div className="design-inspector">
      <div className="panel-header">Inspector</div>
      <div className="pad">
        <button
          className="btn primary"
          style={{ width: "100%", marginBottom: 12 }}
          onClick={() =>
            sendChatMessage(
              `Implement this design (id: ${designId}). First inspect the repository and propose a plan before writing any code.`,
              { withContext: false },
            )
          }
          title={`Ask ${BRAND} to implement this canvas`}
        >
          Implement with {BRAND}
        </button>

        {!selected && (
          <div className="muted" style={{ fontSize: "var(--fs-12)" }}>
            {selectedNodeIds.length > 1
              ? `${selectedNodeIds.length} items selected.`
              : "Select an item to inspect it, or drop a file onto the canvas."}
          </div>
        )}

        {selected && (
          <>
            <div className="field">
              <label>Label</label>
              <input
                className="input"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                onBlur={() => {
                  if (label !== selected.label)
                    m.patchNode.mutate({ nodeId: selected.id, body: { label } });
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                }}
              />
            </div>
            <div className="field">
              <label>Kind</label>
              <div>{kind || selected.type || "default"}</div>
            </div>
            {isArtifact && data.filename ? (
              <div className="field">
                <label>Source file</label>
                <div className="mono" style={{ fontSize: "var(--fs-11)" }}>
                  {String(data.filename)}
                </div>
              </div>
            ) : null}
            <div className="field">
              <label>Position</label>
              <div className="mono" style={{ fontSize: "var(--fs-11)" }}>
                x {Math.round(selected.position.x)}, y{" "}
                {Math.round(selected.position.y)}
              </div>
            </div>

            {isArtifact && (
              <button
                className="btn"
                style={{ width: "100%", marginBottom: 8 }}
                onClick={() => setViewerNode(selected.id)}
              >
                ⛶ Open full screen
              </button>
            )}

            {isArtifact && kind !== "image" && (
              <>
                <div className="row" style={{ marginBottom: 8 }}>
                  <button
                    className="btn sm"
                    onClick={() => setSourceNode(editing ? null : selected.id)}
                  >
                    {editing ? "Hide source" : "Edit source"}
                  </button>
                  <button className="btn sm" onClick={() => void saveToProject()}>
                    Save to project…
                  </button>
                </div>
                {editing && (
                  <div
                    style={{
                      height: 320,
                      border: "1px solid var(--border)",
                      borderRadius: "var(--radius-sm)",
                      overflow: "hidden",
                      marginBottom: 8,
                    }}
                  >
                    <Editor
                      value={source}
                      language={LANGUAGE[kind] ?? "plaintext"}
                      theme={monacoTheme}
                      onChange={(value) => {
                        setSource(value ?? "");
                        setDirty(true);
                      }}
                      options={sourceOptions}
                    />
                  </div>
                )}
                {editing && (
                  <button
                    className="btn primary"
                    style={{ width: "100%", marginBottom: 8 }}
                    disabled={!dirty || m.patchNode.isPending}
                    onClick={saveSource}
                  >
                    {dirty ? "Apply changes" : "Saved"}
                  </button>
                )}
              </>
            )}

            {isArtifact && (
              <button
                className="btn"
                style={{ width: "100%", marginBottom: 8 }}
                onClick={() => exportArtifact(selected)}
              >
                Export this file
              </button>
            )}

            <button
              className="btn danger"
              style={{ width: "100%" }}
              onClick={() => {
                setSourceNode(null);
                m.deleteNode.mutate(selected.id);
              }}
            >
              Delete
            </button>
          </>
        )}
      </div>
    </div>
  );
}
