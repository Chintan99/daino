// The result of "find all references" / "go to implementations".
//
// A list rather than a jump. References are a question you read the answer to,
// and replacing the editor's contents with the first hit throws away the
// question. Lives in a store because the activity bar renders it and the editor
// command produces it.
import { create } from "zustand";
import type { CodeLocation } from "../api/types";

export type ReferenceKind = "references" | "implementations";

export interface ReferenceQuery {
  path: string;
  line: number;
  column: number;
  kind: ReferenceKind;
}

export interface ReferenceResult {
  locations: CodeLocation[];
  detail: string;
  /**
   * "language-server" results are semantic. "index" results are text matches
   * from the repository index — the fallback when nothing is installed — and
   * the panel labels them, because acting on them as exact is how a refactor
   * renames a word inside a comment.
   */
  source: "language-server" | "index";
  available: boolean;
}

interface ReferencesState {
  query: ReferenceQuery | null;
  result: ReferenceResult | null;
  loading: boolean;
  begin: (
    path: string,
    line: number,
    column: number,
    kind?: ReferenceKind,
  ) => void;
  settle: (result: ReferenceResult) => void;
  clear: () => void;
}

export const useReferencesStore = create<ReferencesState>((set) => ({
  query: null,
  result: null,
  loading: false,
  begin: (path, line, column, kind = "references") =>
    set({ query: { path, line, column, kind }, result: null, loading: true }),
  settle: (result) => set({ result, loading: false }),
  clear: () => set({ query: null, result: null, loading: false }),
}));
