"""Windows-friendly subprocess helpers.

Every child process the app starts goes through here so that console windows
never flash on screen and so that whole process trees can be terminated when
the user cancels a separation.
"""

import os
import subprocess
import sys

#: Suppress the console window that would otherwise flash for each child.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

IS_WINDOWS = os.name == "nt"


def startup_info():
    """Return a ``STARTUPINFO`` that hides the child window on Windows."""
    if not IS_WINDOWS:
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def creation_flags(new_group=False):
    if not IS_WINDOWS:
        return 0
    flags = CREATE_NO_WINDOW
    if new_group:
        flags |= CREATE_NEW_PROCESS_GROUP
    return flags


def conda_env_path_entries(env_dir):
    """Return the directories ``conda activate`` would prepend to ``PATH``."""
    if not env_dir:
        return []
    parts = [
        env_dir,
        os.path.join(env_dir, "Library", "mingw-w64", "bin"),
        os.path.join(env_dir, "Library", "usr", "bin"),
        os.path.join(env_dir, "Library", "bin"),
        os.path.join(env_dir, "Scripts"),
        os.path.join(env_dir, "bin"),
    ]
    return [p for p in parts if os.path.isdir(p)]


def build_env(env_dir=None, extra=None):
    """Build an environment mapping that behaves like an activated Conda env.

    Also forces UTF-8 and unbuffered output so Demucs progress reaches us
    promptly and non-ASCII track names survive the round trip.
    """
    env = dict(os.environ)
    entries = conda_env_path_entries(env_dir)
    if entries:
        env["PATH"] = os.pathsep.join(entries + [env.get("PATH", "")])
        env["CONDA_PREFIX"] = env_dir
        env["CONDA_DEFAULT_ENV"] = os.path.basename(env_dir)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # tqdm/Rich behave better when they believe the stream is not a terminal.
    env.pop("PYTHONHOME", None)
    if extra:
        env.update(extra)
    return env


def run_capture(cmd, timeout=60, env_dir=None, env=None, cwd=None):
    """Run ``cmd``, returning ``(returncode, stdout, stderr)`` as text.

    Never raises for process failures; a launch failure is reported as
    return code ``-1`` with the reason in ``stderr``. A timeout is ``-2``.
    """
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env=env if env is not None else build_env(env_dir),
            cwd=cwd,
            startupinfo=startup_info(),
            creationflags=creation_flags(),
        )
    except subprocess.TimeoutExpired:
        return -2, "", "Timed out after %s seconds: %s" % (timeout, _fmt(cmd))
    except (OSError, ValueError) as exc:
        return -1, "", "Could not start %s: %s" % (_fmt(cmd), exc)

    return (
        completed.returncode,
        decode_bytes(completed.stdout),
        decode_bytes(completed.stderr),
    )


def decode_bytes(raw):
    """Decode child-process output, tolerating any Windows code page."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _fmt(cmd):
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(part) for part in cmd)
    return str(cmd)


def kill_tree(process, logger=None):
    """Terminate ``process`` and every child it spawned.

    Demucs launches worker subprocesses, so killing only the parent would
    leave orphans holding the output files open. ``taskkill /T`` walks the
    whole tree; POSIX falls back to killing the process group.
    """
    if process is None or process.poll() is not None:
        return

    pid = process.pid
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=20,
                startupinfo=startup_info(),
                creationflags=creation_flags(),
            )
        except Exception as exc:  # noqa: BLE001 - fall through to terminate()
            if logger is not None:
                logger.debug("taskkill failed for PID %s: %s", pid, exc)
    else:  # pragma: no cover - the app targets Windows
        # Only signal a group the child actually leads. Without
        # start_new_session the child shares our group, and killing that
        # would take the GUI down with it.
        try:
            group = os.getpgid(pid)
            if group == pid:
                os.killpg(group, 15)
        except Exception as exc:  # noqa: BLE001
            if logger is not None:
                logger.debug("killpg failed for PID %s: %s", pid, exc)

    for method in (process.terminate, process.kill):
        if process.poll() is not None:
            break
        try:
            method()
        except Exception:  # noqa: BLE001
            pass

    try:
        process.wait(timeout=15)
    except Exception:  # noqa: BLE001
        if logger is not None:
            logger.warning("Process %s did not exit after termination request", pid)


def open_in_explorer(path, logger=None):
    """Open ``path`` in the system file manager. Returns True on success."""
    try:
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
    except OSError as exc:
        if logger is not None:
            logger.error("Cannot create folder %s: %s", path, exc)
        return False

    try:
        if IS_WINDOWS:
            os.startfile(path)  # noqa: S606 - intended shell open of a local dir
        elif sys.platform == "darwin":  # pragma: no cover
            subprocess.Popen(["open", path])
        else:  # pragma: no cover
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as exc:  # noqa: BLE001
        if logger is not None:
            logger.error("Could not open folder %s: %s", path, exc)
        return False


#: Backwards-compatible private alias.
_decode = decode_bytes
