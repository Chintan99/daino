// Open editor tabs + buffers + current selection.
//
// A tab is either a file buffer or a Git diff view of a path, so a review opens
// beside the code the way it does in an editor rather than in a drawer under it.
import { create } from "zustand";

/**
 * "hunks" and "conflict" are their own kinds rather than modes of "diff",
 * because both need their own controls: one selects hunks to stage, the other
 * chooses between two sides of a merge.
 */
export type TabKind = "file" | "diff" | "hunks" | "conflict";

export interface EditorTab {
  id: string;
  kind: TabKind;
  path: string;
  name: string;
  /** diff tabs only: comparing the index against HEAD rather than the worktree */
  staged: boolean;
}

export interface EditorBuffer {
  path: string;
  name: string;
  language: string;
  baseHash: string; // last-read hash, for conflict detection
  savedContent: string;
  content: string;
  dirty: boolean;
  conflict: boolean;
}

export interface EditorSelection {
  path: string;
  startLine: number;
  endLine: number;
}

/** A location the editor should scroll to and highlight. */
export interface EditorReveal {
  path: string;
  line: number;
  column: number;
  /** Length of the match to select, or 0 to just place the cursor. */
  length: number;
  /** Bumped per request so revealing the same spot twice still scrolls. */
  nonce: number;
}

export const fileTabId = (path: string) => `file:${path}`;
export const diffTabId = (path: string, staged: boolean) =>
  `diff:${staged ? "index" : "work"}:${path}`;
export const hunksTabId = (path: string, staged: boolean) =>
  `hunks:${staged ? "index" : "work"}:${path}`;
export const conflictTabId = (path: string) => `conflict:${path}`;

function basename(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}

interface EditorState {
  tabs: EditorTab[];
  activeTabId: string | null;
  buffers: Record<string, EditorBuffer>;
  selection: EditorSelection | null;
  /** Path of the active *file* tab; null while a diff tab has focus. */
  activePath: string | null;
  /** Set by search and other "go to" affordances; consumed by the editor. */
  reveal: EditorReveal | null;

  openBuffer: (b: Omit<EditorBuffer, "dirty" | "conflict">) => void;
  openDiff: (path: string, staged: boolean) => void;
  /** Stage or unstage part of a file, hunk by hunk. */
  openHunks: (path: string, staged: boolean) => void;
  /** Resolve a merge conflict with both sides side by side. */
  openConflict: (path: string) => void;
  setActive: (path: string) => void;
  setActiveTab: (id: string) => void;
  closeTab: (id: string) => void;
  closeBuffer: (path: string) => void;
  setContent: (path: string, content: string) => void;
  markSaved: (path: string, hash: string, content: string) => void;
  markConflict: (path: string, value: boolean) => void;
  setSelection: (sel: EditorSelection | null) => void;
  revealLocation: (at: Omit<EditorReveal, "nonce">) => void;
}

/** Recompute the focused-file path from whichever tab is active. */
function focusedPath(tabs: EditorTab[], activeTabId: string | null): string | null {
  const tab = tabs.find((t) => t.id === activeTabId);
  return tab && tab.kind === "file" ? tab.path : null;
}

