# Verified deployments to an explicitly configured host

Automatic deployment is off by default. Set repository variable
`TM_AUTODEPLOY_ENABLED=true` only after reviewing the intended target.
A successful same-repository main CI run or manual dispatch must pass the
current-main and successful-CI gates before publishing and deploying.

Keep deployment details in GitHub configuration, never in public source:

- Secrets: `DEPLOY_HOST`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`,
  `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`.
- Variables: `DEPLOY_CONTAINER`, `DEPLOY_PROJECT`, `DEPLOY_PATH`,
  `DEPLOY_PUBLIC_URL`, `DEPLOY_USER` (default ubuntu), `DEPLOY_PORT` (default 22).
- `TM_DEPLOY_PLATFORM` (default linux/amd64) and `TM_DEPLOY_RUNNER`
  (default ubuntu-latest) must match the target architecture.

Pin the real host key in `DEPLOY_KNOWN_HOSTS`; verify it through a trusted
channel before enabling the workflow. The configuration helper sets connection
credentials but deliberately does not enable deployments.

The approved base in `runtime-base.json` is reused only when its dependency
fingerprint and platform match. Otherwise CI builds the full image.
Production receives a prebuilt immutable image and never compiles it.

`deploy-image.py` validates an isolated container, waits for active indexing,
backs up configuration, then replaces only the requested service. Failure
restores the previous image and configuration. Data migrations require a
separate verified snapshot. Do not use this Docker workflow for native systemd
deployments. Operational inventories, receipts and private runbooks belong in
a private repository.

References: [workflow_run](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run),
[OCI whiteouts](https://github.com/opencontainers/image-spec/blob/main/layer.md#whiteouts).
