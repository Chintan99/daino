// Every menu-bar action, in one place.
//
// The menu bar and the keyboard shortcuts both call these functions, so a
// command can never do two different things depending on how it was invoked,
// and the shortcut printed next to a menu item is the one that is actually
// bound (see `useShortcuts`).
import { api } from "../api/client";
import { useAgentStore } from "../store/agentStore";
import { useEditorStore } from "../store/editorStore";
import { useSettingsStore } from "../store/settingsStore";
import { useTerminalStore } from "../store/terminalStore";
import { useUIStore, type BottomTab, type ActivityView } from "../store/uiStore";
import { promptFor, showInfo } from "../store/dialogStore";
import { openFileInEditor } from "./openFile";
import { reloadBuffer, saveBuffer } from "./saveFile";
import { runEditorAction, activeEditor } from "./editorRegistry";
import { getTerminal } from "./terminalRegistry";
import { createTerminal, closeTerminal } from "./terminals";
import { BRAND } from "./branding";
import { queryClient } from "./queryClient";
import { qk } from "../api/hooks";

const editor = () => useEditorStore.getState();
const ui = () => useUIStore.getState();

/** Cmd on macOS, Ctrl elsewhere — used only for rendering shortcut hints. */
export const IS_MAC =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || "");
export const MOD = IS_MAC ? "⌘" : "Ctrl";
export const ALT = IS_MAC ? "⌥" : "Alt";
export const SHIFT = IS_MAC ? "⇧" : "Shift";

// ------------------------------------------------------------------ files

function parentDir(path: string): string {
  const index = path.lastIndexOf("/");
  return index <= 0 ? "" : path.slice(0, index);
}

export async function newFile(): Promise<void> {
  const base = editor().activePath ? `${parentDir(editor().activePath as string)}/` : "";
  const path = await promptFor({
    title: "New file",
    initial: base === "/" ? "" : base,
    placeholder: "relative/path/to/file.ts",
    hint: "Paths are relative to the project root.",
  });
  if (!path?.trim()) return;
  await api.createFile(path.trim(), false);
  await queryClient.invalidateQueries({ queryKey: ["files", "tree"] });
  await openFileInEditor(path.trim());
}

export async function newFolder(): Promise<void> {
  const path = await promptFor({
    title: "New folder",
    initial: "",
    placeholder: "relative/path/to/folder",
  });
  if (!path?.trim()) return;
  await api.createFile(path.trim(), true);
  await queryClient.invalidateQueries({ queryKey: ["files", "tree"] });
}

export async function openFileByPath(): Promise<void> {
  const path = await promptFor({
    title: "Open file",
    initial: "",
    placeholder: "relative/path/to/file.ts",
    hint: "Type a path relative to the project root.",
    confirmLabel: "Open",
  });
  if (path?.trim()) await openFileInEditor(path.trim());
}

export function saveActive(): void {
  const path = editor().activePath;
  if (path) void saveBuffer(path);
}

export function saveAll(): void {
  const { buffers } = editor();
  for (const [path, buffer] of Object.entries(buffers)) {
    if (buffer.dirty) void saveBuffer(path);
  }
}

export function revertActive(): void {
  const path = editor().activePath;
  if (path) void reloadBuffer(path);
}

/** Ask before dropping unsaved work, unless the user turned that off. */
function mayDiscard(name: string): boolean {
  if (!useSettingsStore.getState().confirmDirtyClose) return true;
  return window.confirm(`Discard unsaved changes to ${name}?`);
}

export function closeActiveTab(): void {
  const state = editor();
  const tab = state.tabs.find((t) => t.id === state.activeTabId);
  if (!tab) return;
  const dirty = tab.kind === "file" && state.buffers[tab.path]?.dirty;
  if (dirty && !mayDiscard(tab.name)) return;
  state.closeTab(tab.id);
}

export function closeAllTabs(): void {
  const state = editor();
  const dirty = state.tabs.filter(
    (t) => t.kind === "file" && state.buffers[t.path]?.dirty,
  );
  if (dirty.length && !mayDiscard(`${dirty.length} file(s)`)) return;
  for (const tab of [...state.tabs]) state.closeTab(tab.id);
}

export function cycleTab(delta: number): void {
  const state = editor();
  if (state.tabs.length < 2) return;
  const index = state.tabs.findIndex((t) => t.id === state.activeTabId);
  const next = (index + delta + state.tabs.length) % state.tabs.length;
  state.setActiveTab(state.tabs[next].id);
}

