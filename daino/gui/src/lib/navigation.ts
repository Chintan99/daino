// Semantic navigation in the editor: definition, references, hover, rename.
//
// Registered per editor instance rather than globally, because Monaco's
// providers are keyed by language and this app's languages come and go with the
// open tabs. Everything routes through the backend's language servers; when
// none is installed the commands say so rather than silently doing nothing —
// a menu item that appears to work and does not is worse than one that explains.
import type { Monaco } from "@monaco-editor/react";
import type { editor } from "monaco-editor";
import { api, ApiError } from "../api/client";
import { openFileInEditor } from "../lib/openFile";
import { promptFor } from "../store/dialogStore";
import { useReferencesStore } from "../store/referencesStore";
import { useRenameStore } from "../store/renameStore";
import { useUIStore } from "../store/uiStore";
import type { CodeLocation } from "../api/types";

/** Results nobody can see are results nobody asked for. */
function showReferencesPanel(): void {
  const ui = useUIStore.getState();
  ui.setActivityView("references");
  if (ui.sidebarCollapsed) ui.toggleSidebar();
}

type CodeEditor = editor.IStandaloneCodeEditor;

function positionOf(ed: CodeEditor): { line: number; column: number } | null {
  const position = ed.getPosition();
  if (!position) return null;
  return { line: position.lineNumber, column: position.column };
}

function explain(err: unknown): string {
  return err instanceof ApiError
    ? err.message
    : err instanceof Error
      ? err.message
      : String(err);
}

/** Go to definition, following the first result. */
export async function goToDefinition(path: string, ed: CodeEditor): Promise<void> {
  const at = positionOf(ed);
  if (!at) return;
  try {
    const result = await api.definition(path, at.line, at.column);
    if (!result.available) {
      window.alert(result.detail || "No language server can resolve this.");
      return;
    }
    const target = result.locations[0];
    if (!target) {
      window.alert("No definition found.");
      return;
    }
    await openFileInEditor(target.path, {
      line: target.line,
      column: target.column,
    });
  } catch (err) {
    window.alert(`Could not resolve the definition: ${explain(err)}`);
  }
}

/**
 * Find all references and put them in the side panel.
 *
 * The panel, not a jump: references are a list to read through, and replacing
 * the editor's contents with the first one loses the question you asked.
 */
export async function findReferences(path: string, ed: CodeEditor): Promise<void> {
  const at = positionOf(ed);
  if (!at) return;
  const store = useReferencesStore.getState();
  store.begin(path, at.line, at.column);
  showReferencesPanel();
  try {
    const result = await api.references(path, at.line, at.column);
    store.settle({
      locations: result.locations,
      detail: result.detail,
      // "index" results are text matches, not semantic references. The panel
      // labels them, because acting on them as if they were exact is how a
      // refactor breaks a comment-shaped string.
      source: result.source ?? (result.available ? "language-server" : "index"),
      available: result.available,
    });
  } catch (err) {
    store.settle({
      locations: [],
      detail: explain(err),
      source: "index",
      available: false,
    });
  }
}

/** Show every implementation of the symbol under the cursor. */
export async function findImplementations(
  path: string,
  ed: CodeEditor,
): Promise<void> {
  const at = positionOf(ed);
  if (!at) return;
  const store = useReferencesStore.getState();
  store.begin(path, at.line, at.column, "implementations");
  showReferencesPanel();
  try {
    const result = await api.implementations(path, at.line, at.column);
    store.settle({
      locations: result.locations,
      detail: result.detail,
      source: result.source ?? "language-server",
      available: result.available,
    });
  } catch (err) {
    store.settle({
      locations: [],
      detail: explain(err),
      source: "index",
      available: false,
    });
  }
}

/**
 * Rename the symbol under the cursor, across every file that uses it.
 *
 * Two steps on purpose: compute the edits, show them, then apply. A cross-file
 * rename is the single most consequential thing an editor can do on one
 * keystroke, and it should not happen before anyone has seen its extent.
 */
export async function renameSymbol(path: string, ed: CodeEditor): Promise<void> {
  const at = positionOf(ed);
  if (!at) return;
  const word = ed.getModel()?.getWordAtPosition({
    lineNumber: at.line,
    column: at.column,
  });
  const next = await promptFor({
    title: "Rename symbol",
    hint: "Every file that uses it will be shown before anything is written.",
    initial: word?.word ?? "",
    confirmLabel: "Preview",
  });
  if (!next?.trim()) return;
  try {
    const preview = await api.previewRename(path, at.line, at.column, next.trim());
    if (!preview.available) {
      window.alert(
        preview.detail ||
          "Renaming needs a language server, and none is available for this file.",
      );
      return;
    }
    if (!preview.count) {
      window.alert("The language server found nothing to rename here.");
      return;
    }
    useRenameStore.getState().show({
      symbol: word?.word ?? "",
      newName: next.trim(),
      edits: preview.edits,
      files: preview.files ?? Object.keys(preview.edits).length,
      count: preview.count ?? 0,
    });
  } catch (err) {
    window.alert(`Could not prepare the rename: ${explain(err)}`);
  }
}

/**
 * Attach the navigation commands and providers to one editor.
 *
 * Monaco's own keybindings (F12, Shift+F12, F2) are used so the muscle memory
 * from every other editor works, and the same operations are also exposed as
 * context-menu actions for discovery.
 */
export function registerNavigation(
  ed: CodeEditor,
  monaco: Monaco,
  path: string,
): void {
  ed.addAction({
    id: "daino.goToDefinition",
    label: "Go to Definition",
    keybindings: [monaco.KeyCode.F12],
    contextMenuGroupId: "navigation",
    contextMenuOrder: 1,
    run: () => void goToDefinition(path, ed),
  });
  ed.addAction({
    id: "daino.findReferences",
    label: "Find All References",
    keybindings: [monaco.KeyMod.Shift | monaco.KeyCode.F12],
    contextMenuGroupId: "navigation",
    contextMenuOrder: 2,
    run: () => void findReferences(path, ed),
  });
  ed.addAction({
    id: "daino.findImplementations",
    label: "Go to Implementations",
    keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.F12],
    contextMenuGroupId: "navigation",
    contextMenuOrder: 3,
    run: () => void findImplementations(path, ed),
  });
  ed.addAction({
    id: "daino.rename",
    label: "Rename Symbol…",
    keybindings: [monaco.KeyCode.F2],
    contextMenuGroupId: "modification",
    contextMenuOrder: 1,
    run: () => void renameSymbol(path, ed),
  });
}

/** Where a location list should send the editor. */
export async function openLocation(location: CodeLocation): Promise<void> {
  await openFileInEditor(location.path, {
    line: location.line,
    column: location.column,
  });
}
