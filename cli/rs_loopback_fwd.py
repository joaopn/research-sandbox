#!/usr/bin/env python3
"""rs-loopback-fwd — bridge a box's loopback service onto its bridge interface.

Part of F3 (surfacing a 127.0.0.1-bound in-box service as a webui tab). A great
many dev servers, notebook servers, and multi-process apps bind 127.0.0.1 by
default, which is unreachable from outside the container's netns — that
invisibility IS the container boundary. This tool, staged + launched by the host
reconcile (rscore._reconcile_loopback_bridges) ONLY for an operator-registered
exported port, republishes 127.0.0.1:<port> onto the box's own bridge IP at the
SAME port, so the webui reaches it at rs-project-<name>:<port> exactly as it
reaches a 0.0.0.0-bound service. It never touches the host netns and runs entirely
inside the box.

Binding the box's SPECIFIC bridge IP (not 0.0.0.0) is load-bearing: 0.0.0.0:<port>
overlaps 127.0.0.1:<port> and would EADDRINUSE against the app, but a distinct
specific address coexists with the loopback listener. A genuinely 0.0.0.0-bound app
already holds the bridge IP, so our bind fails cleanly and we exit 0 (the service
is already reachable) — the accepted loopback-only contract (F3 Fork C).

GROUPS (STAGE_PASSTHROUGH_PORTS): one process may serve SEVERAL ports (a
pass-through span) — every argv port gets its own listener and its own pidfile,
all holding this pid; a port that fails to bind is skipped (never aborts its
siblings), and if nothing binds the process exits 0 with no pidfile. The
single-port invocation (rs_sandbox's box expose, the sandbox-box image bake) is
the count==1 case of the same contract.
"""
import asyncio
import os
import socket
import sys
from pathlib import Path

# Read granularity for the relay pumps — NOT a cap (the OS enforces its own socket
# buffer bounds); just how much each read() hands back so a large transfer streams
# rather than buffering whole. 64 KiB is a common pipe / TCP-window chunk.
_CHUNK = 65536
_PIDFILE_DIR = Path("/tmp/rs-loopback-fwd")


def _bridge_ip() -> str:
    """The box's primary non-loopback IP — the address rs-project-<name> resolves
    to on rs-net-<project>. docker seeds /etc/hosts (container-name -> eth0 IP), so
    gethostbyname(gethostname()) returns it on a single-homed box."""
    return socket.gethostbyname(socket.gethostname())


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(_CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _handle(client_reader: asyncio.StreamReader,
                  client_writer: asyncio.StreamWriter, port: int) -> None:
    try:
        backend_reader, backend_writer = await asyncio.open_connection(
            "127.0.0.1", port)
    except OSError:
        # App not up yet (ConnectionRefused) or gone — drop this client, keep
        # serving; it resolves once the app starts listening.
        client_writer.close()
        return
    await asyncio.gather(
        _pump(client_reader, backend_writer),
        _pump(backend_reader, client_writer),
    )


async def _main(ports: list[int]) -> int:
    ip = _bridge_ip()
    servers = []
    bound: list[int] = []
    for port in ports:
        def _make_cb(p: int):
            async def _cb(reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
                await _handle(reader, writer, p)
            return _cb
        try:
            # reuse_address=False is load-bearing (NOT the asyncio Unix default of
            # True): with SO_REUSEADDR OFF, our specific eth0:<port> bind can never
            # coexist with a wildcard 0.0.0.0:<port> app (that coexistence requires
            # BOTH sockets to set SO_REUSEADDR). So the loopback-only contract holds
            # deterministically regardless of the app's own SO_REUSEADDR: app-first ->
            # our bind fails -> we step aside (below); forwarder-first -> a later
            # 0.0.0.0 app fails to bind (the accepted F3 Fork-C behaviour). The happy
            # path (eth0:<port> vs 127.0.0.1:<port>, two distinct specific addresses)
            # coexists either way. A listener never enters TIME_WAIT, so dropping
            # SO_REUSEADDR does not impede a kill-then-relaunch on the same port.
            server = await asyncio.start_server(_make_cb(port), host=ip, port=port,
                                                reuse_address=False)
        except OSError as e:
            # Bind failed — most commonly a 0.0.0.0-bound app already holds the
            # port (already reachable). Skip THIS port only (a group must never
            # abort its siblings), and write no pidfile for it (D4): a doomed
            # bind must never leave a file that poisons the reconcile liveness
            # check.
            print(f"rs-loopback-fwd: bind {ip}:{port} failed ({e}); assuming "
                  f"the service is already reachable — skipping", file=sys.stderr)
            continue
        servers.append(server)
        bound.append(port)
    if not servers:
        return 0
    # Binds succeeded -> record our pid, one pidfile per BOUND port (the host
    # reconcile keys liveness + teardown per port; a group's files all hold the
    # same pid).
    _PIDFILE_DIR.mkdir(parents=True, exist_ok=True)
    for port in bound:
        (_PIDFILE_DIR / f"{port}.pid").write_text(f"{os.getpid()}\n")
    try:
        await asyncio.gather(*(s.serve_forever() for s in servers))
    finally:
        for s in servers:
            s.close()
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args or not all(a.isdigit() for a in args):
        print("usage: rs-loopback-fwd <port> [<port> ...]", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_main([int(a) for a in args]))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
