import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { qk } from "../../api/hooks";
import { useRenameStore } from "../../store/renameStore";
import { reloadBuffer } from "../../lib/saveFile";
import { useEditorStore } from "../../store/editorStore";
import { openFileInEditor } from "../../lib/openFile";

/**
 * What a rename is about to do, before it does it.
 *
 * Shows every file and every line, because "rename this symbol" reads as a
 * local act and is frequently not one — the number of files is the fact people
 * most need and least expect. Any open buffer is reloaded afterwards, so the
 * editor never sits on a version the rename has already replaced.
 */
export function RenamePreview() {
  const pending = useRenameStore((s) => s.pending);
  const clear = useRenameStore((s) => s.clear);
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  if (!pending) return null;

  const paths = Object.keys(pending.edits).sort();

  const apply = async () => {
    setBusy(true);
    try {
      const { written } = await api.applyRename(pending.edits);
      clear();
      // A buffer showing the old text after the file changed underneath it is
      // exactly the stale-buffer problem, so refresh what is open.
      const open = useEditorStore.getState().buffers;
      for (const path of written) {
        if (open[path]) await reloadBuffer(path);
      }
      await qc.invalidateQueries({ queryKey: qk.gitStatus });
    } catch (err) {
      window.alert(
        `Could not apply the rename: ${
          err instanceof Error ? err.message : String(err)
        }`,
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="dialog-backdrop" onClick={clear}>
      <div
        className="dialog"
        onClick={(e) => e.stopPropagation()}
        style={{ width: "min(760px, 92vw)", maxHeight: "80vh" }}
      >
        <h3>
          Rename {pending.symbol ? <code>{pending.symbol}</code> : "symbol"} to{" "}
          <code>{pending.newName}</code>
        </h3>
        <div className="muted" style={{ fontSize: "var(--fs-12)" }}>
          {pending.count} edit{pending.count === 1 ? "" : "s"} across{" "}
          {pending.files} file{pending.files === 1 ? "" : "s"}. Nothing is
          written until you apply.
        </div>
        <div className="scroll-y" style={{ flex: 1, minHeight: 0 }}>
          <table className="dtable">
            <thead>
              <tr>
                <th>File</th>
                <th style={{ width: 90 }}>Edits</th>
                <th style={{ width: 160 }}>Lines</th>
              </tr>
            </thead>
            <tbody>
              {paths.map((path) => (
                <tr
                  key={path}
                  className="click"
                  onClick={() =>
                    void openFileInEditor(path, {
                      line: pending.edits[path][0]?.start_line ?? 1,
                      column: pending.edits[path][0]?.start_column ?? 1,
                    })
                  }
                >
                  <td className="mono ellipsis" title={path}>
                    {path}
                  </td>
                  <td className="num">{pending.edits[path].length}</td>
                  <td className="muted mono">
                    {pending.edits[path]
                      .slice(0, 6)
                      .map((edit) => edit.start_line)
                      .join(", ")}
                    {pending.edits[path].length > 6 ? " …" : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="actions">
          <button className="btn subtle" onClick={clear} disabled={busy}>
            Cancel
          </button>
          <button
            className="btn primary"
            onClick={() => void apply()}
            disabled={busy}
          >
            {busy ? "Applying…" : `Rename in ${pending.files} file${pending.files === 1 ? "" : "s"}`}
          </button>
        </div>
      </div>
    </div>
  );
}
