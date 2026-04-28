# Security

Threat model, isolation layers, and what is and isn't enforced.

### Table of Contents

[◾ Threat model](#-threat-model)
[◾ What is enforced](#-what-is-enforced)
[◾ What is not enforced](#-what-is-not-enforced)
[◾ Isolation layers](#-isolation-layers)
[◾ MCP server contract](#-mcp-server-contract)
[◾ Egress modes](#-egress-modes)
[◾ Inner-bridge firewall](#-inner-bridge-firewall)
[◾ Reporting issues](#-reporting-issues)

---

## ◾ Threat model

The PI runs an LLM (Claude Code) with `bypassPermissions` against potentially-untrusted research data, third-party MCP servers, and downloaded code (pip install, papers, datasets). Two things follow:

- **A worker is semi-trusted at best.** Prompt injection from the data the worker is analyzing, from MCP server responses, or from web pages it fetched can compromise the worker.
- **Supervisor compromise via worker is in scope.** A compromised worker can exfiltrate prompts to its own session, and through artifacts it produces (which the supervisor reads) it can attempt to inject the supervisor. We do *not* try to defend against this; the supervisor is treated as untrusted-from-worker's-perspective for some purposes.

What we *do* defend, in plain language:

- The host is sacred. Nothing the supervisor or workers do should be able to extract host secrets, modify host configuration, or affect other projects.
- A worker can only reach the network and files the PI explicitly granted.
- Tearing down a project removes everything it touched.

What we explicitly do *not* defend against:

- The PI executing malicious code the supervisor (or a compromised worker via the supervisor) tricked them into running. This is the same problem any tool that produces executable artifacts has.
- A determined adversary inside the worker chaining a kernel CVE through sysbox or runc. 
- Supply-chain attacks via the worker's image (pip packages, conda channels). The worker image is yours; pin and audit as you would any container.

## ◾ What is enforced

These are enforced:

| Property | Mechanism |
|---|---|
| Supervisor cannot read host filesystem outside its workspace | sysbox user-namespacing + bind-mount limits (no host paths bind-mounted in besides `/workspace/<proj>` and the registry RO) |
| Supervisor cannot reach the host's docker socket | None pass-through; inner daemon is its own dockerd |
| Supervisor cannot reach the host's RFC1918 LAN | rs-router default-DROPs RFC1918 destinations |
| Supervisor cannot reach another project's workspace | Per-project bind-mount, per-project network namespace |
| Worker cannot reach an MCP it wasn't granted | Four layers: `--mcps` check at spawn; `.mcp.json` lists only allowed; mcp-proxy config only allows; rs-router iptables only routes |
| Worker cannot reach the host filesystem | No host bind-mounts in the worker; only `/workspace/shared/` (RO) from the supervisor |
| Worker cannot reach another worker's workspace | Each worker's workdir is a separate bind-mount |
| Worker cannot egress beyond the configured policy | rs-router iptables on the per-project subnet |
| `project destroy` removes everything | Container, network, workspace dir, credentials snapshot, router rules |

## ◾ What is not enforced

Worth being explicit:

- **A compromised worker can compromise the supervisor.** Prompt injection through deliverables (`research_log.md`, `summary.md`, file names, plot titles, etc.) is possible. The supervisor reads these; an adversarial worker can attempt to mislead. The PI reviewing artifacts is the backstop, by design — *"short enough to skim, complete enough to verify from"*. Don't treat the supervisor's report as gospel for security-relevant assertions.
- **A compromised supervisor can exfiltrate its own OAuth token.** That token is per-project and per-PI; revoking via the Anthropic console invalidates it. Treat token exposure as a "rotate the key, destroy the project" event, not a host compromise.
- **External-MCP tokens you put in the registry are visible to the supervisor.** `${VAR}` interpolation resolves on registry load; the resolved value is in the supervisor's view. Treat any token in `~/.research-sandbox/mcp-registry.json` as exposed at the supervisor's trust level.
- **The host port your `external` MCP listens on must be chosen carefully.** rs-router rules are dest-IP-and-port-keyed. If another service runs on the same port the registry pointed at, the project would reach it. Admin discipline, not code-enforced.
- **Workers' direct internet egress is allowed by default.** `--egress open` lets workers reach anywhere except RFC1918. If you want strict "MCP-mediated only" egress, use `--egress locked` (HTTP/HTTPS/DNS/ICMP) and/or `--inner-firewall`. Even then, "locked" allows arbitrary HTTPS endpoints — true offline-only would be a separate egress mode.

## ◾ Isolation layers

Each project sits inside multiple boundaries that must all be crossed for a worker to reach an arbitrary destination on the host or another project. Top to bottom:

### 1. Host kernel

- [Sysbox](https://github.com/nestybox/sysbox) (preferred): runs the supervisor in a Linux user namespace, with no `--privileged` and no host-kernel-config exposure. UID 0 inside the container is not UID 0 on the host. The fallback is `--privileged` DIND, which is *less* isolated; use sysbox where you can.
- The supervisor's inner Docker daemon uses `crun` as its runtime (avoids sysbox's procfs incompatibility with newer runc versions). Workers under that daemon are normal runc containers within the supervisor's user namespace.

### 2. Per-project network

- Every project gets its own bridge: `rs-net-<project>` (unique /24).
- The supervisor's default route is replaced to point at `rs-router` — the only path out.
- Other projects' subnets are not reachable; docker's default isolation between user-defined bridges holds.

### 3. rs-router (egress filter)

- Alpine + iptables, sits on `rs-sandbox` (the shared "outside" network from the projects' perspective).
- iptables FORWARD rules keyed on the project subnet:
  - Default: drop RFC1918 destinations (host's LAN, other docker bridges).
  - `--egress open`: allow everything else.
  - `--egress locked`: allow only TCP/80, TCP/443, UDP/53, ICMP.
  - Per-MCP allow rules added by `project mcp-allow`: ACCEPT specifically for `(project_subnet, mcp_ip, mcp_port)`.
- Rules are persisted under `/etc/sandbox/rules/` in the router so they survive a router restart.

### 4. mcp-proxy (in-supervisor)

- One per supervisor, listening on `mcp-proxy:8888` on the inner `rs-inner` bridge.
- Renders config from `/workspace/.orchestrator/mcp-proxy/config.json` (which is in turn rendered from the per-project allowlist).
- Routes `/<name>/<rest>` → `<upstream-ip>:<port>/<rest>`. Pinning `Host: mcp-proxy:8888` on every upstream request gives MCP servers a stable allowlist value.
- Rejects `..` segments (incl. URL-encoded) in the upstream path.
- Audit log at `/workspace/.orchestrator/logs/mcp-proxy.jsonl` — one JSON line per request, with `{ts, src, mcp, method, path, status, bytes_in, bytes_out}`.

### 5. Worker config (`.mcp.json`)

- Generated by `rs-worker spawn` from the per-project allowlist + the worker's `--mcps` arg.
- Workers don't know about MCPs not in this file.
- A worker that hardcodes a different URL still hits the proxy (which 404s on unknown names) and the router (which drops to non-allowed destinations).

### 6. Inner-bridge firewall (opt-in)

`--inner-firewall` adds an iptables ACL on the supervisor's `rs-inner` bridge. Bridge-boundary, not intra-bridge — which means worker-to-mcp-proxy on the same bridge is L2-switched and unfiltered (the path we want allowed), while worker-to-anywhere-off-bridge is L3-routed and filtered. Default-OFF until the opt-in path has been dogfooded; the firewall is independent of the rs-router policy and complements it.

## ◾ MCP server contract

For an MCP server to integrate cleanly with the research-sandbox proxy:

1. **Speak streamable-HTTP** (JSON-RPC over POST). Stdio MCPs are deferred.
2. **Listen at `/mcp`** (the Python SDK default), or register with `--path <its-path>`.
3. **Allowlist `mcp-proxy:8888`** in the server's DNS-rebinding-protection settings. The proxy pins this `Host` value on all upstream requests; without the allowlist entry, the server rejects with `421 Misdirected Request`.

A minimal Python-SDK server that satisfies the contract:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-mcp")
mcp.settings.host = "0.0.0.0"
mcp.settings.port = 8000
mcp.settings.transport_security.allowed_hosts = ["mcp-proxy:8888"]

@mcp.tool()
def lookup(query: str) -> str: ...

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

## ◾ Egress modes

Set per-project at `project create`:

| Mode | What's allowed |
|---|---|
| `open` (default) | All TCP/UDP/ICMP outbound except RFC1918 destinations |
| `locked` | TCP/80, TCP/443, UDP/53, ICMP only |

Plus per-MCP ACCEPT rules added by `project mcp-allow`, which take precedence over the egress policy for the specific destination IP+port.

The egress mode is set at `rs-router` and applies to *all* traffic from the project's subnet, including the supervisor's own outbound (e.g. for Claude Code's own API calls — those need 443 in either mode) and worker traffic.

## ◾ Inner-bridge firewall

`--inner-firewall` (opt-in) installs a chain in the supervisor's local iptables:

```
RS-INNER-FW chain (jumped to from FORWARD for source=192.168.99.0/24):
  ACCEPT  RELATED,ESTABLISHED    (return traffic)
  ACCEPT  -s 192.168.99.2        (mcp-proxy's pinned IP — its egress to upstreams)
  LOG     (rate-limited)
  DROP
```

Worker → mcp-proxy on rs-inner is L2-bridged and bypasses FORWARD entirely (so always allowed). Worker → anywhere off-bridge is L3-routed through the supervisor's netns and hits the chain — DROPped unless from the proxy's IP. This lets the proxy still make its upstream calls while preventing workers from making direct off-bridge connections within the supervisor.

The firewall is *defense-in-depth* on top of layers 1–5 above, not a replacement for them. It catches the case where a compromised worker tries to bypass the proxy by hardcoding an upstream URL.
