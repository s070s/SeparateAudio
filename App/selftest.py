"""Self-tests for the parts of SeparateAudio that do not need Demucs.

Run with::

    python -m App.selftest

Covers name sanitisation, input scanning, output resolution, stem
verification, configuration round-trips, progress maths and failure
explanations. Everything runs in a temporary folder; nothing in Input,
Output or Logs is touched.
"""

import json
import logging
import os
import queue
import shutil
import sys
import tempfile
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from App import audiofiles, config as config_mod, envcheck, logging_setup, processor  # noqa: E402

_RESULTS = []


def check(name, condition, detail=""):
    _RESULTS.append((name, bool(condition), detail))
    print("  %s  %s%s" % ("PASS" if condition else "FAIL", name, (" - " + detail) if detail and not condition else ""))
    return bool(condition)


def section(title):
    print("\n%s" % title)
    print("-" * len(title))


# ---------------------------------------------------------------------------
def test_sanitize():
    section("Name sanitisation")
    cases = [
        ('My Song', 'My Song'),
        ('AC/DC - Song', 'AC_DC - Song'),
        ('bad:name*here?', 'bad_name_here_'),
        ('trailing dots...', 'trailing dots'),
        ('trailing spaces   ', 'trailing spaces'),
        ('  ', 'untitled'),
        ('', 'untitled'),
        ('...', 'untitled'),
        ('CON', '_CON'),
        ('con', '_con'),
        ('NUL.mp3', '_NUL.mp3'),
        ('COM1', '_COM1'),
        ('LPT9', '_LPT9'),
        ('normal_name', 'normal_name'),
        ('Ünïcödé Trâck', 'Ünïcödé Trâck'),
        ('a<b>c|d"e', 'a_b_c_d_e'),
    ]
    for raw, expected in cases:
        got = audiofiles.sanitize_folder_name(raw)
        check("sanitize(%r) == %r" % (raw, expected), got == expected, "got %r" % got)

    long_name = "x" * 400
    got = audiofiles.sanitize_folder_name(long_name)
    check("long names are truncated", len(got) <= audiofiles.MAX_FOLDER_NAME, "len=%d" % len(got))
    check("control characters removed",
          "\x07" not in audiofiles.sanitize_folder_name("bell\x07here"))
    check("no result ends with a dot or space",
          not audiofiles.sanitize_folder_name("dots. . .").endswith((".", " ")))


