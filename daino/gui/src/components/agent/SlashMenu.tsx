import { useEffect, useRef } from "react";
import type { SlashCommand } from "../../lib/slashCommands";

/**
 * The command dropdown shown above the composer while a slash command is being
 * typed. It is keyboard-driven from the textarea, so it takes the active index
 * rather than owning it, and uses onMouseDown so a click lands before the
 * textarea's blur.
 */
export function SlashMenu({
  items,
  index,
  onPick,
  onHover,
}: {
  items: SlashCommand[];
  index: number;
  onPick: (cmd: SlashCommand) => void;
  onHover: (index: number) => void;
}) {
  const activeRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: "nearest" });
  }, [index]);

  if (!items.length) return null;

  return (
    <div className="slash-menu" role="listbox" aria-label="Commands">
      {items.map((cmd, i) => (
        <button
          key={cmd.name}
          ref={i === index ? activeRef : undefined}
          role="option"
          aria-selected={i === index}
          className={`slash-item ${i === index ? "active" : ""}`}
          onMouseEnter={() => onHover(i)}
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(cmd);
          }}
        >
          <span className="slash-name">{cmd.name}</span>
          {cmd.usage && <span className="slash-usage">{cmd.usage}</span>}
          {cmd.description && <span className="slash-desc">{cmd.description}</span>}
        </button>
      ))}
    </div>
  );
}
