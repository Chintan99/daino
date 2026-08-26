import { useUIStore, type ActivityView } from "../store/uiStore";

const ITEMS: { id: ActivityView; icon: string; title: string }[] = [
  { id: "explorer", icon: "🗂", title: "Explorer" },
  { id: "search", icon: "🔍", title: "Search" },
  { id: "scm", icon: "⑃", title: "Source Control" },
];

export function ActivityBar() {
  const view = useUIStore((s) => s.activityView);
  const setView = useUIStore((s) => s.setActivityView);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);
  const collapsed = useUIStore((s) => s.sidebarCollapsed);

  return (
    <div className="activitybar">
      {ITEMS.map((it) => (
        <button
          key={it.id}
          className={`activity-item ${view === it.id && !collapsed ? "active" : ""}`}
          title={it.title}
          onClick={() => {
            if (view === it.id && !collapsed) toggleSidebar();
            else {
              setView(it.id);
              if (collapsed) toggleSidebar();
            }
          }}
        >
          {it.icon}
        </button>
      ))}
    </div>
  );
}