export async function copyActivePath(): Promise<void> {
  const path = editor().activePath;
  if (!path) return;
  try {
    await navigator.clipboard.writeText(path);
  } catch {
    /* clipboard permission denied — nothing useful to do */
  }
}

// ----------------------------------------------------------------- editing

export const undo = () => runEditorAction("undo");
export const redo = () => runEditorAction("redo");
export const find = () => runEditorAction("actions.find");
export const replace = () => runEditorAction("editor.action.startFindReplaceAction");
export const selectAll = () => runEditorAction("editor.action.selectAll");
export const toggleComment = () => runEditorAction("editor.action.commentLine");
export const formatDocument = () => runEditorAction("editor.action.formatDocument");
export const goToSymbol = () => runEditorAction("editor.action.quickOutline");
export const foldAll = () => runEditorAction("editor.foldAll");
export const unfoldAll = () => runEditorAction("editor.unfoldAll");

export async function goToLine(): Promise<void> {
  const instance = activeEditor();
  if (!instance) return;
  const answer = await promptFor({
    title: "Go to line",
    initial: String(instance.getPosition()?.lineNumber ?? 1),
    placeholder: "line[:column]",
    confirmLabel: "Go",
  });
  if (!answer) return;
  const [line, column] = answer.split(":").map((part) => Number(part.trim()));
  if (!Number.isFinite(line) || line < 1) return;
  const position = { lineNumber: line, column: Number.isFinite(column) ? column : 1 };
  instance.revealLineInCenter(line);
  instance.setPosition(position);
  instance.focus();
}

export function findInFiles(): void {
  ui().setActiveWorkspaceTab("code");
  ui().setActivityView("search");
  ui().focusSearch();
}

// -------------------------------------------------------------------- view

export function showActivityView(view: ActivityView): void {
  ui().setActiveWorkspaceTab("code");
  ui().setActivityView(view);
  if (ui().sidebarCollapsed) ui().toggleSidebar();
}

export function showBottomTab(tab: BottomTab): void {
  ui().setActiveWorkspaceTab("code");
  ui().setBottomTab(tab);
}

export const toggleSidebar = () => ui().toggleSidebar();
export const toggleBottomPanel = () => ui().setBottomVisible(!ui().bottomVisible);
export const toggleAgentPanel = () => ui().toggleAgent();

/**
 * Start a fresh conversation and attach this tab to it.
 *
 * The point is the clean slate: a session's own history is what each turn ships
 * as context, so a long-running session makes every prompt bigger and answers a
 * new request in the shadow of old ones.
 */
export async function newConversation(): Promise<void> {
  try {
    const created = await api.createSession("");
    await queryClient.invalidateQueries({ queryKey: qk.sessions });
    ui().setAgentVisible(true);
    ui().setSessionTarget(created.id);
  } catch (err) {
    window.alert(`Could not start a conversation: ${message(err)}`);
  }
}

/** Show provider setup in the agent column, expanding it if it is collapsed. */
export function openProviderSettings(): void {
  ui().setAgentVisible(true);
  ui().setAgentView("providers");
}

/** Show agent customization (autonomy, instructions, memory, playbooks). */
export function openAgentSettings(): void {
  ui().setAgentVisible(true);
  ui().setAgentView("settings");
}

export function openConversation(): void {
  ui().setAgentVisible(true);
  ui().setAgentView("chat");
}
export const openWorkspace = (id: string) => ui().setActiveWorkspaceTab(id);

export const zoomIn = () => useSettingsStore.getState().nudgeUIFont(1);
export const zoomOut = () => useSettingsStore.getState().nudgeUIFont(-1);
export const zoomReset = () => useSettingsStore.getState().resetFonts();

// ---------------------------------------------------------------- terminal

export const openNewTerminal = () => void createTerminal();

export function clearActiveTerminal(): void {
  const term = getTerminal(useTerminalStore.getState().activeId);
  term?.clear();
}

export function killActiveTerminal(): void {
  const id = useTerminalStore.getState().activeId;
  if (id) void closeTerminal(id);
}

export function killAllTerminals(): void {
  for (const id of [...useTerminalStore.getState().ids]) void closeTerminal(id);
}

// --------------------------------------------------------------------- run

/**
 * Start the app with the first detected command.
 *
 * The Inspector's Live view is where a command is chosen deliberately; the menu
 * is the one-keystroke path, so it uses detection and then shows that view so
 * the running command and URL are visible rather than implicit.
 */
