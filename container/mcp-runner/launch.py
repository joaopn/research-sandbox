"""launch.py — the fixed command the bridge starts for each MCP session.

Reads the server's argv (written by serve.py) and replaces itself with it, so
the session's process IS the server: its exit ends the session, and the
bridge's shell never sees an operator-supplied word. The server's error
output goes straight to the container's own (the server's Logs): the bridge
runs with its logging off, because it would log every message — tool
arguments and results included.
"""

import json
import os
import sys

ARGV_FILE = "/tmp/rs-mcp-argv.json"
# The container's error output: that of its first process, the bridge.
CONTAINER_STDERR = "/proc/1/fd/2"

try:
    fd = os.open(CONTAINER_STDERR, os.O_WRONLY)
    os.dup2(fd, 2)
    os.close(fd)
except OSError:
    pass                    # it keeps the bridge's pipe (not shown, not fatal)
try:
    with open(ARGV_FILE) as f:
        argv = json.load(f)
except (OSError, ValueError):
    sys.exit("the server's command could not be read")
if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
    sys.exit("the server's command could not be read")
os.execv(argv[0], argv)
