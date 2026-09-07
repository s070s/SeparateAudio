"""Input scanning, Windows-safe naming and output directory resolution.

Input files are only ever read. Nothing in this module writes to, renames or
deletes anything inside the input folder.
"""

import os
import re
import shutil
import time

#: Container formats the app offers. Demucs decodes these through FFmpeg.
SUPPORTED_EXTENSIONS = (
    ".wav",
    ".mp3",
    ".flac",
    ".ogg",
    ".m4a",
    ".aac",
    ".opus",
    ".wma",
    ".aiff",
    ".aif",
    ".alac",
    ".mp4",
)

#: Formats that must never be re-encoded to a lossy intermediate.
LOSSLESS_EXTENSIONS = (".wav", ".flac", ".aiff", ".aif", ".alac")

_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DOTS_SPACES_RE = re.compile(r"[. ]+$")

#: Windows device names that cannot be used as a file or folder name.
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % i for i in range(1, 10)]
    + ["LPT%d" % i for i in range(1, 10)]
    + ["COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"]
)

#: Keeps the deepest stem path comfortably inside the 260-character limit.
MAX_FOLDER_NAME = 110


def sanitize_folder_name(name, fallback="untitled"):
    """Turn an arbitrary track title into a safe Windows folder name.

    Handles invalid characters, control codes, reserved device names, trailing
    dots and spaces, over-long names and names that collapse to nothing.
    """
    if name is None:
        name = ""
    cleaned = _INVALID_CHARS_RE.sub("_", str(name))
    cleaned = cleaned.replace("\t", " ").strip()
    cleaned = _DOTS_SPACES_RE.sub("", cleaned)

    if len(cleaned) > MAX_FOLDER_NAME:
        cleaned = cleaned[:MAX_FOLDER_NAME].rstrip()
        cleaned = _DOTS_SPACES_RE.sub("", cleaned)

    if not cleaned:
        return fallback

    # A reserved name is unusable even with an extension appended.
    stem = cleaned.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        cleaned = "_" + cleaned

    return cleaned or fallback


def is_supported(path):
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS


def is_lossless(path):
    return os.path.splitext(path)[1].lower() in LOSSLESS_EXTENSIONS


class AudioFile(object):
    """A scanned input file plus the display data the UI needs."""

    __slots__ = ("path", "name", "stem", "ext", "size", "mtime")

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.name = os.path.basename(self.path)
        self.stem, ext = os.path.splitext(self.name)
        self.ext = ext.lower()
        try:
            stat = os.stat(self.path)
            self.size = stat.st_size
            self.mtime = stat.st_mtime
        except OSError:
            self.size = 0
            self.mtime = 0.0

    @property
    def size_text(self):
        size = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024.0 or unit == "GB":
                return "%.1f %s" % (size, unit) if unit != "B" else "%d B" % self.size
            size /= 1024.0
        return "%d B" % self.size

    def display(self):
        return "%-58s  %10s" % (
            self.name if len(self.name) <= 58 else self.name[:55] + "...",
            self.size_text,
        )

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<AudioFile %r>" % self.path


