// Keep the Problems panel in step with the open buffers.
//
// Diagnostics are requested for the buffer's *current* text, debounced, so what
// the panel shows describes the code on screen rather than the last save. The
// language server holds an open copy of each document and re-analyses on
// change, which is why sending text is cheap: the alternative is saving before
// every check, which nobody wants and which would make the panel lie between
// keystrokes.
import { api, ApiError } from "../api/client";
import { useEditorStore } from "../store/editorStore";
import {
  toProblem,
  useProblemsStore,
  type Coverage,
} from "../store/problemsStore";

/** Long enough that typing does not spam the server, short enough to feel live. */
const DEBOUNCE_MS = 400;

const timers = new Map<string, number>();
/** Guards against an older, slower response overwriting a newer one. */
const generation = new Map<string, number>();

/**
 * Ask for diagnostics on `path`, debounced per file.
 *
 * Concurrent edits to several files each get their own timer, because a project
 * where saving one file cancelled the analysis of another would be worse than
 * no debounce at all.
 */
export function requestDiagnostics(path: string): void {
  const existing = timers.get(path);
  if (existing) window.clearTimeout(existing);
  timers.set(
    path,
    window.setTimeout(() => {
      timers.delete(path);
      void fetchDiagnostics(path);
    }, DEBOUNCE_MS),
  );
}

/** Fetch immediately, skipping the debounce (used when a file is first opened). */
export async function fetchDiagnostics(path: string): Promise<void> {
  const buffer = useEditorStore.getState().buffers[path];
  if (!buffer) return;
  const ticket = (generation.get(path) ?? 0) + 1;
  generation.set(path, ticket);
  try {
    const result = await api.diagnostics(path, buffer.content);
    // A slower earlier request must not overwrite a newer answer.
    if (generation.get(path) !== ticket) return;
    // Still open? A file closed mid-request has no diagnostics to show.
    if (!useEditorStore.getState().buffers[path]) return;
    const coverage: Coverage = !result.supported
      ? "unsupported"
      : result.available
        ? "language-server"
        : "unavailable";
    useProblemsStore.getState().setFromServer(path, {
      problems: result.diagnostics.map(toProblem),
      coverage,
      detail: result.detail,
    });
  } catch (err) {
    if (generation.get(path) !== ticket) return;
    // A failed request is not a clean file. Record it as a coverage gap so the
    // panel keeps saying "nothing analysed this" rather than "nothing wrong".
    useProblemsStore.getState().setFromServer(path, {
      problems: [],
      coverage: "unavailable",
      detail:
        err instanceof ApiError
          ? err.message
          : `Could not reach the language service: ${
              err instanceof Error ? err.message : String(err)
            }`,
    });
  }
}

/** Drop a file's diagnostics and tell the server to stop tracking it. */
export function releaseDiagnostics(path: string): void {
  const timer = timers.get(path);
  if (timer) window.clearTimeout(timer);
  timers.delete(path);
  generation.delete(path);
  useProblemsStore.getState().clearPath(path);
  // Best effort: a server that never heard of the file does not mind.
  void api.closeDocument(path).catch(() => undefined);
}
