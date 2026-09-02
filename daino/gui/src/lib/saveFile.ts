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

/**
 * Note that `path` changed on disk while it is open in the editor.
 *
 * Called from the live event stream, so an agent rewriting a file the user has
 * open is visible the moment it happens. A buffer with no unsaved edits is
 * simply refreshed — there is nothing to lose and nothing to decide. A dirty
 * one is flagged instead: two versions now exist, and picking between them is
 * the author's call, not this function's.
 */
export function markBufferStale(path: string): void {
  if (!path) return;
  const store = useEditorStore.getState();
  const buf = store.buffers[path];
  if (!buf || buf.conflict) return;
  if (buf.dirty) {
    store.markConflict(path, true);
    return;
  }
  void reloadBuffer(path).catch(() => {
    // The file may have been deleted, or become unreadable. Flagging is the
    // honest fallback: the buffer no longer matches anything on disk.
    useEditorStore.getState().markConflict(path, true);
  });
}

/**
 * Save over whatever is on disk now, abandoning conflict detection for this
 * write. The user has seen the warning and chosen their own version.
 */
export async function saveBufferOverwriting(path: string): Promise<void> {
  const buf = useEditorStore.getState().buffers[path];
  if (!buf) return;
  try {
    // Reading first is what makes this an overwrite rather than a blind write:
    // the current hash is exactly what the server is checking against.
    const current = await api.readFile(path);
    const res = await api.writeFile(path, buf.content, current.hash);
    useEditorStore.getState().markSaved(path, res.hash, buf.content);
    useEditorStore.getState().markConflict(path, false);
  } catch (err) {
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
