# Task RAG Store

This directory is reserved for task retrieval augmentation.

## Layout (containers)
- `containers/host-z/` — host-z task memory
- `containers/local/` — local task memory
- `containers/aliyun/` — Aliyun task memory

Each container can hold:
- `manifest.jsonl`
- `embeddings/` (faiss.index + sqlite + meta)
- `evidence/`
- `retrieval_logs/`

## Notes
- Embeddings use `gemini-embedding-001`
- Do not store secrets here
- host-z/Aliyun should call local service; avoid local RAG-Anything installs