def test_scan(tmp):
    section("Input scanning")
    folder = os.path.join(tmp, "scan")
    sub = os.path.join(folder, "nested")
    os.makedirs(sub)
    names = ["a.wav", "b.MP3", "c.flac", "d.ogg", "e.m4a", "f.aac", "notes.txt", "cover.jpg"]
    for name in names:
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(b"0" * 4096)
    with open(os.path.join(sub, "deep.wav"), "wb") as handle:
        handle.write(b"0" * 4096)

    found, error = audiofiles.scan(folder)
    check("scan returns no error", error is None, str(error))
    check("finds exactly the 6 supported files", len(found) == 6, "got %d" % len(found))
    check("case-insensitive extension match",
          any(item.name == "b.MP3" for item in found))
    check("non-audio files excluded",
          not any(item.name in ("notes.txt", "cover.jpg") for item in found))
    check("non-recursive scan ignores subfolders",
          not any(item.name == "deep.wav" for item in found))

    found_r, _ = audiofiles.scan(folder, recursive=True)
    check("recursive scan includes subfolders",
          any(item.name == "deep.wav" for item in found_r), "got %d" % len(found_r))

    _missing, err = audiofiles.scan(os.path.join(tmp, "does-not-exist"))
    check("missing folder reports an error", err is not None)

    check("results are sorted", [i.name.lower() for i in found] == sorted(i.name.lower() for i in found))
    check("all required formats supported",
          all(audiofiles.is_supported("x" + ext)
              for ext in (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac")))
    check("lossless detection", audiofiles.is_lossless("a.flac") and not audiofiles.is_lossless("a.mp3"))


def test_targets(tmp):
    section("Output resolution")
    out = os.path.join(tmp, "out")
    os.makedirs(out)

    target = audiofiles.target_dir_for(out, os.path.join(tmp, "in", "My Song.mp3"))
    check("target folder drops the extension",
          os.path.basename(target) == "My Song", target)
    check("target sits directly under the output folder",
          os.path.dirname(target) == out, target)

    # Use a real basename so this exercises the sanitiser, not os.path.basename.
    weird = audiofiles.target_dir_for(out, os.path.join(tmp, "in", 'AC:DC "Live"?.flac'))
    check("invalid characters sanitised in target",
          os.path.basename(weird) == "AC_DC _Live__", os.path.basename(weird))
    check("no invalid character survives in the target folder name",
          not any(ch in os.path.basename(weird) for ch in '<>:"/\\|?*'),
          os.path.basename(weird))

    empty_dir = os.path.join(out, "Empty")
    os.makedirs(empty_dir)
    check("an empty existing folder is not treated as occupied",
          not audiofiles.directory_has_content(empty_dir))
    with open(os.path.join(empty_dir, "vocals.wav"), "wb") as handle:
        handle.write(b"x" * 2048)
    check("a folder with files is treated as occupied",
          audiofiles.directory_has_content(empty_dir))

    versioned = audiofiles.versioned_dir(empty_dir)
    check("versioned folder is new", not os.path.exists(versioned))
    check("versioned folder keeps the original name as a prefix",
          os.path.basename(versioned).startswith("Empty_"), versioned)

    # Two different songs must never share a directory. The interesting case
    # is same basename in different folders, which target_dir_for alone cannot
    # separate - deduplicated_dir is what actually prevents the collision.
    a = audiofiles.target_dir_for(out, os.path.join("rock", "intro.wav"))
    b = audiofiles.target_dir_for(out, os.path.join("jazz", "intro.wav"))
    check("same basename in different folders collides by default", a == b, "%s / %s" % (a, b))

    claimed = set()
    first = audiofiles.deduplicated_dir(a, claimed)
    claimed.add(os.path.normcase(first))
    second = audiofiles.deduplicated_dir(b, claimed)
    claimed.add(os.path.normcase(second))
    third = audiofiles.deduplicated_dir(b, claimed)
    check("the second file gets its own folder", first != second, "%s / %s" % (first, second))
    check("the third file gets another folder",
          third not in (first, second), "%s" % third)
    check("the first file keeps the plain name", first == a, first)
    check("disambiguated names are readable",
          os.path.basename(second) == "intro (2)", os.path.basename(second))

    # is_inside protects the input folder from being used as the output folder.
    check("a folder is inside itself", audiofiles.is_inside(out, out))
    check("a child is inside its parent",
          audiofiles.is_inside(os.path.join(out, "sub"), out))
    check("a sibling is not inside", not audiofiles.is_inside(os.path.join(tmp, "other"), out))
    check("a name-prefix sibling is not inside",
          not audiofiles.is_inside(out + "2", out), out + "2")


def test_verify(tmp):
    section("Stem verification")
    good = os.path.join(tmp, "good")
    os.makedirs(good)
    for stem in ("vocals", "drums", "bass", "other"):
        with open(os.path.join(good, "%s.wav" % stem), "wb") as handle:
            handle.write(b"R" * 5000)
    ok, missing, empty, extra = audiofiles.verify_stems(good, ("vocals", "drums", "bass", "other"))
    check("complete output verifies", ok, "missing=%s empty=%s" % (missing, empty))
    check("no extras reported for a clean folder", extra == [], str(extra))

    partial = os.path.join(tmp, "partial")
    os.makedirs(partial)
    with open(os.path.join(partial, "vocals.wav"), "wb") as handle:
        handle.write(b"R" * 5000)
    with open(os.path.join(partial, "drums.wav"), "wb") as handle:
        handle.write(b"")
    ok, missing, empty, _extra = audiofiles.verify_stems(
        partial, ("vocals", "drums", "bass", "other")
    )
    check("incomplete output fails verification", not ok)
    check("missing stems are named", set(missing) == {"bass.wav", "other.wav"}, str(missing))
    check("empty stems are named", empty == ["drums.wav"], str(empty))

    ok, missing, _e, _x = audiofiles.verify_stems(os.path.join(tmp, "nope"), ("vocals",))
    check("a missing folder fails verification", not ok and missing == ["vocals.wav"])


def test_validate(tmp):
    section("Input validation")
    good = os.path.join(tmp, "ok.wav")
    with open(good, "wb") as handle:
        handle.write(b"R" * 8192)
    check("a healthy file validates", audiofiles.validate_input(good) is None)

    tiny = os.path.join(tmp, "tiny.wav")
    with open(tiny, "wb") as handle:
        handle.write(b"R")
    check("an empty file is rejected", audiofiles.validate_input(tiny) is not None)

    check("a missing file is rejected",
          audiofiles.validate_input(os.path.join(tmp, "ghost.wav")) is not None)

    bad_ext = os.path.join(tmp, "notes.txt")
    with open(bad_ext, "wb") as handle:
        handle.write(b"R" * 8192)
    check("an unsupported format is rejected", audiofiles.validate_input(bad_ext) is not None)
    check("a folder is rejected", audiofiles.validate_input(tmp) is not None)


def test_config(tmp):
    section("Configuration")
    os.makedirs(tmp, exist_ok=True)
    path = os.path.join(tmp, "config.json")

    cfg = config_mod.load(path)
    check("defaults load when the file is absent", cfg["model"] == "htdemucs_ft", cfg["model"])
    check("default device is auto", cfg["device"] == "auto")
    check("default policy is prompt", cfg["overwrite_policy"] == "prompt")

    cfg["model"] = "htdemucs"
    cfg["device"] = "cpu"
    check("saving succeeds", config_mod.save(cfg, path))
    reloaded = config_mod.load(path)
    check("values survive a round trip",
          reloaded["model"] == "htdemucs" and reloaded["device"] == "cpu")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json ")
    recovered = config_mod.load(path)
    check("a corrupt config falls back to defaults",
          recovered["model"] == "htdemucs_ft", recovered["model"])
    check("the corrupt file is backed up",
          any(name.startswith("config.json.corrupt-") for name in os.listdir(tmp)),
          str(os.listdir(tmp)))

    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"device": "quantum", "overwrite_policy": "nuke", "keep_logs": "many"}')
    fixed = config_mod.load(path)
    check("invalid device is reset", fixed["device"] == "auto", fixed["device"])
    check("invalid policy is reset", fixed["overwrite_policy"] == "prompt")
    check("invalid keep_logs is reset", fixed["keep_logs"] == 30, str(fixed["keep_logs"]))

    # Moving the project to another PC or drive must not leave it pointing at
    # the original machine's folders.
    from App import paths as paths_mod

    with open(path, "w", encoding="utf-8") as handle:
        json.dump({
            "input_dir": r"C:\Users\SomeoneElse\Desktop\SeparateAudio\Input",
            "output_dir": r"C:\Users\SomeoneElse\Desktop\SeparateAudio\Output",
        }, handle)
    moved = config_mod.load(path)
    check("a config copied from another PC re-bases Input",
          moved["input_dir"] == paths_mod.INPUT_DIR, moved["input_dir"])
    check("a config copied from another PC re-bases Output",
          moved["output_dir"] == paths_mod.OUTPUT_DIR, moved["output_dir"])

    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"input_dir": r"D:\MyMusic", "output_dir": r"D:\Stems"}, handle)
    custom = config_mod.load(path)
    check("a deliberate custom input folder is left alone",
          custom["input_dir"] == r"D:\MyMusic", custom["input_dir"])
    check("a deliberate custom output folder is left alone",
          custom["output_dir"] == r"D:\Stems", custom["output_dir"])

    # Named "Input"/"Output" but not part of a SeparateAudio project, and not
    # yet created - the user's own choice, so it must survive untouched.
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"input_dir": r"D:\Music\Input", "output_dir": r"D:\Music\Output"}, handle)
    theirs = config_mod.load(path)
    check("a user folder merely named 'Input' is not hijacked",
          theirs["input_dir"] == r"D:\Music\Input", theirs["input_dir"])
    check("a user folder merely named 'Output' is not hijacked",
          theirs["output_dir"] == r"D:\Music\Output", theirs["output_dir"])

    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"input_dir": paths_mod.INPUT_DIR,
                   "output_dir": paths_mod.OUTPUT_DIR}, handle)
    local = config_mod.load(path)
    check("a normal local config is untouched",
          local["input_dir"] == paths_mod.INPUT_DIR, local["input_dir"])

    check("htdemucs_ft expects the four standard stems",
          config_mod.expected_stems("htdemucs_ft") == ("vocals", "drums", "bass", "other"))
    check("htdemucs_6s expects six stems",
          len(config_mod.expected_stems("htdemucs_6s")) == 6)
    check("htdemucs_ft is a four-pass bag", config_mod.expected_passes("htdemucs_ft") == 4)
    check("htdemucs is a single pass", config_mod.expected_passes("htdemucs") == 1)


