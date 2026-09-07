"""Environment probe executed *inside* the Conda environment.

Run as::

    <env>\\python.exe _probe.py <model-name>

It prints a single JSON object describing the interpreter, Demucs, PyTorch,
CUDA, Tkinter, FFmpeg and whether the requested model weights are already
cached. It must not import anything from the ``App`` package, and it must
never raise: every probe is individually guarded so one missing component
still yields a useful report.
"""

import json
import os
import shutil
import sys

RESULT = {}


def _try(key, func, default=None):
    try:
        RESULT[key] = func()
    except Exception as exc:  # noqa: BLE001 - a failed probe is data, not a crash
        RESULT[key] = default
        RESULT.setdefault("errors", {})[key] = "%s: %s" % (type(exc).__name__, exc)


def _demucs_version():
    try:
        from importlib.metadata import version

        return version("demucs")
    except Exception:  # noqa: BLE001
        import demucs

        return getattr(demucs, "__version__", "unknown")


def _demucs_entry_module():
    """Return the module name usable with ``python -m`` for separation."""
    import importlib.util

    for candidate in ("demucs.separate", "demucs"):
        try:
            if importlib.util.find_spec(candidate) is not None:
                return candidate
        except Exception:  # noqa: BLE001
            continue
    return None


def _torch_info():
    import torch

    info = {
        "version": torch.__version__,
        "cuda_build": getattr(torch.version, "cuda", None),
        "cuda_available": False,
        "devices": [],
        "cuda_error": None,
    }
    try:
        info["cuda_available"] = bool(torch.cuda.is_available())
        if info["cuda_available"]:
            for index in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(index)
                info["devices"].append(
                    {
                        "index": index,
                        "name": props.name,
                        "total_memory_mb": int(props.total_memory / (1024 * 1024)),
                    }
                )
    except Exception as exc:  # noqa: BLE001 - a broken driver must not be fatal
        info["cuda_available"] = False
        info["cuda_error"] = "%s: %s" % (type(exc).__name__, exc)
    return info


def _hub_checkpoint_dir():
    try:
        import torch

        return os.path.join(torch.hub.get_dir(), "checkpoints")
    except Exception:  # noqa: BLE001
        return os.path.join(
            os.path.expanduser("~"), ".cache", "torch", "hub", "checkpoints"
        )


