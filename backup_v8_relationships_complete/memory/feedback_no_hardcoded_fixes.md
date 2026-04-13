---
name: No hardcoded regex fixes
description: Never use hardcoded keyword replacements or regex post-processing as a fix - must be AI-driven pipeline
type: feedback
---

Never apply hardcoded regex post-processing (like replacing 'and' with '&&') as a "fix" for DAX conversion.
This is the exact anti-pattern we're moving away from — it won't scale to 5000+ workbooks.

**Why:** Each workbook has unique patterns. Hardcoded fixes work for one workbook but break others.
The whole point of the agentic approach is to let Claude handle ALL conversions intelligently.

**How to apply:** If Claude's output has errors, the fix must go through the AI pipeline:
retry with error context, not string replacement. If Claude can't fix it after retries,
log it as a migration warning — don't silently produce broken DAX.
