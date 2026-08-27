// Build the compact JSON context block prepended to an outgoing user message.
// IMPORTANT: only references (paths / line ranges / ids) — never file contents.
import type { ContextChip } from "../store/agentStore";

export function buildContextBlock(
  chips: ContextChip[],
  workspace: string,
): string | null {
  if (chips.length === 0) return null;
  const block: Record<string, unknown> = { workspace };
  const attachments: string[] = [];
  for (const chip of chips) {
    if (chip.kind === "attachment") {
      // Several attachments are a list, not the last one winning.
      const path = chip.payload.attachment;
      if (typeof path === "string") attachments.push(path);
      continue;
    }
    Object.assign(block, chip.payload);
  }
  if (attachments.length) block.attachments = attachments;
  return JSON.stringify(block);
}

export function composeMessage(
  chips: ContextChip[],
  workspace: string,
  text: string,
): string {
  const block = buildContextBlock(chips, workspace);
  return block ? `${block}\n${text}` : text;
}
