"""serve.py — run an installed package's command as an MCP server behind the bridge.

The server container's program (agent/Dockerfile.mcp-runner):

  python3 serve.py --port P --path /mcp --idle-s N -- <command path> [arg ...]

The bridge (supergateway, stateful streamable HTTP) starts one server process
per MCP session, ends it when the session is deleted, when it has been idle N
seconds, or when the process exits (which ends its session). The bridge runs
its server command through a shell, so the operator's command and arguments
never go to it: it is handed only the fixed launcher (launch.py), and the
launcher reads the real argv from a file this program writes. Every process
inherits this container's environment — the server's settings.

Nothing is installed or fetched here: a command that is not in the package
volume is refused, and the container ends.
"""

import json
import os
import sys

PKG = "/rs/pkg"
ARGV_FILE = "/tmp/rs-mcp-argv.json"
BRIDGE = "/opt/rs-mcp/bridge/node_modules/.bin/supergateway"
LAUNCHER = "/opt/rs-mcp/launch.py"
USAGE = "usage: serve.py --port P --path PATH --idle-s N -- COMMAND [ARG ...]"


def main(argv: list) -> None:
    try:
        cut = argv.index("--")
    except ValueError:
        sys.exit(USAGE)
    opts, command = argv[:cut], argv[cut + 1:]
    if len(opts) != 6 or opts[0::2] != ["--port", "--path", "--idle-s"] or not command:
        sys.exit(USAGE)
    port, path, idle_s = opts[1], opts[3], opts[5]
    if not (port.isdigit() and idle_s.isdigit() and path.startswith("/")):
        sys.exit(USAGE)
    program = command[0]
    if not program.startswith(PKG + "/") or not os.access(program, os.X_OK):
        sys.exit(f"the command {os.path.basename(program)} is not installed in this "
                 "server's package; Update on its row installs the package again")
    fd = os.open(ARGV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(command, f)
    print(f"serving {os.path.basename(program)} over streamable HTTP on port "
          f"{port} at {path} (one process per session)", flush=True)
    os.execv(BRIDGE, [
        BRIDGE,
        "--stdio", f"python3 {LAUNCHER}",
        "--outputTransport", "streamableHttp",
        "--stateful",
        "--port", port,
        "--streamableHttpPath", path,
        "--sessionTimeout", str(int(idle_s) * 1000),
        # Off: at any other level it logs every message, tool arguments and
        # results included. The server's own error output reaches the
        # container's log through the launcher.
        "--logLevel", "none",
    ])


if __name__ == "__main__":
    main(sys.argv[1:])
