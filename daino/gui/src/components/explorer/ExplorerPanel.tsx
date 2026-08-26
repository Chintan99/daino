import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  useFileTree,
  useFileMutations,
  useGitStatus,
} from "../../api/hooks";
import type { TreeEntry } from "../../api/types";
import { useEditorStore } from "../../store/editorStore";
import { openFileInEditor } from "../../lib/openFile";
import { FileTreeNode, type TreeCallbacks } from "./FileTreeNode";

interface Menu {
  x: number;
  y: number;
  entry: TreeEntry;
}
interface Prompt {
  title: string;
  initial: string;
  onSubmit: (value: string) => void;
}

function parentDir(path: string): string {
  const i = path.lastIndexOf("/");
  return i <= 0 ? "" : path.slice(0, i);
}

export function ExplorerPanel() {
  const qc = useQueryClient();
  const { data, isLoading } = useFileTree("");
  const { data: git } = useGitStatus();
  const { create, rename, remove } = useFileMutations();
  const activePath = useEditorStore((s) => s.activePath);
  const closeBuffer = useEditorStore((s) => s.closeBuffer);

  const [menu, setMenu] = useState<Menu | null>(null);
  const [prompt, setPrompt] = useState<Prompt | null>(null);

  const gitMap = useMemo(() => {
    const map: Record<string, string> = {};
    if (git) {
      for (const e of git.staged) map[e.path] = (e.status || "A")[0];
      for (const e of git.modified) map[e.path] = (e.status || "M")[0];
      for (const e of git.untracked) map[e.path] = "?";
    }
    return map;
  }, [git]);

  const cb: TreeCallbacks = {
    onOpen: (p) => void openFileInEditor(p),
    onContextMenu: (e, entry) => {
      e.preventDefault();
      setMenu({ x: e.clientX, y: e.clientY, entry });
    },
    gitMap,
    activePath,
  };

  const refresh = () => qc.invalidateQueries({ queryKey: ["files", "tree"] });

  const askCreate = (isDir: boolean, baseDir: string) => {
    setPrompt({
      title: isDir ? "New folder" : "New file",
      initial: baseDir ? `${baseDir}/` : "",
      onSubmit: (value) => {
        if (value.trim()) create.mutate({ path: value.trim(), is_dir: isDir });
      },
    });
  };

  const askRename = (entry: TreeEntry) => {
    setPrompt({
      title: `Rename ${entry.name}`,
      initial: entry.path,
      onSubmit: (value) => {
        if (value.trim() && value.trim() !== entry.path)
          rename.mutate({ source: entry.path, dest: value.trim() });
      },
    });
  };

  const doDelete = (entry: TreeEntry) => {
    // CONFIRM before deleting
    if (
      window.confirm(
        `Delete "${entry.path}"?${
          entry.type === "directory" ? " This removes the folder and its contents." : ""
        }`,
      )
    ) {
      remove.mutate(entry.path);
      closeBuffer(entry.path);
    }
  };

  return (
    <div
      className="panel"
      onClick={() => menu && setMenu(null)}
    >
      <div className="panel-header">
        Explorer
        <span className="spacer" />
        <button className="btn icon" title="New file" onClick={() => askCreate(false, "")}>
          ＋
        </button>
        <button className="btn icon" title="New folder" onClick={() => askCreate(true, "")}>
          ▤
        </button>
        <button className="btn icon" title="Refresh" onClick={refresh}>
          ⟳
        </button>
      </div>
      <div className="panel-body">
        {isLoading && <div className="empty">Loading…</div>}
        {data && data.entries.length === 0 && (
          <div className="empty">Empty workspace</div>
        )}
        <div className="tree">
          {data?.entries
            .slice()
            .sort((a, b) => {
              if (a.type !== b.type) return a.type === "directory" ? -1 : 1;
              return a.name.localeCompare(b.name);
            })
            .map((entry) => (
              <FileTreeNode key={entry.path} entry={entry} depth={0} cb={cb} />
            ))}
        </div>
      </div>

      {menu && (
        <div
          style={{
            position: "fixed",
            top: menu.y,
            left: menu.x,
            background: "var(--bg-2)",
            border: "1px solid var(--border-strong)",
            borderRadius: 6,
            zIndex: 2000,
            minWidth: 160,
            padding: 4,
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {menu.entry.type === "file" && (
            <MenuItem
              label="Open"
              onClick={() => {
                void openFileInEditor(menu.entry.path);
                setMenu(null);
              }}
            />
          )}
          <MenuItem
            label="New file…"
            onClick={() => {
              askCreate(
                false,
                menu.entry.type === "directory"
                  ? menu.entry.path
                  : parentDir(menu.entry.path),
              );
              setMenu(null);
            }}
          />
          <MenuItem
            label="New folder…"
            onClick={() => {
              askCreate(
                true,
                menu.entry.type === "directory"
                  ? menu.entry.path
                  : parentDir(menu.entry.path),
              );
              setMenu(null);
            }}
          />
          <MenuItem
            label="Rename…"
            onClick={() => {
              askRename(menu.entry);
              setMenu(null);
            }}
          />
          <MenuItem
            label="Delete"
            danger
            onClick={() => {
              doDelete(menu.entry);
              setMenu(null);
            }}
          />
        </div>
      )}

      {prompt && (
        <PromptDialog
          title={prompt.title}
          initial={prompt.initial}
          onCancel={() => setPrompt(null)}
          onSubmit={(v) => {
            prompt.onSubmit(v);
            setPrompt(null);
          }}
        />
      )}
    </div>
  );
}

function MenuItem({
  label,
  onClick,
  danger,
}: {
  label: string;
  onClick: () => void;
  danger?: boolean;
}) {
  return (
    <button
      className="btn subtle"
      style={{
        display: "block",
        width: "100%",
        textAlign: "left",
        color: danger ? "var(--red)" : undefined,
      }}
      onClick={onClick}
    >
      {label}
    </button>
  );
}

function PromptDialog({
  title,
  initial,
  onCancel,
  onSubmit,
}: {
  title: string;
  initial: string;
  onCancel: () => void;
  onSubmit: (value: string) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <div className="dialog-backdrop" onClick={onCancel}>
      <div className="dialog" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        <input
          className="input"
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") onSubmit(value);
            if (e.key === "Escape") onCancel();
          }}
          placeholder="relative/path"
        />
        <div className="actions">
          <button className="btn subtle" onClick={onCancel}>
            Cancel
          </button>
          <button className="btn primary" onClick={() => onSubmit(value)}>
            OK
          </button>
        </div>
      </div>
    </div>
  );
}
