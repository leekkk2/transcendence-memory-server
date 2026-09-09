# Verified main deployments to AWS EVA

`Deploy AWS EVA` runs after successful `CI/CD` on a same-repository push to main.
It checks that the exact commit is still current main before publishing and
again before deployment. A stale, failed, fork or feature build cannot deploy.
Manual workflow dispatch requires the same successful-CI gate.

Repository configuration:
- `AWS_EVA_HOST` variable: current public SSH address; overrides legacy `DEPLOY_HOST` secret.
- `DEPLOY_USER` / `DEPLOY_PORT`: default ubuntu / 22.
- Existing `DEPLOY_SSH_KEY`, `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` secrets.
- `deploy/aws-eva.known_hosts` pins the host key under a stable HostKeyAlias.
- AWS Compose project: `tm-eva-restored-20260718`.
- Service directory: `/home/ubuntu/services/transcendence-memory-restore-20260718`.

The preflight verifies the registry credential and real SSH access. Missing
credentials fail clearly instead of producing a successful skipped deployment.
Maintenance-branch pushes only run this read-only preflight.

The native ARM runner reuses the approved digest in `runtime-base.json` when
its dependency fingerprint matches. App directories receive opaque OCI
whiteouts so removed code/assets cannot survive from an earlier layer. The
commit is recorded in both OCI labels and `/capabilities`.
When dependencies or Dockerfile change, the workflow builds `tm-full` natively
with registry build cache. Production never builds an image.

`deploy-image.py` pulls first, checks the new image with a temporary isolated
data volume, waits for active indexing jobs, then saves the current `.env` and
Compose override before recreating only rag-server. It preserves runtime
model/routing configuration and existing data volumes. Failure restores the
configuration and old image. It checks Docker health, exact source revision,
container object counts, public revision and real main retrieval. Receipts are
saved under the service directory's `.deployments/`. Backups are config-only;
data migrations require a separate snapshot/migration procedure.

No unrelated container/image cleanup runs. New service images update both
TM_IMAGE and the Compose override to avoid later accidental downgrades.

Manual deployment to another host uses the same script with its own project,
container, directory, public URL and a validated native image digest. Optional
`--expected-image` prevents overwriting another deployment. `--dry-run` only
shows the proposed target, without pulling/recreating services.

References: [GitHub workflow_run](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run),
[OCI whiteouts](https://github.com/opencontainers/image-spec/blob/main/layer.md#whiteouts).
