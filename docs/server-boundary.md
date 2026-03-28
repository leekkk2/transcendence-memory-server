# Server Boundary

## Purpose

Clarify what this repository owns as the private server layer of transcendence-memory.

## This repo owns
- retrieval service endpoints
- indexing / embedding / ingest execution
- server-side storage behavior
- runtime scripts and service wrappers
- server deployment-facing implementation details

## This repo does not own
- the whole workspace orchestration story
- all agent/client integration behavior
- the final public abstraction layer
- the skill packaging and distribution model

## Practical separation

### Belongs here
- server runtime code
- API behavior
- storage/index/search implementation
- server deployment configuration
- server troubleshooting docs

### Belongs elsewhere
- long-running cross-repo planning → `transcendence-memory-workspace`
- enhancer instructions for agents → `skills-hub`
- sanitized public abstractions → `transcendence-memory`

## Current implementation focus

The current codebase is still centered on the earlier task-RAG implementation. The immediate goal is not a destructive rewrite, but to evolve this codebase into a clearer private server home for the broader memory system.
