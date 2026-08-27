import ReactMarkdown from "react-markdown";
import type { SessionMessage } from "../../api/types";
import { BRAND } from "../../lib/branding";
import { ChangesetCard } from "./ChangesetCard";

const ROLE_CLASS: Record<string, string> = {
  user: "user",
  agent: "",
  tool: "tool",
  error: "error",
  status: "status",
  summary: "summary",
  approval: "approval",
};

export function AgentMessage({ message }: { message: SessionMessage }) {
  // The closing changeset is a structured card, not prose.
  if (message.kind === "changeset") {
    return <ChangesetCard metadata={message.metadata} />;
  }

  const cls = ROLE_CLASS[message.kind] ?? "";
  const label =
    message.kind === "user"
      ? "You"
      : message.kind === "agent"
        ? BRAND
        : message.kind;
  return (
    <div className={`msg ${cls}`}>
      <div className="role">{label}</div>
      <div className="md">
        <ReactMarkdown>{message.content || ""}</ReactMarkdown>
      </div>
    </div>
  );
}
