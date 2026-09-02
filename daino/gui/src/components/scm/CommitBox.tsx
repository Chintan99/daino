import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";

/**
 * Compose a commit from what is staged.
 *
 * Deliberately says how many files it will include, because the one thing
 * people get wrong about a commit box is what is in it. Nothing here stages
 * anything: the button commits the index exactly as the sections above left it.
 */
export function CommitBox() {
  const qc = useQueryClient();
  const [message, setMessage] = useState("");
  const [amend, setAmend] = useState(false);
  const [busy, setBusy] = useState(false);
  const { data: context } = useQuery({
    queryKey: ["git", "commit-context"],
    queryFn: api.gitCommitContext,
  });

  // Amending is only useful if it starts from the message being amended.
  useEffect(() => {
    if (amend && context?.previous_message && !message) {
      setMessage(context.previous_message);
    }
  }, [amend, context?.previous_message, message]);

  if (!context?.repository) return null;
  const staged = context.staged ?? [];
  const merging = !!context.merging;
  const conflicts = context.conflicts ?? [];
  const ready = merging || amend || staged.length > 0;

  const commit = async () => {
    setBusy(true);
    try {
      await api.gitCommit(message.trim(), { amend });
      setMessage("");
      setAmend(false);
      await qc.invalidateQueries({ queryKey: ["git"] });
    } catch (err) {
      window.alert(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="scm-commit">
      {merging && (
        <div className="scm-merge-note">
          <strong>Merge in progress</strong>
          {conflicts.length > 0 ? (
            <div className="muted">
              {conflicts.length} file{conflicts.length === 1 ? "" : "s"} still
              conflicted. Resolve them before committing.
            </div>
          ) : (
            <div className="muted">All conflicts resolved — commit to finish.</div>
          )}
        </div>
      )}
      <textarea
        className="input"
        rows={3}
        placeholder={
          merging ? "Merge commit message" : "Message (what changed, and why)"
        }
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        onKeyDown={(e) => {
          // ⌘/Ctrl+Enter commits, matching every other editor.
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && message.trim() && ready) {
            e.preventDefault();
            void commit();
          }
        }}
      />
      <div className="scm-commit-actions">
        <button
          className="btn primary sm"
          disabled={busy || !message.trim() || !ready || conflicts.length > 0}
          title={
            conflicts.length > 0
              ? "Resolve the remaining conflicts first"
              : staged.length === 0 && !amend && !merging
                ? "Stage something to commit"
                : "Commit the staged changes"
          }
          onClick={() => void commit()}
        >
          {busy
            ? "Committing…"
            : amend
              ? "Amend commit"
              : `Commit${staged.length ? ` ${staged.length} file${staged.length === 1 ? "" : "s"}` : ""}`}
        </button>
        {context.can_amend && (
          <label className="check sm" title="Replace the previous commit instead">
            <input
              type="checkbox"
              checked={amend}
              onChange={(e) => {
                setAmend(e.target.checked);
                if (!e.target.checked) setMessage("");
              }}
            />
            Amend
          </label>
        )}
      </div>
    </div>
  );
}
