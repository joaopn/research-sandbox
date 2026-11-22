---
title: Multiple projects
parent: User guide
nav_order: 17
---
# Multiple projects

Projects are fully independent. They share:
- Supervisor / worker / proxy *images* (built once per `research.py start`)
- The router (`rs-router`) and `rs-sandbox` network
- Shared MCPs you've registered

They don't share:
- Networks (each project has its own `rs-net-<proj>`)
- Workspaces (each in `<PROJECTS_DIR>/<proj>/`)
- Credentials (each does its own OAuth)
- MCP allowance (`project mcp-allow` is per-project)

You can run as many concurrent projects as your host can handle. SSH ports are auto-assigned.
