"""RAG layer: answer questions from official UDISE+ documents and the gold tables.

Phase 1 (this package so far): document ingestion only. Documents listed in
docs_corpus/manifest.csv are split into chunks (text and tables), embedded with a local
model and stored in a persistent ChromaDB index. No LLM calls. See rag/README.md.
"""
