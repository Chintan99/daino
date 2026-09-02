/**
 * A "beta" badge on the WORKSPACE tab.
 *
 * Workspace runs execute a plan unattended, write documents, and reach into
 * DESIGN and CODE. Saying so on the tab sets the right expectation before
 * someone hands it a day of work — which is cheaper than discovering the
 * expectation was wrong afterwards.
 */
export function WorkspaceMark() {
  return (
    <span className="tab-badge beta" title="Workspace is in beta — behaviour may change">
      beta
    </span>
  );
}
