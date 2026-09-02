import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";

/**
 * One file's change, hunk by hunk, each individually stageable.
 *
 * The reason to have this at all: a file usually holds more than one idea, and
 * committing them together is how a history stops being reviewable. Selecting
 * hunks lets one commit be one idea without stashing the rest.
 */
export function HunkView({
  path,
  staged,
  onDone,
}: {
  path: string;
  staged: boolean;
  onDone: () => void;
}) {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const { data, isLoading } = useQuery({
    queryKey: ["git", "hunks", path, staged],
    queryFn: () => api.gitHunks(path, staged),
  });

  const toggle = (index: number) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });

  const apply = async () => {
    if (selected.size === 0) return;
    setBusy(true);
    try {
      const indices = [...selected].sort((a, b) => a - b);
      if (staged) await api.gitUnstageHunks(path, indices);
      else await api.gitStageHunks(path, indices);
      setSelected(new Set());
      await qc.invalidateQueries({ queryKey: ["git"] });
      onDone();
    } catch (err) {
      window.alert(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (isLoading) return <div className="empty">Loading…</div>;
  if (data?.binary) {
    return <div className="empty">A binary file has no hunks to choose between.</div>;
  }
  if (!data?.hunks.length) {
    return (
      <div className="empty">
        {staged ? "Nothing staged for this file." : "No unstaged change in this file."}
      </div>
    );
  }

  return (
    <div className="panel" style={{ height: "100%" }}>
      <div className="toolbar">
        <span className="mono ellipsis" title={path}>
          {path}
        </span>
        <span className="badge">{staged ? "staged" : "working tree"}</span>
        <span className="grow" />
        <button
          className="btn subtle sm"
          onClick={() =>
            setSelected(
              selected.size === data.hunks.length
                ? new Set()
                : new Set(data.hunks.map((hunk) => hunk.index)),
            )
          }
        >
          {selected.size === data.hunks.length ? "Select none" : "Select all"}
        </button>
        <button
          className="btn primary sm"
          disabled={busy || selected.size === 0}
          onClick={() => void apply()}
        >
          {staged ? "Unstage" : "Stage"} {selected.size || ""}{" "}
          {selected.size === 1 ? "hunk" : "hunks"}
        </button>
      </div>
      <div className="scroll-y" style={{ flex: 1 }}>
        {data.hunks.map((hunk) => (
          <div
            key={hunk.index}
            className={`hunk ${selected.has(hunk.index) ? "selected" : ""}`}
          >
            <label className="hunk-head">
              <input
                type="checkbox"
                checked={selected.has(hunk.index)}
                onChange={() => toggle(hunk.index)}
              />
              <span className="mono">{hunk.header}</span>
              <span className="grow" />
              <span className="added">+{hunk.added}</span>{" "}
              <span className="removed">−{hunk.removed}</span>
            </label>
            <pre className="mono hunk-body">
              {hunk.lines.map((line, index) => (
                <div key={index} className={`hunk-line ${line.kind}`}>
                  {line.kind === "added" ? "+" : line.kind === "removed" ? "−" : " "}
                  {line.text || " "}
                </div>
              ))}
            </pre>
          </div>
        ))}
      </div>
    </div>
  );
}
