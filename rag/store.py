"""The persistent ChromaDB index: one collection, one record per chunk.

Chroma is used only as storage and search for vectors we compute ourselves: the collection
has no embedding function of its own (so Chroma never downloads its default model), and
anonymous telemetry is switched off. Distance is cosine.

Each record carries the file it came from (metadata `file`), so all chunks of one document
can be replaced or deleted together.
"""
from pathlib import Path

ADD_BATCH = 500


class Store:
    def __init__(self, path: Path, collection: str, embedder_name: str, dim: int,
                 reset: bool = False):
        import chromadb
        from chromadb.config import Settings

        Path(path).mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(path),
                                                settings=Settings(anonymized_telemetry=False))
        self.name, self.embedder_name, self.dim = collection, embedder_name, dim
        if reset:
            self._drop()
        self.collection = self._open()

    def _open(self):
        col = self.client.get_or_create_collection(
            self.name, embedding_function=None,
            configuration={"hnsw": {"space": "cosine"}},
            metadata={"embedder": self.embedder_name, "dim": self.dim})
        stored = (col.metadata or {}).get("embedder")
        if stored != self.embedder_name:
            raise RuntimeError(
                f"index collection {self.name!r} was built with embedder {stored!r}, not "
                f"{self.embedder_name!r}; vectors from two models cannot be mixed. "
                "Run with --full-refresh to rebuild it.")
        return col

    def _drop(self) -> None:
        """Delete the collection if it exists (--full-refresh)."""
        if self.name in [c.name for c in self.client.list_collections()]:
            self.client.delete_collection(self.name)

    def count(self) -> int:
        return self.collection.count()

    def file_ids(self, file: str) -> list[str]:
        return self.collection.get(where={"file": file}, include=[])["ids"]

    def files(self) -> set[str]:
        """Every document key that has chunks in the index."""
        metas = self.collection.get(include=["metadatas"])["metadatas"]
        return {m["file"] for m in metas}

    def delete_file(self, file: str) -> int:
        ids = self.file_ids(file)
        if ids:
            self.collection.delete(ids=ids)
        return len(ids)

    def replace_file(self, file: str, ids, embeddings, documents, metadatas) -> None:
        """Delete every chunk of this file, then add the new ones (no stale chunks left)."""
        self.delete_file(file)
        for i in range(0, len(ids), ADD_BATCH):
            s = slice(i, i + ADD_BATCH)
            self.collection.add(ids=ids[s], embeddings=embeddings[s], documents=documents[s],
                                metadatas=metadatas[s])
