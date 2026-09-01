import { openFileInEditor } from "../../lib/openFile";
import type { Workspace } from "../../api/types";
import { fmtDateTime } from "../insights/format";

/**
 * Every page the agent read while researching this workspace.
 *
 * Registered automatically when a fetch succeeds rather than when the model
 * remembers to record one — a bibliography that depends on the model's
 * diligence is a bibliography with holes in it. The fetched text is cached, so
 * a claim stays checkable after the page has changed or gone.
 */
export function SourcesPanel({ workspace }: { workspace: Workspace }) {
  if (workspace.sources.length === 0) {
    return (
      <div className="ws-sources">
        <div className="section-title">Sources</div>
        <div className="empty">
          Nothing read yet. Ask the agent to research something and every page it
          opens is recorded here.
        </div>
      </div>
    );
  }

  return (
    <div className="ws-sources">
      <div className="section-title">Sources — {workspace.sources.length}</div>
      {workspace.sources.map((source, index) => (
        <div key={source.id} className="ws-source">
          <div className="ws-source-head">
            <span className="ws-source-index mono">[{index + 1}]</span>
            <a
              className="ws-source-title"
              href={source.url}
              target="_blank"
              rel="noreferrer noopener"
              title={source.url}
            >
              {source.title || source.url}
            </a>
          </div>
          <div className="mono muted ws-source-url">{source.url}</div>
          {source.snippet && <div className="ws-source-snippet">{source.snippet}</div>}
          <div className="ws-source-meta">
            <span className="muted">{fmtDateTime(source.retrieved_at)}</span>
            {source.cache_path && (
              <button
                className="btn subtle sm"
                onClick={() => void openFileInEditor(source.cache_path)}
                title="The text as Daino read it"
              >
                Cached text
              </button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
