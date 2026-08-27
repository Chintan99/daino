import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../../api/client";
import { qk } from "../../../api/hooks";
import type { AgentConfig, EffectiveInstructions } from "../../../api/types";
import { useEditorStore } from "../../../store/editorStore";
import { openFileInEditor } from "../../../lib/openFile";

/**
 * DAINO.md instructions — always-on guidance, in precedence layers.
 *
 * The list is what the resolver can actually pick up: the user-level file, the
 * repository file, and any scoped file in a subdirectory. Because layers
 * override each other, "which rule wins for the file I have open?" is a real
 * question, so the panel answers it with the resolver's own output rather than
 * leaving the user to guess.
 */
export function InstructionsSection({
  config,
  sessionId,
}: {
  config: AgentConfig;
  sessionId: string;
}) {
  const qc = useQueryClient();
  const activePath = useEditorStore((s) => s.activePath);
  const [globalDraft, setGlobalDraft] = useState<string | null>(null);
  const [effective, setEffective] = useState<EffectiveInstructions | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const files = config.instructions.files;

  const editGlobal = async () => {
    setBusy("global");
    setError("");
    try {
      const answer = await api.globalInstructions();
      setGlobalDraft(answer.content);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const saveGlobal = async () => {
    if (globalDraft === null) return;
    setBusy("global");
    setError("");
    try {
      await api.saveGlobalInstructions(globalDraft);
      setGlobalDraft(null);
      await qc.invalidateQueries({ queryKey: qk.agentConfig(sessionId) });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const createRepositoryFile = async (relative: string) => {
    setBusy("create");
    setError("");
    try {
      await api.createFile(relative, false);
      await qc.invalidateQueries({ queryKey: ["files", "tree"] });
      await qc.invalidateQueries({ queryKey: qk.agentConfig(sessionId) });
      await openFileInEditor(relative);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  // Show what applies to the file being edited; that is the useful question.
  useEffect(() => {
    let cancelled = false;
    api
      .effectiveInstructions(activePath ?? "")
      .then((answer) => {
        if (!cancelled) setEffective(answer);
      })
      .catch(() => {
        if (!cancelled) setEffective(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activePath, config]);

  return (
    <div className="cfg-section">
      <div className="section-title">Instruction files</div>
      <div className="muted field-hint">
        Closer files override broader ones. Your request and the repository's own
        source always win over all of them.
      </div>

      {files.map((file) => (
        <div className={`inst-row ${file.exists ? "" : "missing"}`} key={file.path}>
          <span className="grow">
            <span className="name">{file.label}</span>
            <span className="detail mono">
              {file.relative_path || file.path}
              {file.exists ? ` · ${file.bytes} B` : " · not created"}
            </span>
          </span>
          {file.scope === "global" ? (
            <button className="btn sm" disabled={busy !== ""} onClick={editGlobal}>
              {file.exists ? "Edit" : "Create"}
            </button>
          ) : file.exists ? (
            <button
              className="btn sm"
              onClick={() => void openFileInEditor(file.relative_path)}
            >
              Open
            </button>
          ) : (
            <button
              className="btn sm"
              disabled={busy !== ""}
              onClick={() => void createRepositoryFile(file.relative_path)}
            >
              Create
            </button>
          )}
        </div>
      ))}

      {globalDraft !== null && (
        <div className="inst-editor">
          <div className="muted field-hint">
            Your user-level instructions, shared by every project.
          </div>
          <textarea
            className="input"
            rows={10}
            value={globalDraft}
            maxLength={config.instructions.max_bytes}
            onChange={(e) => setGlobalDraft(e.target.value)}
            placeholder={"# My conventions\\nprefer small commits\\n"}
          />
          <div className="provider-actions">
            <span className="grow" />
            <button className="btn subtle" onClick={() => setGlobalDraft(null)}>
              Cancel
            </button>
            <button className="btn primary" disabled={busy !== ""} onClick={saveGlobal}>
              {busy === "global" ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      )}

      {effective && (
        <>
          <div className="section-title">
            {activePath ? `Applies to ${activePath}` : "Applies by default"}
          </div>
          {effective.sources.length === 0 ? (
            <div className="empty">No instructions apply yet.</div>
          ) : (
            <>
              <div className="muted field-hint">
                {effective.sources.length} layer
                {effective.sources.length === 1 ? "" : "s"}, broadest first.
              </div>
              <pre className="inst-preview">{effective.text}</pre>
            </>
          )}
        </>
      )}

      {error && <div className="test-result bad">{error}</div>}
    </div>
  );
}
