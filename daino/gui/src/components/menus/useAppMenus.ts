// The application menu, as data.
//
// One hook builds every menu from live state, so the checkmarks, the greyed-out
// items, and the values shown beside a submenu are always the truth rather than
// a copy of it. Actions live in `lib/commands`; nothing here does work itself
// except the settings writes, which are a one-line mutation each.
import { useMemo } from "react";
import { api } from "../../api/client";
import { useSettings, useSettingsMutation } from "../../api/hooks";
import type { SettingsPatch } from "../../api/types";
import { useAgentStore } from "../../store/agentStore";
import { useEditorStore } from "../../store/editorStore";
import { showInfo } from "../../store/dialogStore";
import {
  useSettingsStore,
  CODE_FONT_MAX,
  CODE_FONT_MIN,
  UI_FONT_MAX,
  UI_FONT_MIN,
  type ThemeName,
} from "../../store/settingsStore";
import { useTerminalStore } from "../../store/terminalStore";
import { useUIStore, type BottomTab } from "../../store/uiStore";
import { WORKSPACE_TABS } from "../../tabs/registry";
import * as cmd from "../../lib/commands";
import { ALT, MOD, SHIFT } from "../../lib/commands";
import type { MenuDefinition, MenuNode } from "../ui/MenuBar";

const THEMES: { id: ThemeName; label: string; hint: string }[] = [
  { id: "dark", label: "Dark", hint: "The default near-black field" },
  { id: "light", label: "Light", hint: "Same palette, inverted" },
  { id: "contrast", label: "High contrast", hint: "Maximum separation" },
];

const UI_FONT_STEPS = [12, 13, 14, 15, 16, 18];
const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;
const BOTTOM_TABS: { id: BottomTab; label: string }[] = [
  { id: "terminal", label: "Terminal" },
  { id: "output", label: "Output" },
  { id: "problems", label: "Problems" },
  { id: "tests", label: "Tests" },
];

const sep: MenuNode = { type: "separator" };

