# Task RAG Store

This directory is a documentation/reference directory inside the repo.
Live runtime data still lives under `WORKSPACE/tasks/rag/...`.

## Canonical runtime layout

Each runtime container under `tasks/rag/containers/<container>/` can hold:

- `lancedb/` — canonical retrieval store
- `memory_objects.jsonl` — canonical typed client-object store
- `evidence/`
- `retrieval_logs/`

## Notes

- Mainline retrieval is `LanceDB-only`
- Do not store secrets here
- iMac/Aliyun should call Eva service; avoid local duplicate deployments
