"""RAG layer: answer questions from official UDISE+ documents and the gold tables.

Phase 1: document ingestion (chunks of text and tables, embedded locally, ChromaDB index).
Phase 2: retrieval (BM25, vector, hybrid, reranked). Phase 3: a safe read-only SQL tool.
Phase 4: the LangGraph agent (`python -m rag.ask "..."`), the only part that calls an LLM.
See rag/README.md.
"""
