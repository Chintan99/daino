// Comparing an unsaved buffer against the version now on disk.
//
// The conflict bar offers "Reload from disk" and "Keep mine", and both of them
// discard something. Neither is a decision anyone should make blind, so this
// backs a third option: show the two versions side by side first.
import { create } from "zustand";
import { api } from "../api/client";
import { useEditorStore } from "../store/editorStore";

interface StaleDiff {
  path: string;
  /** What is on disk right now. */
  disk: string;
  /** The unsaved buffer. */
  mine: string;
  language: string;
}

interface StaleDiffState {
  open: StaleDiff | null;
  show: (diff: StaleDiff) => void;
  close: () => void;
}

export const useStaleDiffStore = create<StaleDiffState>((set) => ({
  open: null,
  show: (open) => set({ open }),
  close: () => set({ open: null }),
}));

/** Read the file fresh and put it beside the buffer's unsaved content. */
export async function openStaleDiff(path: string): Promise<void> {
  const buf = useEditorStore.getState().buffers[path];
  if (!buf) return;
  try {
    const file = await api.readFile(path);
    useStaleDiffStore.getState().show({
      path,
      disk: file.content,
      mine: buf.content,
      language: file.language || buf.language,
    });
  } catch (err) {
    window.alert(
      `Could not read ${path} to compare: ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
  }
}
