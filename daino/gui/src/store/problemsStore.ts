// Diagnostics for the open files, and an honest account of what produced them.
//
// Two sources, deliberately kept apart:
//
// * A **language server** (pyright, tsserver, gopls, rust-analyzer) — real
//   semantic analysis, run out of process by the backend. Authoritative.
// * **Monaco's own workers** — syntax-only for TS/JS (semantic validation is
//   off: the worker has no node_modules, so it would report "cannot find
//   module" for every real import), plus full validation for JSON, CSS and
//   HTML, which are self-contained and therefore accurate.
//
// What matters is the third state neither source produces on its own: *nothing
// analysed this file*. A missing language server yields an empty list exactly
// like a clean file does, and rendering those the same way is how a Problems
// panel comes to assert a clean bill of health it has no evidence for. So each
// file records its coverage alongside its diagnostics, and the panel says which
// one it is.
import { create } from "zustand";
import type { Diagnostic, DiagnosticSeverity } from "../api/types";

export type { DiagnosticSeverity };

/** Where a file's diagnostics came from, or why there are none. */
export type Coverage =
  | "language-server" // analysed semantically
  | "editor" // Monaco only: syntax, or a self-contained language
  | "unsupported" // no analyser exists for this file type
  | "unavailable"; // one exists but is not installed here

export interface Problem {
  path: string;
  line: number;
  column: number;
  severity: DiagnosticSeverity;
  message: string;
  source: string;
}

export interface FileDiagnostics {
  problems: Problem[];
  coverage: Coverage;
  /** Why coverage is limited, when it is. Shown verbatim to the user. */
  detail: string;
}

interface ProblemsState {
  /** Keyed by file path, so closing a file drops exactly its diagnostics. */
  byPath: Record<string, FileDiagnostics>;
  /** Problems Monaco reported, kept separately so neither source clobbers the other. */
  editorByPath: Record<string, Problem[]>;
  setFromServer: (path: string, entry: FileDiagnostics) => void;
  setFromEditor: (path: string, problems: Problem[]) => void;
  clearPath: (path: string) => void;
}

export const useProblemsStore = create<ProblemsState>((set) => ({
  byPath: {},
  editorByPath: {},

  setFromServer: (path, entry) =>
    set((s) => ({ byPath: { ...s.byPath, [path]: entry } })),

  setFromEditor: (path, problems) =>
    set((s) => ({ editorByPath: { ...s.editorByPath, [path]: problems } })),

  clearPath: (path) =>
    set((s) => {
      const byPath = { ...s.byPath };
      const editorByPath = { ...s.editorByPath };
      delete byPath[path];
      delete editorByPath[path];
      return { byPath, editorByPath };
    }),
}));

const RANK: Record<DiagnosticSeverity, number> = {
  error: 0,
  warning: 1,
  info: 2,
  hint: 3,
};

export function toProblem(item: Diagnostic): Problem {
  return {
    path: item.path,
    line: item.line,
    column: item.column,
    severity: item.severity,
    message: item.message,
    source: item.source || item.code || "",
  };
}

/** One key per problem, so the same finding from both sources appears once. */
function identity(problem: Problem): string {
  return `${problem.path}:${problem.line}:${problem.column}:${problem.message}`;
}

/**
 * Every current problem, worst first, then by file and line.
 *
 * Merges the two sources and de-duplicates: a syntax error is something both
 * Monaco and a language server will report, and it is one problem.
 */
export function mergedProblems(
  byPath: Record<string, FileDiagnostics>,
  editorByPath: Record<string, Problem[]>,
): Problem[] {
  const seen = new Set<string>();
  const all: Problem[] = [];
  for (const entry of [
    ...Object.values(byPath).flatMap((item) => item.problems),
    ...Object.values(editorByPath).flat(),
  ]) {
    const key = identity(entry);
    if (seen.has(key)) continue;
    seen.add(key);
    all.push(entry);
  }
  return all.sort(
    (a, b) =>
      RANK[a.severity] - RANK[b.severity] ||
      a.path.localeCompare(b.path) ||
      a.line - b.line ||
      a.column - b.column,
  );
}

/**
 * Files that nothing has analysed, with the reason.
 *
 * This is the list that keeps the panel honest: as long as it is non-empty, an
 * absence of problems is an absence of evidence.
 */
export function coverageGaps(
  byPath: Record<string, FileDiagnostics>,
): { path: string; coverage: Coverage; detail: string }[] {
  return Object.entries(byPath)
    .filter(([, entry]) => entry.coverage === "unavailable")
    .map(([path, entry]) => ({
      path,
      coverage: entry.coverage,
      detail: entry.detail,
    }))
    .sort((a, b) => a.path.localeCompare(b.path));
}

/** Error and warning totals, for the bottom-tab badge. */
export function problemCounts(problems: Problem[]): {
  errors: number;
  warnings: number;
} {
  let errors = 0;
  let warnings = 0;
  for (const problem of problems) {
    if (problem.severity === "error") errors += 1;
    else if (problem.severity === "warning") warnings += 1;
  }
  return { errors, warnings };
}
