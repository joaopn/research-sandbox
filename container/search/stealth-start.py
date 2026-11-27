#!/usr/bin/env python3
"""The stealth sidecar's start (rs-search-stealth, under dumb-init).

Runs Playwright's tool server on TOOL_PORT for the search agent, configured
(stealth-config.json) to drive its browser through `remoteEndpoint` at
ws://127.0.0.1:RELAY_PORT/WS_PATH. On that loopback port this program
listens itself: at the tool server's first connection it starts camoufox's
browser server on 127.0.0.1:BROWSER_PORT and relays the connection to it, so
the browser (hundreds of MB once running) is started only by a search that
uses the stealth tier, and it listens on the loopback only — the raw browser
control port gives full browser control and bypasses the agent's deny list,
so nothing on the search's network may reach it. A browser server that has
ended (it crashed) is started again at the next connection.

The container ends when the tool server ends. Stdlib only (the image's
Python, which also carries camoufox).
"""

from __future__ import annotations

import asyncio
import sys

TOOL_PORT = 8931            # service.py SIDECAR_PORT — MIRROR (and EXPOSE)
RELAY_PORT = 9330           # stealth-config.json remoteEndpoint's port — MIRROR
BROWSER_PORT = 9331         # camoufox's browser server, loopback only
WS_PATH = "camoufox"        # stealth-config.json remoteEndpoint's path — MIRROR
CONFIG = "/opt/rs-search-stealth/config.json"
OUTPUT_DIR = "/output"      # the search volume's files folder (service.py)

# How long camoufox's browser server may take to accept connections once
# started. Measured: 1.2 to 2.6 s from its start to its address on an idle
# machine (the Firefox launch inside it about 0.4 s). Thirty times the slow
# end is room for a host busy with several searches; past it the browser is
# not coming, and the tool server's connection is closed (the agent's call
# fails with Playwright's connection error, which the agent reports).
BROWSER_START_S = 60
# Between two checks that the browser server accepts: each is one loopback
# connect (well under a millisecond); a tenth of a second adds at most that to
# a first stealth page that takes over a second anyway.
BROWSER_POLL_S = 0.1
# What one relay read takes at most (a buffer, not a limit).
RELAY_PIECE = 65536

TOOL_ARGV = ["playwright-mcp", "--headless", "--isolated", "--image-responses=omit",
             "--snapshot-mode=none", f"--config={CONFIG}", f"--output-dir={OUTPUT_DIR}",
             f"--port={TOOL_PORT}", "--host=0.0.0.0", "--allowed-hosts=*"]
# Firefox's PDF viewer off: a PDF the browser opens is downloaded (a file of
# the search) instead of shown. Measured: with the viewer on, a PDF opens in
# it and no page action saves it; the cost is that the page can read that
# this Firefox has no PDF viewer.
BROWSER_ARGV = [sys.executable, "-c",
                "from camoufox.server import launch_server\n"
                f"launch_server(headless=True, host='127.0.0.1', port={BROWSER_PORT}, "
                f"ws_path={WS_PATH!r}, firefox_user_prefs={{'pdfjs.disabled': True}})\n"]


def say(msg: str) -> None:
    sys.stderr.write(f"rs-search-stealth: {msg}\n")
    sys.stderr.flush()


class Browser:
    """camoufox's browser server, started at the first connection."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.proc: asyncio.subprocess.Process | None = None
        self.ended: asyncio.Task | None = None

    async def _accepts(self) -> bool:
        try:
            _r, w = await asyncio.open_connection("127.0.0.1", BROWSER_PORT)
        except OSError:
            return False
        w.close()
        return True

    async def ensure(self) -> bool:
        async with self.lock:
            if self.ended is not None and not self.ended.done():
                return True
            if self.proc is not None:
                say(f"the browser server ended (exit {self.proc.returncode}); starting it again")
            loop = asyncio.get_running_loop()
            start = loop.time()
            self.proc = await asyncio.create_subprocess_exec(*BROWSER_ARGV,
                                                             stdin=asyncio.subprocess.DEVNULL)
            self.ended = asyncio.create_task(self.proc.wait())
            while loop.time() - start < BROWSER_START_S:
                if self.ended.done():
                    say(f"the browser server stopped at its start (exit {self.proc.returncode})")
                    return False
                if await self._accepts():
                    say(f"the browser server is up ({loop.time() - start:.1f} s)")
                    return True
                await asyncio.sleep(BROWSER_POLL_S)
            say(f"the browser server did not accept connections within {BROWSER_START_S} s")
            self.proc.kill()
            await self.ended
            return False

    def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.kill()


async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            piece = await reader.read(RELAY_PIECE)
            if not piece:
                break
            writer.write(piece)
            await writer.drain()
    except OSError:
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def main() -> int:
    browser = Browser()

    async def relay(c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
        if not await browser.ensure():
            c_writer.close()
            return
        try:
            b_reader, b_writer = await asyncio.open_connection("127.0.0.1", BROWSER_PORT)
        except OSError as e:
            say(f"the browser server refused a connection ({e.strerror or e})")
            c_writer.close()
            return
        await asyncio.gather(pump(c_reader, b_writer), pump(b_reader, c_writer))

    server = await asyncio.start_server(relay, "127.0.0.1", RELAY_PORT)
    tool = await asyncio.create_subprocess_exec(*TOOL_ARGV, stdin=asyncio.subprocess.DEVNULL)
    code = await tool.wait()
    say(f"the tool server ended (exit {code})")
    server.close()
    browser.stop()
    return code if code is not None else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
