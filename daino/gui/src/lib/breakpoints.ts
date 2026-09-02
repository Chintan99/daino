// Breakpoints in the editor gutter.
//
// The markers are drawn from the *server's* breakpoint list, not from a local
// one, for two reasons. They survive a page reload and a restart of the
// debuggee, because they belong to the user rather than to a run. And the
// adapter is allowed to move or refuse one — a click on a blank line goes to
// the nearest runnable line, or nowhere — so the marker has to show where
// execution will actually stop, which only the server knows.
import type { Monaco } from "@monaco-editor/react";
import type { editor, IDisposable } from "monaco-editor";
import { api } from "../api/client";
import { breakpointsFor, useDebugStore } from "../store/debugStore";

type CodeEditor = editor.IStandaloneCodeEditor;

/**
 * Wire gutter clicks and marker rendering for one editor.
 *
 * Returns a disposable that detaches everything: the click listener, the store
 * subscription, and the decorations.
 */
export function registerBreakpoints(
  ed: CodeEditor,
  monaco: Monaco,
  path: string,
): IDisposable {
  let collection = ed.createDecorationsCollection([]);

  const draw = () => {
    const items = breakpointsFor(useDebugStore.getState().breakpoints, path);
    collection.set(
      items.map((item) => {
        // Where it will actually stop, which is not always where it was set.
        const line = item.actual_line || item.line;
        const state = !item.verified ? "pending" : item.moved ? "moved" : "verified";
        return {
          range: new monaco.Range(line, 1, line, 1),
          options: {
            isWholeLine: false,
            glyphMarginClassName: `bp-glyph bp-${state}`,
            glyphMarginHoverMessage: {
              value:
                item.message ||
                (item.moved
                  ? `Set on line ${item.line}; the debugger stops on ${line}.`
                  : item.condition
                    ? `Stops when: ${item.condition}`
                    : "Breakpoint"),
            },
          },
        };
      }),
    );
  };

  // The glyph margin is where the dot goes; without it there is nowhere to click.
  ed.updateOptions({ glyphMargin: true });

  const clicks = ed.onMouseDown((event) => {
    if (event.target.type !== monaco.editor.MouseTargetType.GUTTER_GLYPH_MARGIN) {
      return;
    }
    const line = event.target.position?.lineNumber;
    if (!line) return;
    void api
      .toggleBreakpoint(path, line)
      .then((status) => useDebugStore.getState().apply(status))
      .catch(() => undefined);
  });

  const unsubscribe = useDebugStore.subscribe(draw);
  draw();

  return {
    dispose() {
      clicks.dispose();
      unsubscribe();
      collection.clear();
      collection = ed.createDecorationsCollection([]);
    },
  };
}
