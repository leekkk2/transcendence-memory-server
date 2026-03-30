# Server Boundary

## Position

`transcendence-memory-server` is the private server-side repository in the broader Transcendence Memory system.

## This repo owns

- authenticated HTTP endpoints
- LanceDB-only ingest and retrieval behavior
- typed object persistence and structured ingest
- runtime scripts and server wrappers
- server-facing storage/index behavior
- private deployment-facing documentation and configuration

## This repo does not own

- workspace-level planning, handoff, and long-running orchestration
- agent/client enhancer prompts, hooks, and packaging
- the future public abstraction layer in `transcendence-memory`
- broad cross-repo product governance

## Practical separation

### Belongs here

- server runtime code
- API behavior
- storage, index, and search implementation
- deployment configuration
- server troubleshooting and bootstrap notes

### Belongs elsewhere

- cross-repo planning and program state → `transcendence-memory-workspace`
- agent-side enhancer behavior → `skills-hub`
- sanitized reusable abstractions → `transcendence-memory`

## Current boundary constraints

- keep the historical `task_rag_*` naming family for runtime entrypoints that remain in use
- keep one canonical backend design: `LanceDB-only`
- treat typed client objects as first-class server-side persisted sources
- treat structured ingest as a server-side capability, not as a client enhancer concern
- treat this repo as the private server home, not as the entire memory platform
