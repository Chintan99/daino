import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import Editor from "@monaco-editor/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, ApiError } from "../../api/client";
import { qk, useArtifact } from "../../api/hooks";
import { useEditorOptions, useMonacoTheme } from "../../lib/editorPrefs";
import { useUIStore } from "../../store/uiStore";
import { openFileInEditor } from "../../lib/openFile";
import { sendChatMessage } from "../../lib/agent";
import type { Workspace } from "../../api/types";
import { HistoryPanel } from "./HistoryPanel";
// Registers the Daino themes and the language workers on the monaco
// instance. Imported here rather than at app start so the 4 MB editor
// arrives with the first component that renders one.
import "../../lib/monaco";

type Mode = "read" | "edit" | "history";

const LANGUAGE: Record<string, string> = {
  ".md": "markdown",
  ".markdown": "markdown",
  ".json": "json",
  ".yaml": "yaml",
  ".yml": "yaml",
  ".csv": "plaintext",
  ".html": "html",
  ".txt": "plaintext",
};

/**
 * One document: rendered, editable, and with its history.
 *
 * Read mode is the default because these are documents rather than source —
 * most visits are to read one, and the rendered form is what the work is for.
 */
export function ArtifactView({ workspace }: { workspace: Workspace }) {
  const qc = useQueryClient();
  const path = useUIStore((s) => s.activeArtifactPath);
  const { data, isLoading } = useArtifact(workspace.id, path);
  const [mode, setMode] = useState<Mode>("read");
  const [draft, setDraft] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  // Set when a save collided with a newer version. Held rather than alerted,
  // because the choice it presents — reload or keep mine — is the whole point.
  const [conflict, setConflict] = useState("");
  const [exporting, setExporting] = useState("");
  const setArtifactPath = useUIStore((s) => s.setActiveArtifactPath);
  const theme = useMonacoTheme();
  const options = useEditorOptions({ wordWrap: "on", renderOverviewRuler: false });

  const content = data?.content ?? "";

  // A different document, or one the agent has just rewritten, replaces the
  // draft — unless there are unsaved edits, which must never be discarded.
  useEffect(() => {
    if (!dirty) setDraft(content);
  }, [content, dirty]);

  useEffect(() => {
    setMode("read");
    setDirty(false);
    setConflict("");
  }, [path]);

  const isMarkdown = !!path && /\.(md|markdown)$/i.test(path);

  const language = useMemo(() => {
    const suffix = path ? path.slice(path.lastIndexOf(".")) : "";
    return LANGUAGE[suffix] ?? "plaintext";
  }, [path]);

  if (!path) {
    return (
      <div className="empty" style={{ margin: "auto" }}>
        Select a document, or ask the agent to draft one.
      </div>
    );
  }

  /**
   * Save the draft, refusing to silently overwrite work done since it loaded.
   *
   * `force` is the deliberate "keep mine": the digest is dropped, so the write
   * goes through. The version it replaces is still a revision, so both sides of
   * the collision remain recoverable from HISTORY either way.
   */
  const save = async (force = false) => {
    setSaving(true);
    try {
      await api.writeArtifact(
        workspace.id,
        path,
        draft,
        force ? "" : (data?.artifact.digest ?? ""),
      );
      setDirty(false);
      setConflict("");
      await qc.invalidateQueries({ queryKey: qk.workspaceItem(workspace.id) });
      await qc.invalidateQueries({
        queryKey: qk.workspaceArtifact(workspace.id, path),
      });
      await qc.invalidateQueries({
        queryKey: qk.workspaceRevisions(workspace.id, path),
      });
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Not an error to dismiss: two versions exist and only the person
        // editing can say which one should win.
        setConflict(
          err.message ||
            "This document changed since you opened it. Reload it, or keep your version.",
        );
      } else {
        window.alert(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSaving(false);
    }
  };

  /** Throw the draft away and take what is on disk now. */
  const reload = async () => {
    setDirty(false);
    setConflict("");
    const fresh = await qc.fetchQuery({
      queryKey: qk.workspaceArtifact(workspace.id, path),
      queryFn: () => api.readArtifact(workspace.id, path),
    });
    setDraft(fresh.content);
  };

  /**
   * Render this document into a file someone can open.
   *
   * Only offered for markdown: the renderings are made *from* the markdown, so
   * exporting a .docx would mean exporting a rendering of a rendering.
   */
  const exportAs = async (format: string) => {
    if (!path) return;
    setExporting(format);
    try {
      const artifact = await api.createWorkspaceDeliverable(workspace.id, { path, format });
      await qc.invalidateQueries({ queryKey: qk.workspaceItem(workspace.id) });
      setArtifactPath(artifact.path);
    } catch (err) {
      window.alert(err instanceof Error ? err.message : String(err));
    } finally {
      setExporting("");
    }
  };

  const ask = () => {
    const repoPath = `${workspace.folder}/${path}`;
    sendChatMessage(
      `In the workspace "${workspace.name}", revise the document at ${repoPath}. ` +
        `Read it first, then make the change. `,
      { withContext: false },
    );
  };

  return (
    <div className="ws-viewer">
      <div className="ws-viewer-bar">
        <span className="ws-viewer-title" title={`${workspace.folder}/${path}`}>
          {data?.artifact.title ?? path}
        </span>
        {dirty && <span className="badge warn">unsaved</span>}
        <span className="grow" />
        <div className="segmented">
          <button
            className={mode === "read" ? "active" : ""}
            onClick={() => setMode("read")}
          >
            READ
          </button>
          <button
            className={mode === "edit" ? "active" : ""}
            onClick={() => setMode("edit")}
          >
            EDIT
          </button>
          <button
            className={mode === "history" ? "active" : ""}
            onClick={() => setMode("history")}
            title={`${data?.artifact.revisions ?? 0} saved versions`}
          >
            HISTORY {data?.artifact.revisions ? `(${data.artifact.revisions})` : ""}
          </button>
        </div>
        {mode === "edit" && (
          <button
            className="btn primary"
            disabled={!dirty || saving}
            onClick={() => void save()}
          >
            Save
          </button>
        )}
        <button className="btn subtle" onClick={ask} title="Ask the agent to revise it">
          Ask
        </button>
        {isMarkdown && (
          <ExportMenu workspace={workspace} path={path ?? ""} busy={exporting} onExport={exportAs} />
        )}
        <button
          className="btn subtle"
          title="Open in CODE"
          onClick={() => void openFileInEditor(`${workspace.folder}/${path}`)}
        >
          ↗
        </button>
      </div>

      {conflict && (
        <div className="conflict-bar">
          <span>⚠ {conflict}</span>
          <button className="btn subtle" onClick={() => void reload()}>
            Reload from disk
          </button>
          <button
            className="btn danger"
            title="Overwrite the newer version with yours — it stays in HISTORY"
            onClick={() => void save(true)}
          >
            Keep mine
          </button>
        </div>
      )}

      {isLoading && <div className="empty">Loading…</div>}
      {!isLoading && data && !data.readable && (
        <div className="empty">
          This file cannot be shown here — it is binary, or too large. Open it in
          CODE instead.
        </div>
      )}

      {!isLoading && data?.readable && mode === "read" && (
        <div className="scroll-y ws-doc-body">
          <div className="md-block">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
          </div>
        </div>
      )}

      {!isLoading && data?.readable && mode === "edit" && (
        <div className="ws-editor">
          <Editor
            value={draft}
            language={language}
            theme={theme}
            options={options}
            onChange={(value) => {
              setDraft(value ?? "");
              setDirty(true);
            }}
          />
        </div>
      )}

      {mode === "history" && <HistoryPanel workspace={workspace} path={path} />}
    </div>
  );
}


/** Word, Excel, PowerPoint, PDF — created beside the document they come from. */
function ExportMenu({
  busy,
  onExport,
}: {
  workspace: Workspace;
  path: string;
  busy: string;
  onExport: (format: string) => Promise<void>;
}) {
  const formats: [string, string][] = [
    ["docx", "Word"],
    ["pptx", "Deck"],
    ["xlsx", "Sheet"],
    ["pdf", "PDF"],
  ];
  return (
    <span className="ws-export">
      {formats.map(([format, label]) => (
        <button
          key={format}
          className="btn subtle sm"
          disabled={!!busy}
          title={`Create a .${format} from this document`}
          onClick={() => void onExport(format)}
        >
          {busy === format ? "…" : label}
        </button>
      ))}
    </span>
  );
}
