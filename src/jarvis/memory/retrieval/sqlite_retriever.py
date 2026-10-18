"""Search and metadata retrieval delegated to store-enforced access controls."""

from jarvis.memory.models import MemoryFilters, MemoryRecord, RetrievalContext
from jarvis.memory.storage.sqlite_store import MemoryStore


class SQLiteMemoryRetriever:
    """Bounded search facade; all permission and validity filtering is enforced by MemoryStore."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def search(
        self,
        query: str,
        context: RetrievalContext,
        filters: MemoryFilters | None = None,
        *,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        return self._store.search(query, context, filters, limit=limit)

    def filter_current(
        self,
        context: RetrievalContext,
        filters: MemoryFilters | None = None,
        *,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        return self._store.list_current(context, filters, limit=limit)