export const useEditorStore = create<EditorState>((set) => ({
  tabs: [],
  activeTabId: null,
  buffers: {},
  selection: null,
  activePath: null,
  reveal: null,

  openBuffer: (b) =>
    set((s) => {
      const id = fileTabId(b.path);
      const exists = s.buffers[b.path];
      const buffers = { ...s.buffers };
      buffers[b.path] = exists
        ? // refresh from a disk read, keeping it non-dirty
          { ...exists, ...b, savedContent: b.content, dirty: false, conflict: false }
        : { ...b, dirty: false, conflict: false };
      const tabs = s.tabs.some((t) => t.id === id)
        ? s.tabs
        : [...s.tabs, { id, kind: "file" as const, path: b.path, name: b.name, staged: false }];
      return { buffers, tabs, activeTabId: id, activePath: b.path };
    }),

  openDiff: (path, staged) =>
    set((s) => {
      const id = diffTabId(path, staged);
      const tabs = s.tabs.some((t) => t.id === id)
        ? s.tabs
        : [
            ...s.tabs,
            { id, kind: "diff" as const, path, name: basename(path), staged },
          ];
      return { tabs, activeTabId: id, activePath: null };
    }),

  openHunks: (path, staged) =>
    set((s) => {
      const id = hunksTabId(path, staged);
      const tabs = s.tabs.some((t) => t.id === id)
        ? s.tabs
        : [
            ...s.tabs,
            { id, kind: "hunks" as const, path, name: basename(path), staged },
          ];
      return { tabs, activeTabId: id, activePath: null };
    }),

  openConflict: (path) =>
    set((s) => {
      const id = conflictTabId(path);
      const tabs = s.tabs.some((t) => t.id === id)
        ? s.tabs
        : [
            ...s.tabs,
            { id, kind: "conflict" as const, path, name: basename(path), staged: false },
          ];
      return { tabs, activeTabId: id, activePath: null };
    }),

  setActive: (path) =>
    set((s) => {
      const id = fileTabId(path);
      if (!s.tabs.some((t) => t.id === id)) return {};
      return { activeTabId: id, activePath: path };
    }),

  setActiveTab: (id) =>
    set((s) => ({ activeTabId: id, activePath: focusedPath(s.tabs, id) })),

  closeTab: (id) =>
    set((s) => {
      const closing = s.tabs.find((t) => t.id === id);
      if (!closing) return {};
      const tabs = s.tabs.filter((t) => t.id !== id);
      const buffers = { ...s.buffers };
      // A file's buffer outlives its tab only if some other tab still shows it.
      if (
        closing.kind === "file" &&
        !tabs.some((t) => t.kind === "file" && t.path === closing.path)
      ) {
        delete buffers[closing.path];
      }
      let activeTabId = s.activeTabId;
      if (activeTabId === id) activeTabId = tabs.length ? tabs[tabs.length - 1].id : null;
      const selection =
        s.selection && !buffers[s.selection.path] ? null : s.selection;
      return {
        tabs,
        buffers,
        activeTabId,
        activePath: focusedPath(tabs, activeTabId),
        selection,
      };
    }),

  closeBuffer: (path) =>
    set((s) => {
      const tabs = s.tabs.filter((t) => !(t.kind === "file" && t.path === path));
      const buffers = { ...s.buffers };
      delete buffers[path];
      let activeTabId = s.activeTabId;
      if (!tabs.some((t) => t.id === activeTabId))
        activeTabId = tabs.length ? tabs[tabs.length - 1].id : null;
      const selection = s.selection?.path === path ? null : s.selection;
      return {
        tabs,
        buffers,
        activeTabId,
        activePath: focusedPath(tabs, activeTabId),
        selection,
      };
    }),

  setContent: (path, content) =>
    set((s) => {
      const buf = s.buffers[path];
      if (!buf) return {};
      return {
        buffers: {
          ...s.buffers,
          [path]: { ...buf, content, dirty: content !== buf.savedContent },
        },
      };
    }),

  markSaved: (path, hash, content) =>
    set((s) => {
      const buf = s.buffers[path];
      if (!buf) return {};
      return {
        buffers: {
          ...s.buffers,
          [path]: {
            ...buf,
            baseHash: hash,
            savedContent: content,
            content,
            dirty: false,
            conflict: false,
          },
        },
      };
    }),

  markConflict: (path, value) =>
    set((s) => {
      const buf = s.buffers[path];
      if (!buf) return {};
      return { buffers: { ...s.buffers, [path]: { ...buf, conflict: value } } };
    }),

  setSelection: (selection) => set({ selection }),

  revealLocation: (at) =>
    set((s) => ({ reveal: { ...at, nonce: (s.reveal?.nonce ?? 0) + 1 } })),
}));
