"""Discovery and validation of the Conda environment that runs Demucs.

Nothing here assumes a fixed Anaconda path. Conda roots are discovered from
the environment, ``PATH``, ``~/.conda/environments.txt``, the Windows registry
and the usual install locations, in that order of trust.
"""

import glob
import json
import os
import re
import sys

from App import paths, procutil

#: Directories Anaconda / Miniconda / Miniforge are typically installed into.
_COMMON_ROOT_TEMPLATES = (
    r"{userprofile}\anaconda3",
    r"{userprofile}\Anaconda3",
    r"{userprofile}\miniconda3",
    r"{userprofile}\Miniconda3",
    r"{userprofile}\miniforge3",
    r"{userprofile}\mambaforge",
    r"{localappdata}\anaconda3",
    r"{localappdata}\miniconda3",
    r"{localappdata}\Continuum\anaconda3",
    r"{programdata}\anaconda3",
    r"{programdata}\Anaconda3",
    r"{programdata}\miniconda3",
    r"{programdata}\Miniconda3",
    r"C:\anaconda3",
    r"C:\Anaconda3",
    r"C:\miniconda3",
    r"C:\Miniconda3",
    r"{programfiles}\Anaconda3",
)


class EnvironmentReport(object):
    """Structured result of a full environment probe."""

    def __init__(self):
        #: True only after a probe actually inspected the environment. A
        #: report that never ran must never be treated as usable.
        self.probed = False
        self.conda_root = None
        self.conda_exe = None
        self.env_dir = None
        self.env_python = None
        self.env_name = paths.ENV_NAME
        self.python_version = None
        self.demucs_version = None
        self.demucs_entry = None
        self.torch_version = None
        self.cuda_build = None
        self.cuda_available = False
        self.cuda_devices = []
        self.cuda_error = None
        self.tkinter_version = None
        self.ffmpeg_path = None
        self.ffmpeg_version = None
        self.model_name = None
        self.model_known = None
        self.model_cached = None
        self.model_missing = []
        self.available_models = []
        self.checkpoint_dir = None
        self.backends = {}
        self.probe_errors = {}
        self.problems = []
        self.notes = []

    # -- derived state ---------------------------------------------------
    @property
    def ready(self):
        """True when a separation can actually be started."""
        return self.probed and not self.problems

    def resolve_device(self, requested):
        """Map the user's device choice onto a concrete Demucs ``-d`` value."""
        requested = (requested or "auto").lower()
        if requested == "cuda":
            return "cuda" if self.cuda_available else "cpu"
        if requested == "cpu":
            return "cpu"
        return "cuda" if self.cuda_available else "cpu"

    def summary_line(self):
        if self.problems:
            return "Not ready - %s" % self.problems[0]
        device = "CUDA (%s)" % self.cuda_devices[0]["name"] if self.cuda_devices else "CPU only"
        return "Ready - Python %s, Demucs %s, FFmpeg %s, %s" % (
            self.python_version or "?",
            self.demucs_version or "?",
            "yes" if self.ffmpeg_path else "missing",
            device,
        )

    def details_text(self):
        lines = [
            "Conda root      : %s" % (self.conda_root or "not found"),
            "Conda executable: %s" % (self.conda_exe or "not found"),
            "Environment     : %s" % (self.env_dir or "not found"),
            "Python          : %s (%s)" % (self.python_version or "?", self.env_python or "?"),
            "Demucs          : %s (entry point: %s)"
            % (self.demucs_version or "not installed", self.demucs_entry or "?"),
            "PyTorch         : %s (built for CUDA %s)"
            % (self.torch_version or "not installed", self.cuda_build or "n/a"),
            "CUDA available  : %s" % ("yes" if self.cuda_available else "no"),
        ]
        for device in self.cuda_devices:
            lines.append(
                "  GPU %d         : %s (%d MB)"
                % (device["index"], device["name"], device["total_memory_mb"])
            )
        if self.cuda_error:
            lines.append("CUDA note       : %s" % self.cuda_error)
        lines += [
            "FFmpeg          : %s" % (self.ffmpeg_version or "not found"),
            "FFmpeg path     : %s" % (self.ffmpeg_path or "-"),
            "Tkinter (env)   : %s" % (self.tkinter_version or "not available"),
            "Model           : %s" % (self.model_name or "?"),
            "Model weights   : %s"
            % (
                "cached"
                if self.model_cached
                else ("will download on first use" if self.model_cached is False else "unknown")
            ),
            "Checkpoint dir  : %s" % (self.checkpoint_dir or "-"),
        ]
        if self.model_missing:
            lines.append("Missing weights : %s" % ", ".join(self.model_missing))
        if self.backends:
            installed = ", ".join(
                "%s %s" % (name, ver) for name, ver in sorted(self.backends.items()) if ver
            )
            lines.append("Audio backends  : %s" % (installed or "none"))
        if self.notes:
            lines.append("")
            lines.append("Notes:")
            lines.extend("  - %s" % note for note in self.notes)
        if self.problems:
            lines.append("")
            lines.append("Problems:")
            lines.extend("  - %s" % problem for problem in self.problems)
        if self.probe_errors:
            lines.append("")
            lines.append("Probe diagnostics:")
            lines.extend("  %s: %s" % (key, value) for key, value in self.probe_errors.items())
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Conda discovery
# ---------------------------------------------------------------------------

