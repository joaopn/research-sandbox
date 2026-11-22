---
title: Image rebuild rules
parent: User guide
nav_order: 20
---
# Image rebuild rules

Edits to `research.py` take effect immediately (it runs on the host with your Python).

Edits to anything in `cli/`, `container/`, or `agent/` require an image rebuild. After rebuilding, *existing* projects don't pick up the change — destroy + recreate is the canonical path.

| Edited path | Rebuild | Reach existing projects |
|---|---|---|
| `cli/rs_worker.py` | supervisor | destroy + create |
| `container/supervisor/*` | supervisor | destroy + create |
| `container/analysis/*` | supervisor (stages worker template) | destroy + create |
| `agent/Dockerfile.supervisor` | supervisor | destroy + create |
| `agent/Dockerfile.analysis-base` | analysis-base | re-`project create` (worker image is staged into inner daemon at create time) |
| `agent/Dockerfile.mcp-proxy` | mcp-proxy | destroy + create |
| `agent/entrypoint.supervisor.sh` | supervisor | destroy + create |
| `agent/entrypoint.worker.sh` | analysis-base | re-`project create` |

Rebuild command is the same in all cases:

```bash
python research.py start --rebuild
```
