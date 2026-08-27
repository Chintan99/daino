import { useMemo } from "react";
import { DiffEditor } from "@monaco-editor/react";
import { useGitFile } from "../../api/hooks";
import { api } from "../../api/client";
import { useQueryClient } from "@tanstack/react-query";
import { useEditorOptions, useMonacoTheme } from "../../lib/editorPrefs";
import { openFileInEditor } from "../../lib/openFile";

/** Count changed lines the cheap way, for the header's ± summary. */
function lineStat(original: string, modified: string) {
  const before = original ? original.split("\n") : [];
  const after = modified ? modified.split("\n") : [];
  const seen = new Map<string, number>();
  for (const line of before) seen.set(line, (seen.get(line) ?? 0) + 1);
  let added = 0;
  for (const line of after) {
    const count = seen.get(line) ?? 0;
    if (count > 0) seen.set(line, count - 1);
    else added += 1;
  }
  let removed = 0;
  for (const count of seen.values()) removed += count;
  return { added, removed };
}

export function GitDiffView({
  path,
  staged,
}: {
  path: string;
  staged: boolean;
}) {
  const qc = useQueryClient();
  const { data, isLoading, refetch } = useGitFile(path, staged);
  const theme = useMonacoTheme();
  const options = useEditorOptions({
    readOnly: true,
    renderSideBySide: true,
    ignoreTrimWhitespace: false,
    renderOverviewRuler: false,
    diffWordWrap: "off",
    originalEditable: false,
  });

  const stat = useMemo(
    () => lineStat(data?.original ?? "", data?.modified ?? ""),
    [data?.original, data?.modified],
  );

  const afterMutation = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["git"] }),
      refetch(),
    ]);
  };

  return (
    <div className="panel" style={{ background: "var(--bg-0)" }}>
      <div className="diff-bar">
        <span className="path">{path}</span>
        <span className="badge">
          {staged ? "index ⇄ HEAD" : "working tree ⇄ index"}
        </span>
        {!isLoading && data && !data.binary && (
          <span className="diff-stat mono">
            <span className="add">+{stat.added}</span>{" "}
            <span className="del">−{stat.removed}</span>
          </span>
        )}
        <span className="grow" />
        <button
          className="btn subtle sm"
          onClick={() => void openFileInEditor(path)}
          title="Open the file for editing"
        >
          Open file
        </button>
        {staged ? (
          <button
            className="btn subtle sm"
            onClick={async () => {
              await api.gitUnstage([path]);
              await afterMutation();
            }}
          >
            Unstage
          </button>
        ) : (
          <button
            className="btn subtle sm"
            onClick={async () => {
              await api.gitStage([path]);
              await afterMutation();
            }}
          >
            Stage
          </button>
        )}
        <button className="btn icon" title="Refresh" onClick={() => void refetch()}>
          ⟳
        </button>
      </div>

      <div className="panel-body" style={{ position: "relative", overflow: "hidden" }}>
        {isLoading && <div className="empty">Loading diff…</div>}
        {!isLoading && data?.binary && (
          <div className="empty">
            {path} is binary or too large to diff in the editor.
          </div>
        )}
        {!isLoading && data && !data.binary && (
          <DiffEditor
            original={data.original}
            modified={data.modified}
            language={data.language}
            theme={theme}
            options={options}
          />
        )}
      </div>
    </div>
  );
}