def _expand(template):
    return template.format(
        userprofile=os.environ.get("USERPROFILE", os.path.expanduser("~")),
        localappdata=os.environ.get("LOCALAPPDATA", ""),
        programdata=os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
        programfiles=os.environ.get("PROGRAMFILES", r"C:\Program Files"),
    )


def _is_conda_root(path):
    if not path or not os.path.isdir(path):
        return False
    markers = (
        os.path.join(path, "Scripts", "conda.exe"),
        os.path.join(path, "condabin", "conda.bat"),
        os.path.join(path, "_conda.exe"),
        os.path.join(path, "bin", "conda"),
    )
    return any(os.path.exists(marker) for marker in markers)


def _registry_roots():
    roots = []
    if os.name != "nt":
        return roots
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return roots

    keys = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Python\ContinuumAnalytics"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Python\ContinuumAnalytics"),
    ]
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as handle:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(handle, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(handle, name + r"\InstallPath") as install:
                            value, _ = winreg.QueryValueEx(install, "")
                            if value:
                                roots.append(os.path.normpath(value))
                    except OSError:
                        continue
        except OSError:
            continue
    return roots


def _environments_txt_roots():
    """Read ``~/.conda/environments.txt`` - conda's own registry of prefixes."""
    found = []
    listing = os.path.join(os.path.expanduser("~"), ".conda", "environments.txt")
    try:
        with open(listing, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                prefix = line.strip()
                if not prefix or not os.path.isdir(prefix):
                    continue
                found.append(os.path.normpath(prefix))
    except OSError:
        pass
    return found


def discover_conda_roots(explicit=None):
    """Return candidate Conda installation roots, most trustworthy first."""
    candidates = []

    def add(path):
        if not path:
            return
        path = os.path.normpath(os.path.abspath(path))
        if path not in candidates:
            candidates.append(path)

    add(explicit)

    conda_exe = os.environ.get("CONDA_EXE")
    if conda_exe and os.path.isfile(conda_exe):
        # <root>\Scripts\conda.exe or <root>\condabin\conda.bat
        add(os.path.dirname(os.path.dirname(conda_exe)))

    for var in ("CONDA_ROOT", "CONDA_PREFIX_1", "_CONDA_ROOT"):
        add(os.environ.get(var))

    prefix = os.environ.get("CONDA_PREFIX")
    if prefix:
        add(prefix)
        # If we are inside a named env, the root is two levels up.
        parent = os.path.dirname(prefix)
        if os.path.basename(parent).lower() == "envs":
            add(os.path.dirname(parent))

    import shutil as _shutil

    for exe_name in ("conda.exe", "conda.bat", "conda"):
        located = _shutil.which(exe_name)
        if located:
            add(os.path.dirname(os.path.dirname(located)))

    for prefix in _environments_txt_roots():
        parent = os.path.dirname(prefix)
        if os.path.basename(parent).lower() == "envs":
            add(os.path.dirname(parent))
        else:
            add(prefix)

    for root in _registry_roots():
        add(root)

    for template in _COMMON_ROOT_TEMPLATES:
        add(_expand(template))

    return [path for path in candidates if _is_conda_root(path)]


def conda_executable(root):
    """Return a runnable conda entry point inside ``root``, or ``None``."""
    for relative in (
        os.path.join("Scripts", "conda.exe"),
        os.path.join("condabin", "conda.bat"),
        "_conda.exe",
        os.path.join("bin", "conda"),
    ):
        candidate = os.path.join(root, relative)
        if os.path.isfile(candidate):
            return candidate
    return None


def env_python_path(env_dir):
    """Return the interpreter inside a Conda env directory, if present."""
    for relative in ("python.exe", os.path.join("bin", "python")):
        candidate = os.path.join(env_dir, relative)
        if os.path.isfile(candidate):
            return candidate
    return None


def find_env(env_name=paths.ENV_NAME, explicit_root=None, explicit_python=None, notes=None):
    """Locate the target environment.

    Returns ``(conda_root, conda_exe, env_dir, env_python)`` with ``None`` for
    anything that could not be found. Anything overridden in config.json that
    turns out not to exist is reported through ``notes`` rather than silently
    ignored.
    """
    if explicit_python:
        if os.path.isfile(explicit_python):
            env_dir = os.path.dirname(explicit_python)
            root = None
            parent = os.path.dirname(env_dir)
            if os.path.basename(parent).lower() == "envs":
                root = os.path.dirname(parent)
            return root, (conda_executable(root) if root else None), env_dir, explicit_python
        if notes is not None:
            notes.append(
                "config.json sets env_python to '%s', which does not exist. "
                "Falling back to automatic detection." % explicit_python
            )

    if explicit_root and not _is_conda_root(explicit_root) and notes is not None:
        notes.append(
            "config.json sets conda_root to '%s', which is not a Conda "
            "installation. Falling back to automatic detection." % explicit_root
        )

    roots = discover_conda_roots(explicit_root)

    # 1. Direct hit: <root>\envs\<env_name>
    for root in roots:
        env_dir = os.path.join(root, "envs", env_name)
        python = env_python_path(env_dir)
        if python:
            return root, conda_executable(root), env_dir, python

    # 2. Any prefix registered in environments.txt with a matching basename.
    for prefix in _environments_txt_roots():
        if os.path.basename(prefix).lower() != env_name.lower():
            continue
        python = env_python_path(prefix)
        if python:
            parent = os.path.dirname(prefix)
            root = os.path.dirname(parent) if os.path.basename(parent).lower() == "envs" else None
            return root, (conda_executable(root) if root else None), prefix, python

    # 3. Ask conda itself (slowest, so it comes last).
    for root in roots:
        exe = conda_executable(root)
        if not exe:
            continue
        code, out, _ = procutil.run_capture([exe, "env", "list", "--json"], timeout=90)
        if code != 0:
            continue
        try:
            prefixes = json.loads(out).get("envs", [])
        except (ValueError, AttributeError):
            continue
        for prefix in prefixes:
            if os.path.basename(prefix).lower() == env_name.lower():
                python = env_python_path(prefix)
                if python:
                    return root, exe, prefix, python

    root = roots[0] if roots else None
    return root, (conda_executable(root) if root else None), None, None


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

_PROBE_RE = re.compile(r"<<<PROBE>>>(.*?)<<<END>>>", re.DOTALL)


def probe(cfg, logger=None, timeout=240):
    """Run the full environment check and return an :class:`EnvironmentReport`."""
    report = EnvironmentReport()
    env_name = cfg.get("env_name") or paths.ENV_NAME
    model = cfg.get("model") or "htdemucs_ft"
    report.env_name = env_name
    report.model_name = model

    root, exe, env_dir, env_python = find_env(
        env_name,
        explicit_root=cfg.get("conda_root") or None,
        explicit_python=cfg.get("env_python") or None,
        notes=report.notes,
    )
    for note in report.notes:
        if logger is not None:
            logger.warning(note)
    report.conda_root = root
    report.conda_exe = exe
    report.env_dir = env_dir
    report.env_python = env_python

    if logger is not None:
        logger.info("Conda root: %s", root or "not found")
        logger.info("Environment '%s': %s", env_name, env_dir or "not found")

    # A usable interpreter is what actually matters. An explicit ``env_python``
    # in config.json is enough on its own, even when no Conda root can be
    # derived from it, so the root is only reported as the problem when there
    # is no interpreter either.
    if not env_python:
        if not root:
            report.problems.append(
                "No Conda installation was found. Install Anaconda or Miniconda, then "
                "run setup_env.bat in the SeparateAudio folder."
            )
        else:
            report.problems.append(
                "The Conda environment '%s' was not found. Run setup_env.bat in the "
                "SeparateAudio folder to create it." % env_name
            )
        return report

    if not root:
        report.notes.append(
            "Using the interpreter at %s directly; no Conda installation was "
            "detected around it." % env_python
        )

    probe_script = os.path.join(paths.APP_DIR, "_probe.py")
    if not os.path.isfile(probe_script):
        report.problems.append("Internal error: App\\_probe.py is missing.")
        return report

    code, out, err = procutil.run_capture(
        [env_python, "-I", "-u", probe_script, model],
        timeout=timeout,
        env_dir=env_dir,
    )

    match = _PROBE_RE.search(out or "")
    if not match:
        detail = (err or out or "no output").strip().splitlines()
        report.problems.append(
            "Could not query the '%s' environment (exit code %s)." % (env_name, code)
        )
        if detail:
            report.notes.append(detail[-1][:400])
        if logger is not None:
            logger.error("Probe failed (rc=%s). stdout=%r stderr=%r", code, out, err)
        return report

    try:
        data = json.loads(match.group(1))
    except ValueError as exc:
        report.problems.append("Could not read the environment report (%s)." % exc)
        return report

    _apply_probe(report, data)
    _classify(report, cfg)

    if logger is not None:
        logger.info("Environment probe: %s", report.summary_line())
        logger.debug("Environment details:\n%s", report.details_text())
    return report


def _apply_probe(report, data):
    report.probed = True
    report.python_version = data.get("python_version")
    report.env_python = data.get("executable") or report.env_python
    report.demucs_version = data.get("demucs_version")
    report.demucs_entry = data.get("demucs_entry")
    report.tkinter_version = data.get("tkinter_version")
    report.probe_errors = data.get("errors") or {}
    report.backends = data.get("backends") or {}

    torch_info = data.get("torch") or {}
    report.torch_version = torch_info.get("version")
    report.cuda_build = torch_info.get("cuda_build")
    report.cuda_available = bool(torch_info.get("cuda_available"))
    report.cuda_devices = torch_info.get("devices") or []
    report.cuda_error = torch_info.get("cuda_error")

    ffmpeg = data.get("ffmpeg") or {}
    report.ffmpeg_path = ffmpeg.get("path")
    report.ffmpeg_version = ffmpeg.get("version")

    model_info = data.get("model") or {}
    report.model_known = model_info.get("known")
    report.model_cached = model_info.get("cached")
    report.model_missing = model_info.get("missing") or []
    report.available_models = model_info.get("available") or []
    report.checkpoint_dir = model_info.get("checkpoint_dir")


def _classify(report, cfg):
    """Turn raw probe data into blocking problems and non-blocking notes."""
    if not report.demucs_version:
        report.problems.append(
            "Demucs is not installed in the '%s' environment. Run setup_env.bat "
            "to install it." % report.env_name
        )
    elif not report.demucs_entry:
        report.problems.append(
            "Demucs is installed but its command-line entry point is missing. "
            "Reinstall it with: conda run -n %s pip install --force-reinstall demucs"
            % report.env_name
        )

    if not report.torch_version:
        report.problems.append(
            "PyTorch is not installed in the '%s' environment. Run setup_env.bat."
            % report.env_name
        )

    if not report.ffmpeg_path:
        report.problems.append(
            "FFmpeg was not found in the '%s' environment. Run setup_env.bat, or "
            "install it with: conda install -n %s -c conda-forge ffmpeg"
            % (report.env_name, report.env_name)
        )

    if report.model_known is False:
        message = "Model '%s' is not recognised by this version of Demucs." % report.model_name
        if report.available_models:
            message += " Available models: %s." % ", ".join(report.available_models)
        report.problems.append(message)

    # -- notes: things worth telling the user that do not block processing --
    if report.model_cached is False:
        report.notes.append(
            "Model weights for '%s' are not cached yet. The first separation will "
            "download them (about 320 MB for htdemucs_ft); after that the app works fully "
            "offline." % report.model_name
        )
    elif report.model_cached is None and report.model_known is None:
        report.notes.append(
            "Could not confirm whether '%s' is cached; Demucs will fetch it if needed."
            % report.model_name
        )

    requested = (cfg.get("device") or "auto").lower()
    if requested == "cuda" and not report.cuda_available:
        report.notes.append(
            "CUDA was requested but is not available; the CPU will be used instead."
        )
    if report.cuda_error:
        report.notes.append("CUDA probe reported: %s" % report.cuda_error)
    if not report.cuda_available and not report.cuda_error:
        report.notes.append("No CUDA GPU detected - separation will run on the CPU.")

    if report.backends and not report.backends.get("torchaudio") and not report.backends.get(
        "soundfile"
    ):
        report.notes.append(
            "Neither torchaudio nor soundfile is installed; Demucs will rely on "
            "FFmpeg for decoding."
        )


def find_checkpoint_dir():
    """Best-effort location of the Torch hub checkpoint cache."""
    hub = os.environ.get("TORCH_HOME")
    if hub:
        return os.path.join(hub, "hub", "checkpoints")
    return os.path.join(os.path.expanduser("~"), ".cache", "torch", "hub", "checkpoints")


def cached_checkpoints():
    """Return the list of cached ``.th`` weight files (for diagnostics)."""
    return sorted(glob.glob(os.path.join(find_checkpoint_dir(), "*.th")))


if __name__ == "__main__":  # pragma: no cover - manual diagnostics
    sys.path.insert(0, os.path.dirname(paths.APP_DIR))
    from App import config as _config

    _cfg = _config.load()
    print(probe(_cfg).details_text())
