"""Entry point for SeparateAudio.

Works either as ``python -m App.main`` from the project root or as
``python App\\main.py``; the project root is added to ``sys.path`` so the
``App`` package resolves in both cases.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _write_crash_file(text):
    """Record a startup crash where the user can find it.

    Under pythonw.exe there is no console at all, and logging may not be up
    yet, so this is the only trace an early failure would otherwise leave.
    """
    try:
        import time

        logs = os.path.join(_ROOT, "Logs")
        os.makedirs(logs, exist_ok=True)
        name = "startup_error_%s.log" % time.strftime("%Y%m%d_%H%M%S")
        with open(os.path.join(logs, name), "w", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:  # noqa: BLE001
        pass


def _write_stderr(text):
    """Write to stderr only if there is one.

    launch.bat starts the app with pythonw.exe so no console window appears,
    and there sys.stderr is None - an unguarded write would turn every
    startup error into a silent crash.
    """
    try:
        if sys.stderr is not None:
            sys.stderr.write(text)
    except Exception:  # noqa: BLE001
        pass


def _report_startup_failure(exc):
    """Show a readable message when the GUI cannot even be created."""
    message = (
        "SeparateAudio could not start.\n\n%s: %s\n\n"
        "If this mentions tkinter, the Python being used has no Tk support. "
        "Launch the app with launch.bat so the correct environment is used."
        % (type(exc).__name__, exc)
    )
    _write_stderr(message + "\n")
    _write_crash_file(message)
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("SeparateAudio", message)
        root.destroy()
    except Exception:  # noqa: BLE001 - nothing more we can do
        pass


def main(argv=None):
    try:
        from App.gui import main as gui_main
    except Exception as exc:  # noqa: BLE001
        _report_startup_failure(exc)
        return 2

    try:
        return gui_main(argv)
    except Exception as exc:  # noqa: BLE001
        import traceback

        _write_stderr(traceback.format_exc())
        _write_crash_file(traceback.format_exc())
        try:
            from App import logging_setup

            logging_setup.get_logger().critical(
                "Fatal error in the GUI:\n%s", traceback.format_exc()
            )
        except Exception:  # noqa: BLE001
            pass
        _report_startup_failure(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
