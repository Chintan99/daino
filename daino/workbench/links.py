"""How a workspace's outputs relate, and which of them may have gone stale.

A workspace accumulates documents that come from other documents: an analysis
from an upload, an architecture from the analysis, a proposal from the
architecture. Nothing recorded that, so when the architecture changed, the
proposal quietly went on describing the old one.

The schema is one flat edge table, because everything the product needs is one
hop: "what came from this" and "what is now behind". Edges read in the source's
voice — ``proposal.md derived_from architecture`` — and carry the target's
revision at the time they were made, which is the whole staleness mechanism:
if the target has moved past that number, the source was written against
something that no longer exists.

Staleness is advisory and stays that way. Daino says a document *may* be
outdated; it never rewrites one because something upstream moved. An agent that
silently regenerates a document a person has been editing is a worse failure
than a stale one, and only the person knows which.
"""

from __future__ import annotations

from sqlalchemy import select

from daino.persistence import Database
from daino.persistence.models import WorkspaceLink as LinkRow
from daino.utils.ids import new_id
from daino.workbench.models import ArtifactLink, StaleArtifact
from daino.workbench.service import WorkbenchService

#: Relations whose target changing makes the source questionable. ``references``
#: is deliberately absent: citing a document does not mean tracking it.
DERIVING: frozenset[str] = frozenset(
    {"derived_from", "generated_from", "depends_on", "implements", "describes"}
)


class LinkStore:
    """Record and read the relationships between a workspace's outputs."""

    def __init__(self, database: Database, workbench: WorkbenchService) -> None:
        self.database = database
        self.workbench = workbench

    def link(
        self,
        workspace_id: str,
        *,
        source_path: str,
        target_path: str,
        relation: str = "references",
        source_kind: str = "artifact",
        target_kind: str = "artifact",
        title: str = "",
    ) -> ArtifactLink:
        """Record that ``source_path`` came from ``target_path``.

        Idempotent on the triple, so an agent that links the same pair twice
        updates the revision stamp rather than growing the graph.
        """
        revision = self._revision(workspace_id, target_path) if target_kind == "artifact" else 0
        with self.database.session() as session:
            row = session.scalars(
                select(LinkRow)
                .where(LinkRow.workspace_id == workspace_id)
                .where(LinkRow.source_path == source_path)
                .where(LinkRow.target_path == target_path)
                .where(LinkRow.relation == relation)
            ).first()
            if row is None:
                row = LinkRow(
                    id=new_id("wslink"),
                    workspace_id=workspace_id,
                    source_path=source_path,
                    source_kind=source_kind,
                    target_path=target_path,
                    target_kind=target_kind,
                    relation=relation,
                    title=title[:255],
                )
                session.add(row)
            row.target_revision = revision
            row.source_kind = source_kind
            row.target_kind = target_kind
            if title:
                row.title = title[:255]
            identifier = row.id
        return next(item for item in self.links_for(workspace_id) if item.id == identifier)

    def links_for(self, workspace_id: str) -> list[ArtifactLink]:
        with self.database.session() as session:
            rows = session.scalars(
                select(LinkRow)
                .where(LinkRow.workspace_id == workspace_id)
                .order_by(LinkRow.created_at)
            ).all()
            return [_describe(row) for row in rows]

    def unlink(self, workspace_id: str, link_id: str) -> None:
        with self.database.session() as session:
            row = session.get(LinkRow, link_id)
            if row is not None and row.workspace_id == workspace_id:
                session.delete(row)

    def acknowledge(self, workspace_id: str, link_id: str) -> None:
        """Take the staleness warning down without changing the document.

        "Ignore" has to mean something durable, or the warning returns on the
        next render and the user learns to ignore warnings generally.
        """
        with self.database.session() as session:
            row = session.get(LinkRow, link_id)
            if row is None or row.workspace_id != workspace_id:
                return
            row.target_revision = self._revision(workspace_id, row.target_path)

    def stale(self, workspace_id: str) -> list[StaleArtifact]:
        """Documents written against a version of something that has since moved."""
        found: list[StaleArtifact] = []
        for link in self.links_for(workspace_id):
            if link.target_kind != "artifact" or link.relation not in DERIVING:
                continue
            current = self._revision(workspace_id, link.target_path)
            if current <= link.target_revision:
                continue
            found.append(
                StaleArtifact(
                    link_id=link.id,
                    path=link.source_path,
                    source_of_truth=link.target_path,
                    relation=link.relation,
                    seen_revision=link.target_revision,
                    current_revision=current,
                    reason=(
                        f"{link.source_path} was written from {link.target_path}, "
                        f"which has changed {current - link.target_revision} time(s) since."
                    ),
                )
            )
        return found

    def _revision(self, workspace_id: str, relative: str) -> int:
        revisions = self.workbench.revisions(workspace_id, relative)
        return revisions[0].version if revisions else 0


def _describe(row: LinkRow) -> ArtifactLink:
    return ArtifactLink(
        id=row.id,
        source_path=row.source_path,
        source_kind=row.source_kind,  # type: ignore[arg-type]
        target_path=row.target_path,
        target_kind=row.target_kind,  # type: ignore[arg-type]
        relation=row.relation,  # type: ignore[arg-type]
        title=row.title,
        target_revision=row.target_revision,
        created_at=row.created_at,
    )
