import { useState } from "react";
import { useFileTree } from "../../api/hooks";
import type { TreeEntry } from "../../api/types";

export interface TreeCallbacks {
  onOpen: (path: string) => void;
  onContextMenu: (e: React.MouseEvent, entry: TreeEntry) => void;
  gitMap: Record<string, string>;
  activePath: string | null;
}

export function FileTreeNode({
  entry,
  depth,
  cb,
}: {
  entry: TreeEntry;
  depth: number;
  cb: TreeCallbacks;
}) {
  const [expanded, setExpanded] = useState(false);
  const isDir = entry.type === "directory";
  // lazy: only fetch children once the directory is expanded
  const { data, isLoading } = useFileTree(entry.path, isDir && expanded);

  const mark = cb.gitMap[entry.path];

  return (
    <div>
      <div
        className={`tree-row ${cb.activePath === entry.path ? "active" : ""}`}
        style={{ paddingLeft: 8 + depth * 12 }}
        onClick={() => (isDir ? setExpanded((v) => !v) : cb.onOpen(entry.path))}
        onContextMenu={(e) => cb.onContextMenu(e, entry)}
        title={entry.path}
      >
        <span className="tree-twist">
          {isDir ? (expanded ? "▾" : "▸") : ""}
        </span>
        <span className="tree-name">
          {isDir ? "📁" : "📄"} {entry.name}
        </span>
        {mark && <span className={`git-mark git-${mark}`}>{mark}</span>}
      </div>
      {isDir && expanded && (
        <div>
          {isLoading && (
            <div className="tree-row muted" style={{ paddingLeft: 8 + (depth + 1) * 12 }}>
              …
            </div>
          )}
          {data?.entries
            .slice()
            .sort((a, b) => {
              if (a.type !== b.type) return a.type === "directory" ? -1 : 1;
              return a.name.localeCompare(b.name);
            })
            .map((child) => (
              <FileTreeNode
                key={child.path}
                entry={child}
                depth={depth + 1}
                cb={cb}
              />
            ))}
        </div>
      )}
    </div>
  );
}
