"""orch_files — host-side access to a project's settings folder.

A project's settings live in ``<workspace>/.orchestrator/``, and the workspace
is the project's own ``/workspace``: the project's agent can replace any file
in that folder, or the folder itself, with a link — a relative one resolves on
the host, into another project's records or anywhere the operator can write.
So the host never reaches that folder by path:

  * the workspace is opened normally (its path is the host's own, under
    PROJECTS_DIR), and ``.orchestrator`` is opened RELATIVE to it with
    ``O_NOFOLLOW | O_DIRECTORY`` — a link or anything but a folder there is
    refused, never followed;
  * every file in it is opened relative to that folder's descriptor with
    ``O_NOFOLLOW`` (and ``O_NONBLOCK`` + a regular-file check on reads, so a
    planted FIFO or folder can never block or confuse a read);
  * a write goes to a fresh, randomly named temp file in the folder
    (``O_CREAT | O_EXCL | O_NOFOLLOW``), then renames over the name — so a link
    or file planted at any fixed temp name redirects nothing.

Three outcomes for a read: the bytes; ``FileNotFoundError`` when the file or
the folder (or the workspace) does not exist; ``Unusable`` when something is
there but cannot be used (a link at the file or at the folder, a non-regular
file, an oversize file, any other OSError). ``Unusable`` is an ``OSError``, so
a reader that already treats an OSError as "unreadable" keeps doing so.

Stdlib only; host-side only (not baked into any image). Imported by rscore,
role_mcp and extension.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from pathlib import Path

ORCH_DIR = ".orchestrator"

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC
_TMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
# What open(2) answers for a link under O_NOFOLLOW (Linux ELOOP, BSD EMLINK)
# and for a non-folder under O_DIRECTORY.
_LINK_ERRNOS = {errno.ELOOP, errno.EMLINK, errno.ENOTDIR}
# What rename(2) / unlink(2) answer when a folder sits at a file's name
# (Linux unlink gives EISDIR; POSIX allows EPERM, mapped in unlink only).
_TARGET_NOT_FILE_ERRNOS = {errno.EISDIR, errno.ENOTEMPTY, errno.EEXIST}


class Unusable(OSError):
    """The project's settings folder, or a file in it, is there but RS does
    not use it: a link, not a folder / not a regular file, too large, or
    unreadable. The message names the path and never a command."""


class WorkspaceUnopenable(Unusable):
    """The project's workspace folder itself cannot be opened (the project's
    agent can make it so). An Unusable like any other, except for create,
    which repairs a leftover workspace's mode and so must not refuse it."""


def folder_path(workspace) -> Path:
    return Path(workspace) / ORCH_DIR


# Messages name the folder as the project sees it, never the host path: the
# remedy is applied from inside the project, and a refusal reaches the browser,
# which never shows the operator's PROJECTS_DIR.
SHOWN_FOLDER = "/workspace/" + ORCH_DIR


def _shown(name: str | None = None) -> str:
    return SHOWN_FOLDER if name is None else f"{SHOWN_FOLDER}/{name}"


def _folder_refusal(workspace) -> str:
    return (f"the project's settings folder {_shown()} is a link "
            f"or not a folder; RS does not read or write through it. Put a real "
            f"folder back from inside the project")


def _file_refusal(workspace, name: str) -> str:
    return (f"{_shown(name)} in the project's settings folder "
            f"is a link or not a regular file; RS does not read or write "
            f"through it. Put a real file back from inside the project")


@contextlib.contextmanager
def open_folder(workspace, *, create: bool = False):
    """Yield a descriptor of ``<workspace>/.orchestrator``, opened without
    following a link. ``create`` makes the workspace and the folder first when
    they are absent (a link planted at the folder name is then refused, not
    followed). Raises FileNotFoundError when absent (and not created),
    Unusable when the folder is a link or not a folder."""
    ws = Path(workspace)
    if create:
        ws.mkdir(parents=True, exist_ok=True)
    try:
        ws_fd = os.open(ws, _DIR_FLAGS)
    except FileNotFoundError:
        # Absent, as for the folder; reworded so the host path never reaches
        # a message built from it.
        raise FileNotFoundError(errno.ENOENT, "the project's folder /workspace "
                                "does not exist") from None
    except OSError as e:
        # The project's agent owns /workspace and can make it unopenable; the
        # host path never goes into the message.
        raise WorkspaceUnopenable(f"the project's folder /workspace cannot be opened "
                       f"({e.strerror})") from None
    try:
        if create:
            try:
                os.mkdir(ORCH_DIR, dir_fd=ws_fd)
            except FileExistsError:
                pass
        try:
            dfd = os.open(ORCH_DIR, _DIR_FLAGS | os.O_NOFOLLOW, dir_fd=ws_fd)
        except FileNotFoundError:
            raise
        except OSError as e:
            if e.errno in _LINK_ERRNOS:
                raise Unusable(_folder_refusal(ws)) from None
            raise Unusable(f"the project's settings folder {_shown()} "
                           f"cannot be opened ({e.strerror})") from None
    finally:
        os.close(ws_fd)
    try:
        yield dfd
    finally:
        os.close(dfd)


