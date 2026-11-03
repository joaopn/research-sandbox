# Security

Threat model, isolation layers, and what is and isn't enforced.

### Table of Contents

[◾ Threat model](#-threat-model)
[◾ What is enforced](#-what-is-enforced)
[◾ What is not enforced](#-what-is-not-enforced)
[◾ Isolation layers](#-isolation-layers)
[◾ Host hardening (operator-applied)](#-host-hardening-operator-applied)
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
- **A GitHub SSH key you give a box is readable by the agent in that box.** The optional key (`RS_GITHUB_SSH_KEY_FILE`, or the create form's Authentication box) is installed at `~/.ssh/github` inside the container. Boxes run their agent under `bypassPermissions`, and that agent reads repo content which may be adversarial — so an account-wide key is a prompt-injection-to-force-push path. RS **cannot** scope the key: unlike a PAT, which GitHub lets you make fine-grained and per-repo, an SSH key carries whatever access its GitHub account has. **Use a per-repo deploy key**, read-only unless the box genuinely needs to push. The create form requires an explicit acknowledgment tick before accepting a key; that is a prompt, not an enforcement. Rotate/revoke on GitHub if a box is compromised — the same "rotate the key, destroy the project" posture as the OAuth token above.
- **The key and the git identity transit host disk during a container swap.** A sysbox `project start` and a docker-box editor toggle both destroy the container, so `~/.ssh/` and `~/.gitconfig` are moved into the project workspace (`.ssh-stash`, `.gitconfig-stash`) and moved back at next boot — the same mechanism the Claude credentials already use. The window is the swap itself, not the project's running lifetime. `~/.git-credentials` is deliberately *not* carried, so a dev supervisor's gitea token is never parked on host disk.
- **External-MCP tokens you put in the registry are visible to the supervisor.** `${VAR}` interpolation resolves on registry load; the resolved value is in the supervisor's view. Treat any token in `~/.research-sandbox/mcp-registry.json` as exposed at the supervisor's trust level.
- **The host port your `external` MCP listens on must be chosen carefully.** rs-router rules are dest-IP-and-port-keyed. If another service runs on the same port the registry pointed at, the project would reach it. Admin discipline, not code-enforced.
- **Workers' direct internet egress is allowed by default.** `--egress open` lets workers reach anywhere except RFC1918. If you want strict "MCP-mediated only" egress, use `--egress locked` (HTTP/HTTPS, and DNS to the configured resolvers only) and/or `--inner-firewall`. Even then, "locked" allows arbitrary HTTPS endpoints — true offline-only would be a separate egress mode.

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
  - `--egress locked`: allow only TCP/80, TCP/443, and UDP/TCP 53 to the resolvers configured in `SANDBOX_DNS`; no ICMP (errors still arrive as related traffic; echo does not).
  - Per-MCP allow rules added by `project mcp-allow`: ACCEPT specifically for `(project_subnet, mcp_ip, mcp_port)`.
- Rules are persisted under `/etc/sandbox/rules/` in the router so they survive a router restart.

### 4. mcp-proxy (in-supervisor)

- One per supervisor, listening on `mcp-proxy:8888` on the inner `rs-inner` bridge.
- Renders config from `/workspace/.orchestrator/mcp-proxy/config.json` (which is in turn rendered from the per-project allowlist).
- Routes `/<name>/<rest>` → `<upstream-ip>:<port>/<rest>`. Pinning `Host: mcp-proxy:8888` on every upstream request gives MCP servers a stable allowlist value.
- Rejects `..` segments (incl. URL-encoded) in the upstream path.
- Audit log at `/workspace/.orchestrator/logs/mcp-proxy.jsonl` — one JSON line per request, with `{ts, src, mcp, method, path, status, bytes_in, bytes_out}`. A `status` of 499 means the client disconnected before its response was delivered: the proxy shut the upstream connection down, and the record also carries `error` and, when the upstream had answered, `upstream_status`.

### 5. Worker config (`.mcp.json`)

- Generated by `rs-worker spawn` from the per-project allowlist + the worker's `--mcps` arg.
- Workers don't know about MCPs not in this file.
- A worker that hardcodes a different URL still hits the proxy (which 404s on unknown names) and the router (which drops to non-allowed destinations).

### 6. Inner-bridge firewall (opt-in)

`--inner-firewall` adds an iptables ACL on the supervisor's `rs-inner` bridge. Bridge-boundary, not intra-bridge — which means worker-to-mcp-proxy on the same bridge is L2-switched and unfiltered (the path we want allowed), while worker-to-anywhere-off-bridge is L3-routed and filtered. Default-OFF until the opt-in path has been dogfooded; the firewall is independent of the rs-router policy and complements it.

### 7. Inner-bridge guard (always on)

Every box, worker and role service is a container of the project's *inner* Docker daemon and leaves the project by NAT through the project container, so without a guard it reaches everything one hop from the project: the project container itself (its sshd, its editor), the host at the project bridge's gateway (its sshd), the box registry, Gitea, the webui — none of which the egress router sees. The guard runs in every dind project at boot and drops new connections from the inner bridges toward private addresses and toward the project container, keeping exactly what the lanes use: the internet, Gitea's HTTP port at the staged address (dev, fetch and history boxes clone and push through it), and the MCP proxy's registered upstreams. Details under "Inner-bridge guard" below.

### 8. Box images: a read-only registry, pulls pinned by digest

The box images every dind project runs come from a local registry. Two instances share one storage directory: `rs-registry`, the one every project's inner Docker daemon pulls from, is read-only (it answers 405 to any upload, manifest write or delete) and sits on the project networks; `rs-registry-push`, the only writable one, listens on the operator's loopback port and is where `research` pushes. A project — or anything inside it — can therefore never replace a box image the fleet pulls. On top of that, every delivery records the digest the registry served for the pushed tag in the project's marker and pulls by that digest afterwards, so a tag cannot be redirected and a tampered blob fails the pull. A project created before digests were recorded is re-pushed from the operator's machine at its first restart when the machine still carries its pinned image; otherwise it adopts the tag once and is pinned from then on.

### 9. The webui listens on the router network only

The webui joins every project network so it can open terminals to supervisors, but it listens only on its own address inside the router network (`rs-sandbox`), where Docker delivers the operator's published ports; the login page, the origin ports and the pass-through ports refuse a connection that arrives from a project's own bridge. Two other paths can still reach those listeners: a webui bound to an address the router does not drop — the shared-address range 100.64.0.0/10 that Tailscale uses is not in the router's drop list — is reachable from an open-egress project through the router; and a webui published on every host address (`0.0.0.0`) is reachable at each project's bridge gateway through Docker's port forwarding, which host rule 1 below closes. Bind the webui to loopback or to one specific address, and apply the host rules if it must listen on a tailnet or LAN address.

## ◾ Host hardening (operator-applied)

RS installs nothing on the host outside its own containers: it never writes a host firewall rule, never changes a host service, and never checks whether you did. Two paths out of a project can only be closed on the host, so this section lists them as suggestions you apply yourself, with your own privileges.

**What is open without them.** A project container's root (a user-namespaced root under sysbox, a real root under privileged dind) holds `CAP_NET_ADMIN` over the container's own network stack — the inner Docker daemon needs it — so it can replace the default route RS injected — pointing it at the bridge's gateway instead of `rs-router` — and the host forwards and masquerades that traffic like any Docker network: the egress policy is bypassed. The same gateway address is the host itself, so any host service listening on all interfaces (sshd by default) answers to a project directly, without passing the router. Both hold for the sysbox and privileged dind substrates; a plain `docker`-substrate project (the `sandbox` workflow) has Docker's default capability set, which lacks `CAP_NET_ADMIN`, and cannot re-route.

**Every bridge RS creates for a project or the reviewer has a recognisable host interface name.** Those bridges (`rs-net-<project>` and `rs-reviewer`) get a host interface named `rsn` followed by twelve hex characters, instead of Docker's `br-<id>`, so two generic rules cover every project, present and future, and are installed once. The shared `rs-sandbox` bridge, through which the shared containers reach the internet, deliberately keeps Docker's `br-*` name so rule 1 never matches it. Projects created before this naming keep their `br-*` interface and are covered only after a destroy + recreate; an `rs-reviewer` network from an earlier version likewise — while no review runs, detach the router and remove it by hand (`docker network disconnect rs-reviewer rs-router 2>/dev/null; docker network rm rs-reviewer` — the router may still be attached from the last review, and a network removed under a stopped router would brick the router's next start), and the next review recreates it with the prefix.

**Order matters.** Run `research start` on this version of RS first: it pins every shared container's default route (router, webui, registry, Gitea) to `rs-sandbox`, which Docker otherwise places on a project bridge. Installed before that, the first rule below would drop the router's own outbound traffic.

**Rule 1 — containment.** New connections entering from an RS bridge may not leave by any other interface. This closes the re-route (the project's traffic can only go to neighbours on its own bridge, where `rs-router` is) and leaves intact: the router path (on-bridge), published ports (connections enter from outside; replies are established traffic), same-project neighbours, and external MCPs (the router reaches `host.docker.internal` from `rs-sandbox`, not from a project bridge).

```bash
sudo iptables -I DOCKER-USER 1 -i rsn+ ! -o rsn+ -m conntrack --ctstate NEW -j DROP
```

**Rule 2 — the host itself.** New connections from an RS bridge to the host are dropped. This closes the host's sshd (and anything else bound to `0.0.0.0`) from every project and from every box inside one.

```bash
sudo iptables -I INPUT 1 -i rsn+ -m conntrack --ctstate NEW -j DROP
```

Both rules are idempotent to check (`iptables -C …` with the same arguments) and to remove (`iptables -D …`). Persist them with your distribution's mechanism (`iptables-persistent`, a `DOCKER-USER` snippet in your firewall tool); Docker recreates its own chains at daemon start but leaves `DOCKER-USER` and `INPUT` to you. On Docker's nftables backend there is no `DOCKER-USER` chain: the same two expressions go into a chain of your own on the `forward` and `input` hooks that runs before Docker's — `iifname "rsn*" oifname != "rsn*" ct state new drop` and `iifname "rsn*" ct state new drop`.

**sshd.** Independently of rule 2, bind the host's sshd to the address you actually use (`ListenAddress <LAN address>` in `sshd_config`) rather than every interface; a service that does not listen on the bridges has nothing to be reached at.

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
| `locked` | TCP/80, TCP/443, and UDP/TCP 53 to the configured resolvers (`SANDBOX_DNS`) only; no ICMP |

Plus per-MCP ACCEPT rules added by `project mcp-allow`, which take precedence over the egress policy for the specific destination IP+port.

The egress mode is set at `rs-router` and applies to *all* traffic from the project's subnet, including the supervisor's own outbound (e.g. for Claude Code's own API calls — those need 443 in either mode) and worker traffic.

## ◾ Inner-bridge guard (always on)

`rs-inner-guard` installs two chains in the project container's own iptables at every boot, re-applies them after every MCP allowlist change (through `mcp-reload`) and after the host stages the project's Gitea wiring. Both are rebuilt in one transaction, so a live jump never points at an empty chain.

```
RS-INNER-GUARD (jumped to from DOCKER-USER, i.e. FORWARD):
  RETURN  -i <uplink>                 (traffic entering from the project network: the webui's probes into boxes, the published SSH port)
  RETURN  RELATED,ESTABLISHED
  DROP    ctstate DNAT                (a box reaching a published port through the project's address, whichever box it lands on)
  RETURN  ! -o <uplink>               (inner-to-inner: Docker's own isolation decides)
  RETURN  -d <gitea address> -p tcp --dport 3000                                 (when the project's Gitea wiring is staged)
  RETURN  -i <rs-inner bridge> -s 192.168.99.2 -d <ip> -p tcp --dport <port>   (one per upstream in the project's MCP allowlist)
  DROP    -d 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16

RS-INNER-GUARD-IN (jumped to from INPUT):
  RETURN  -i lo                       (the project's own loopback traffic)
  RETURN  -i <uplink>
  RETURN  RELATED,ESTABLISHED
  DROP
```

The uplink is the interface of the project's default route. The Gitea address and the upstream pairs are read from the project's staged wiring and validated as one IPv4 address and one port each; a value that fails validation is announced and skipped, the drops stand regardless. What the guard closes, measured: a box's connection to its project's sshd and editor, to the host's sshd at the bridge gateway, to the registry, and to another box's published port. What it keeps: the internet (a public destination's next hop is the private router, but the rule keys on the destination), Gitea over HTTP, the proxy's upstreams, and everything the project initiates toward its boxes. Residuals: boxes on one bridge still reach each other directly (a dev box has its own bridge for that reason); a box's `ping` of its gateway fails; the project's own root can remove the rules — that side is the operator's host hardening above.

## ◾ Inner-bridge firewall

`--inner-firewall` (opt-in) installs a chain in the supervisor's local iptables:

```
RS-INNER-FW chain (jumped to from FORWARD for source=192.168.99.0/24):
  RETURN  RELATED,ESTABLISHED    (return traffic)
  RETURN  -s 192.168.99.2        (mcp-proxy's pinned IP — its egress to upstreams)
  RETURN  role-MCP and box source ranges
  LOG     (rate-limited)
  DROP
```

What the chain lets through RETURNs rather than ACCEPTs, so the always-on inner guard (above) and Docker's own inter-bridge isolation still apply to it.

Worker → mcp-proxy on rs-inner is L2-bridged and bypasses FORWARD entirely (so always allowed). Worker → anywhere off-bridge is L3-routed through the supervisor's netns and hits the chain — DROPped unless from the proxy's IP. This lets the proxy still make its upstream calls while preventing workers from making direct off-bridge connections within the supervisor.

The firewall is *defense-in-depth* on top of layers 1–5 above, not a replacement for them. It catches the case where a compromised worker tries to bypass the proxy by hardcoding an upstream URL.
