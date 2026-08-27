// A single app-level dialog slot: prompts and reference sheets.
//
// The menu bar needs to ask for a path, a line number, or show the keyboard
// reference from anywhere, so the request lives in a store and one host
// component renders it. Prompts resolve a promise, which keeps the calling
// command readable: `const name = await promptFor(...)`.
import { create } from "zustand";

export interface InfoRow {
  label: string;
  value: string;
}
export interface InfoSection {
  heading: string;
  rows: InfoRow[];
}

export interface PromptRequest {
  kind: "prompt";
  title: string;
  hint?: string;
  initial: string;
  placeholder?: string;
  confirmLabel?: string;
  resolve: (value: string | null) => void;
}

export interface InfoRequest {
  kind: "info";
  title: string;
  hint?: string;
  sections: InfoSection[];
}

export type DialogRequest = PromptRequest | InfoRequest;

interface DialogState {
  request: DialogRequest | null;
  open: (request: DialogRequest) => void;
  close: () => void;
}

export const useDialogStore = create<DialogState>((set) => ({
  request: null,
  open: (request) => set({ request }),
  close: () => set({ request: null }),
}));

/** Ask for one line of text. Resolves null when the user cancels. */
export function promptFor(options: {
  title: string;
  initial?: string;
  hint?: string;
  placeholder?: string;
  confirmLabel?: string;
}): Promise<string | null> {
  return new Promise((resolve) => {
    const state = useDialogStore.getState();
    // A second prompt would strand the first promise; cancel it explicitly.
    if (state.request?.kind === "prompt") state.request.resolve(null);
    state.open({
      kind: "prompt",
      title: options.title,
      hint: options.hint,
      initial: options.initial ?? "",
      placeholder: options.placeholder,
      confirmLabel: options.confirmLabel,
      resolve,
    });
  });
}

export function showInfo(
  title: string,
  sections: InfoSection[],
  hint?: string,
): void {
  useDialogStore.getState().open({ kind: "info", title, sections, hint });
}
