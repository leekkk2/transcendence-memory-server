# Contract 2026-09-09 / server 0.22 / CLI 0.2

This release adds score aliases, capabilities and write receipts. Legacy `score`,
`vectorScore`, `rerankScore`, `accepted`, and `index_hint` keep their meaning.
LanceDB storage schemas and vectors are not migrated. L2 is now explicit and its
squared output is verified with a 3-4-5 vector test (`_distance=25`).

Use `/capabilities` for the contract/build revision and locally configured
capabilities. It does not claim that every upstream model is reachable. Lite
servers may correctly return search=true/query=false. Administrative profile
identity distinguishes request_model, optional resolved_model/model_revision,
and operator_declared versus unknown evidence. Aliases are never guessed.

The native Python CLI is the Windows engine; do not fork a separate HTTP client
into PowerShell. Transport policy uses auto/direct/proxy, respects configured
proxy intent and verifies TLS. Only replay-safe operations may try one alternate
route. Writes, /query and tools apply are not automatically replayed after 5xx
or unknown transport outcomes. Verbose logs exclude request bodies and secrets.

Queue coalescing uses one SQLite immediate transaction so concurrent requests
cannot create duplicate pending jobs. It still retains a follow-up when a job
is already running. No speculative 3–5 second delay was introduced.

Evaluation seed in `tests/fixtures/retrieval_eval` is an isolated synthetic
40-case regression set, not a claim about the production main corpus. Calibration
and holdout split by source family. Negative acceptance is undefined unless an
explicit distance decision threshold is supplied; no threshold is written to prod.

The two deployments (lite and full) require independent acceptance and model
configuration. Shared build identity does not imply shared data or profiles.
