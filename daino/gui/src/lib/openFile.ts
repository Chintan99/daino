// Shared helper: read a file from the backend and open it in the editor.
import { api, ApiError } from "../api/client";
import { useEditorStore } from "../store/editorStore";
import { useUIStore } from "../store/uiStore";

function basename(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}

/** Open a Git diff for `path` as an editor tab, VSCode-style. */
export function openDiffInEditor(path: string, staged: boolean): void {
  useUIStore.getState().setActiveWorkspaceTab("code");
  useUIStore.getState().setLastDiffPath(path);
  useEditorStore.getState().openDiff(path, staged);
}

/** Open the hunk view for a file, to stage or unstage part of it. */
export function openHunksInEditor(path: string, staged: boolean): void {
  useUIStore.getState().setActiveWorkspaceTab("code");
  useEditorStore.getState().openHunks(path, staged);
}

/** Open a merge conflict with both sides shown. */
export function openConflictInEditor(path: string): void {
  useUIStore.getState().setActiveWorkspaceTab("code");
  useEditorStore.getState().openConflict(path);
}

/** Where in a file to land, when opening from search or a problem list. */
export interface FileLocation {
  line: number;
  column?: number;
  /** Length of the match, so it can be selected rather than merely scrolled to. */
  length?: number;
}

export async function openFileInEditor(
  path: string,
  at?: FileLocation,
): Promise<void> {
  // switch to CODE workspace so the editor is visible
  useUIStore.getState().setActiveWorkspaceTab("code");
  const reveal = () => {
    if (!at) return;
    useEditorStore.getState().revealLocation({
      path,
      line: at.line,
      column: at.column ?? 1,
      length: at.length ?? 0,
    });
  };
  const already = useEditorStore.getState().buffers[path];
  if (already) {
    useEditorStore.getState().setActive(path);
    reveal();
    return;
  }
  try {
    const file = await api.readFile(path);
    useEditorStore.getState().openBuffer({
      path: file.path,
      name: basename(file.path),
      language: file.language || "plaintext",
      baseHash: file.hash,
      savedContent: file.content,
      content: file.content,
    });
    reveal();
  } catch (err) {
    const msg =
      err instanceof ApiError
        ? err.status === 413
          ? "File is too large to open."
          : err.status === 415
            ? "File is binary and cannot be opened."
            : err.message
        : String(err);
    // Surface a lightweight buffer describing the problem.
    useEditorStore.getState().openBuffer({
      path,
      name: basename(path),
      language: "plaintext",
      baseHash: "",
      savedContent: `// Could not open ${path}\n// ${msg}\n`,
      content: `// Could not open ${path}\n// ${msg}\n`,
    });
  }
}
