# JARVIS Project Status

**Last updated:** Initial scaffold — 17 October 2026  
**Current stage:** Phase 00 — feasibility and architecture decision  
**Current branch after bootstrap:** `phase/00-feasibility`  
**Status:** Scaffold only; no autonomous agent capability is implemented yet.

## Verified in scaffold
- FastAPI health endpoint exists at `/health`.
- Automated health endpoint test exists.
- Setup, check and run scripts are provided.

## Not implemented yet
- Real model providers or routing.
- Goal planning and durable task state.
- Tools beyond the health endpoint.
- Persistent memory.
- Windows automation, Android, communication integrations, voice or vision.

## Next task
Use `docs/prompts/00-feasibility.md` to complete bounded research and record an ADR. Do not start Phase 01 until the architecture decision is reviewed.

## Current checks
Run `.\scripts\setup-dev.ps1` then `.\scripts\check.ps1` from PowerShell at the repo root. Record actual output after running locally.
