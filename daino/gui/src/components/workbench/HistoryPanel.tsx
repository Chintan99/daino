import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { DiffEditor } from "@monaco-editor/react";
import { api } from "../../api/client";
import { qk, useArtifact, useArtifactRevisions } from "../../api/hooks";
import { useEditorOptions, useMonacoTheme } from "../../lib/editorPrefs";
import { confirmFor } from "../../store/dialogStore";
import type { Workspace } from "../../api/types";
import { fmtDateTime } from "../insights/format";

/**
 * Every saved version of one document, and the way back to any of them.
 *
 * The case this exists for is an agent rewriting a document you had edited.
 * Versions are recorded from the file-change event rather than from the write
 * path, so a hand edit in CODE and an agent edit are captured identically —
 * which is exactly the pair you need to be able to compare.
 */
export function HistoryPanel({
  workspace,
  path,
}: {
  workspace: Workspace;
  path: string;
}) {
  const qc = useQueryClient();
  const { data } = useArtifactRevisions(workspace.id, path);
  const { data: current } = useArtifact(workspace.id, path);
  const [selected, setSelected] = useState<number | null>(null);
  const [previous, setPrevious] = useState("");
  const [busy, setBusy] = useState(false);
  const theme = useMonacoTheme();
  const options = useEditorOptions({
    readOnly: true,
    renderSideBySide: true,
    originalEditable: false,
    renderOverviewRuler: false,
  });

  const revisions = data?.revisions ?? [];

  // Default to the newest version that is not simply what is on screen.
  useEffect(() => {
    if (selected === null && revisions.length > 1) setSelected(revisions[1].version);
  }, [revisions, selected]);

  useEffect(() => {
    if (selected === null) return;
    let live = true;
    void api
      .artifactRevision(workspace.id, path, selected)
      .then((result) => live && setPrevious(result.content))
      .catch(() => live && setPrevious(""));
    return () => {
      live = false;
    };
  }, [workspace.id, path, selected]);

  const restore = async (version: number) => {
    const ok = await confirmFor({
      title: `Restore version ${version}`,
      message:
        "The current text is kept as a new version, so this can be undone the same way.",
      confirmLabel: "Restore",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await api.restoreArtifactRevision(workspace.id, path, version);
      await qc.invalidateQueries({ queryKey: qk.workspaceItem(workspace.id) });
      await qc.invalidateQueries({ queryKey: qk.workspaceArtifact(workspace.id, path) });
      await qc.invalidateQueries({ queryKey: qk.workspaceRevisions(workspace.id, path) });
      setSelected(null);
    } finally {
      setBusy(false);
    }
  };

  if (revisions.length === 0) {
    return <div className="empty">No saved versions yet.</div>;
  }

  return (
    <div className="split" style={{ flex: 1 }}>
      <div className="split-left" style={{ width: 240 }}>
        <div className="scroll-y" style={{ flex: 1 }}>
          {revisions.map((revision, index) => (
            <div
              key={revision.version}
              className={`ws-revision ${selected === revision.version ? "active" : ""}`}
              onClick={() => setSelected(revision.version)}
            >
              <div className="ws-revision-head">
                <span className="mono">v{revision.version}</span>
                <span className={`badge ${revision.author === "agent" ? "info" : ""}`}>
                  {revision.author}
                </span>
                {index === 0 && <span className="badge ok">current</span>}
              </div>
              <div className="muted">{fmtDateTime(revision.saved_at)}</div>
              {index > 0 && (
                <button
                  className="btn subtle sm"
                  disabled={busy}
                  onClick={(e) => {
                    e.stopPropagation();
                    void restore(revision.version);
                  }}
                >
                  Restore
                </button>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className="split-right">
        {selected === null ? (
          <div className="empty">Select a version to compare it with the current text.</div>
        ) : (
          <div className="ws-editor">
            <DiffEditor
              original={previous}
              modified={current?.content ?? ""}
              language="markdown"
              theme={theme}
              options={options}
            />
          </div>
        )}
      </div>
    </div>
  );
}
