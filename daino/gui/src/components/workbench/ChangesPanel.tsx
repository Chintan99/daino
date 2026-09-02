import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { qk, useChangeDiff, useWorkspaceChanges } from "../../api/hooks";
import type { ChangeEntry, ChangeSet, Workspace } from "../../api/types";

const ACTION: Record<ChangeEntry["action"], string> = {
  created: "new",
  updated: "changed",
  deleted: "removed",
};

/**
 * What Daino changed, grouped by the step that changed it.
 *
 * Git for knowledge work, without asking anyone to learn git: a set of
 * documents, what happened to each, and two buttons. Rejecting restores the
 * previous version through the same history the Documents tab already shows —
 * this view decides *what* to undo, it does not store anything of its own.
 */
export function ChangesPanel({ workspace }: { workspace: Workspace }) {
  const { data } = useWorkspaceChanges(workspace.id);
  const changes = data?.changes ?? [];
  const [open, setOpen] = useState<string | null>(null);

  if (changes.length === 0) {
    return (
      <div className="empty">
        Nothing to review yet. What the agent writes appears here, grouped by the
        step that wrote it.
      </div>
    );
  }

  return (
    <div className="ws-changes">
      {changes.map((change) => (
        <ChangeCard
          key={change.id}
          workspace={workspace}
          change={change}
          openPath={open}
          onOpen={(path) => setOpen(open === path ? null : path)}
        />
      ))}
    </div>
  );
}

function ChangeCard({
  workspace,
  change,
  openPath,
  onOpen,
}: {
  workspace: Workspace;
  change: ChangeSet;
  openPath: string | null;
  onOpen: (path: string) => void;
}) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);

  const decide = async (accepted: boolean, path?: string) => {
    setBusy(true);
    try {
      await api.decideWorkspaceChange(workspace.id, change.id, { accepted, path });
      await qc.invalidateQueries({ queryKey: qk.workspaceChanges(workspace.id) });
      await qc.invalidateQueries({ queryKey: qk.workspaceItem(workspace.id) });
      await qc.invalidateQueries({ queryKey: ["workspaces", workspace.id, "artifact"] });
    } catch (err) {
      window.alert(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const pending = change.entries.filter((entry) => entry.status === "pending").length;

  return (
    <section className={`ws-change ${change.status}`}>
      <header className="ws-change-head">
        <span className="ws-change-title">
          Daino changed {change.entries.length}{" "}
          {change.entries.length === 1 ? "document" : "documents"}
        </span>
        <span className={`ws-change-status ${change.status}`}>{change.status}</span>
      </header>

      {change.summary && <p className="ws-change-summary">{change.summary}</p>}

      <ul className="ws-change-list">
        {change.entries.map((entry) => (
          <li key={entry.id} className={`ws-change-entry ${entry.status}`}>
            <button className="ws-change-path" onClick={() => onOpen(entry.path)}>
              {entry.path}
            </button>
            <span className="ws-change-action">{ACTION[entry.action]}</span>
            {entry.status === "pending" ? (
              <span className="ws-change-buttons">
                <button
                  className="btn icon"
                  title="Keep this change"
                  disabled={busy}
                  onClick={() => void decide(true, entry.path)}
                >
                  ✓
                </button>
                <button
                  className="btn icon"
                  title="Undo it — the previous version comes back"
                  disabled={busy}
                  onClick={() => void decide(false, entry.path)}
                >
                  ↺
                </button>
              </span>
            ) : (
              <span className="ws-change-decided">{entry.status}</span>
            )}
            {openPath === entry.path && (
              <ChangeDiff workspace={workspace} change={change} path={entry.path} />
            )}
          </li>
        ))}
      </ul>

      {pending > 0 && (
        <footer className="ws-change-foot">
          <button className="btn subtle" disabled={busy} onClick={() => void decide(true)}>
            Accept all
          </button>
          <button className="btn subtle" disabled={busy} onClick={() => void decide(false)}>
            Reject all
          </button>
        </footer>
      )}
    </section>
  );
}

/** The before and after of one artifact, as lines rather than as a merge tool. */
function ChangeDiff({
  workspace,
  change,
  path,
}: {
  workspace: Workspace;
  change: ChangeSet;
  path: string;
}) {
  const { data, isLoading } = useChangeDiff(workspace.id, change.id, path);
  if (isLoading) return <div className="hint">Reading history…</div>;
  if (!data) return null;
  if (data.note && data.lines.length === 0) return <div className="hint">{data.note}</div>;
  return (
    <pre className="ws-change-diff">
      {data.lines.map((line, index) => (
        <div key={index} className={`ws-diff-line ${markerClass(line.marker)}`}>
          <span className="ws-diff-marker">{line.marker}</span>
          {line.text}
        </div>
      ))}
    </pre>
  );
}

function markerClass(marker: string): string {
  return marker === "+" ? "added" : marker === "-" ? "removed" : "same";
}
