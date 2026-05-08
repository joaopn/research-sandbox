#!/opt/conda/bin/python
"""code-server lazy-start TCP proxy.

Listens on CODE_SERVER_STUB_PORT (the port the webui's reverse proxy
hits via container DNS, `rs-project-<proj>:<port>`). On the first
client connection, spawns code-server on 127.0.0.1:CODE_SERVER_UPSTREAM_PORT
and forwards bytes bidirectionally. After CODE_SERVER_IDLE_SECONDS
with no active connections, SIGTERMs the child; the next request
respawns it.

This is the centerpiece of the slim-RAM strategy: zero code-server
RAM cost when the editor tab isn't open, regardless of how many
projects exist on the host.

Why TCP-layer proxy and not application-layer:
- Works for both HTTP and WS without parsing either.
- ~80 LOC of stdlib asyncio; no third-party deps.
- Upstream sees connections from 127.0.0.1, which is fine because
  code-server is invoked with --auth=none (auth lives in the webui's
  cookie session, terminated before traffic reaches us).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time

# ---- knobs (named, with reasoning — see CLAUDE.md "no magic numbers") -----

# How long to wait for code-server's listening socket to become accept-ready
# after we spawn it. Cold-start typically completes in 2-5s on a warm cache;
# 30s is generous enough that a slow disk doesn't false-fail, short enough
# that a wedged binary surfaces quickly to the user.
UPSTREAM_BOOT_TIMEOUT_SECONDS = 30

# Polling interval for the upstream-listening probe during boot. 0.1s gives
# 100ms tail latency on the request that triggers boot, which is below the
# "feels instant" threshold once the upstream is ready.
UPSTREAM_BOOT_POLL_SECONDS = 0.1

# Reap-loop check interval. The actual reap can lag idle-eligibility by
# up to this amount; 30s strikes a balance between "checks often" (so RAM
# is reclaimed promptly) and "doesn't burn CPU in a tight loop".
REAP_CHECK_INTERVAL_SECONDS = 30

# Grace period after SIGTERM before falling back to SIGKILL on the
# upstream code-server. code-server's hot-exit (saving unsaved buffers
# to user-data-dir on exit) typically completes in ~1s. 5s is the
# upper bound where we'd rather force-kill than wait.
SIGTERM_GRACE_SECONDS = 5

# Per-direction read chunk size. 64 KiB matches aiohttp's default and the
# typical kernel TCP buffer; nothing rides on this number being exact.
PIPE_CHUNK_BYTES = 65536

# ---- runtime config from env ------------------------------------------------

LISTEN_HOST = os.environ.get("CODE_SERVER_STUB_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ["CODE_SERVER_STUB_PORT"])
UPSTREAM_HOST = os.environ.get("CODE_SERVER_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ["CODE_SERVER_UPSTREAM_PORT"])
IDLE_SECONDS = int(os.environ["CODE_SERVER_IDLE_SECONDS"])
WORKSPACE = os.environ.get("CODE_SERVER_WORKSPACE", "/workspace")
USER_DATA_DIR = os.environ.get(
    "CODE_SERVER_USER_DATA_DIR",
    f"{WORKSPACE}/.local/share/code-server")
EXTENSIONS_DIR = os.environ.get(
    "CODE_SERVER_EXTENSIONS_DIR",
    f"{WORKSPACE}/.local/share/code-server/extensions")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s code-server-stub %(levelname)s %(message)s")
log = logging.getLogger("stub")


class State:
    """Single-instance mutable state for the stub. The asyncio.Lock guards
    the spawn path so concurrent first-requests don't race two code-server
    children."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.last_activity: float = time.time()
        self.active_conns: int = 0
        self.lock = asyncio.Lock()


S = State()


async def _wait_for_upstream() -> None:
    """Poll UPSTREAM_HOST:UPSTREAM_PORT until accept-ready or timeout."""
    deadline = time.time() + UPSTREAM_BOOT_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            _, w = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
            return
        except OSError:
            await asyncio.sleep(UPSTREAM_BOOT_POLL_SECONDS)
    raise RuntimeError(
        f"upstream {UPSTREAM_HOST}:{UPSTREAM_PORT} did not come up in "
        f"{UPSTREAM_BOOT_TIMEOUT_SECONDS}s")