def test_progress():
    section("Progress maths")
    state = processor._ProgressState(4)
    check("start of the first pass is 0", abs(state.update(0) - 0.0) < 1e-6)
    check("half of the first pass is 12.5%", abs(state.update(50) - 0.125) < 1e-6)
    state.update(100)
    value = state.update(0)  # second pass begins
    check("a restarting bar advances the pass counter", abs(value - 0.25) < 1e-6, str(value))
    state.update(100)
    state.update(0)
    state.update(100)
    state.update(0)
    final = state.update(100)
    check("the last pass reaches 100%", abs(final - 1.0) < 1e-6, str(final))
    check("progress never exceeds 1.0", state.update(100) <= 1.0)

    single = processor._ProgressState(1)
    check("single-pass models map percent directly", abs(single.update(42) - 0.42) < 1e-6)

    ring = processor._RingBuffer(3)
    for i in range(10):
        ring.add("line %d" % i)
    check("the ring buffer keeps only the last lines",
          ring.text() == "line 7\nline 8\nline 9", ring.text())


def test_percent_parsing():
    section("Demucs output parsing")
    samples = [
        (" 37%|###5      | 44.1/120.0 [00:12<00:20]", 37),
        ("100%|##########| 120.0/120.0", 100),
        ("  0%|          | 0.00/120.0", 0),
        ("  7%|6         | 8.0/120.0", 7),
    ]
    for line, expected in samples:
        match = processor._PERCENT_RE.search(line)
        check("parses %r" % line[:22], match is not None and int(match.group(1)) == expected)
    check("plain text is not mistaken for progress",
          processor._PERCENT_RE.search("Separating track My Song.wav") is None)

    bag = processor._BAG_RE.search("Selected model is a bag of 4 models. You will see that many")
    check("bag size is detected", bag is not None and bag.group(1) == "4")


