import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import Editor from "@monaco-editor/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../../api/client";
import { qk, useArtifact } from "../../api/hooks";
import { useEditorOptions, useMonacoTheme } from "../../lib/editorPrefs";
import { useUIStore } from "../../store/uiStore";
import { openFileInEditor } from "../../lib/openFile";
import { sendChatMessage } from "../../lib/agent";
import type { Workspace } from "../../api/types";
import { HistoryPanel } from "./HistoryPanel";

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

  const save = async () => {
    setSaving(true);
    try {
      await api.writeArtifact(workspace.id, path, draft);
      setDirty(false);
      await qc.invalidateQueries({ queryKey: qk.workspaceItem(workspace.id) });
      await qc.invalidateQueries({
        queryKey: qk.workspaceArtifact(workspace.id, path),
      });
      await qc.invalidateQueries({
        queryKey: qk.workspaceRevisions(workspace.id, path),
      });
    } catch (err) {
      window.alert(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
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