async def ensure_upstream() -> None:
    """Spawn code-server if it isn't running. Idempotent under concurrent
    callers via S.lock."""
    async with S.lock:
        if S.proc is not None and S.proc.returncode is None:
            return
        cmd = [
            "code-server",
            "--bind-addr", f"{UPSTREAM_HOST}:{UPSTREAM_PORT}",
            "--auth", "none",
            "--disable-telemetry",
            "--disable-update-check",
            "--disable-getting-started-override",
            "--user-data-dir", USER_DATA_DIR,
            "--extensions-dir", EXTENSIONS_DIR,
            WORKSPACE,
        ]
        log.info(f"spawning code-server: {' '.join(cmd)}")
        S.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            await _wait_for_upstream()
        except Exception:
            # Upstream never came up — clean the failed child so the next
            # request retries from scratch instead of hanging on a dead pid.
            try:
                S.proc.kill()
            except ProcessLookupError:
                pass
            S.proc = None
            raise
        log.info(f"code-server up on {UPSTREAM_HOST}:{UPSTREAM_PORT}")


async def _pipe(src: asyncio.StreamReader,
                dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await src.read(PIPE_CHUNK_BYTES)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


async def handle_client(client_reader: asyncio.StreamReader,
                        client_writer: asyncio.StreamWriter) -> None:
    S.active_conns += 1
    S.last_activity = time.time()
    try:
        try:
            await ensure_upstream()
        except Exception as e:
            log.warning(f"ensure_upstream failed: {e}")
            client_writer.close()
            try:
                await client_writer.wait_closed()
            except Exception:
                pass
            return

        try:
            up_reader, up_writer = await asyncio.open_connection(
                UPSTREAM_HOST, UPSTREAM_PORT)
        except OSError as e:
            log.warning(f"upstream connect failed post-spawn: {e}")
            client_writer.close()
            try:
                await client_writer.wait_closed()
            except Exception:
                pass
            return

        await asyncio.gather(
            _pipe(client_reader, up_writer),
            _pipe(up_reader, client_writer),
            return_exceptions=True,
        )
    finally:
        S.active_conns -= 1
        S.last_activity = time.time()


async def reap_loop() -> None:
    """Periodically check whether code-server has been idle long enough to
    reap. SIGTERM with a grace window; SIGKILL on grace expiry."""
    while True:
        await asyncio.sleep(REAP_CHECK_INTERVAL_SECONDS)
        if S.proc is None or S.proc.returncode is not None:
            continue
        if S.active_conns > 0:
            continue
        idle_for = time.time() - S.last_activity
        if idle_for < IDLE_SECONDS:
            continue
        log.info(f"reaping code-server (idle for {int(idle_for)}s)")
        try:
            S.proc.terminate()
            try:
                await asyncio.wait_for(
                    S.proc.wait(), timeout=SIGTERM_GRACE_SECONDS)
            except asyncio.TimeoutError:
                log.warning(
                    f"code-server did not exit in {SIGTERM_GRACE_SECONDS}s "
                    "after SIGTERM; sending SIGKILL")
                try:
                    S.proc.kill()
                except ProcessLookupError:
                    pass
                await S.proc.wait()
        except ProcessLookupError:
            pass
        S.proc = None


async def shutdown(server: asyncio.AbstractServer) -> None:
    log.info("stub shutting down")
    server.close()
    await server.wait_closed()
    if S.proc is not None and S.proc.returncode is None:
        try:
            S.proc.terminate()
            await asyncio.wait_for(
                S.proc.wait(), timeout=SIGTERM_GRACE_SECONDS)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                S.proc.kill()
            except ProcessLookupError:
                pass


async def main() -> None:
    server = await asyncio.start_server(
        handle_client, LISTEN_HOST, LISTEN_PORT)
    log.info(
        f"listening on {LISTEN_HOST}:{LISTEN_PORT}; "
        f"upstream {UPSTREAM_HOST}:{UPSTREAM_PORT}; "
        f"idle reap after {IDLE_SECONDS}s")

    loop = asyncio.get_running_loop()
    stopper = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopper.set)

    asyncio.create_task(reap_loop())
    serve_task = asyncio.create_task(server.serve_forever())

    await stopper.wait()
    serve_task.cancel()
    await shutdown(server)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