def test_failure_messages():
    section("Failure explanations")

    class _Req(object):
        logger = None

    worker = processor.SeparationWorker.__new__(processor.SeparationWorker)
    worker.request = _Req()

    cases = [
        ("torch.cuda.OutOfMemoryError: CUDA out of memory", "CPU"),
        ("FileNotFoundError: no such file or directory", "moved or renamed"),
        ("ModuleNotFoundError: No module named 'julius'", "setup_env.bat"),
        ("PermissionError: [Errno 13] Permission denied", "permissions"),
        ("urllib.error.URLError: <urlopen error>", "internet"),
        # The exact wording Demucs 4.0.1 emits for an undecodable track.
        ("When trying to load using ffmpeg, got the following error: "
         "FFmpeg could not read the file.", "corrupt"),
    ]
    for tail, expected in cases:
        message = worker._explain_failure(1, tail)
        check("explains %r" % tail[:28], expected.lower() in message.lower(), message)

    generic = worker._explain_failure(9, "something entirely unexpected")
    check("unknown failures mention the exit code and the log",
          "9" in generic and "log" in generic.lower(), generic)


def test_disk_estimates(tmp):
    section("Disk estimates")
    path = os.path.join(tmp, "sizer.wav")
    with open(path, "wb") as handle:
        handle.write(b"0" * (176400 * 10))  # ten seconds of CD-quality audio
    seconds = processor.estimate_seconds(path)
    check("duration estimate is about ten seconds", 9.0 <= seconds <= 11.0, str(seconds))
    needed = processor.estimate_required_mb(path, 4)
    check("space estimate is positive and sane", 0 < needed < 200, str(needed))
    check("free space is reported", audiofiles.free_space_mb(tmp) > 0)


