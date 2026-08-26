// Pure helpers for keeping the canvas in step with the stored design.
import type { Node } from "reactflow";
import type { DesignNode } from "../../api/types";

/**
 * Re-apply the interaction state React Flow owns after a server sync.
 *
 * Rebuilding nodes from the stored design throws away `selected` and
 * `dragging`, which live only in React Flow. Without carrying them across, any
 * refetch — including the one a node's own position patch triggers — silently
 * deselects whatever the reader had just clicked.
 */
export function preserveInteractionState(previous: Node[], incoming: Node[]): Node[] {
  if (previous.length === 0) return incoming;
  const state = new Map(
    previous.map((node) => [node.id, { selected: node.selected, dragging: node.dragging }]),
  );
  return incoming.map((node) => {
    const carried = state.get(node.id);
    return carried ? { ...node, ...carried } : node;
  });
}

/**
 * Did this node actually move?
 *
 * React Flow reports a plain click as a zero-distance drag, so writing the
 * position back on every drag-stop turned every click into a document
 * revision — and every revision into a lost selection.
 */
export function hasMoved(stored: DesignNode | undefined, node: Node): boolean {
  if (!stored) return true;
  return (
    Math.round(stored.position.x) !== Math.round(node.position.x) ||
    Math.round(stored.position.y) !== Math.round(node.position.y)
  );
}
