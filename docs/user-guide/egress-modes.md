---
title: Egress modes
parent: User guide
nav_order: 16
---
# Egress modes

Set at `project create` time with `--egress`, or in the create form. The `research` workflow defaults to the operator's configured default (`DEFAULT_EGRESS` in `.env`, `open` unless changed); every other shipped workflow defaults to `locked`.

| Mode | rs-router behavior |
|---|---|
| `open` | Private addresses blocked (the host's own LAN, other Docker networks); all other outbound allowed. Sensible for research workloads that need pip, apt, package downloads. |
| `locked` | Only TCP/80, TCP/443 and DNS (UDP/TCP 53) to the resolvers configured in `SANDBOX_DNS`; no ICMP; everything else dropped. Use when the project shouldn't egress to anything but HTTPS web traffic. |

In both modes the router drops private addresses (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16), but it filters only traffic routed through it: a project's root can route around it, and the host answers at the project's bridge gateway. Two firewall rules the operator applies on the host close both paths; see [Security: host hardening](../SECURITY.md#-host-hardening-operator-applied).

Plus, independently, `--inner-firewall` adds a bridge-boundary ACL inside the supervisor's nested Docker daemon: workers can only egress rs-inner via mcp-proxy. Defense-in-depth — workers' direct internet access (subject to egress mode) is unaffected; the firewall affects how workers reach `mcp-proxy` vs other rs-inner destinations.

Change egress after creation: destroy + recreate.
