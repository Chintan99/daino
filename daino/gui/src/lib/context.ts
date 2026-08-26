// Build the compact JSON context block prepended to an outgoing user message.
// IMPORTANT: only references (paths / line ranges / ids) — never file contents.
import type { ContextChip } from "../store/agentStore";

export function buildContextBlock(
  chips: ContextChip[],
  workspace: string,
): string | null {
  if (chips.length === 0) return null;
  const block: Record<string, unknown> = { workspace };
  for (const chip of chips) {
    Object.assign(block, chip.payload);
  }
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
