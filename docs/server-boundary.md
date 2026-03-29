# Server Boundary

## Position

`transcendence-memory-server` is the private server-side repository in the broader Transcendence Memory system.
The local directory still remains `rag-everything/`, but the repo should now be understood by its server role rather than the old project name.

## This repo owns

- authenticated HTTP endpoints
- manifest build, memory ingest, embedding, and retrieval execution
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

- keep the historical `task_rag_*` script names for now
- keep the current local directory layout unchanged in this bootstrap pass
- use `transcendence-memory-server` in documentation to describe repo identity
- treat this repo as the private server home, not as the entire memory platform
