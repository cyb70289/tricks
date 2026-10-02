---
name: terse-docs
description: Use when writing or editing documentation, or when asked to refine a doc.
---

# Project Docs

Docs are concise, focused on key points, and serve two readers.

## Audience

- **Human (user):** can quickly grasp what the project is and how to use it.
- **Agent (developer):** can understand the architecture, find the relevant source modules, and know where to dive deeper.

## Include

- What it is and how to use it (minimal example first).
- Architecture overview and module map: what each module is responsible for and how they relate.
- Pointers for going deeper (key entry points, related docs).
- Caveats and implications that could trip up users or agents: gotchas, constraints, side effects, non-obvious behavior.

## Avoid

- Verbose explanations of individual features. State what it does and how to use it, nothing more.
- Change-log-style narration after a bug fix or new feature ("we fixed X by doing Y"). Update the doc to reflect current behavior only.
- Details prone to change: source line numbers, test counts, version-specific numbers, exhaustive file listings, benchmark results.

## Before finishing

- Is every sentence needed by the human or agent reader? Cut it if not.
- Does the doc describe the current state, not the history of getting there?
- Are caveats captured where they'd actually be seen?
- Is there any stale information to be removed?
