"""Loading and saving of ``config.json``.

The file is optional: if it is missing or unreadable the app falls back to the
documented defaults and keeps running, so a corrupt config can never prevent
startup.
"""

import json
import os
import shutil
import tempfile
import time

from App import paths

#: Values written when no configuration exists yet.
DEFAULTS = {
    "input_dir": paths.INPUT_DIR,
    "output_dir": paths.OUTPUT_DIR,
    "model": "htdemucs_ft",
    "device": "auto",
    "overwrite_policy": "prompt",
}

#: Optional keys the app understands but does not require.
OPTIONAL_DEFAULTS = {
    # Explicit Conda root / environment python. Empty means "auto-detect".
    "conda_root": "",
    "env_python": "",
    "env_name": paths.ENV_NAME,
    # How many run logs to keep in Logs\ before the oldest are pruned.
    "keep_logs": 30,
    # Extra CLI arguments appended to every Demucs invocation.
    "extra_demucs_args": [],
    # Remembered window geometry, e.g. "1000x760+120+60".
    "window_geometry": "",
}

VALID_DEVICES = ("auto", "cpu", "cuda")
VALID_POLICIES = ("prompt", "skip", "overwrite", "rename")

#: Models offered in the UI. Any Demucs model name may be typed in config.json.
KNOWN_MODELS = (
    "htdemucs_ft",
    "htdemucs",
    "htdemucs_6s",
    "hdemucs_mmi",
    "mdx",
    "mdx_extra",
    "mdx_q",
    "mdx_extra_q",
)

#: Models that emit six stems instead of the standard four.
SIX_STEM_MODELS = ("htdemucs_6s",)

#: Number of internal passes a bagged model performs, used for progress maths.
MODEL_PASSES = {
    "htdemucs_ft": 4,
    "mdx": 4,
    "mdx_extra": 4,
    "mdx_q": 4,
    "mdx_extra_q": 4,
}

STANDARD_STEMS = ("vocals", "drums", "bass", "other")
SIX_STEMS = ("vocals", "drums", "bass", "other", "guitar", "piano")


def two_stems_target(extra_args):
    """Return the stem named by a ``--two-stems`` argument, or ``None``.

    Demucs accepts both ``--two-stems vocals`` and ``--two-stems=vocals`` and,
    being plain argparse, lets the last one win when it is repeated. Values are
    lower-cased because Demucs matches them against the model's own source
    names, which are always lower case.
    """
    if not extra_args:
        return None

    found = None
    arguments = list(extra_args)
    for position, argument in enumerate(arguments):
        if argument == "--two-stems":
            if position + 1 < len(arguments):
                found = arguments[position + 1].strip().lower() or found
        elif argument.startswith("--two-stems="):
            found = argument.split("=", 1)[1].strip().lower() or found
    return found


def expected_stems(model, extra_args=None):
    """Return the stem names a given model is expected to produce.

    ``--two-stems=vocals`` makes Demucs emit ``vocals`` and ``no_vocals``
    instead of the model's full set, so the expected list has to follow it.
    Without this the run produces perfectly good audio and is then reported as
    failed for "missing" the stems that were never going to be written.
    """
    target = two_stems_target(extra_args)
    if target:
        return (target, "no_%s" % target)
    return SIX_STEMS if model in SIX_STEM_MODELS else STANDARD_STEMS


def expected_passes(model):
    """Return how many separation passes ``model`` runs (bagged models > 1)."""
    return MODEL_PASSES.get(model, 1)


def default_config():
    cfg = dict(DEFAULTS)
    cfg.update(OPTIONAL_DEFAULTS)
    return cfg