def test_move(tmp):
    section("Moving stems")
    source = os.path.join(tmp, "work")
    dest = os.path.join(tmp, "final")
    os.makedirs(source)
    for stem in ("vocals", "drums"):
        with open(os.path.join(source, "%s.wav" % stem), "wb") as handle:
            handle.write(b"R" * 3000)

    moved, errors = audiofiles.move_into(audiofiles.find_stems(source), dest, None)
    check("both stems moved", len(moved) == 2, str(moved))
    check("no move errors", not errors, str(errors))
    check("destination created", os.path.isdir(dest))
    check("sources are gone", not audiofiles.find_stems(source))
    check("targets are non-empty",
          all(os.path.getsize(p) == 3000 for p in moved))

    # Moving again over existing files must replace them, not fail.
    os.makedirs(source, exist_ok=True)
    with open(os.path.join(source, "vocals.wav"), "wb") as handle:
        handle.write(b"N" * 4000)
    moved2, errors2 = audiofiles.move_into(audiofiles.find_stems(source), dest, None)
    check("re-moving replaces cleanly", len(moved2) == 1 and not errors2, str(errors2))
    check("the replacement took effect",
          os.path.getsize(os.path.join(dest, "vocals.wav")) == 4000)
    check("no staging file is left behind",
          not any(n.endswith(".incoming") for n in os.listdir(dest)), str(os.listdir(dest)))

    # find_any_audio must see non-WAV output so it is reported, not deleted.
    extras = os.path.join(tmp, "extras")
    os.makedirs(extras)
    for name in ("vocals.mp3", "drums.flac", "notes.txt"):
        with open(os.path.join(extras, name), "wb") as handle:
            handle.write(b"R" * 2000)
    check("find_stems ignores non-WAV output", audiofiles.find_stems(extras) == [])
    check("find_any_audio sees the non-WAV output",
          len(audiofiles.find_any_audio(extras)) == 2,
          str([os.path.basename(p) for p in audiofiles.find_any_audio(extras)]))


def test_env_report():
    section("Environment report")
    report = envcheck.EnvironmentReport()
    check("a blank report is not ready", not report.ready)
    report.problems.append("Demucs missing")
    check("summary shows the first problem", "Demucs missing" in report.summary_line())

    unprobed = envcheck.EnvironmentReport()
    unprobed.python_version = "3.11.9"
    unprobed.demucs_version = "4.0.1"
    unprobed.ffmpeg_path = "C:/x/ffmpeg.exe"
    check("a report that never probed is not ready", not unprobed.ready)

    ready = envcheck.EnvironmentReport()
    ready.probed = True
    ready.python_version = "3.11.9"
    ready.demucs_version = "4.0.1"
    ready.ffmpeg_path = "C:/x/ffmpeg.exe"
    check("a problem-free report is ready", ready.ready)
    check("auto falls back to CPU without CUDA", ready.resolve_device("auto") == "cpu")
    check("cuda falls back to CPU without CUDA", ready.resolve_device("cuda") == "cpu")
    ready.cuda_available = True
    check("auto picks CUDA when available", ready.resolve_device("auto") == "cuda")
    check("cpu is always honoured", ready.resolve_device("cpu") == "cpu")
    check("details render without error", len(ready.details_text()) > 100)

    roots = envcheck.discover_conda_roots()
    check("conda discovery returns only real roots",
          all(os.path.isdir(r) for r in roots), str(roots))


