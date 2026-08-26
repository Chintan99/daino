// Open editor tabs + buffers + current selection.
import { create } from "zustand";

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

interface EditorState {
  order: string[]; // open file paths, tab order
  buffers: Record<string, EditorBuffer>;
  activePath: string | null;
  selection: EditorSelection | null;

  openBuffer: (b: Omit<EditorBuffer, "dirty" | "conflict">) => void;
  setActive: (path: string) => void;
  closeBuffer: (path: string) => void;
  setContent: (path: string, content: string) => void;
  markSaved: (path: string, hash: string, content: string) => void;
  markConflict: (path: string, value: boolean) => void;
  setSelection: (sel: EditorSelection | null) => void;
}

export const useEditorStore = create<EditorState>((set) => ({
  order: [],
  buffers: {},
  activePath: null,
  selection: null,

  openBuffer: (b) =>
    set((s) => {
      const exists = s.buffers[b.path];
      const buffers = { ...s.buffers };
      if (exists) {
        // refresh from disk read, keep it non-dirty
        buffers[b.path] = {
          ...exists,
          ...b,
          savedContent: b.content,
          dirty: false,
          conflict: false,
        };
      } else {
        buffers[b.path] = { ...b, dirty: false, conflict: false };
      }
      const order = s.order.includes(b.path) ? s.order : [...s.order, b.path];
      return { buffers, order, activePath: b.path };
    }),

  setActive: (path) => set({ activePath: path }),

  closeBuffer: (path) =>
    set((s) => {
      const order = s.order.filter((p) => p !== path);
      const buffers = { ...s.buffers };
      delete buffers[path];
      let activePath = s.activePath;
      if (activePath === path) {
        activePath = order.length ? order[order.length - 1] : null;
      }
      const selection =
        s.selection && s.selection.path === path ? null : s.selection;
      return { order, buffers, activePath, selection };
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
      return {
        buffers: { ...s.buffers, [path]: { ...buf, conflict: value } },
      };
    }),

  setSelection: (selection) => set({ selection }),
}));
