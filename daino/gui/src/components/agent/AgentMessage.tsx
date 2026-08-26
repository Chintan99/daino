import ReactMarkdown from "react-markdown";
import type { SessionMessage } from "../../api/types";
import { BRAND } from "../../lib/branding";

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
