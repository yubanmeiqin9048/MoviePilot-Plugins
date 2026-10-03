# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- `CONTEXT.md` at the repo root
- `docs/adr/` for ADRs that touch the area being explored

If any of these files don't exist, proceed silently. The `/domain-modeling` skill creates them lazily when terms or decisions are resolved.

## File structure

This is a single-context repo:

- `CONTEXT.md` contains the domain vocabulary.
- `docs/adr/` contains system-wide decisions.

## Use the glossary's vocabulary

When output names a domain concept, use the term as defined in `CONTEXT.md`. If a needed concept is missing, note it for `/domain-modeling`.

## Flag ADR conflicts

If output contradicts an existing ADR, surface it explicitly rather than silently overriding it.
