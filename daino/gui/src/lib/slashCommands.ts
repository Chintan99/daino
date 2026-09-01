// Slash commands for the agent composer, mirroring the terminal client's set
// (daino/tui/keybindings.py). The dropdown lists them; `runGuiSlashCommand`
// carries out the ones with a browser equivalent (navigation, session, stop).
// The rest fall through to the agent as an instruction.
import { useUIStore } from "../store/uiStore";
import { useAgentStore } from "../store/agentStore";
import { openDocs } from "./commands";

export interface SlashCommand {
  name: string;
  description: string;
  usage?: string;
  /** Enum values this command takes; picking one applies it immediately. */
  options?: string[];
}

export const EFFORT_LEVELS = [
  "auto",
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
];
export const AUTONOMY_MODES = ["plan", "ask", "session", "full"];
export const VERBOSE_OPTIONS = ["on", "off"];

export const SLASH_COMMANDS: SlashCommand[] = [
  { name: "/help", description: "Open help and documentation" },
  { name: "/clear", description: "Clear the visible conversation" },
  { name: "/new", description: "Start a new conversation session", usage: "[title]" },
  {
    name: "/mode",
    description: "Set agent autonomy",
    usage: "[plan|ask|session|full]",
    options: AUTONOMY_MODES,
  },
  { name: "/ask", description: "Ask a repository-grounded question", usage: "<question>" },
  { name: "/plan", description: "Create a persisted mission plan", usage: "<instruction>" },
  { name: "/build", description: "Plan or execute an approved mission", usage: "[instruction]" },
  { name: "/run", description: "Plan a complete coding mission", usage: "<instruction>" },
  { name: "/team", description: "Split work across parallel sub-agents", usage: "<instruction>" },
  { name: "/review", description: "Review the active mission changes" },
  { name: "/test", description: "Run verification", usage: "[targeted|failed|full|command]" },
  {
    name: "/qa",
    description: "Open or run the Inspector's QA and vulnerability assessment",
    usage: "[run]",
  },
  { name: "/inspect", description: "Open the Inspector's pre-push scan" },
  { name: "/status", description: "Show active project and mission status" },
  { name: "/missions", description: "Open the mission browser" },
  { name: "/tasks", description: "List crash-safe unfinished tasks" },
  {
    name: "/memory",
    description: "Inspect or manage durable memory",
    usage: "[search|project|decisions|failures|user|forget|verify]",
  },
  { name: "/resume", description: "Resume or open a mission", usage: "[mission-id]" },
  { name: "/cancel", description: "Cancel the current generation or mission" },
  { name: "/files", description: "Open the repository file browser", usage: "[query]" },
  { name: "/diff", description: "Open the Git diff viewer", usage: "[staged]" },
  { name: "/checkpoints", description: "Open checkpoints" },
  { name: "/checkpoint", description: "Create a checkpoint", usage: "[description]" },
  { name: "/restore", description: "Restore a checkpoint after confirmation", usage: "<checkpoint-id>" },
  { name: "/model", description: "Select a session model", usage: "[profile]" },
  {
    name: "/effort",
    description: "Set session reasoning effort",
    usage: "[auto|none|minimal|low|medium|high|xhigh|max]",
    options: EFFORT_LEVELS,
  },
  {
    name: "/verbose",
    description: "Show or hide detailed live progress",
    usage: "[on|off]",
    options: VERBOSE_OPTIONS,
  },
  { name: "/provider", description: "Open providers or test a connection", usage: "[name]" },
  { name: "/globalprovider", description: "Configure providers shared by every project" },
  { name: "/runtime", description: "Switch the session runtime", usage: "[local|docker|ssh]" },
  { name: "/index", description: "Rebuild repository intelligence" },
  { name: "/playbooks", description: "Browse engineering playbooks" },
  { name: "/deploy", description: "Run a deployment operation", usage: "<action> <target>" },
  { name: "/logs", description: "Open redacted event logs" },
  { name: "/map", description: "Open the prompt execution map" },
  { name: "/settings", description: "Open validated project settings" },
];

/** Commands the browser carries out itself (rather than passing to the agent). */
export function runGuiSlashCommand(raw: string): boolean {
  const name = raw.trim().split(/\s+/)[0].toLowerCase();
  const ui = useUIStore.getState();
  const agent = useAgentStore.getState();
  const openSidebar = () => {
    if (ui.sidebarCollapsed) ui.toggleSidebar();
  };
  const insights = (view: Parameters<typeof ui.setInsightsView>[0]) => {
    ui.setActiveWorkspaceTab("insights");
    ui.setInsightsView(view);
  };

  switch (name) {
    case "/help":
      openDocs();
      return true;
    case "/clear":
      agent.resetForSession();
      return true;
    case "/cancel":
      agent.requestStop();
      return true;
    case "/settings":
    case "/mode":
    case "/memory":
    case "/model":
    case "/effort":
    case "/verbose":
    case "/runtime":
      // Session configuration lives in the agent settings panel.
      ui.setAgentView("settings");
      return true;
    case "/provider":
    case "/globalprovider":
      ui.setAgentView("providers");
      return true;
    case "/files":
      ui.setActiveWorkspaceTab("code");
      ui.setActivityView("explorer");
      openSidebar();
      return true;
    case "/diff":
      ui.setActiveWorkspaceTab("code");
      ui.setActivityView("scm");
      openSidebar();
      return true;
    case "/logs":
      insights("logs");
      return true;
    case "/map":
      insights("map");
      return true;
    case "/qa":
    case "/inspect":
      ui.setActiveWorkspaceTab("inspector");
      ui.setInspectorView("scan");
      return true;
    case "/missions":
      insights("missions");
      return true;
    case "/checkpoints":
      insights("checkpoints");
      return true;
    case "/status":
      insights("repository");
      return true;
    default:
      return false;
  }
}