def test_command_building(tmp):
    section("Demucs command")

    class _Report(object):
        env_python = os.path.join(tmp, "python.exe")
        env_dir = tmp
        demucs_entry = "demucs.separate"
        cuda_available = False

        def resolve_device(self, requested):
            return "cpu"

    cfg = config_mod.default_config()
    cfg["output_dir"] = tmp
    request = processor.JobRequest(["C:\\In\\A Song.mp3"], cfg, _Report(), None)
    worker = processor.SeparationWorker(request, queue.Queue())
    command = worker._build_command("C:\\In\\A Song.mp3", os.path.join(tmp, "work"))

    check("uses the environment interpreter", command[0] == _Report.env_python, command[0])
    check("runs demucs as a module", "-m" in command and "demucs.separate" in command)
    check("passes the model", command[command.index("-n") + 1] == "htdemucs_ft")
    check("passes the device", command[command.index("-d") + 1] == "cpu")
    check("writes into the workspace", command[command.index("-o") + 1].endswith("work"))
    check("the input path comes last", command[-1] == "C:\\In\\A Song.mp3")
    check("uses -- so filenames cannot look like flags", command[-2] == "--")
    check("expects the four standard stems",
          request.expected_stems == ("vocals", "drums", "bass", "other"))
    check("knows htdemucs_ft runs four passes", request.expected_passes == 4)


def test_extra_args_guard():
    section("Extra Demucs arguments")
    kept, dropped = processor._safe_extra_args(
        ["--shifts", "2", "--mp3", "--mp3-bitrate", "320", "-o", "C:/elsewhere",
         "--two-stems", "vocals"]
    )
    check("legitimate flags are kept",
          kept == ["--shifts", "2", "--two-stems", "vocals"], str(kept))
    check("--mp3 is dropped", "--mp3" in dropped)
    check("the bitrate value is dropped with its flag", "320" in dropped, str(dropped))
    check("a redirected output folder is dropped", "C:/elsewhere" in dropped, str(dropped))
    check("stems can never be written as MP3",
          not any(a.startswith("--mp3") or a == "--flac" for a in kept), str(kept))

    kept2, dropped2 = processor._safe_extra_args(["--mp3-bitrate=320", "--shifts=1"])
    check("inline --flag=value form is handled",
          kept2 == ["--shifts=1"] and dropped2 == ["--mp3-bitrate=320"],
          "kept=%s dropped=%s" % (kept2, dropped2))

    kept3, dropped3 = processor._safe_extra_args([])
    check("an empty list stays empty", kept3 == [] and dropped3 == [])


def test_logging_degrades(tmp):
    section("Logging resilience")
    os.makedirs(tmp, exist_ok=True)
    blocker = os.path.join(tmp, "a-file")
    with open(blocker, "w", encoding="utf-8") as handle:
        handle.write("not a directory")

    logger, log_path = logging_setup.setup(os.path.join(blocker, "Logs"), keep_logs=5)
    check("an unwritable log folder does not crash startup", logger is not None)
    check("the failure is reported as no log path", log_path is None, str(log_path))
    try:
        logger.info("this must not raise")
        logged = True
    except Exception as exc:  # noqa: BLE001
        logged = False
        print("     %s" % exc)
    check("logging still works without a file", logged)

    good = os.path.join(tmp, "goodlogs")
    logger2, path2 = logging_setup.setup(good, keep_logs=3)
    logger2.info("hello")
    check("a writable folder yields a log file", path2 and os.path.isfile(path2), str(path2))

    # Pruning must only ever touch the app's own files. Use a folder with no
    # open handler so the count is exact - the live log file is held open by
    # Windows and is deliberately never removed.
    archive = os.path.join(tmp, "archive")
    os.makedirs(archive, exist_ok=True)
    keep_me = os.path.join(archive, "important-notes.txt")
    with open(keep_me, "w", encoding="utf-8") as handle:
        handle.write("user file")
    other_log = os.path.join(archive, "some-other-app.log")
    with open(other_log, "w", encoding="utf-8") as handle:
        handle.write("not ours")
    for index in range(6):
        name = "separateaudio_2020010%d_120000.log" % index
        with open(os.path.join(archive, name), "w", encoding="utf-8") as handle:
            handle.write("old")

    logging_setup.prune_old_logs(archive, 3, logger2)
    remaining = sorted(os.listdir(archive))
    ours = [n for n in remaining if n.startswith("separateaudio_")]
    check("unrelated files are never pruned", "important-notes.txt" in remaining, str(remaining))
    check("other applications' logs are never pruned",
          "some-other-app.log" in remaining, str(remaining))
    check("old run logs are pruned to exactly the limit", len(ours) == 3, str(ours))
    check("the newest run logs are the ones kept",
          ours == ["separateaudio_20200103_120000.log",
                   "separateaudio_20200104_120000.log",
                   "separateaudio_20200105_120000.log"], str(ours))

    check("the live log file is never pruned",
          path2 and os.path.isfile(path2), str(path2))


