---
name: architect
description: PRD/SPEC architecture skill for ARC Official STEP loop. FORCE-LOAD on prd and spec STEPs before writing PRD or SPEC. Use GSC MCP prd/spec_read/spec_write/state_* tools; prefer HTML SPEC under SPEC/arcbench; never invent Markdown SPEC that forces migrate.
---

# architect (ARC Official STEP)

Use this skill at the start of **prd** and **spec** STEPs.

## Required actions
1. Call GSC MCP `prd` / `spec_read` / `spec_write` / `state_read` / `state_update` as needed.
2. Persist PRD under `PRD/` and/or `.arc/steps/<module-id>/prd*`.
3. Persist HTML SPEC under `SPEC/arcbench/` (HTML 2.0, not Markdown).
4. Keep outputs small; do not dump lockfiles or huge schemas.

## Exit
Write a short receipt JSON listing `skills_loaded` and `artifacts` paths for the harness gate.
