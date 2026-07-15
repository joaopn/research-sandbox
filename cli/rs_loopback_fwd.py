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


async def _main(port: int) -> int:
    ip = _bridge_ip()

    async def _cb(reader: asyncio.StreamReader,
                  writer: asyncio.StreamWriter) -> None:
        await _handle(reader, writer, port)

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
        server = await asyncio.start_server(_cb, host=ip, port=port,
                                            reuse_address=False)
    except OSError as e:
        # Bind failed — most commonly a 0.0.0.0-bound app already holds the port
        # (already reachable). Clean exit, and NO pidfile (D4): a doomed launch must
        # never leave a file that poisons the reconcile liveness check.
        print(f"rs-loopback-fwd: bind {ip}:{port} failed ({e}); assuming the "
              f"service is already reachable — exiting", file=sys.stderr)
        return 0
    # Bind succeeded -> record our pid so the host reconcile can find + reap us.
    _PIDFILE_DIR.mkdir(parents=True, exist_ok=True)
    (_PIDFILE_DIR / f"{port}.pid").write_text(f"{os.getpid()}\n")
    async with server:
        await server.serve_forever()
    return 0


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("usage: rs-loopback-fwd <port>", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_main(int(sys.argv[1])))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
