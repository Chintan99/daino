// Shared helper: read a file from the backend and open it in the editor.
import { api, ApiError } from "../api/client";
import { useEditorStore } from "../store/editorStore";
import { useUIStore } from "../store/uiStore";

function basename(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}

export async function openFileInEditor(path: string): Promise<void> {
  // switch to CODE workspace so the editor is visible
  useUIStore.getState().setActiveWorkspaceTab("code");
  const already = useEditorStore.getState().buffers[path];
  if (already) {
    useEditorStore.getState().setActive(path);
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
