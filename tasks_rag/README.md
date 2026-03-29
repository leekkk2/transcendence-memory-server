# Task RAG Store

This directory is reserved for task retrieval augmentation.

## Current Repo Note

The runtime scripts in this repo still read and write from `tasks/rag/...` under `WORKSPACE`.
`tasks_rag/` currently serves as a reference/documentation directory inside the repo, not the live runtime root.

## Layout (containers)
- `containers/imac/` — iMac task memory
- `containers/eva/` — Eva task memory
- `containers/aliyun/` — Aliyun task memory

Each container can hold:
- `manifest.jsonl`
- `embeddings/` (faiss.index + sqlite + meta)
- `evidence/`
- `retrieval_logs/`

## Notes
- Embeddings use `gemini-embedding-001`
- Do not store secrets here
- iMac/Aliyun should call Eva service; avoid local RAG-Anything installs
