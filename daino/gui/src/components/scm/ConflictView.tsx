import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DiffEditor } from "@monaco-editor/react";
import { api, ApiError } from "../../api/client";
import { useEditorOptions, useMonacoTheme } from "../../lib/editorPrefs";
import { openFileInEditor } from "../../lib/openFile";
// Registers the Daino themes and the language workers on the monaco
// instance. Imported here rather than at app start so the 4 MB editor
// arrives with the first component that renders one.
import "../../lib/monaco";

/**
 * Resolving a merge conflict, with all three sides visible.
 *
 * "Ours" and "theirs" are shown as content rather than as flags, because those
 * words reverse their meaning during a rebase and picking the wrong one is a
 * well-known way to lose an afternoon. Here you are choosing between two texts
 * you can read, not between two labels you have to remember the rules for.
 */
export function ConflictView({ path }: { path: string }) {
  const qc = useQueryClient();
  const theme = useMonacoTheme();
  const [busy, setBusy] = useState(false);
  const options = useEditorOptions({
    readOnly: true,
    renderSideBySide: true,
    renderOverviewRuler: false,
    originalEditable: false,
  });
  const { data, isLoading } = useQuery({
    queryKey: ["git", "conflict", path],
    queryFn: () => api.gitConflictSides(path),
  });

  const resolve = async (side: "ours" | "theirs") => {
    setBusy(true);
    try {
      await api.gitResolveConflict(path, side);
      await qc.invalidateQueries({ queryKey: ["git"] });
    } catch (err) {
      window.alert(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const keepEdited = async () => {
    setBusy(true);
    try {
      await api.gitMarkResolved([path]);
      await qc.invalidateQueries({ queryKey: ["git"] });
    } catch (err) {
      window.alert(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (isLoading) return <div className="empty">Loading…</div>;
  if (!data) return <div className="empty">This file is no longer conflicted.</div>;

  return (
    <div className="panel" style={{ height: "100%" }}>
      <div className="toolbar">
        <span className="mono ellipsis" title={path}>
          {path}
        </span>
        <span className="badge warn">conflict</span>
        <span className="grow" />
        <button
          className="btn subtle sm"
          disabled={busy || data.ours === null}
          title="Keep this branch's version of the whole file"
          onClick={() => void resolve("ours")}
        >
          Take ours
        </button>
        <button
          className="btn subtle sm"
          disabled={busy || data.theirs === null}
          title="Take the incoming version of the whole file"
          onClick={() => void resolve("theirs")}
        >
          Take theirs
        </button>
        <button
          className="btn subtle sm"
          title="Open the file and resolve it by hand"
          onClick={() => void openFileInEditor(path)}
        >
          Edit
        </button>
        <button
          className="btn primary sm"
          disabled={busy}
          title="Accept the file as it stands now"
          onClick={() => void keepEdited()}
        >
          Mark resolved
        </button>
      </div>
      <div className="conflict-legend">
        <span>
          <strong>Left:</strong> ours — this branch
        </span>
        <span>
          <strong>Right:</strong> theirs — the incoming change
        </span>
        {data.base === null && (
          <span className="muted">No common ancestor: both sides added this file.</span>
        )}
      </div>
      <div style={{ flex: 1, minHeight: 0 }}>
        <DiffEditor
          original={data.ours ?? "(deleted on this branch)"}
          modified={data.theirs ?? "(deleted on the incoming branch)"}
          language={data.language}
          theme={theme}
          options={options}
          height="100%"
        />
      </div>
    </div>
  );
}
