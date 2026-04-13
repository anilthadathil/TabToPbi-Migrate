---
name: Claude model benchmarks for DAX conversion
description: Speed and quality comparison of Haiku vs Sonnet vs Opus for Tableau-to-DAX formula conversion, tested on complex LOD formulas (April 2026)
type: reference
---

## Benchmark: Complex LOD formulas (Just Orders table, 51 formulas)

Tested with `claude --model <model> --print --output-format text`

| Count | Haiku (4.5) | Sonnet (4.6) | Default/Opus |
|-------|-------------|--------------|--------------|
| 1 | - | - | 43.0s |
| 2 | - | - | 50.1s |
| 5 | 24.3s (4.9s/f) | 27.9s (5.6s/f) | 67.2s (13.4s/f) |
| 10 | 27.8s (2.8s/f) | 67.1s (6.7s/f) | TIMEOUT 562s |
| 15 | 22.8s (1.5s/f) | 179.6s (12.0s/f) | TIMEOUT |
| 20 | 25.3s (1.3s/f) | 92.9s (4.6s/f) | - |
| 30 | 37.0s (1.2s/f) | TIMEOUT 300s | - |

## CLI overhead (per-call fixed cost)
- `claude --version`: 0.7s
- Simple "say HELLO" prompt: 7.5s
- 1 simple formula: 6.5s
- 5 simple formulas: 8.0s
- Overhead is ~7s per subprocess call regardless of payload

## Quality comparison (15 complex LOD formulas)
- Haiku: 15/15 parsed, 15/15 passed Layer 1 validation
- Sonnet: 15/15 parsed, 15/15 passed Layer 1 validation
- Both produce correct CALCULATE/MAXX/SUMMARIZE/VALUES patterns for LOD

## Decision
- **Haiku for batch conversion**: 8-10x faster, same quality for structured conversion tasks
- **Sonnet/Opus for Layer 2 corrections**: deeper reasoning about model schema needed
- **Chunk size 30**: optimal for Haiku (37s for 30 formulas, 1.2s/formula)
- **Dynamic timeout**: based on formula complexity (LOD count, nesting depth)