def scan(input_dir, recursive=False):
    """Return ``(files, error)`` - supported audio files sorted by name."""
    if not input_dir:
        return [], "No input folder is configured."
    if not os.path.isdir(input_dir):
        return [], "Input folder does not exist: %s" % input_dir

    found = []
    try:
        if recursive:
            for root, dirs, names in os.walk(input_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in names:
                    full = os.path.join(root, name)
                    if is_supported(full) and os.path.isfile(full):
                        found.append(AudioFile(full))
        else:
            for name in os.listdir(input_dir):
                full = os.path.join(input_dir, name)
                if is_supported(full) and os.path.isfile(full):
                    found.append(AudioFile(full))
    except PermissionError:
        return [], "Permission denied reading %s" % input_dir
    except OSError as exc:
        return [], "Could not read %s (%s)" % (input_dir, exc)

    found.sort(key=lambda item: item.name.lower())
    return found, None


def timestamp_suffix():
    return time.strftime("%Y%m%d_%H%M%S")


def directory_has_content(path):
    """True when ``path`` exists and contains at least one entry."""
    if not os.path.isdir(path):
        return False
    try:
        return any(True for _ in os.scandir(path))
    except OSError:
        return True  # assume occupied rather than risk clobbering


def versioned_dir(base_dir):
    """Return an unused sibling of ``base_dir`` with a timestamp suffix."""
    candidate = "%s_%s" % (base_dir, timestamp_suffix())
    counter = 2
    while os.path.exists(candidate):
        candidate = "%s_%s_%d" % (base_dir, timestamp_suffix(), counter)
        counter += 1
    return candidate


def deduplicated_dir(base_dir, claimed):
    """Return a folder name not already claimed by another file in this batch.

    Two different inputs can sanitise to the same folder - most obviously
    ``rock\\intro.wav`` and ``jazz\\intro.wav`` when subfolders are included,
    but also names that differ only in characters the sanitiser replaces.
    Without this they would overwrite each other's stems.
    """
    if os.path.normcase(base_dir) not in claimed:
        return base_dir
    counter = 2
    while True:
        candidate = "%s (%d)" % (base_dir, counter)
        if os.path.normcase(candidate) not in claimed and not os.path.exists(candidate):
            return candidate
        counter += 1


def target_dir_for(output_dir, audio_path):
    """The default (non-versioned) output folder for an input file."""
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    return os.path.join(output_dir, sanitize_folder_name(stem))


def is_inside(child, parent):
    """True when ``child`` is ``parent`` itself or lives underneath it.

    Used to refuse an output folder that sits inside the input folder, which
    would let the overwrite policy touch the user's original recordings.
    """
    try:
        child_n = os.path.normcase(os.path.abspath(child))
        parent_n = os.path.normcase(os.path.abspath(parent))
    except (OSError, ValueError):
        return False
    if child_n == parent_n:
        return True
    return child_n.startswith(parent_n.rstrip("\\/") + os.sep)


def free_space_mb(path):
    """Free space on the volume holding ``path``, in MB (-1 if unknown)."""
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free // (1024 * 1024)
    except OSError:
        return -1


def validate_input(audio_path, min_bytes=1024):
    """Return an error string if the file cannot be processed, else ``None``."""
    if not audio_path:
        return "No file given."
    if not os.path.exists(audio_path):
        return "File no longer exists."
    if not os.path.isfile(audio_path):
        return "Not a file."
    if not is_supported(audio_path):
        return "Unsupported format (%s)." % (os.path.splitext(audio_path)[1] or "no extension")
    try:
        size = os.path.getsize(audio_path)
    except OSError as exc:
        return "Could not read the file (%s)." % exc
    if size < min_bytes:
        return "File is empty or too small to be valid audio (%d bytes)." % size
    try:
        with open(audio_path, "rb") as handle:
            handle.read(1)
    except PermissionError:
        return "Permission denied - the file may be open in another program."
    except OSError as exc:
        return "Could not read the file (%s)." % exc
    return None


def find_stems(search_root):
    """Recursively collect ``.wav`` files produced by Demucs under a folder."""
    results = []
    for root, _dirs, names in os.walk(search_root):
        for name in names:
            if name.lower().endswith(".wav"):
                results.append(os.path.join(root, name))
    results.sort()
    return results


def find_any_audio(search_root):
    """Every audio file under a folder, whatever the extension.

    Used to tell "Demucs produced nothing" apart from "Demucs produced
    something we did not expect", so unexpected output is reported and kept
    rather than silently discarded.
    """
    results = []
    for root, _dirs, names in os.walk(search_root):
        for name in names:
            if is_supported(name):
                results.append(os.path.join(root, name))
    results.sort()
    return results


def move_into(sources, destination, logger=None):
    """Move files into ``destination``, falling back to copy across volumes.

    An existing file at the destination is only ever removed once its
    replacement is fully written, so a failure part-way through can never
    leave the user with neither the old nor the new stem.

    Returns ``(moved_paths, errors)``.
    """
    moved = []
    errors = []
    os.makedirs(destination, exist_ok=True)
    for source in sources:
        target = os.path.join(destination, os.path.basename(source))
        try:
            # os.replace is atomic on the same volume and overwrites in place.
            os.replace(source, target)
            moved.append(target)
            continue
        except OSError as exc:
            move_error = exc

        # Different volume (or the rename was refused): copy to a sibling
        # temporary file first, then swap it in atomically.
        staged = target + ".incoming"
        try:
            shutil.copy2(source, staged)
            os.replace(staged, target)
            moved.append(target)
            if logger is not None:
                logger.debug("Copied instead of moved: %s (%s)", source, move_error)
            try:
                os.remove(source)
            except OSError:
                pass
        except OSError as copy_exc:
            try:
                if os.path.exists(staged):
                    os.remove(staged)
            except OSError:
                pass
            errors.append("%s: %s" % (os.path.basename(source), copy_exc))
            if logger is not None:
                logger.error("Could not place %s into %s: %s", source, destination, copy_exc)
    return moved, errors


def verify_stems(directory, expected, min_bytes=1024):
    """Check that the expected stems exist and are non-empty.

    Returns ``(ok, missing, empty, extra)``.
    """
    try:
        present = {
            name.lower(): os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.lower().endswith(".wav")
        }
    except OSError:
        # Keep the same ".wav" naming the success path uses so callers can
        # render one consistent message.
        return False, ["%s.wav" % stem for stem in expected], [], []

    missing = []
    empty = []
    for stem in expected:
        filename = "%s.wav" % stem
        path = present.get(filename)
        if not path:
            missing.append(filename)
            continue
        try:
            if os.path.getsize(path) < min_bytes:
                empty.append(filename)
        except OSError:
            empty.append(filename)

    expected_files = {"%s.wav" % stem for stem in expected}
    extra = sorted(name for name in present if name not in expected_files)
    return (not missing and not empty), missing, empty, extra
