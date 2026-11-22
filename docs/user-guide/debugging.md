---
title: Debugging
parent: User guide
nav_order: 15
---
# Debugging

### Worker mid-run

Inside the supervisor:

```bash
rs-worker tail <name> -f       # stream-json log live
rs-worker attach <name>        # byobu exec into the worker for poking around
rs-worker status <name>        # JSON: container + registry + log tail
```

The host can also browse `container_volumes/<proj>/workspace/workers/<name>/work/` directly — `outputs/`, `research_log.md`, `log.jsonl` are all plain files.

### Worker crashed

Container is `exited`, no `/workspace/DONE` sentinel. Look at the tail of `log.jsonl` — Claude Code's stream-json includes the assistant message that came right before the failure, and any error events. Re-spawn under a new name (`*_v2`) with an amended plan; the original name is now reserved.

### MCP not reachable

Three places to look, in order:

```bash
# (host) is the per-project router rule installed?
docker exec rs-router iptables -L FORWARD -n | grep <project-subnet>

# (supervisor) is the proxy config rendered correctly?
docker exec rs-project-<proj> cat /workspace/.orchestrator/mcp-proxy/config.json

# (supervisor) audit log — what does the proxy see?
docker exec rs-project-<proj> tail /workspace/.orchestrator/logs/mcp-proxy.jsonl
```

If the proxy isn't logging the request at all, the worker isn't reaching it (check `.mcp.json` URL, check inner firewall if enabled). If the proxy is logging requests with a non-2xx status, the upstream MCP is rejecting — most often DNS-rebinding-protection (allowlist `mcp-proxy:8888`) or wrong upstream path (use `--path`).

### Supervisor itself

```bash
python research.py project attach <proj>     # interactive byobu
docker logs rs-project-<proj>                # entrypoint output
docker exec rs-project-<proj> sudo cat /tmp/dockerd.log    # inner daemon log
```
