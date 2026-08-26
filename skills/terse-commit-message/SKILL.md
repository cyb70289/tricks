---
name: terse-commit-message
description: Use when creating a Git commit or updating an existing commit message.
---

The default and strongly preferred output is exactly one subject line.
Keep the subject line within 50 characters, and no more than 72.
All characters in the subject line are lowercase.

If the project has clear module boundaries and the change is scoped to a specific module, prefix the subject line with "module: ". Use only the first level of hierarchy, nothing deeper.
E.g., ui: implement open state viewer

Add a body only when the subject alone would be materially misleading.
Wrap body text at no more than 72 characters per line.
Use normal sentence casing for the body.
