"""install.py — install ONE package at ONE exact version into the package volume.

Runs in the one-shot install container of an MCP server RS runs from a package
(agent/Dockerfile.mcp-runner). That container gets no setting and no secret:
a package's install scripts run here and see nothing of the server's
configuration. The host validated every argument (registry, name, exact
version, command) before this runs; each reaches npm or uv as one argv
element, never through a shell.

  python3 install.py <npm|pypi> <package> <version> <command>

Exit 0 when the package is installed and installs <command>; otherwise the
installer's own output (on stdout/stderr, which the host streams into the
operation's log) says why, and the exit is non-zero.
"""

import os
import subprocess
import sys

PKG = "/rs/pkg"
# Where each registry's install puts the commands it provides. The host
# derives the same paths (cli/mcp_run.py command_path); keep them in step.
NPM_BIN = os.path.join(PKG, "node_modules", ".bin")
VENV = os.path.join(PKG, "venv")
PYPI_BIN = os.path.join(VENV, "bin")


def run(argv: list) -> None:
    print("+ " + " ".join(argv), flush=True)
    r = subprocess.run(argv)
    if r.returncode != 0:
        sys.exit(f"the installer exited with code {r.returncode}")


def main(argv: list) -> None:
    if len(argv) != 4:
        sys.exit("usage: install.py <npm|pypi> <package> <version> <command>")
    registry, package, version, command = argv
    if registry == "npm":
        run(["npm", "install", "--prefix", PKG, "--no-audit", "--no-fund",
             "--no-update-notifier", f"{package}@{version}"])
        bindir = NPM_BIN
    elif registry == "pypi":
        run(["uv", "venv", "--no-python-downloads", "--python", sys.executable, VENV])
        run(["uv", "pip", "install", "--python", os.path.join(PYPI_BIN, "python"),
             f"{package}=={version}"])
        bindir = PYPI_BIN
    else:
        sys.exit(f"unknown registry {registry}")
    path = os.path.join(bindir, command)
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        sys.exit(f"the package {package} {version} installs no command named "
                 f"{command}")
    print(f"installed {package} {version}; its command {command} is ready", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
