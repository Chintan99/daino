import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import { qk } from "../../api/hooks";
import { openFileInEditor } from "../../lib/openFile";
import type { Workspace } from "../../api/types";

/** Matches the server's ceiling, so a refusal is explained before the request. */
const MAX_BYTES = 8_000_000;

/**
 * Files brought into the workspace, and what Daino could make of them.
 *
 * A PDF or spreadsheet is unreadable to the agent as-is, so each upload is
 * extracted to markdown beside the original and it is the extraction the agent
 * reads. When that fails — a scan with no text layer, or a missing parser — the
 * row says so rather than leaving an empty document to be summarised
 * confidently.
 */
export function UploadPanel({ workspace }: { workspace: Workspace }) {
  const qc = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");

  const send = async (files: readonly File[]) => {
    if (files.length === 0 || busy) return;
    setBusy(true);
    setError("");
    try {
      for (const file of files) {
        if (file.size > MAX_BYTES) {
          setError(`${file.name} is larger than ${MAX_BYTES / 1_000_000} MB.`);
          continue;
        }
        await api.uploadToWorkspace(workspace.id, file.name, await encode(file));
      }
      await qc.invalidateQueries({ queryKey: qk.workspaceItem(workspace.id) });
      await qc.invalidateQueries({ queryKey: qk.workspaces });
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : err instanceof Error ? err.message : String(err),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ws-uploads">
      <div className="section-title">Uploads</div>

      <div
        className={`ws-drop ${dragging ? "over" : ""}`}
        onDragOver={(e) => {
          if (!e.dataTransfer.types.includes("Files")) return;
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          void send([...e.dataTransfer.files]);
        }}
        onClick={() => input.current?.click()}
      >
        {busy ? "Reading…" : "Drop files here, or click to choose"}
        <div className="muted">PDF, Word, Excel, PowerPoint, CSV, markdown, text</div>
        <input
          ref={input}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            void send([...(e.target.files ?? [])]);
            e.target.value = "";
          }}
        />
      </div>

      {error && <div className="ws-upload-error">{error}</div>}

      {workspace.uploads.length === 0 && !busy && (
        <div className="empty">Nothing uploaded yet.</div>
      )}

      {workspace.uploads.map((upload) => (
        <div key={upload.path} className="ws-upload">
          <div className="ws-upload-head">
            <span className="ws-upload-name">{upload.path.replace("uploads/", "")}</span>
            <span className="muted">{formatBytes(upload.bytes)}</span>
          </div>
          {upload.extracted_path ? (
            <button
              className="btn subtle sm"
              onClick={() => void openFileInEditor(upload.extracted_path)}
              title="Read the text Daino extracted"
            >
              Extracted text
            </button>
          ) : (
            <div className="ws-upload-warning">
              {upload.warning || "Daino cannot read this format."}
            </div>
          )}
          {upload.extracted_path && upload.warning && (
            <div className="ws-upload-warning">{upload.warning}</div>
          )}
        </div>
      ))}
    </div>
  );
}

/**
 * Base64 in chunks.
 *
 * ``String.fromCharCode(...bytes)`` on a whole multi-megabyte file overflows the
 * argument limit, which is why ``lib/attach.ts`` chunks too.
 */
async function encode(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

function formatBytes(value: number): string {
  if (value < 1000) return `${value} B`;
  if (value < 1_000_000) return `${Math.round(value / 1000)} KB`;
  return `${(value / 1_000_000).toFixed(1)} MB`;
}
