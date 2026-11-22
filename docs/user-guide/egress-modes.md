---
title: Egress modes
parent: User guide
nav_order: 16
---
# Egress modes

Set at `project create` time with `--egress`, default `open`:

| Mode | rs-router behavior |
|---|---|
| `open` | RFC1918 blocked (the host's own LAN); all other outbound allowed. Sensible default for research workloads that need pip, apt, package downloads. |
| `locked` | Only TCP/80, TCP/443 and DNS (UDP/TCP 53) to the resolvers configured in `SANDBOX_DNS`; no ICMP; everything else dropped. Use when the project shouldn't egress to anything but HTTPS web traffic. |

Plus, independently, `--inner-firewall` adds a bridge-boundary ACL inside the supervisor's nested Docker daemon: workers can only egress rs-inner via mcp-proxy. Defense-in-depth — workers' direct internet access (subject to egress mode) is unaffected; the firewall affects how workers reach `mcp-proxy` vs other rs-inner destinations.

Change egress after creation: destroy + recreate.
