import { useState } from "react";
import type { Changeset, ChangesetFile } from "../../api/types";
import { openDiffInEditor, openFileInEditor } from "../../lib/openFile";

/**
 * What the turn edited, as one card at the end of it.
 *
 * The individual diffs already scrolled past while the agent worked; this
 * answers the question that remains afterwards — which files, and how much —
 * and makes each row a way into the change rather than a label for it: the name
 * opens the file, **Review** opens its diff beside the code.
 */
const PREVIEW_ROWS = 3;

function parse(metadata: Record<string, unknown>): Changeset | null {
  const files = metadata.files;
  if (!Array.isArray(files) || files.length === 0) return null;
  return {
    files: files as ChangesetFile[],
    added: Number(metadata.added ?? 0),
    removed: Number(metadata.removed ?? 0),
    verified: (metadata.verified ?? null) as boolean | null,
  };
}

export function ChangesetCard({ metadata }: { metadata: Record<string, unknown> }) {
  const [expanded, setExpanded] = useState(false);
  const changeset = parse(metadata);
  if (!changeset) return null;

  const { files, added, removed, verified } = changeset;
  const shown = expanded ? files : files.slice(0, PREVIEW_ROWS);
  const hidden = files.length - shown.length;

  return (
    <div className="changeset">
      <div className="changeset-head">
        <span className="mark" aria-hidden="true">
          ±
        </span>
        <span className="grow">
          <span className="title">
            Edited {files.length} file{files.length === 1 ? "" : "s"}
          </span>
          <span className="counts">
            <span className="added">+{added}</span>{" "}
            <span className="removed">-{removed}</span>
          </span>
        </span>
        {verified === false && (
          <span className="badge bad" title="Verification failed for this turn">
            unverified
          </span>
        )}
        <button
          className="btn sm"
          title="Open every changed file's diff"
          onClick={() => files.forEach((file) => openDiffInEditor(file.path, false))}
        >
          Review
        </button>
      </div>

      {shown.map((file) => {
        const directory = file.path.includes("/")
          ? file.path.slice(0, file.path.lastIndexOf("/") + 1)
          : "";
        const name = file.path.slice(directory.length);
        return (
          <div className="changeset-row" key={file.path}>
            <button
              className="path"
              title={`Open ${file.path}`}
              onClick={() => void openFileInEditor(file.path)}
            >
              {directory && <span className="dir">{directory}</span>}
              <span className="name">{name}</span>
              {file.change !== "modified" && (
                <span className="change">{file.change}</span>
              )}
            </button>
            <span className="counts">
              <span className="added">+{file.added}</span>{" "}
              <span className="removed">-{file.removed}</span>
            </span>
            <button
              className="btn subtle sm"
              title="Open this file's diff"
              onClick={() => openDiffInEditor(file.path, false)}
            >
              diff
            </button>
          </div>
        );
      })}

      {files.length > PREVIEW_ROWS && (
        <button className="changeset-more" onClick={() => setExpanded(!expanded)}>
          {expanded ? "Show fewer files" : `Show ${hidden} more file${hidden === 1 ? "" : "s"}`}
          <span className="chev">{expanded ? "⌃" : "⌄"}</span>
        </button>
      )}
    </div>
  );
}
