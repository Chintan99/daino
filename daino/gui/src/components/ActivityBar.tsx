import type { ReactNode } from "react";
import { useUIStore, type ActivityView } from "../store/uiStore";

/**
 * Icons are inline SVG rather than text glyphs.
 *
 * The bar is sized for a comfortable target (see --activity-item), and at that
 * size box-drawing characters read as mismatched blocks: they are drawn at
 * whatever weight the system font happens to use. These follow `currentColor`,
 * so they also track the active theme.
 */
function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

const ICONS: Record<ActivityView | "panel", ReactNode> = {
  explorer: (
    <Icon>
      <path d="M4 6.5A1.5 1.5 0 0 1 5.5 5h3.2l1.6 2h8.2A1.5 1.5 0 0 1 20 8.5v9A1.5 1.5 0 0 1 18.5 19h-13A1.5 1.5 0 0 1 4 17.5z" />
      <path d="M4 10.5h16" />
    </Icon>
  ),
  search: (
    <Icon>
      <circle cx="10.5" cy="10.5" r="5.5" />
      <path d="M14.6 14.6 20 20" />
    </Icon>
  ),
  scm: (
    <Icon>
      <circle cx="7" cy="6" r="2.2" />
      <circle cx="7" cy="18" r="2.2" />
      <circle cx="17" cy="10" r="2.2" />
      <path d="M7 8.2v7.6" />
      <path d="M17 12.2c0 3-2.4 3.8-5.4 4.2" />
      <path d="M9.2 6h3.4A4.4 4.4 0 0 1 17 7.8" />
    </Icon>
  ),
  panel: (
    <Icon>
      <rect x="3.5" y="4.5" width="17" height="15" rx="1.8" />
      <path d="M3.5 14.5h17" />
    </Icon>
  ),
};

const ITEMS: { id: ActivityView; title: string }[] = [
  { id: "explorer", title: "Explorer" },
  { id: "search", title: "Search" },
  { id: "scm", title: "Source Control" },
];

export function ActivityBar() {
  const view = useUIStore((s) => s.activityView);
  const setView = useUIStore((s) => s.setActivityView);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);
  const collapsed = useUIStore((s) => s.sidebarCollapsed);
  const bottomVisible = useUIStore((s) => s.bottomVisible);
  const setBottomVisible = useUIStore((s) => s.setBottomVisible);

  return (
    <div className="activitybar">
      {ITEMS.map((it) => (
        <button
          key={it.id}
          className={`activity-item ${view === it.id && !collapsed ? "active" : ""}`}
          title={it.title}
          aria-label={it.title}
          onClick={() => {
            if (view === it.id && !collapsed) toggleSidebar();
            else {
              setView(it.id);
              if (collapsed) toggleSidebar();
            }
          }}
        >
          {ICONS[it.id]}
        </button>
      ))}
      <span className="grow" />
      <button
        className={`activity-item ${bottomVisible ? "active" : ""}`}
        title={bottomVisible ? "Hide the panel" : "Show the panel"}
        aria-label={bottomVisible ? "Hide the panel" : "Show the panel"}
        onClick={() => setBottomVisible(!bottomVisible)}
      >
        {ICONS.panel}
      </button>
    </div>
  );
}