def _model_signatures(model):
    """Return the checkpoint signatures a (possibly bagged) model needs."""
    import demucs

    remote_dir = os.path.join(os.path.dirname(demucs.__file__), "remote")
    yaml_path = os.path.join(remote_dir, model + ".yaml")
    if not os.path.isfile(yaml_path):
        return None

    signatures = []
    in_models = False
    with open(yaml_path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("models:"):
                in_models = True
                inline = stripped[len("models:"):].strip()
                if inline.startswith("[") and inline.endswith("]"):
                    body = inline[1:-1]
                    signatures = [
                        part.strip().strip("'\"") for part in body.split(",") if part.strip()
                    ]
                    in_models = False
                continue
            if in_models:
                if stripped.startswith("- "):
                    signatures.append(stripped[2:].strip().strip("'\""))
                elif not line.startswith((" ", "\t")):
                    in_models = False
    return signatures or None


def _available_models():
    """Every model name Demucs ships a spec for, for error messages."""
    try:
        import demucs

        remote_dir = os.path.join(os.path.dirname(demucs.__file__), "remote")
        return sorted(
            name[:-5] for name in os.listdir(remote_dir) if name.endswith(".yaml")
        )
    except Exception:  # noqa: BLE001
        return []


def _model_status(model):
    """Report whether the model is known and whether its weights are cached."""
    status = {
        "name": model,
        "known": None,
        "cached": None,
        "signatures": [],
        "missing": [],
        "checkpoint_dir": _hub_checkpoint_dir(),
        "available": _available_models(),
    }
    try:
        signatures = _model_signatures(model)
    except Exception as exc:  # noqa: BLE001
        status["error"] = "%s: %s" % (type(exc).__name__, exc)
        return status

    if signatures is None:
        # No spec file. Demucs also accepts a bare checkpoint signature, so
        # treat the name as valid only if a matching checkpoint is cached;
        # otherwise it is a typo and should be reported as such rather than
        # failing later with a confusing Demucs error.
        try:
            present = os.listdir(status["checkpoint_dir"])
        except OSError:
            present = []
        looks_like_signature = any(
            name.startswith(model + "-") or name == model + ".th" for name in present
        )
        status["known"] = bool(looks_like_signature)
        status["cached"] = bool(looks_like_signature)
        return status

    status["known"] = True
    status["signatures"] = signatures

    checkpoint_dir = status["checkpoint_dir"]
    try:
        present = os.listdir(checkpoint_dir)
    except OSError:
        present = []

    missing = []
    for signature in signatures:
        if not any(name.startswith(signature + "-") or name == signature + ".th" for name in present):
            missing.append(signature)
    status["missing"] = missing
    status["cached"] = not missing
    return status


def _ffmpeg():
    info = {"path": None, "version": None}
    exe = shutil.which("ffmpeg")
    if not exe:
        # Conda places binaries in Library\bin, which may not be on PATH when
        # the interpreter is invoked directly rather than through `activate`.
        candidate = os.path.join(sys.prefix, "Library", "bin", "ffmpeg.exe")
        if os.path.isfile(candidate):
            exe = candidate
    if not exe:
        return info

    info["path"] = exe
    try:
        import subprocess

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.run(
            [exe, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=30,
            creationflags=creationflags,
        )
        first = proc.stdout.decode("utf-8", errors="replace").splitlines()
        if first:
            info["version"] = first[0].strip()
    except Exception as exc:  # noqa: BLE001
        info["version"] = "unavailable (%s)" % exc
    return info


def _audio_backends():
    backends = {}
    for module in ("torchaudio", "soundfile", "julius", "einops", "lameenc", "openunmix"):
        try:
            __import__(module)
            from importlib.metadata import version

            try:
                backends[module] = version(module)
            except Exception:  # noqa: BLE001
                backends[module] = "installed"
        except Exception:  # noqa: BLE001
            backends[module] = None
    return backends


def main():
    # This script is run with -I (isolated), so PYTHONIOENCODING is ignored.
    # Force UTF-8 explicitly: GPU names and paths can contain non-ASCII text
    # that the console code page cannot encode.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - older streams simply keep their encoding
            pass

    model = sys.argv[1] if len(sys.argv) > 1 else "htdemucs_ft"

    RESULT["python_version"] = "%d.%d.%d" % sys.version_info[:3]
    RESULT["executable"] = sys.executable
    RESULT["prefix"] = sys.prefix

    _try("demucs_version", _demucs_version)
    _try("demucs_entry", _demucs_entry_module)
    _try("torch", _torch_info, {})
    _try("tkinter_version", lambda: str(__import__("tkinter").TkVersion))
    _try("ffmpeg", _ffmpeg, {})
    _try("model", lambda: _model_status(model), {})
    _try("backends", _audio_backends, {})

    sys.stdout.write("<<<PROBE>>>" + json.dumps(RESULT) + "<<<END>>>\n")

    # Exit non-zero when something essential is missing, so batch scripts can
    # test errorlevel instead of having to parse the JSON. The app itself
    # reads the JSON and does not depend on this code.
    missing = []
    if not RESULT.get("demucs_version"):
        missing.append("demucs")
    if not (RESULT.get("torch") or {}).get("version"):
        missing.append("torch")
    if not (RESULT.get("ffmpeg") or {}).get("path"):
        missing.append("ffmpeg")
    if RESULT.get("model", {}).get("known") is False:
        missing.append("model '%s'" % model)
    if missing:
        sys.stderr.write("MISSING: %s\n" % ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
