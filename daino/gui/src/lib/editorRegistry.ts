// The focused Monaco instance, so menu commands can act on it.
//
// Undo, Find, Go to line, and Format are editor actions: the menu needs the
// *same* editor the user is typing in, and React state is the wrong channel for
// an imperative handle. Editors register on mount and deregister on unmount.
import type { editor } from "monaco-editor";

type CodeEditor = editor.IStandaloneCodeEditor;

let focused: CodeEditor | null = null;
const open = new Set<CodeEditor>();

export function registerEditor(instance: CodeEditor): () => void {
  open.add(instance);
  focused = instance;
  const sub = instance.onDidFocusEditorText(() => {
    focused = instance;
  });
  return () => {
    sub.dispose();
    open.delete(instance);
    if (focused === instance) focused = open.values().next().value ?? null;
  };
}

/** The editor a menu command should target, or null when none is open. */
export function activeEditor(): CodeEditor | null {
  if (focused && open.has(focused)) return focused;
  return open.values().next().value ?? null;
}

/** Run a built-in editor action (`editor.action.*`) on the focused editor. */
export function runEditorAction(action: string): boolean {
  const ed = activeEditor();
  if (!ed) return false;
  ed.focus();
  const handle = ed.getAction(action);
  if (handle) {
    void handle.run();
    return true;
  }
  // Undo/redo/clipboard are triggers rather than registered actions.
  ed.trigger("menu", action, null);
  return true;
}
