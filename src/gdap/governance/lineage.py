"""Data lineage (§16).

Lineage is recorded as edges between typed nodes (``source``, ``dataset_version``, ``pipeline``,
``job``, ``analysis``, ``report``, ``model``). Reading it back is a bounded breadth-first walk, so
"where did this number come from?" is answerable in one API call.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from sqlalchemy.orm import Session

from gdap.observability.logging import get_logger
from gdap.storage.repositories import LineageRepository

log = get_logger(__name__)

NODE_TYPES = (
    "source",
    "dataset",
    "dataset_version",
    "pipeline",
    "job",
    "analysis",
    "report",
    "model",
    "alert",
)


class LineageTracker:
    def __init__(self, session: Session, org_id: str) -> None:
        self.repo = LineageRepository(session, org_id)

    def record(
        self,
        *,
        upstream_type: str,
        upstream_id: str,
        downstream_type: str,
        downstream_id: str,
        operation: str,
        job_id: str | None = None,
    ) -> None:
        try:
            self.repo.create(
                upstream_type=upstream_type,
                upstream_id=upstream_id,
                downstream_type=downstream_type,
                downstream_id=downstream_id,
                operation=operation,
                job_id=job_id,
            )
        except Exception as exc:  # pragma: no cover
            log.error("lineage_write_failed", operation=operation, error=str(exc))

    def graph(self, node_type: str, node_id: str, *, depth: int = 3) -> dict[str, Any]:
        """Bounded bidirectional walk around one node."""
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add_node(kind: str, identifier: str, distance: int, direction: str) -> None:
            key = f"{kind}:{identifier}"
            if key not in nodes:
                nodes[key] = {
                    "type": kind,
                    "id": identifier,
                    "distance": distance,
                    "direction": direction,
                }

        add_node(node_type, node_id, 0, "self")
        queue: deque[tuple[str, str, int]] = deque([(node_type, node_id, 0)])
        while queue:
            kind, identifier, distance = queue.popleft()
            if distance >= depth or (kind, identifier) in seen:
                continue
            seen.add((kind, identifier))

            for row in self.repo.upstream(kind, identifier):
                edges.append(_edge(row))
                add_node(row.upstream_type, row.upstream_id, distance + 1, "upstream")
                queue.append((row.upstream_type, row.upstream_id, distance + 1))
            for row in self.repo.downstream(kind, identifier):
                edges.append(_edge(row))
                add_node(row.downstream_type, row.downstream_id, distance + 1, "downstream")
                queue.append((row.downstream_type, row.downstream_id, distance + 1))

        unique_edges = {(e["from"], e["to"], e["operation"]): e for e in edges}
        return {
            "root": {"type": node_type, "id": node_id},
            "nodes": list(nodes.values()),
            "edges": list(unique_edges.values()),
            "depth": depth,
        }

    def for_job(self, job_id: str) -> list[dict[str, Any]]:
        return [_edge(row) for row in self.repo.for_job(job_id)]


def _edge(row: Any) -> dict[str, Any]:
    return {
        "from": f"{row.upstream_type}:{row.upstream_id}",
        "to": f"{row.downstream_type}:{row.downstream_id}",
        "operation": row.operation,
        "job_id": row.job_id,
        "at": row.at.isoformat() if row.at else None,
    }
