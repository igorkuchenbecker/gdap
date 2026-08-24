"""Ingestion engine: batch, incremental and append loads with checkpoints (§6)."""

from gdap.ingestion.engine import IngestionEngine, IngestRequest

__all__ = ["IngestRequest", "IngestionEngine"]
