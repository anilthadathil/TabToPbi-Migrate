---
name: Testing workflow - one workbook at a time
description: Always test workbooks one at a time, wait for user confirmation before moving to next
type: feedback
originSessionId: 56e448e2-1e0e-4baf-bb00-060d412d1914
---
Test one workbook at a time. Wait for the user to confirm the result (check PBI Desktop, verify relationships, visuals, data) before running the next workbook.

**Why:** Running multiple workbooks back-to-back causes PBI Desktop session conflicts (compatibility level downgrades, port collisions). User needs to visually verify each result.

**How to apply:** After running migrate.py, stop and wait for user to say "ok" or report an error before proceeding to the next test.
