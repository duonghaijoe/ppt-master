# CLAUDE.md

This file is the project entry point for Claude Code.

Before any PPT generation task, **you MUST first read [`skills/ppt-master/SKILL.md`](skills/ppt-master/SKILL.md)** — the authoritative workflow for project creation, role switching, serial execution, quality gates, post-processing, and export.

## Project Overview

PPT Master is an AI-driven presentation generation system. Multi-role collaboration (Strategist → Image_Generator → Executor) converts source documents (PDF/DOCX/URL/Markdown) into natively editable PPTX with real PowerPoint shapes (DrawingML).

**Core Pipeline**: `Source Document → Create Project → [Template] → Strategist Eight Confirmations → [Image_Generator] → Executor → Quality Check → Post-processing → Export PPTX`

> Topic-only requests with no source material: run the standalone [`topic-research`](skills/ppt-master/workflows/topic-research.md) workflow before SKILL.md Step 1 to gather web materials.
>
> Phase B resumption (split-mode execution): when the user opens a fresh chat and says "继续生成 projects/<x>" or similar, run the standalone [`resume-execute`](skills/ppt-master/workflows/resume-execute.md) workflow to enter Phase B (SVG generation + export) without re-running Phase A.
>
> Decks containing data charts: run the standalone [`verify-charts`](skills/ppt-master/workflows/verify-charts.md) workflow between the executor and post-processing steps to calibrate chart coordinates.
>
> Recorded narration / video export: run the standalone [`generate-audio`](skills/ppt-master/workflows/generate-audio.md) workflow after post-processing.
>
> Post-export iteration: whenever the user asks to change anything on a generated slide ("改一下", "调字号", "那里看着不对", "把图片换大点"), the [`visual-edit`](skills/ppt-master/workflows/visual-edit.md) workflow is available — surface it as an option. If the user describes the change with enough specificity to apply directly ("第 3 页副标题字号改 32"), edit the SVG directly instead; if they're vaguely pointing at "somewhere" on the deck, run the workflow.

## Execution Requirements

- Read [`skills/ppt-master/SKILL.md`](skills/ppt-master/SKILL.md) before starting a PPT task.
- For standalone template creation, read [`skills/ppt-master/workflows/create-template.md`](skills/ppt-master/workflows/create-template.md).
- Role-specific rules live in [`skills/ppt-master/references/`](skills/ppt-master/references/).
- Technical SVG/PPT constraints live in [`skills/ppt-master/references/shared-standards.md`](skills/ppt-master/references/shared-standards.md).
- Canvas choices live in [`skills/ppt-master/references/canvas-formats.md`](skills/ppt-master/references/canvas-formats.md).
- Icon library details live in [`skills/ppt-master/templates/icons/README.md`](skills/ppt-master/templates/icons/README.md).
- Before editing prompt files under `skills/ppt-master/references/` or Python under `skills/ppt-master/scripts/`, consult the matching style rule in [`docs/rules/`](docs/rules/).

## Compatibility Boundary

- This repository is a workflow/skill package, not an app or service scaffold.
- Do NOT assume conventions like `.worktrees/`, `tests/`, or mandatory branch setup unless the user explicitly requests them.
- On conflict with a generic coding skill, prioritize [`skills/ppt-master/SKILL.md`](skills/ppt-master/SKILL.md) and this file inside this repository.

## Command Quick Reference

Convenience summary only — full workflow in [`skills/ppt-master/SKILL.md`](skills/ppt-master/SKILL.md).

```bash
# Source content conversion
python3 skills/ppt-master/scripts/source_to_md/pdf_to_md.py <PDF_file>
python3 skills/ppt-master/scripts/source_to_md/doc_to_md.py <DOCX_or_other_file>
python3 skills/ppt-master/scripts/source_to_md/ppt_to_md.py <PPTX_file>
python3 skills/ppt-master/scripts/source_to_md/web_to_md.py <URL>

# Project management
python3 skills/ppt-master/scripts/project_manager.py init <project_name> --format ppt169
python3 skills/ppt-master/scripts/project_manager.py import-sources <project_path> <source_files_or_URLs...> --move
python3 skills/ppt-master/scripts/project_manager.py validate <project_path>

# Image tools and SVG quality check
python3 skills/ppt-master/scripts/analyze_images.py <project_path>/images
python3 skills/ppt-master/scripts/image_gen.py "prompt" --aspect_ratio 16:9 --image_size 1K -o <project_path>/images
python3 skills/ppt-master/scripts/svg_quality_checker.py <project_path>

# Post-processing pipeline: run sequentially, one command at a time
python3 skills/ppt-master/scripts/total_md_split.py <project_path>
python3 skills/ppt-master/scripts/finalize_svg.py <project_path>
python3 skills/ppt-master/scripts/svg_to_pptx.py <project_path>
```

## Per-Project Skill File

Every project under `projects/` accumulates its own theme, brand palette, header
pattern, repeatable scripts, and gotchas. Capture that context in a project-local
`SKILL.md` so future sessions can pick it up cold.

### Rules

1. **Create on completion.** After the first successful PPTX export for a project,
   write `projects/<group>/<project>/SKILL.md` before declaring the work done.
   The file is the project's reusable context memory.
2. **Refresh on goal completion.** Whenever you complete a goal that changes brand,
   layout, content rules, scripts, or naming conventions for an existing project,
   update its `SKILL.md` (and append a row to its **Update Log**) before moving on
   or considering the turn complete. If the user changes direction mid-task, refresh
   the SKILL.md as part of the same turn so it never lags reality.
3. **Project-local only.** Project SKILLs always live at the project root, never
   under `skills/` or `~/.claude/skills/`. The repo-level skills under
   `skills/ppt-master/` stay generic; project SKILLs hold the specifics.
4. **Read before editing.** Before regenerating, re-skinning, or editing slides
   for an existing project, read its `SKILL.md` first — it overrides anything
   inferred from the source files when there is a conflict.

### Required sections

Every project `SKILL.md` should cover:

- **When to Use** — phrases or intents that should route here
- **Project Coordinates** — root path, canvas, slide count, source/twin decks, final export path
- **Brand Palette (Locked)** — every hex with its role and where it appears
- **Typography (Locked)** — font stacks
- **Page Chrome / Header Pattern** — y-coordinates of stripe, logo, eyebrow, title, footer
- **Content Source** — where copy lives (source docs, twin v1, total.md), what to edit when
- **Repeatable Tasks** — exact shell commands for re-skin, re-export, header refresh, notes split
- **Hard Rules / Gotchas** — venv vs system Python, drift-allowed colors, do-not-touch decks, header collision zones
- **Update Log** — table of `date | change`

Reference example: [`projects/katalon/new_ways_of_working_internal_ppt169_20260509/SKILL.md`](projects/katalon/new_ways_of_working_internal_ppt169_20260509/SKILL.md).

## Architecture

- `skills/ppt-master/SKILL.md` — main workflow authority.
- `skills/ppt-master/references/` — role definitions and technical specifications.
- `skills/ppt-master/scripts/` — runnable tool scripts.
- `skills/ppt-master/scripts/docs/` — topic-focused script docs.
- `skills/ppt-master/templates/` — layout templates, chart templates, icon library.
- `examples/` — example projects.
- `projects/` — user project workspace. Each project carries its own `SKILL.md`
  with brand, header pattern, and regen commands (see § Per-Project Skill File).