def _coerce(cfg, logger=None):
    """Repair obviously invalid values in-place and report what was changed."""
    fixed = []

    for key in ("input_dir", "output_dir"):
        value = cfg.get(key)
        if not isinstance(value, str) or not value.strip():
            cfg[key] = DEFAULTS[key]
            fixed.append(key)
        else:
            cfg[key] = os.path.abspath(os.path.expandvars(os.path.expanduser(value.strip())))

    model = cfg.get("model")
    if not isinstance(model, str) or not model.strip():
        cfg["model"] = DEFAULTS["model"]
        fixed.append("model")
    else:
        cfg["model"] = model.strip()

    device = str(cfg.get("device", "")).strip().lower()
    if device not in VALID_DEVICES:
        cfg["device"] = DEFAULTS["device"]
        fixed.append("device")
    else:
        cfg["device"] = device

    policy = str(cfg.get("overwrite_policy", "")).strip().lower()
    if policy not in VALID_POLICIES:
        cfg["overwrite_policy"] = DEFAULTS["overwrite_policy"]
        fixed.append("overwrite_policy")
    else:
        cfg["overwrite_policy"] = policy

    for key in ("conda_root", "env_python", "env_name", "window_geometry"):
        value = cfg.get(key, OPTIONAL_DEFAULTS[key])
        cfg[key] = value.strip() if isinstance(value, str) else OPTIONAL_DEFAULTS[key]
    if not cfg["env_name"]:
        cfg["env_name"] = paths.ENV_NAME

    try:
        cfg["keep_logs"] = max(1, int(cfg.get("keep_logs", 30)))
    except (TypeError, ValueError):
        cfg["keep_logs"] = OPTIONAL_DEFAULTS["keep_logs"]
        fixed.append("keep_logs")

    extra = cfg.get("extra_demucs_args", [])
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        cfg["extra_demucs_args"] = []
        fixed.append("extra_demucs_args")

    if fixed and logger is not None:
        logger.warning("config.json contained invalid values; reset: %s", ", ".join(fixed))
    return cfg


def _rebase_moved_dirs(cfg, logger=None):
    """Re-point Input/Output at this copy of the project after a move.

    ``config.json`` stores absolute paths, so copying the whole folder to
    another PC (or another drive) would otherwise leave it pointing at the
    original machine's folders. Only a path that looks like it belonged to a
    *different* SeparateAudio installation is re-based - a deliberate custom
    folder such as ``D:\\Stems`` is left exactly as the user set it, even if
    it does not exist yet.
    """
    for key, default in (("input_dir", paths.INPUT_DIR), ("output_dir", paths.OUTPUT_DIR)):
        value = cfg.get(key)
        if not value:
            continue

        trimmed = value.rstrip("\\/")
        parent = os.path.dirname(trimmed)
        if os.path.basename(trimmed).lower() not in ("input", "output"):
            continue  # a folder the user named themselves
        if os.path.normcase(parent) == os.path.normcase(paths.ROOT_DIR):
            continue  # already ours

        # It is named like one of our folders but sits under a different root.
        # Only re-base when that root really is another copy of this project:
        # either it still exists and contains the app, or it is gone but was
        # named the same as this project folder. Anything else - say a user's
        # own D:\Music\Input - is left alone even if it does not exist yet.
        other_install = os.path.isfile(os.path.join(parent, "App", "main.py"))
        looks_like_our_project = (
            not os.path.isdir(value)
            and os.path.basename(parent).lower() == os.path.basename(paths.ROOT_DIR).lower()
        )
        if not (other_install or looks_like_our_project):
            continue

        cfg[key] = default
        if logger is not None:
            logger.warning(
                "%s pointed at %s, which belongs to %s - this project has been "
                "moved or copied. Using %s instead.",
                key, value,
                "another SeparateAudio installation" if other_install else "a location that does not exist here",
                default,
            )
    return cfg


def load(path=None, logger=None):
    """Return the configuration dict, merging ``config.json`` over defaults."""
    path = path or paths.CONFIG_PATH
    cfg = default_config()

    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                cfg.update(data)
            else:
                raise ValueError("top-level JSON value is not an object")
            if logger is not None:
                logger.info("Loaded configuration from %s", path)
        except Exception as exc:  # noqa: BLE001 - never fail startup on config
            backup = "%s.corrupt-%s.bak" % (path, time.strftime("%Y%m%d_%H%M%S"))
            try:
                shutil.copy2(path, backup)
            except OSError:
                backup = "(backup failed)"
            if logger is not None:
                logger.error(
                    "Could not read %s (%s); using defaults. Original saved as %s",
                    path, exc, backup,
                )
    elif logger is not None:
        logger.info("No config.json found at %s; using defaults", path)

    return _rebase_moved_dirs(_coerce(cfg, logger), logger)


def save(cfg, path=None, logger=None):
    """Write the configuration atomically. Returns True on success."""
    path = path or paths.CONFIG_PATH
    payload = dict(cfg)
    # Keep the documented five keys first for readability.
    ordered = {key: payload.pop(key) for key in DEFAULTS if key in payload}
    ordered.update(payload)

    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp_name = None
    try:
        os.makedirs(directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=directory, prefix=".config-", suffix=".tmp"
        )
        tmp_name = handle.name
        try:
            json.dump(ordered, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(tmp_name, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        if logger is not None:
            logger.error("Could not save configuration to %s: %s", path, exc)
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return False
