"""Filesystem locations used by SeparateAudio.

Everything is derived from the project root (the parent of this ``App``
package) so the whole tree can be moved without editing code.
"""

import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(APP_DIR)

INPUT_DIR = os.path.join(ROOT_DIR, "Input")
OUTPUT_DIR = os.path.join(ROOT_DIR, "Output")
LOGS_DIR = os.path.join(ROOT_DIR, "Logs")
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")

#: Name of the scratch folder created *inside* the output directory so that
#: moving finished stems into place is a same-volume rename rather than a copy.
WORK_DIRNAME = ".separateaudio_work"

#: Name of the Conda environment the app expects to run Demucs from.
ENV_NAME = "separateaudio"


def ensure_dirs(*extra):
    """Create the standard project directories (and any extras) if missing.

    Returns a list of the directories that could not be created, as
    ``(path, error_message)`` tuples. An empty list means everything is ready.
    """
    failures = []
    for path in (APP_DIR, INPUT_DIR, OUTPUT_DIR, LOGS_DIR) + tuple(extra):
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            failures.append((path, str(exc)))
    return failures


def work_dir_for(output_dir):
    """Return the scratch workspace path that belongs to ``output_dir``."""
    return os.path.join(output_dir, WORK_DIRNAME)
