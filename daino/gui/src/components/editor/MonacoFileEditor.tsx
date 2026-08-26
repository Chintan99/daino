import { useRef } from "react";
import Editor, { type OnMount } from "@monaco-editor/react";
import type { editor } from "monaco-editor";
import { useEditorStore } from "../../store/editorStore";
import { saveBuffer } from "../../lib/saveFile";

export function MonacoFileEditor({ path }: { path: string }) {
  const buffer = useEditorStore((s) => s.buffers[path]);
  const setContent = useEditorStore((s) => s.setContent);
  const setSelection = useEditorStore((s) => s.setSelection);
  const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null);

  const onMount: OnMount = (ed, monaco) => {
    editorRef.current = ed;
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
      theme="vs-dark"
      onMount={onMount}
      onChange={(value) => setContent(path, value ?? "")}
      options={{
        fontSize: 13,
        fontFamily:
          "'SFMono-Regular', 'JetBrains Mono', Menlo, Consolas, monospace",
        minimap: { enabled: true },
        lineNumbers: "on",
        renderWhitespace: "selection",
        scrollBeyondLastLine: false,
        automaticLayout: true,
        tabSize: 2,
        smoothScrolling: true,
      }}
    />
  );
}
