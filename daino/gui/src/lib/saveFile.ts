// Save the given open buffer via PUT /api/files/write, handling 409 conflicts.
import { api, ApiError } from "../api/client";
import { useEditorStore } from "../store/editorStore";

export async function saveBuffer(path: string): Promise<void> {
  const buf = useEditorStore.getState().buffers[path];
  if (!buf || !buf.dirty) return;
  try {
    const res = await api.writeFile(path, buf.content, buf.baseHash);
    useEditorStore.getState().markSaved(path, res.hash, buf.content);
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      useEditorStore.getState().markConflict(path, true);
      return;
    }
    // eslint-disable-next-line no-console
    console.error("Failed to save", path, err);
    window.alert(
      `Could not save ${path}: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
}

// Reload the file from disk, discarding local edits (used to resolve conflicts).
export async function reloadBuffer(path: string): Promise<void> {
  const buf = useEditorStore.getState().buffers[path];
  if (!buf) return;
  const file = await api.readFile(path);
  useEditorStore.getState().openBuffer({
    path: file.path,
    name: buf.name,
    language: file.language || buf.language,
    baseHash: file.hash,
    savedContent: file.content,
    content: file.content,
  });
  useEditorStore.getState().markConflict(path, false);
}
