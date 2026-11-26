---
title: Authoring an MCP server
parent: User guide
nav_order: 14
---
# Authoring an MCP server

## An image RS runs (Services → MCP)

A server RS runs from a container image (Add server → a server RS runs) is reached by the MCP gateway at `http://rs-mcpsrv-<name>:<port><path>`. Its image's contract:

1. **Serve streamable HTTP** (JSON-RPC over POST) on the port and path you give at Add (default path `/mcp`), listening on all addresses of the container.
2. **Accept its own container name as `Host`.** Requests arrive with `Host: rs-mcpsrv-<name>:<port>`; a server with DNS-rebinding protection lists that value (for the Python SDK, `mcp.settings.transport_security.allowed_hosts`).
3. **Read its settings from the environment.** Each setting is an environment value, read literally (no `$` expansion). A secret that only works on the command line or from a file is not supported.
4. **Work without extra privileges.** The container runs with every capability dropped, no privilege escalation, no published port and no host folder. A data folder you name at Add is a docker volume kept across restarts.
5. **Expect a narrow way out.** It reaches ports 80 and 443 on the internet and DNS; anything on the local network or this machine only when you name it as a destination (`host:port`).

From the host:

```bash
printf '%s\n' "$API_KEY" | python research.py mcp server add myserver --image ghcr.io/owner/server:1.2 \
    --port 8080 --path /mcp --setting LIBRARY_ID=123 --secret-stdin API_KEY \
    --destination db.lab.example:5432
```

## A package RS runs (Services → MCP)

A server RS runs from an npm or PyPI package is installed once at one exact version and run by RS behind its own bridge. The package's contract:

1. **Install a command that speaks MCP over stdio.** RS runs the command you name (with your arguments) and talks JSON-RPC to it on its standard input and output; anything the server prints for people goes to its standard error, which is what Logs shows.
2. **Read its settings from the environment.** Each setting is an environment value, read literally. A secret that only works on the command line or from a file is not supported.
3. **Need nothing at install time but the package itself.** The install gets no setting and no secret; a package whose install needs a key cannot be run.
4. **Fetch nothing when it starts.** Everything it needs must be installed with the package: a start runs offline as far as the package is concerned (its own calls to its service go out on ports 80 and 443, or to a destination you name).
5. **Expect a process per session.** Each MCP session gets its own process, started on its first call and ended with the session or after ten idle minutes; keep state that must outlive a session in the data folder.

From the host:

```bash
printf '%s\n' "$ZOTERO_API_KEY" | python research.py mcp server add zotero \
    --package pypi:zotero-mcp-server==0.9.0 --command zotero-mcp --arg serve \
    --setting ZOTERO_LIBRARY_ID=123 --setting ZOTERO_LIBRARY_TYPE=user \
    --setting ZOTERO_LOCAL=false --secret-stdin ZOTERO_API_KEY
```

An argument that starts with `-` is written `--arg=--flag`.

## A server for the per-project registry (host commands)

Three constraints define the contract between the research-sandbox and an MCP server:

1. **Streamable-HTTP transport.** Stdio MCPs are deferred. The Python SDK's `FastMCP("name").run(transport="streamable-http")` works out of the box; in any SDK, the server must speak JSON-RPC over POST.
2. **Endpoint at `/mcp`** (Python SDK default), or any path of your choosing — register with `--path <your-path>`.
3. **DNS-rebinding-protection allows `mcp-proxy:8888`.** The proxy pins this `Host` value on every upstream request, regardless of what the worker sent. Stable, can't be spoofed.

Minimal Python SDK example:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-mcp")
mcp.settings.host = "0.0.0.0"
mcp.settings.port = 8000
mcp.settings.transport_security.allowed_hosts = ["mcp-proxy:8888"]

@mcp.tool()
def lookup(query: str) -> str:
    return f"result for {query}"

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

Register and use:

```bash
docker build -t my-mcp:latest .
python research.py mcp add    mymcp --kind shared --image my-mcp:latest --port 8000
python research.py mcp enable mymcp        # required before mcp-allow; also flags auto-start
python research.py mcp start  mymcp        # launch now (or `research start` brings up enabled)
python research.py project mcp-allow myproj mymcp
# inside supervisor, in your plan, ask the worker to use mymcp; spawn with --mcps mymcp
```

`mcp add` only registers. `mcp enable` is the gate: `project mcp-allow`
refuses to grant a disabled MCP. For shared MCPs, `mcp enable` additionally
arms auto-start — `research start` launches every enabled shared MCP after
the router comes up. External MCPs have no container lifecycle, so `mcp
start`/`stop` skip them; `enable` for an external is just the visibility
gate before `project mcp-allow`.

External (already-running) MCPs:

```bash
# Service running on the docker host
python research.py mcp add mymcp --kind external --host host.docker.internal:9000

# Service reachable at a remote IP or DNS name
python research.py mcp add mymcp --kind external --host 10.0.5.42:8443
```

A DNS name given to `--host` is resolved on the machine running `research`, once, when the MCP is allowed into a project (`project create`, `project mcp allow`, `project mcp sync`), and that address is what the router, the project's proxy and the project's inner guard then use; a name that does not resolve there refuses the allow. If the address behind a name changes, deny and re-allow the MCP.

Auth headers (e.g. for hosted MCPs):

```bash
python research.py mcp add api --kind external --host host.docker.internal:9000 \
    --header "Authorization=Bearer ${API_TOKEN}"
```

`${VAR}` is interpolated against the host environment when the registry is loaded; the resolved value is visible to the supervisor (and is treated as a per-project secret, no different from the supervisor's own OAuth token in scope). Don't put long-lived production tokens here.
