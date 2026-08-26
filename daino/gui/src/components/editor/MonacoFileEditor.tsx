import { useEffect, useRef } from "react";
import Editor, { type OnMount } from "@monaco-editor/react";
import type { editor } from "monaco-editor";
import { useEditorStore } from "../../store/editorStore";
import { saveBuffer } from "../../lib/saveFile";
import { DAINO_THEME, EDITOR_OPTIONS } from "../../lib/monaco";

export function MonacoFileEditor({ path }: { path: string }) {
  const buffer = useEditorStore((s) => s.buffers[path]);
  const setContent = useEditorStore((s) => s.setContent);
  const setSelection = useEditorStore((s) => s.setSelection);
  const reveal = useEditorStore((s) => s.reveal);
  const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null);

  /**
   * Scroll to, select, and briefly flash a requested location.
   *
   * Search results have to *land* somewhere: opening the file alone looks like
   * nothing happened when it was already the active tab.
   */
  useEffect(() => {
    const ed = editorRef.current;
    if (!ed || !reveal || reveal.path !== path) return;
    const { line, column, length } = reveal;
    const range = {
      startLineNumber: line,
      startColumn: column,
      endLineNumber: line,
      endColumn: length > 0 ? column + length : column,
    };
    ed.revealRangeInCenterIfOutsideViewport(range);
    ed.setPosition({ lineNumber: line, column });
    if (length > 0) ed.setSelection(range);
    ed.focus();
    const flash = ed.createDecorationsCollection([
      {
        range,
        options: {
          className: length > 0 ? "reveal-flash" : undefined,
          isWholeLine: length === 0,
          linesDecorationsClassName: "reveal-flash-gutter",
        },
      },
    ]);
    const timer = window.setTimeout(() => flash.clear(), 1600);
    return () => {
      window.clearTimeout(timer);
      flash.clear();
    };
  }, [reveal, path]);

  const onMount: OnMount = (ed, monaco) => {
    editorRef.current = ed;
    // A file opened *by* a search result mounts after the request was made.
    const pending = useEditorStore.getState().reveal;
    if (pending && pending.path === path) {
      ed.revealLineInCenter(pending.line);
      ed.setPosition({ lineNumber: pending.line, column: pending.column });
    }
    // Ctrl/Cmd+S → save the active buffer
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
      const active = useEditorStore.getState().activePath;
      if (active) void saveBuffer(active);
    });
    ed.onDidChangeCursorSelection((e) => {
      const sel = e.selection;
      setSelection({
        path,
        startLine: sel.startLineNumber,
        endLine: sel.endLineNumber,
      });
    });
  };

  if (!buffer) return <div className="empty">No file open</div>;

  return (
    <Editor
      key={path}
      path={path}
      language={buffer.language}
      value={buffer.content}
      theme={DAINO_THEME}
      onMount={onMount}
      onChange={(value) => setContent(path, value ?? "")}
      options={{
        ...EDITOR_OPTIONS,
        minimap: { enabled: true, renderCharacters: false, maxColumn: 80 },
        lineNumbers: "on",
        renderWhitespace: "selection",
        tabSize: 2,
        stickyScroll: { enabled: true },
      }}
    />
  );
}