export function useAppMenus(): MenuDefinition[] {
  const settings = useSettingsStore();
  const ui = useUIStore();
  const tabs = useEditorStore((s) => s.tabs);
  const buffers = useEditorStore((s) => s.buffers);
  const activePath = useEditorStore((s) => s.activePath);
  const turnRunning = useAgentStore((s) => s.turnRunning);
  const terminalIds = useTerminalStore((s) => s.ids);
  const activeTerminal = useTerminalStore((s) => s.activeId);
  const { data: project } = useSettings();
  const patch = useSettingsMutation();

  const dirtyCount = Object.values(buffers).filter((b) => b.dirty).length;
  const hasEditor = tabs.length > 0;

  return useMemo(() => {
    /** Write a project setting, reporting a rejection rather than swallowing it. */
    const apply = (body: SettingsPatch) =>
      patch.mutate(body, {
        onError: (err: unknown) =>
          window.alert(
            `Could not save that setting: ${
              err instanceof Error ? err.message : String(err)
            }`,
          ),
      });

    const check = (label: string, key: Parameters<typeof settings.toggle>[0], hint?: string): MenuNode => ({
      type: "item",
      label,
      hint,
      checked: Boolean(settings[key]),
      onSelect: () => settings.toggle(key),
    });

    const file: MenuDefinition = {
      id: "file",
      label: "File",
      items: [
        {
          type: "item",
          label: "New conversation",
          hint: "A fresh session — none of this one's history",
          disabled: turnRunning,
          onSelect: () => void cmd.newConversation(),
        },
        sep,
        { type: "item", label: "New file…", onSelect: () => void cmd.newFile() },
        { type: "item", label: "New folder…", onSelect: () => void cmd.newFolder() },
        {
          type: "item",
          label: "Open file…",
          shortcut: `${MOD} O`,
          onSelect: () => void cmd.openFileByPath(),
        },
        sep,
        {
          type: "item",
          label: "Save",
          shortcut: `${MOD} S`,
          disabled: !activePath || !buffers[activePath]?.dirty,
          onSelect: cmd.saveActive,
        },
        {
          type: "item",
          label: dirtyCount > 1 ? `Save all (${dirtyCount})` : "Save all",
          shortcut: `${SHIFT} ${MOD} S`,
          disabled: dirtyCount === 0,
          onSelect: cmd.saveAll,
        },
        {
          type: "item",
          label: "Revert file",
          hint: "Re-read from disk, discarding local edits",
          disabled: !activePath,
          onSelect: cmd.revertActive,
        },
        sep,
        {
          type: "item",
          label: "Close editor",
          shortcut: `${ALT} W`,
          disabled: !hasEditor,
          onSelect: cmd.closeActiveTab,
        },
        {
          type: "item",
          label: "Close all editors",
          disabled: !hasEditor,
          onSelect: cmd.closeAllTabs,
        },
        sep,
        {
          type: "item",
          label: "Copy path of active file",
          disabled: !activePath,
          onSelect: () => void cmd.copyActivePath(),
        },
        {
          type: "item",
          label: "Reload configuration from disk",
          hint: "Picks up an edit made outside the IDE",
          onSelect: () => {
            void api
              .reloadSettings()
              .catch((err: unknown) =>
                window.alert(`Could not reload configuration: ${String(err)}`),
              );
          },
        },
      ],
    };

    const edit: MenuDefinition = {
      id: "edit",
      label: "Edit",
      items: [
        { type: "item", label: "Undo", shortcut: `${MOD} Z`, onSelect: cmd.undo },
        { type: "item", label: "Redo", shortcut: `${SHIFT} ${MOD} Z`, onSelect: cmd.redo },
        sep,
        { type: "item", label: "Find", shortcut: `${MOD} F`, onSelect: cmd.find },
        {
          type: "item",
          label: "Replace",
          shortcut: `${ALT} ${MOD} F`,
          onSelect: cmd.replace,
        },
        {
          type: "item",
          label: "Find in files",
          shortcut: `${SHIFT} ${MOD} F`,
          onSelect: cmd.findInFiles,
        },
        sep,
        {
          type: "item",
          label: "Toggle line comment",
          shortcut: `${MOD} /`,
          onSelect: cmd.toggleComment,
        },
        { type: "item", label: "Format document", onSelect: cmd.formatDocument },
        { type: "item", label: "Fold all", onSelect: cmd.foldAll },
        { type: "item", label: "Unfold all", onSelect: cmd.unfoldAll },
        sep,
        { type: "item", label: "Select all", shortcut: `${MOD} A`, onSelect: cmd.selectAll },
      ],
    };

    const go: MenuDefinition = {
      id: "go",
      label: "Go",
      items: [
        {
          type: "item",
          label: "Go to file…",
          onSelect: () => void cmd.openFileByPath(),
        },
        {
          type: "item",
          label: "Go to line…",
          shortcut: `${MOD} G`,
          disabled: !activePath,
          onSelect: () => void cmd.goToLine(),
        },
        {
          type: "item",
          label: "Go to symbol…",
          shortcut: `${SHIFT} ${MOD} O`,
          disabled: !activePath,
          onSelect: cmd.goToSymbol,
        },
        sep,
        {
          type: "item",
          label: "Next editor",
          shortcut: `${ALT} ${MOD} →`,
          disabled: tabs.length < 2,
          onSelect: () => cmd.cycleTab(1),
        },
        {
          type: "item",
          label: "Previous editor",
          shortcut: `${ALT} ${MOD} ←`,
          disabled: tabs.length < 2,
          onSelect: () => cmd.cycleTab(-1),
        },
        sep,
        { type: "item", label: "Explorer", onSelect: () => cmd.showActivityView("explorer") },
        { type: "item", label: "Search", onSelect: () => cmd.showActivityView("search") },
        {
          type: "item",
          label: "Source control",
          onSelect: () => cmd.showActivityView("scm"),
        },
        sep,
        {
          type: "item",
          label: "Execution map",
          onSelect: () => {
            cmd.openWorkspace("insights");
            ui.setInsightsView("map");
          },
        },
        {
          type: "item",
          label: "QA report",
          onSelect: () => {
            cmd.openWorkspace("insights");
            ui.setInsightsView("qa");
          },
        },
      ],
    };

    const view: MenuDefinition = {
      id: "view",
      label: "View",
      items: [
        { type: "label", text: "Workspace" },
        ...WORKSPACE_TABS.map<MenuNode>((tab) => ({
          type: "item",
          label: tab.label.charAt(0) + tab.label.slice(1).toLowerCase(),
          hint: tab.hint,
          checked: ui.activeWorkspaceTab === tab.id,
          onSelect: () => cmd.openWorkspace(tab.id),
        })),
        sep,
        {
          type: "item",
          label: "Sidebar",
          shortcut: `${MOD} B`,
          checked: !ui.sidebarCollapsed,
          onSelect: cmd.toggleSidebar,
        },
        {
          type: "item",
          label: "Panel",
          shortcut: `${MOD} J`,
          checked: ui.bottomVisible,
          onSelect: cmd.toggleBottomPanel,
        },
        {
          type: "item",
          label: "Agent",
          shortcut: `${MOD} I`,
          checked: ui.agentVisible,
          onSelect: cmd.toggleAgentPanel,
        },
        {
          type: "submenu",
          label: "Panel view",
          value: BOTTOM_TABS.find((t) => t.id === ui.bottomTab)?.label,
          items: BOTTOM_TABS.map<MenuNode>((tab) => ({
            type: "item",
            label: tab.label,
            checked: ui.bottomTab === tab.id && ui.bottomVisible,
            onSelect: () => cmd.showBottomTab(tab.id),
          })),
        },
        sep,
        {
          type: "item",
          label: "Zoom in",
          shortcut: `${MOD} =`,
          disabled: settings.uiFontSize >= UI_FONT_MAX,
          onSelect: cmd.zoomIn,
        },
        {
          type: "item",
          label: "Zoom out",
          shortcut: `${MOD} -`,
          disabled: settings.uiFontSize <= UI_FONT_MIN,
          onSelect: cmd.zoomOut,
        },
        { type: "item", label: "Reset zoom", shortcut: `${MOD} 0`, onSelect: cmd.zoomReset },
      ],
    };

    const run: MenuDefinition = {
      id: "run",
      label: "Run",
      items: [
        {
          type: "item",
          label: "Start preview",
          hint: "Uses the first detected run command",
          onSelect: () => void cmd.startPreview(),
        },
        { type: "item", label: "Stop preview", onSelect: () => void cmd.stopPreview() },
        sep,
        {
          type: "item",
          label: "Run QA scan",
          hint: "Architecture, security, quality, tests, dependencies",
          onSelect: () => void cmd.runQA(),
        },
        { type: "item", label: "Cancel QA scan", onSelect: () => void cmd.cancelQA() },
        sep,
        {
          type: "item",
          label: "Index repository",
          onSelect: () => void cmd.reindexRepository(),
        },
        sep,
        {
          type: "item",
          label: "Stop agent turn",
          disabled: !turnRunning,
          danger: true,
          onSelect: cmd.stopAgentTurn,
        },
      ],
    };

    const terminal: MenuDefinition = {
      id: "terminal",
      label: "Terminal",
      items: [
        { type: "item", label: "New terminal", shortcut: "Ctrl `", onSelect: cmd.openNewTerminal },
        {
          type: "item",
          label: "Clear terminal",
          disabled: !activeTerminal,
          onSelect: cmd.clearActiveTerminal,
        },
        sep,
        {
          type: "submenu",
          label: "Active shell",
          disabled: terminalIds.length === 0,
          value: activeTerminal
            ? `shell ${terminalIds.indexOf(activeTerminal) + 1}`
            : undefined,
          items: terminalIds.map<MenuNode>((id, index) => ({
            type: "item",
            label: `shell ${index + 1}`,
            checked: id === activeTerminal,
            onSelect: () => {
              useTerminalStore.getState().setActive(id);
              cmd.showBottomTab("terminal");
            },
          })),
        },
        sep,
        {
          type: "item",
          label: "Kill terminal",
          disabled: !activeTerminal,
          danger: true,
          onSelect: cmd.killActiveTerminal,
        },
        {
          type: "item",
          label: "Kill all terminals",
          disabled: terminalIds.length === 0,
          danger: true,
          onSelect: cmd.killAllTerminals,
        },
        sep,
        {
          type: "item",
          label: "Show panel",
          shortcut: `${MOD} J`,
          checked: ui.bottomVisible,
          onSelect: cmd.toggleBottomPanel,
        },
      ],
    };

    // ------------------------------------------------------------- settings

    const fontSubmenu = (
      label: string,
      value: number,
      nudge: (delta: number) => void,
      min: number,
      max: number,
    ): MenuNode => ({
      type: "submenu",
      label,
      value: `${value}px`,
      items: [
        {
          type: "item",
          label: "Increase",
          disabled: value >= max,
          onSelect: () => nudge(1),
        },
        {
          type: "item",
          label: "Decrease",
          disabled: value <= min,
          onSelect: () => nudge(-1),
        },
      ],
    });

    const providerItems: MenuNode[] = project?.providers.length
      ? project.providers.map<MenuNode>((provider) => {
          // A provider reads as "active" when every agent role routes to it.
          const profiles = project.models
            .filter((model) => model.provider === provider.name)
            .map((model) => model.name);
          const active =
            profiles.length > 0 &&
            project.roles.every((role) => profiles.includes(project.routing[role] ?? ""));
          return {
            type: "item",
            label: provider.name,
            hint: `${provider.type} · ${provider.model}`,
            checked: active,
            onSelect: () => apply({ default_provider: provider.name }),
          };
        })
      : [
          {
            type: "item",
            label: "No provider configured",
            hint: "Add one with `daino providers add`",
            disabled: true,
            onSelect: () => {},
          },
        ];

    const routingItems: MenuNode[] = project?.roles.length
      ? project.roles.map<MenuNode>((role) => ({
          type: "submenu",
          label: role,
          value: project.routing[role] || "unset",
          items: project.models.length
            ? project.models.map<MenuNode>((model) => ({
                type: "item",
                label: model.name,
                hint: `${model.provider} · ${model.model}`,
                checked: project.routing[role] === model.name,
                onSelect: () => apply({ routing: { [role]: model.name } }),
              }))
            : [
                {
                  type: "item",
                  label: "No model profile configured",
                  disabled: true,
                  onSelect: () => {},
                },
              ],
        }))
      : [
          {
            type: "item",
            label: "Settings unavailable",
            disabled: true,
            onSelect: () => {},
          },
        ];

    const settingsMenu: MenuDefinition = {
      id: "settings",
      label: "Settings",
      items: [
        { type: "label", text: "Appearance" },
        {
          type: "submenu",
          label: "Theme",
          value: THEMES.find((t) => t.id === settings.theme)?.label,
          items: THEMES.map<MenuNode>((theme) => ({
            type: "item",
            label: theme.label,
            hint: theme.hint,
            checked: settings.theme === theme.id,
            onSelect: () => settings.set("theme", theme.id),
          })),
        },
        {
          type: "submenu",
          label: "Interface font size",
          value: `${settings.uiFontSize}px`,
          items: [
            {
              type: "item",
              label: "Increase",
              shortcut: `${MOD} =`,
              disabled: settings.uiFontSize >= UI_FONT_MAX,
              onSelect: cmd.zoomIn,
            },
            {
              type: "item",
              label: "Decrease",
              shortcut: `${MOD} -`,
              disabled: settings.uiFontSize <= UI_FONT_MIN,
              onSelect: cmd.zoomOut,
            },
            sep,
            ...UI_FONT_STEPS.map<MenuNode>((size) => ({
              type: "item",
              label: `${size}px`,
              checked: settings.uiFontSize === size,
              onSelect: () => settings.set("uiFontSize", size),
            })),
          ],
        },
        fontSubmenu(
          "Editor font size",
          settings.editorFontSize,
          settings.nudgeEditorFont,
          CODE_FONT_MIN,
          CODE_FONT_MAX,
        ),
        fontSubmenu(
          "Terminal font size",
          settings.terminalFontSize,
          settings.nudgeTerminalFont,
          CODE_FONT_MIN,
          CODE_FONT_MAX,
        ),
        { type: "item", label: "Reset font sizes", onSelect: settings.resetFonts },
        sep,
        { type: "label", text: "Editor" },
        {
          type: "submenu",
          label: "Editor behaviour",
          items: [
            check("Word wrap", "wordWrap"),
            check("Minimap", "minimap"),
            check("Line numbers", "lineNumbers"),
            check("Render whitespace", "renderWhitespace"),
            check("Sticky scroll", "stickyScroll"),
            sep,
            check("Auto save", "autoSave", "Save a buffer one second after typing stops"),
            check("Confirm before discarding changes", "confirmDirtyClose"),
          ],
        },
        {
          type: "submenu",
          label: "Tab size",
          value: String(settings.tabSize),
          items: [2, 4, 8].map<MenuNode>((size) => ({
            type: "item",
            label: String(size),
            checked: settings.tabSize === size,
            onSelect: () => settings.set("tabSize", size),
          })),
        },
        sep,
        { type: "label", text: "Agent" },
        {
          type: "item",
          label: "Agent settings…",
          hint: "Autonomy, instructions, memory, playbooks",
          onSelect: cmd.openAgentSettings,
        },
        {
          type: "submenu",
          label: "Provider",
          value: project?.providers.length
            ? `${project.providers.length} configured`
            : "none",
          items: [
            {
              type: "item",
              label: "Manage providers…",
              hint: "Add, edit, and test a connection in the agent panel",
              onSelect: cmd.openProviderSettings,
            },
            sep,
            ...providerItems,
            sep,
            {
              type: "item",
              label: "Check provider health…",
              hint: "Makes a real request to each provider",
              onSelect: () => void showProviderHealth(),
            },
          ],
        },
        {
          type: "submenu",
          label: "Model routing",
          hint: "Which model each agent role uses",
          items: routingItems,
        },
        {
          type: "submenu",
          label: "Conversation",
          items: [
            check(
              "Attach editor context",
              "sendWithContext",
              "Send the open file, selection, and diff with each message",
            ),
            check("Show reasoning", "showThinking"),
          ],
        },
        sep,
        { type: "label", text: "Execution" },
        {
          type: "submenu",
          label: "Runtime",
          value: project?.runtime.default,
          items: (["docker", "local", "ssh"] as const).map<MenuNode>((runtime) => ({
            type: "item",
            label: runtime,
            hint:
              runtime === "docker"
                ? "Sandboxed container (recommended)"
                : runtime === "local"
                  ? "This machine, unsandboxed"
                  : "A configured remote host",
            checked: project?.runtime.default === runtime,
            disabled: !project,
            onSelect: () => apply({ runtime }),
          })),
        },
        {
          type: "submenu",
          label: "Network access",
          value: project?.runtime.network_access,
          items: (["restricted", "allowed"] as const).map<MenuNode>((mode) => ({
            type: "item",
            label: mode,
            checked: project?.runtime.network_access === mode,
            disabled: !project,
            onSelect: () => apply({ network_access: mode }),
          })),
        },
        {
          type: "submenu",
          label: "Approvals",
          items: project
            ? [
                {
                  type: "item",
                  label: "Ask before installing packages",
                  checked: project.security.require_approval_for_install,
                  onSelect: () =>
                    apply({
                      require_approval_for_install:
                        !project.security.require_approval_for_install,
                    }),
                },
                {
                  type: "item",
                  label: "Ask before network access",
                  checked: project.security.require_approval_for_network,
                  onSelect: () =>
                    apply({
                      require_approval_for_network:
                        !project.security.require_approval_for_network,
                    }),
                },
                {
                  type: "item",
                  label: "Ask before production actions",
                  checked: project.security.require_approval_for_production,
                  onSelect: () =>
                    apply({
                      require_approval_for_production:
                        !project.security.require_approval_for_production,
                    }),
                },
                sep,
                {
                  type: "item",
                  label: "Require review before commit",
                  checked: project.verification.require_review,
                  onSelect: () =>
                    apply({ require_review: !project.verification.require_review }),
                },
              ]
            : [
                {
                  type: "item",
                  label: "Settings unavailable",
                  disabled: true,
                  onSelect: () => {},
                },
              ],
        },
        {
          type: "submenu",
          label: "Notifications",
          hint: "When to interrupt you about a turn",
          value: project?.notifications.enabled ? undefined : "off",
          items: project
            ? [
                {
                  type: "item",
                  label: "Notify me",
                  hint: "Master switch for the three moments below",
                  checked: project.notifications.enabled,
                  onSelect: () =>
                    apply({ notifications_enabled: !project.notifications.enabled }),
                },
                sep,
                {
                  type: "item",
                  label: "When a turn completes",
                  checked: project.notifications.on_completed,
                  disabled: !project.notifications.enabled,
                  onSelect: () =>
                    apply({ notify_on_completed: !project.notifications.on_completed }),
                },
                {
                  type: "item",
                  label: "When something fails",
                  checked: project.notifications.on_failed,
                  disabled: !project.notifications.enabled,
                  onSelect: () => apply({ notify_on_failed: !project.notifications.on_failed }),
                },
                {
                  type: "item",
                  label: "When an approval is needed",
                  checked: project.notifications.on_approval,
                  disabled: !project.notifications.enabled,
                  onSelect: () =>
                    apply({ notify_on_approval: !project.notifications.on_approval }),
                },
                sep,
                {
                  type: "item",
                  label: "Desktop notification",
                  hint: "A real OS notification, not just in-app",
                  checked: project.notifications.desktop,
                  disabled: !project.notifications.enabled,
                  onSelect: () => apply({ notify_desktop: !project.notifications.desktop }),
                },
                {
                  type: "item",
                  label: "Terminal bell",
                  hint: "Most terminals turn this into a tab badge",
                  checked: project.notifications.terminal_bell,
                  disabled: !project.notifications.enabled,
                  onSelect: () =>
                    apply({ notify_terminal_bell: !project.notifications.terminal_bell }),
                },
              ]
            : [
                {
                  type: "item",
                  label: "Settings unavailable",
                  disabled: true,
                  onSelect: () => {},
                },
              ],
        },
        {
          type: "item",
          label: "Keep awake while working",
          hint: "Stops the machine sleeping mid-turn, and this display dimming",
          checked: project?.keep_awake ?? false,
          disabled: !project,
          onSelect: () => apply({ keep_awake: !project?.keep_awake }),
        },
        sep,
        { type: "label", text: "Diagnostics" },
        {
          type: "submenu",
          label: "Log level",
          value: project?.observability.log_level,
          items: LOG_LEVELS.map<MenuNode>((level) => ({
            type: "item",
            label: level,
            checked: project?.observability.log_level === level,
            disabled: !project,
            onSelect: () => apply({ log_level: level }),
          })),
        },
        check(
          "Verbose event stream",
          "verboseEvents",
          "Show every agent event in Output, not just the summary",
        ),
        sep,
        {
          type: "item",
          label: "Reset interface settings",
          danger: true,
          onSelect: () => {
            if (window.confirm("Reset theme, fonts, and editor preferences?"))
              settings.resetAll();
          },
        },
      ],
    };

    const help: MenuDefinition = {
      id: "help",
      label: "Help",
      items: [
        { type: "item", label: "Documentation", onSelect: cmd.openDocs },
        { type: "item", label: "API reference", onSelect: cmd.openApiReference },
        { type: "item", label: "Keyboard shortcuts", onSelect: cmd.showShortcuts },
        sep,
        { type: "item", label: "About", onSelect: () => void cmd.showAbout() },
      ],
    };

    return [file, edit, go, view, run, terminal, settingsMenu, help];
  }, [
    settings,
    ui,
    tabs,
    buffers,
    activePath,
    dirtyCount,
    hasEditor,
    turnRunning,
    terminalIds,
    activeTerminal,
    project,
    patch,
  ]);
}

/** Probe every provider and report the result in the reference dialog. */
async function showProviderHealth(): Promise<void> {
  try {
    const result = await api.providerHealth();
    showInfo(
      "Provider health",
      [
        {
          heading: "Configured providers",
          rows: result.providers.map((provider) => ({
            label: `${provider.name} · ${provider.model}`,
            value: `${provider.connected ? "✓" : "✗"} ${provider.detail}`,
          })),
        },
      ],
      "Each row is a live request made just now.",
    );
  } catch (err) {
    window.alert(
      `Could not reach the providers: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
}
