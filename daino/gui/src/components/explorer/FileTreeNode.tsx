import { useRef, useState } from "react";
import { useFileTree } from "../../api/hooks";
import type { TreeEntry } from "../../api/types";

export interface TreeCallbacks {
  onOpen: (path: string) => void;
  onContextMenu: (e: React.MouseEvent, entry: TreeEntry) => void;
  gitMap: Record<string, string>;
  activePath: string | null;
  /**
   * Optional double-click handler for files. When set, a single click is
   * deferred briefly so a double-click can pre-empt it — the Design panel uses
   * this to place a file on the canvas on single click and open it in the
   * editor on double click.
   */
  onFileDoubleClick?: (path: string) => void;
}

// Long enough to catch a real double-click, short enough not to feel laggy.
const DOUBLE_CLICK_MS = 220;

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
  // Pending single-click, held so a following double-click can cancel it.
  const clickTimer = useRef<number | null>(null);

  const mark = cb.gitMap[entry.path];

  const onFileClick = () => {
    if (!cb.onFileDoubleClick) {
      cb.onOpen(entry.path);
      return;
    }
    if (clickTimer.current !== null) window.clearTimeout(clickTimer.current);
    clickTimer.current = window.setTimeout(() => {
      clickTimer.current = null;
      cb.onOpen(entry.path);
    }, DOUBLE_CLICK_MS);
  };

  const onFileDoubleClick = () => {
    if (!cb.onFileDoubleClick) return;
    if (clickTimer.current !== null) {
      window.clearTimeout(clickTimer.current);
      clickTimer.current = null;
    }
    cb.onFileDoubleClick(entry.path);
  };

  return (
    <div>
      <div
        className={`tree-row ${cb.activePath === entry.path ? "active" : ""}`}
        style={{ paddingLeft: 8 + depth * 12 }}
        onClick={() => (isDir ? setExpanded((v) => !v) : onFileClick())}
        onDoubleClick={() => (isDir ? undefined : onFileDoubleClick())}
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