def check_folder(workspace) -> None:
    """Raise Unusable when the settings folder is a link or not a folder;
    nothing when it is a folder or absent. A pre-flight for an operation that
    will write there."""
    try:
        with open_folder(workspace):
            pass
    except FileNotFoundError:
        pass


def read_bytes(workspace, name: str, *, max_bytes: int | None = None) -> bytes:
    """The bytes of ``.orchestrator/<name>``. FileNotFoundError when it (or the
    folder) is absent; Unusable when it or the folder is a link, it is not a
    regular file, it is larger than ``max_bytes``, or it cannot be read."""
    with open_folder(workspace) as dfd:
        try:
            fd = os.open(name, _READ_FLAGS, dir_fd=dfd)
        except FileNotFoundError:
            raise
        except OSError as e:
            if e.errno in _LINK_ERRNOS:
                raise Unusable(_file_refusal(workspace, name)) from None
            raise Unusable(f"{_shown(name)} in the project's "
                           f"settings folder cannot be opened ({e.strerror})") from None
        # The type check comes before fdopen, which refuses a folder with its
        # own error and would hide the plain "not a regular file" reason.
        try:
            regular = stat.S_ISREG(os.fstat(fd).st_mode)
        except OSError:
            regular = False
        if not regular:
            os.close(fd)
            raise Unusable(_file_refusal(workspace, name))
        # One owner for the descriptor: the file object never closes it
        # (closefd=False), the finally closes it exactly once on every path.
        try:
            with open(fd, "rb", closefd=False) as fh:
                raw = fh.read() if max_bytes is None else fh.read(max_bytes + 1)
        except OSError as e:
            raise Unusable(f"{_shown(name)} in the "
                           f"project's settings folder cannot be read ({e.strerror})") from None
        finally:
            os.close(fd)
    if max_bytes is not None and len(raw) > max_bytes:
        raise Unusable(f"{_shown(name)} in the project's "
                       f"settings folder is larger than {max_bytes} bytes")
    return raw


def read_text(workspace, name: str, *, max_bytes: int | None = None) -> str:
    """read_bytes decoded as UTF-8. A file that is not UTF-8 is Unusable too
    (the project can write any bytes there), so a reader that treats an
    OSError as unreadable never meets a stray UnicodeDecodeError."""
    raw = read_bytes(workspace, name, max_bytes=max_bytes)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Unusable(f"{_shown(name)} in the project's settings folder is not "
                       f"UTF-8 text") from None


def write_text(workspace, name: str, text: str, *, mode: int | None = None,
               create: bool = False) -> None:
    """Replace ``.orchestrator/<name>`` with ``text`` atomically: a fresh,
    randomly named temp file in the folder (never a fixed name), written,
    renamed over ``name``. Its mode is ``mode`` when given; otherwise 0666
    less the process umask, exactly what ``Path.write_text`` gave the file
    before (the kernel applies the umask at create). ``create`` makes the folder when
    it is absent. On any failure the temp file is unlinked and the error
    re-raised; Unusable when the folder is a link or not a folder."""
    data = text.encode("utf-8")
    with open_folder(workspace, create=create) as dfd:
        tmp = f".{name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, _TMP_FLAGS, 0o666 if mode is None else 0o600,
                     dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as fh:
                if mode is not None:
                    os.fchmod(fh.fileno(), mode)
                fh.write(data)
            try:
                os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except OSError as e:
                # A folder planted at the target name: refuse with the
                # message, not a bare error a caller cannot word.
                if e.errno in _TARGET_NOT_FILE_ERRNOS:
                    raise Unusable(_file_refusal(workspace, name)) from None
                raise
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise


def unlink(workspace, name: str) -> None:
    """Remove ``.orchestrator/<name>`` (a link there is removed itself, never
    its target). FileNotFoundError when absent; Unusable when the folder is a
    link or not a folder."""
    with open_folder(workspace) as dfd:
        try:
            os.unlink(name, dir_fd=dfd)
        except OSError as e:
            if e.errno in _TARGET_NOT_FILE_ERRNOS or e.errno == errno.EPERM:
                raise Unusable(_file_refusal(workspace, name)) from None
            raise