def test_ui_log_filtering():
    section("UI log filtering")
    captured = []
    handler = logging_setup.CallbackHandler(lambda text, level: captured.append(text))
    record = logging.LogRecord(
        "separateaudio", logging.ERROR, __file__, 1,
        "Something failed\nTraceback (most recent call last):\n  File x\nValueError: boom",
        (), None,
    )
    handler.emit(record)
    check("a message reached the UI", len(captured) == 1, str(captured))
    if captured:
        check("the raw traceback is not shown in the UI",
              "Traceback" not in captured[0], captured[0])
        check("the headline survives", "Something failed" in captured[0], captured[0])
        check("the user is pointed at the log file",
              "log file" in captured[0], captured[0])

    captured.clear()
    single = logging.LogRecord(
        "separateaudio", logging.INFO, __file__, 1, "All good", (), None
    )
    handler.emit(single)
    check("single-line messages pass through unchanged",
          captured and captured[0].endswith("All good"), str(captured))


def test_queue_drain():
    section("Queue handling")
    q = queue.Queue()
    for i in range(5):
        q.put({"type": "test", "i": i})
    drained = processor.drain(q)
    check("drains everything available", len(drained) == 5)
    check("drain on an empty queue returns nothing", processor.drain(q) == [])
    for i in range(500):
        q.put({"type": "test", "i": i})
    check("drain respects its limit", len(processor.drain(q, limit=10)) == 10)


def main():
    print("SeparateAudio self-test")
    print("=" * 60)
    tmp = tempfile.mkdtemp(prefix="separateaudio-selftest-")
    try:
        test_sanitize()
        test_scan(tmp)
        test_targets(tmp)
        test_verify(tmp)
        test_validate(tmp)
        test_config(os.path.join(tmp, "cfg"))
        test_progress()
        test_percent_parsing()
        test_failure_messages()
        test_disk_estimates(tmp)
        test_move(tmp)
        test_env_report()
        test_command_building(tmp)
        test_extra_args_guard()
        test_logging_degrades(os.path.join(tmp, 'logging'))
        test_ui_log_filtering()
        test_queue_drain()
    except Exception:
        traceback.print_exc()
        _RESULTS.append(("selftest harness", False, "crashed"))
    finally:
        # The logging tests leave file handlers open, and Windows will not
        # delete a folder that still has an open handle inside it.
        app_logger = logging_setup.get_logger()
        for handler in list(app_logger.handlers):
            app_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)
        if os.path.exists(tmp):
            print("\nNote: could not fully remove %s" % tmp)

    passed = sum(1 for _n, ok, _d in _RESULTS if ok)
    failed = [(n, d) for n, ok, d in _RESULTS if not ok]
    print("\n" + "=" * 60)
    print("%d passed, %d failed, %d total" % (passed, len(failed), len(_RESULTS)))
    for name, detail in failed:
        print("  FAILED: %s %s" % (name, detail))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
