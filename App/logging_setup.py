"""Run logging for SeparateAudio.

One log file is created per launch in ``Logs\\`` and older ones are pruned so
the folder cannot grow without bound. A second, in-memory handler feeds short
human-readable lines to the GUI activity pane.
"""

import logging
import os
import re
import sys
import time

from App import paths

LOGGER_NAME = "separateaudio"

_FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(threadName)-14s | %(name)s | %(message)s"
_UI_FORMAT = "%(asctime)s  %(message)s"

_RUN_LOG_RE = re.compile(r"^separateaudio_\d{8}_\d{6}(?:_\d+)?\.log$", re.IGNORECASE)


def _warn(text):
    """Write to stderr when there is one (pythonw.exe leaves it as None)."""
    try:
        if sys.stderr is not None:
            sys.stderr.write(text + "\n")
    except Exception:  # noqa: BLE001
        pass


class CallbackHandler(logging.Handler):
    """Forward formatted records to a callable (used by the GUI log pane)."""

    def __init__(self, callback, level=logging.INFO):
        super().__init__(level=level)
        self._callback = callback
        self.setFormatter(logging.Formatter(_UI_FORMAT, datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            text = self.format(record)
            # Tracebacks belong in the log file, not in the user's face. Show
            # the headline and point at the log for the rest.
            first, separator, _rest = text.partition("\n")
            if separator:
                text = first.rstrip() + "  (full details in the log file)"
            self._callback(text, record.levelno)
        except Exception:  # noqa: BLE001 - logging must never break the app
            pass


def prune_old_logs(logs_dir, keep, logger=None):
    """Delete the oldest run logs, keeping the ``keep`` most recent ones.

    Only files matching the app's own naming pattern are ever considered, so
    unrelated files dropped into ``Logs\\`` are never touched.
    """
    try:
        entries = []
        for name in os.listdir(logs_dir):
            if not _RUN_LOG_RE.match(name):
                continue
            full = os.path.join(logs_dir, name)
            if os.path.isfile(full):
                entries.append((os.path.getmtime(full), full))
    except OSError:
        return

    entries.sort(reverse=True)
    for _, full in entries[max(1, int(keep)):]:
        try:
            os.remove(full)
        except OSError as exc:
            if logger is not None:
                logger.debug("Could not prune old log %s: %s", full, exc)


def _unique_log_path(logs_dir):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = os.path.join(logs_dir, "separateaudio_%s.log" % stamp)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(logs_dir, "separateaudio_%s_%d.log" % (stamp, counter))
        counter += 1
    return candidate


def setup(logs_dir=None, keep_logs=30, level=logging.DEBUG):
    """Configure and return ``(logger, log_path)``.

    If the log directory cannot be written the logger still works (console
    only) and ``log_path`` is ``None`` - logging problems must not stop the
    user from separating audio.
    """
    logs_dir = logs_dir or paths.LOGS_DIR
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass

    log_path = None
    try:
        os.makedirs(logs_dir, exist_ok=True)
        log_path = _unique_log_path(logs_dir)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        logger.addHandler(file_handler)
    except OSError as exc:
        log_path = None
        _warn("SeparateAudio: cannot write logs to %s (%s)" % (logs_dir, exc))

    # Under pythonw.exe - which launch.bat uses so no console window appears -
    # sys.stderr is None. Attaching a StreamHandler to it would make every
    # single log record raise internally.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.INFO)
        stream.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(stream)

    if log_path:
        prune_old_logs(logs_dir, keep_logs, logger)

    return logger, log_path


def get_logger():
    return logging.getLogger(LOGGER_NAME)


def attach_ui_handler(callback, level=logging.INFO):
    """Attach a :class:`CallbackHandler` to the app logger and return it."""
    handler = CallbackHandler(callback, level=level)
    get_logger().addHandler(handler)
    return handler