export async function startPreview(): Promise<void> {
  ui().setActiveWorkspaceTab("inspector");
  ui().setInspectorView("live");
  try {
    const detected = await api.previewDetect();
    const first = detected.commands[0];
    if (!first) {
      window.alert("No preview command was detected for this project.");
      return;
    }
    await api.previewStart(first.command, first.default_url);
    await queryClient.invalidateQueries({ queryKey: qk.previewStatus });
  } catch (err) {
    window.alert(`Could not start the preview: ${message(err)}`);
  }
}

export async function stopPreview(): Promise<void> {
  try {
    await api.previewStop();
    await queryClient.invalidateQueries({ queryKey: qk.previewStatus });
  } catch (err) {
    window.alert(`Could not stop the preview: ${message(err)}`);
  }
}

/**
 * Run a full inspection from the menu and show it.
 *
 * No target URL is passed: the backend probes whatever app the Live view has
 * running, which is what the one-keystroke path should mean.
 */
export async function runQA(): Promise<void> {
  ui().setActiveWorkspaceTab("inspector");
  ui().setInspectorView("scan");
  try {
    await api.qaRun();
    await queryClient.invalidateQueries({ queryKey: qk.qaLatest });
  } catch (err) {
    window.alert(`Could not start the inspection: ${message(err)}`);
  }
}

export async function cancelQA(): Promise<void> {
  try {
    await api.qaCancel();
    await queryClient.invalidateQueries({ queryKey: qk.qaLatest });
  } catch (err) {
    window.alert(`Could not cancel the QA scan: ${message(err)}`);
  }
}

export async function reindexRepository(): Promise<void> {
  try {
    const result = await api.reindex();
    await queryClient.invalidateQueries({ queryKey: qk.repository });
    window.alert(
      `Indexed ${result.file_count} files${
        result.frameworks.length ? ` · ${result.frameworks.join(", ")}` : ""
      }.`,
    );
  } catch (err) {
    window.alert(`Could not index the repository: ${message(err)}`);
  }
}

export function stopAgentTurn(): void {
  useAgentStore.getState().send?.({ type: "cancel" });
}

// -------------------------------------------------------------------- help

export function openDocs(): void {
  window.open("/docs", "_blank", "noreferrer,noopener");
}

export function openApiReference(): void {
  window.open("/api-docs", "_blank", "noreferrer,noopener");
}

export function showShortcuts(): void {
  showInfo(
    "Keyboard shortcuts",
    [
      {
        heading: "File",
        rows: [
          { label: "Save", value: `${MOD} S` },
          { label: "Save all", value: `${SHIFT} ${MOD} S` },
          { label: "Open file…", value: `${MOD} O` },
          { label: "Close editor", value: `${MOD} W` },
          { label: "Next / previous editor", value: `${ALT} ${MOD} → / ←` },
        ],
      },
      {
        heading: "Edit",
        rows: [
          { label: "Find", value: `${MOD} F` },
          { label: "Replace", value: `${ALT} ${MOD} F` },
          { label: "Find in files", value: `${SHIFT} ${MOD} F` },
          { label: "Go to line", value: `${MOD} G` },
          { label: "Go to symbol", value: `${SHIFT} ${MOD} O` },
          { label: "Toggle comment", value: `${MOD} /` },
        ],
      },
      {
        heading: "View",
        rows: [
          { label: "Toggle sidebar", value: `${MOD} B` },
          { label: "Toggle panel", value: `${MOD} J` },
          { label: "Toggle agent", value: `${MOD} I` },
          { label: "Interface zoom in / out", value: `${MOD} + / ${MOD} -` },
          { label: "Reset zoom", value: `${MOD} 0` },
        ],
      },
      {
        heading: "Agent",
        rows: [
          { label: "Cycle autonomy mode", value: `${SHIFT} ⇥` },
          { label: "Next model profile", value: `${MOD} M` },
        ],
      },
      {
        heading: "Terminal",
        rows: [{ label: "New terminal", value: `${MOD} \`` }],
      },
    ],
    "The menu bar shows the same bindings next to each command.",
  );
}

export async function showAbout(): Promise<void> {
  let version = "unknown";
  let project = "";
  try {
    const health = await api.health();
    version = health.version;
    project = health.project;
  } catch {
    /* offline backend — still show what the browser knows */
  }
  showInfo(
    `About ${BRAND}`,
    [
      {
        heading: "Build",
        rows: [
          { label: "Version", value: version },
          { label: "Project", value: project || "—" },
          { label: "Interface", value: "browser IDE" },
        ],
      },
    ],
    `${BRAND} is a local-first AI coding agent. Nothing leaves this machine except model requests.`,
  );
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
