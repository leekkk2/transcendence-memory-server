# Legacy native deployment scripts

These scripts target the **pre-Docker** native systemd path
(`rag-everything.service` directly invoking `uvicorn` from a venv on the host).
They are **not** used by any current code path. Kept here for archaeological
reference and for users who deliberately want to run the server outside Docker.

The supported deployment is `docker compose` managed by
`deploy/systemd/rag-everything.service`. See `docs/deployment/`.

| File | Original purpose |
|------|------------------|
| `run_task_rag_server.sh` | Activated `.venv-task-rag-server`, sourced `.env`, ran uvicorn. |
| `bootstrap_dev.sh`       | Created the venv, pip-installed deps, ran tests. |

If you maintain a non-Docker install: these scripts are reference material,
not a supported product. The Docker path is what gets CI coverage.
