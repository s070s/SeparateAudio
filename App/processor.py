"""The Demucs worker thread.

Separation runs in a background thread that drives a Demucs subprocess. The
thread never touches Tkinter: it publishes plain dictionaries onto a
:class:`queue.Queue` which the GUI drains from an ``after()`` callback.
"""

import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid

from App import audiofiles, config as config_mod, paths, procutil

#: Demucs (tqdm) progress lines look like ``  37%|###   | 44.1/120.0 [...]``.
_PERCENT_RE = re.compile(r"(\d{1,3})%\|")
_BAG_RE = re.compile(r"bag of (\d+) models", re.IGNORECASE)

#: Rough bytes-per-second of audio, used only to estimate disk requirements.
_WAV_BYTES_PER_SEC = 176400
_BYTES_PER_SEC_GUESS = {
    ".wav": 176400,
    ".aiff": 176400,
    ".aif": 176400,
    ".flac": 105000,
    ".alac": 105000,
    ".mp3": 24000,
    ".m4a": 24000,
    ".aac": 24000,
    ".ogg": 20000,
    ".opus": 12000,
    ".wma": 24000,
    ".mp4": 24000,
}

# Outcome codes used in messages and in the summary counters.
OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"
CANCELLED = "cancelled"


#: Demucs flags that would change the output away from WAV. The app promises
#: WAV stems and promises never to downgrade a lossless source, so these are
#: refused even if someone puts them in ``extra_demucs_args``.
_FORMAT_FLAGS = frozenset(
    ["--mp3", "--flac", "--mp3-bitrate", "--mp3-preset", "-o", "--out", "--filename"]
)


def _safe_extra_args(extra, logger=None):
    """Strip output-format arguments from user-supplied Demucs flags.

    Returns ``(kept, dropped)``. Anything dropped is logged rather than
    silently ignored.
    """
    kept, dropped = [], []
    skip_next = False
    for argument in extra:
        if skip_next:
            dropped.append(argument)
            skip_next = False
            continue
        base = argument.split("=", 1)[0]
        if base in _FORMAT_FLAGS:
            dropped.append(argument)
            # These flags take a value; drop that too when it is separate.
            skip_next = "=" not in argument and base in (
                "--mp3-bitrate", "--mp3-preset", "-o", "--out", "--filename",
            )
            continue
        kept.append(argument)
    if dropped and logger is not None:
        logger.warning(
            "Ignoring extra_demucs_args that would change the output format or "
            "location: %s. Stems are always written as WAV.", " ".join(dropped),
        )
    return kept, dropped


class JobRequest(object):
    """Everything the worker needs; assembled on the GUI thread before start."""

    def __init__(self, files, cfg, report, logger):
        self.files = list(files)
        self.output_dir = cfg["output_dir"]
        self.model = cfg["model"]
        self.requested_device = cfg["device"]
        self.device = report.resolve_device(cfg["device"])
        self.overwrite_policy = cfg["overwrite_policy"]
        self.extra_args, self.dropped_args = _safe_extra_args(
            cfg.get("extra_demucs_args") or [], logger
        )
        self.env_python = report.env_python
        self.env_dir = report.env_dir
        self.demucs_entry = report.demucs_entry or "demucs.separate"
        # Derived from the arguments that survived filtering, since those are
        # the only ones Demucs will actually see.
        self.expected_stems = config_mod.expected_stems(cfg["model"], self.extra_args)
        self.expected_passes = config_mod.expected_passes(cfg["model"])
        self.logger = logger


def estimate_seconds(path):
    """Very rough audio duration estimate from file size, for disk checks."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    per_sec = _BYTES_PER_SEC_GUESS.get(os.path.splitext(path)[1].lower(), 24000)
    return size / float(per_sec)


def estimate_required_mb(path, stem_count):
    """Space needed for the temporary stems plus the final copy, in MB."""
    seconds = estimate_seconds(path)
    # Stems are written once into the workspace and then moved (same volume),
    # but allow for one full extra copy in case a cross-volume copy is needed.
    total_bytes = seconds * _WAV_BYTES_PER_SEC * max(1, stem_count) * 2
    return int(total_bytes / (1024 * 1024)) + 64


class SeparationWorker(threading.Thread):
    """Processes a queue of audio files, one Demucs run at a time."""

    def __init__(self, request, out_queue):
        super().__init__(name="demucs-worker", daemon=True)
        self.request = request
        self.queue = out_queue
        self.cancel_event = threading.Event()
        self._process = None
        self._process_lock = threading.Lock()
        self._policy_for_all = None
        #: Unique per worker, so a later batch can never be handed the scratch
        #: folder of an earlier one. The process id alone was not enough: it is
        #: constant for the whole session while ``index`` restarts at 0 on every
        #: Start, so pressing Start again reused - and erased - a workspace that
        #: a failed run had deliberately left stems in.
        self._run_token = uuid.uuid4().hex[:8]
        #: Output folders already spoken for by earlier files in this batch,
        #: so two inputs can never write into the same folder.
        self._claimed_dirs = set()
        self.counts = {OK: 0, FAILED: 0, SKIPPED: 0, CANCELLED: 0}

    # -- public API ------------------------------------------------------
    def cancel(self):
        """Ask the worker to stop; safe to call from the GUI thread."""
        self.cancel_event.set()
        with self._process_lock:
            process = self._process
        if process is not None:
            procutil.kill_tree(process, self.request.logger)

    @property
    def cancelled(self):
        return self.cancel_event.is_set()

    # -- messaging -------------------------------------------------------
    def _emit(self, kind, **payload):
        payload["type"] = kind
        self.queue.put(payload)

    def _operation(self, text):
        self._emit("operation", text=text)

    def _overall(self, done_files, fraction_in_file):
        total = len(self.request.files) or 1
        value = (done_files + max(0.0, min(1.0, fraction_in_file))) / total
        self._emit("overall", fraction=max(0.0, min(1.0, value)))

    # -- main loop -------------------------------------------------------
    def run(self):
        logger = self.request.logger
        total = len(self.request.files)
        logger.info(
            "Batch start: %d file(s), model=%s, device=%s (requested %s), output=%s",
            total, self.request.model, self.request.device,
            self.request.requested_device, self.request.output_dir,
        )
        started_at = time.time()

        try:
            for index, audio_path in enumerate(self.request.files):
                if self.cancel_event.is_set():
                    for remaining in self.request.files[index:]:
                        self._emit(
                            "job_done",
                            path=remaining,
                            name=os.path.basename(remaining),
                            status=CANCELLED,
                            error="Cancelled before processing started.",
                            output=None,
                        )
                        self.counts[CANCELLED] += 1
                    break

                self._emit(
                    "job_start",
                    index=index,
                    total=total,
                    path=audio_path,
                    name=os.path.basename(audio_path),
                )
                self._overall(index, 0.0)

                try:
                    status, output_dir, error = self._process_one(audio_path, index)
                except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
                    status, output_dir = FAILED, None
                    error = "Unexpected error: %s" % exc
                    logger.error(
                        "Unhandled error while processing %s\n%s",
                        audio_path, traceback.format_exc(),
                    )

                self.counts[status] = self.counts.get(status, 0) + 1
                self._emit(
                    "job_done",
                    path=audio_path,
                    name=os.path.basename(audio_path),
                    status=status,
                    output=output_dir,
                    error=error,
                )
                self._emit("counts", **dict(self.counts))
                self._overall(index + 1, 0.0)
        finally:
            elapsed = time.time() - started_at
            logger.info(
                "Batch finished in %.1fs - %d ok, %d failed, %d skipped, %d cancelled",
                elapsed, self.counts[OK], self.counts[FAILED],
                self.counts[SKIPPED], self.counts[CANCELLED],
            )
            self._emit(
                "finished",
                cancelled=self.cancel_event.is_set(),
                elapsed=elapsed,
                counts=dict(self.counts),
            )

    # -- per-file pipeline ----------------------------------------------
    def _process_one(self, audio_path, index):
        logger = self.request.logger
        name = os.path.basename(audio_path)
        logger.info("--- %s ---", name)
        logger.info("Input: %s", audio_path)

        # 1. Validate ----------------------------------------------------
        self._operation("Checking file")
        problem = audiofiles.validate_input(audio_path)
        if problem:
            logger.error("Validation failed for %s: %s", name, problem)
            return FAILED, None, problem

        # 2. Resolve the destination ------------------------------------
        self._operation("Preparing output folder")
        try:
            target_dir, decision = self._resolve_target(audio_path)
        except _Cancelled:
            return CANCELLED, None, "Cancelled while waiting for a decision."
        if decision == SKIPPED:
            logger.info("Skipped %s - output already exists", name)
            return SKIPPED, target_dir, "Output already exists; skipped at your request."
        if decision == CANCELLED:
            return CANCELLED, None, "Cancelled."

        # 3. Disk space --------------------------------------------------
        required_mb = estimate_required_mb(audio_path, len(self.request.expected_stems))
        free_mb = audiofiles.free_space_mb(self.request.output_dir)
        logger.info("Disk: %s MB free, roughly %s MB needed", free_mb, required_mb)
        if 0 <= free_mb < required_mb:
            message = (
                "Not enough free disk space: about %d MB is needed but only %d MB "
                "is available on the output drive." % (required_mb, free_mb)
            )
            logger.error(message)
            return FAILED, None, message

        # 4. Run Demucs in a private workspace ---------------------------
        # Kept short on purpose: Demucs appends <model>\<full track name>\ to
        # this path, so a long workspace name could push the stems past the
        # 260-character limit even when the final destination is well within it.
        # The per-worker token keeps two copies of the app - and two batches in
        # the same copy - from stepping on each other's workspace. It must stay
        # unique across batches: this folder is rmtree'd below, and a failed run
        # may have left the user's only copy of some stems in it.
        work_root = paths.work_dir_for(self.request.output_dir)
        work_dir = os.path.join(work_root, "w%s_%d" % (self._run_token, index))
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
            os.makedirs(work_dir, exist_ok=True)
        except OSError as exc:
            message = "Could not create the temporary workspace: %s" % exc
            logger.error(message)
            return FAILED, None, message

        started = time.time()
        keep_work = False
        try:
            code, tail = self._run_demucs(audio_path, work_dir, index)
            duration = time.time() - started

            if code != 0:
                # A cancellation is only a cancellation when Demucs did not
                # finish. If it exited cleanly the track is complete, so keep
                # the result even if Stop was pressed while it was writing.
                if self.cancel_event.is_set():
                    preserved = self._preserve_partial(work_dir, target_dir)
                    logger.info("Cancelled during %s after %.1fs", name, duration)
                    note = "Cancelled."
                    if preserved:
                        note += " Partial output kept in %s" % preserved
                    return CANCELLED, preserved, note

                message = self._explain_failure(code, tail)
                logger.error(
                    "Demucs exited with code %s for %s after %.1fs. Last output:\n%s",
                    code, name, duration, tail,
                )
                preserved = self._preserve_partial(work_dir, target_dir)
                if preserved:
                    message += " Partial output kept in %s." % preserved
                return FAILED, preserved, message

            # 5-7. Collect, verify and place the stems -------------------
            self._operation("Collecting stems")
            produced = audiofiles.find_stems(work_dir)
            logger.info("Demucs produced %d file(s) in %.1fs", len(produced), duration)
            if not produced:
                other = audiofiles.find_any_audio(work_dir)
                if other:
                    keep_work = True
                    message = (
                        "Demucs produced %d file(s) but none of them were WAV stems. "
                        "They have been left in %s." % (len(other), work_dir)
                    )
                    logger.error("%s Files: %s", message, [os.path.basename(o) for o in other])
                    return FAILED, work_dir, message
                message = (
                    "Demucs finished but produced no audio files. See the log for "
                    "the full output."
                )
                logger.error("%s Last output:\n%s", message, tail)
                return FAILED, None, message

            self._operation("Saving stems")
            try:
                os.makedirs(target_dir, exist_ok=True)
            except OSError as exc:
                keep_work = True
                message = (
                    "Could not create the output folder %s (%s). The separated stems "
                    "have been left in %s." % (target_dir, exc, work_dir)
                )
                logger.error(message)
                return FAILED, work_dir, message

            moved, move_errors = audiofiles.move_into(produced, target_dir, logger)
            if move_errors:
                # Whatever could not be placed is still in the workspace; never
                # throw away a finished separation because of a write failure.
                keep_work = bool(audiofiles.find_stems(work_dir))
                message = "Could not write all stems: %s" % "; ".join(move_errors[:3])
                if keep_work:
                    message += " The remaining stems were left in %s." % work_dir
                logger.error(message)
                return FAILED, target_dir, message

            ok, missing, empty, extra = audiofiles.verify_stems(
                target_dir, self.request.expected_stems
            )
            logger.info(
                "Output: %s (%d file(s)%s)",
                target_dir, len(moved), ", extra: %s" % ", ".join(extra) if extra else "",
            )
            if not ok:
                parts = []
                if missing:
                    parts.append("missing %s" % ", ".join(missing))
                if empty:
                    parts.append("empty %s" % ", ".join(empty))
                message = "Separation finished but the output is incomplete (%s)." % "; ".join(
                    parts
                )
                logger.error("%s Files present: %s", message, [os.path.basename(m) for m in moved])
                return FAILED, target_dir, message

            logger.info("SUCCESS: %s -> %s", name, target_dir)
            return OK, target_dir, None
        finally:
            self._cleanup(work_dir, work_root, keep_work)

    # -- destination resolution -----------------------------------------
    def _resolve_target(self, audio_path):
        """Return ``(target_dir, decision)`` honouring the overwrite policy."""
        logger = self.request.logger
        default_dir = audiofiles.target_dir_for(self.request.output_dir, audio_path)

        # Another file in this same batch may already own that folder - for
        # example two tracks called "intro.wav" in different subfolders.
        deduped = audiofiles.deduplicated_dir(default_dir, self._claimed_dirs)
        if deduped != default_dir:
            logger.warning(
                "Output folder %s is already used by another file in this batch; "
                "writing %s to %s instead",
                default_dir, os.path.basename(audio_path), deduped,
            )
            default_dir = deduped

        if not audiofiles.directory_has_content(default_dir):
            self._claimed_dirs.add(os.path.normcase(default_dir))
            return default_dir, None

        policy = self._policy_for_all or self.request.overwrite_policy
        if policy == "prompt":
            action, apply_all = self._ask_overwrite(audio_path, default_dir)
            if apply_all:
                self._policy_for_all = action
            policy = action

        if policy == "skip":
            return default_dir, SKIPPED
        if policy == "cancel":
            self.cancel_event.set()
            return default_dir, CANCELLED
        if policy == "rename":
            new_dir = audiofiles.versioned_dir(default_dir)
            logger.info("Output exists; writing a new version to %s", new_dir)
            self._claimed_dirs.add(os.path.normcase(new_dir))
            return new_dir, None

        logger.warning(
            "Output exists; overwriting stem files in %s (other files are kept)", default_dir
        )
        self._claimed_dirs.add(os.path.normcase(default_dir))
        return default_dir, None

    def _ask_overwrite(self, audio_path, target_dir):
        """Block until the GUI answers the overwrite question."""
        answer = {}
        event = threading.Event()
        self._emit(
            "ask_overwrite",
            name=os.path.basename(audio_path),
            target=target_dir,
            answer=answer,
            event=event,
        )
        while not event.wait(0.2):
            if self.cancel_event.is_set():
                raise _Cancelled()
        return answer.get("action", "skip"), bool(answer.get("apply_all"))

    # -- Demucs invocation ----------------------------------------------
    def _build_command(self, audio_path, work_dir):
        command = [
            self.request.env_python,
            "-u",
            "-m",
            self.request.demucs_entry,
            "-n", self.request.model,
            "-d", self.request.device,
            "-o", work_dir,
        ]
        command.extend(self.request.extra_args)
        command.append("--")
        command.append(audio_path)
        return command

    def _run_demucs(self, audio_path, work_dir, index):
        """Launch Demucs and stream its output. Returns ``(returncode, tail)``."""
        logger = self.request.logger
        command = self._build_command(audio_path, work_dir)
        logger.info("Command: %s", " ".join('"%s"' % c if " " in c else c for c in command))

        device_label = "GPU" if self.request.device == "cuda" else "CPU"
        self._operation("Separating on %s with %s" % (device_label, self.request.model))

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=procutil.build_env(self.request.env_dir),
                cwd=self.request.env_dir or None,
                startupinfo=procutil.startup_info(),
                creationflags=procutil.creation_flags(new_group=True),
                bufsize=0,
            )
        except (OSError, ValueError) as exc:
            logger.error("Could not start Demucs: %s", exc)
            return -1, "Could not start Demucs: %s" % exc

        with self._process_lock:
            self._process = process

        if self.cancel_event.is_set():
            procutil.kill_tree(process, logger)

        tail = _RingBuffer(80)
        state = _ProgressState(self.request.expected_passes)
        readers = [
            threading.Thread(
                target=self._pump,
                args=(process.stdout, "out", tail, state, index),
                name="demucs-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=self._pump,
                args=(process.stderr, "err", tail, state, index),
                name="demucs-stderr",
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        while process.poll() is None:
            if self.cancel_event.wait(0.2):
                logger.info("Cancellation requested - terminating Demucs (PID %s)", process.pid)
                self._operation("Stopping Demucs")
                procutil.kill_tree(process, logger)
                break

        try:
            process.wait(timeout=30)
        except Exception:  # noqa: BLE001
            procutil.kill_tree(process, logger)

        for reader in readers:
            reader.join(timeout=10)

        with self._process_lock:
            self._process = None

        return process.returncode if process.returncode is not None else -1, tail.text()

    def _pump(self, stream, channel, tail, state, index):
        """Read one child stream, splitting on both newlines and carriage returns."""
        logger = self.request.logger
        if stream is None:
            return
        buffer = b""
        try:
            while True:
                # The pipes are opened unbuffered (bufsize=0), so they are raw
                # FileIO objects whose read(n) is a single syscall that returns
                # as soon as any data arrives. read1 is used when a buffered
                # stream turns up instead, where read(n) would block for n bytes.
                if hasattr(stream, "read1"):
                    chunk = stream.read1(65536)
                else:
                    chunk = stream.read(65536)
                if not chunk:
                    break
                buffer += chunk
                pieces = re.split(rb"[\r\n]", buffer)
                buffer = pieces.pop()
                for piece in pieces:
                    self._handle_line(procutil.decode_bytes(piece).strip(), channel, tail, state, index)
            if buffer:
                self._handle_line(procutil.decode_bytes(buffer).strip(), channel, tail, state, index)
        except (OSError, ValueError):
            pass  # the pipe closed while we were reading; the exit code tells the story
        except Exception:  # noqa: BLE001
            logger.debug("Reader thread error:\n%s", traceback.format_exc())
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _handle_line(self, line, channel, tail, state, index):
        if not line:
            return
        tail.add("[%s] %s" % (channel, line))

        bag = _BAG_RE.search(line)
        if bag:
            try:
                state.total_passes = max(1, int(bag.group(1)))
            except ValueError:
                pass

        match = _PERCENT_RE.search(line)
        if match:
            try:
                percent = int(match.group(1))
            except ValueError:
                return
            fraction = state.update(percent)
            self._emit("file_progress", fraction=fraction, percent=int(fraction * 100))
            self._overall(index, fraction)
            return

        # Non-progress chatter is worth keeping, but only in the log file.
        self.request.logger.debug("demucs[%s]: %s", channel, line)
        lowered = line.lower()
        if "error" in lowered or "traceback" in lowered or "warning" in lowered:
            self.request.logger.warning("demucs[%s]: %s", channel, line)

    # -- failure handling ------------------------------------------------
    def _explain_failure(self, code, tail):
        """Turn a Demucs failure into a sentence a user can act on."""
        lowered = (tail or "").lower()
        if "out of memory" in lowered or "cuda error" in lowered or "cublas" in lowered:
            return (
                "The GPU ran out of memory or reported an error. Switch the Device "
                "selector to CPU and try again."
            )
        if "no such file" in lowered or "filenotfounderror" in lowered:
            return "Demucs could not read the input file. It may have been moved or renamed."
        # Demucs reports an unreadable track as "When trying to load using
        # ffmpeg, got the following error: FFmpeg could not read the file."
        if (
            "could not read the file" in lowered
            or "trying to load using" in lowered
            or "invalid data" in lowered
            or "could not be decoded" in lowered
        ):
            return (
                "This file could not be decoded - it is corrupt, empty, or not "
                "really audio despite its extension. Check that it plays in a "
                "media player, or convert it to WAV first."
            )
        if "ffmpeg" in lowered and ("not found" in lowered or "cannot" in lowered):
            return (
                "FFmpeg could not decode this file. Check that the file plays in a "
                "media player, or convert it to WAV first."
            )
        if "soundfile" in lowered:
            return "The audio file appears to be corrupt or in an unsupported encoding."
        if "modulenotfounderror" in lowered or "importerror" in lowered:
            return (
                "A Python dependency is missing from the environment. Run "
                "setup_env.bat to repair it."
            )
        if "connection" in lowered or "urlopen" in lowered or "download" in lowered:
            return (
                "The model weights could not be downloaded. Connect to the internet "
                "once so the model can be cached, then retry."
            )
        if "permission" in lowered:
            return "A file could not be written - check folder permissions."
        return (
            "Demucs failed with exit code %s. Open the log file for the full output."
            % code
        )

    def _preserve_partial(self, work_dir, target_dir):
        """Keep whatever Demucs managed to write instead of discarding it."""
        produced = audiofiles.find_stems(work_dir)
        if not produced:
            return None
        destination = "%s_incomplete_%s" % (target_dir, audiofiles.timestamp_suffix())
        moved, _errors = audiofiles.move_into(produced, destination, self.request.logger)
        if moved:
            self.request.logger.info(
                "Preserved %d partial file(s) in %s", len(moved), destination
            )
            return destination
        return None

    def _cleanup(self, work_dir, work_root, keep_work=False):
        """Remove the scratch folder, unless it still holds unsaved audio."""
        if keep_work:
            self.request.logger.warning(
                "Keeping the temporary workspace because it still contains output: %s",
                work_dir,
            )
        else:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass
        try:
            if os.path.isdir(work_root) and not os.listdir(work_root):
                os.rmdir(work_root)
        except OSError:
            pass


class _Cancelled(Exception):
    """Raised internally when the user cancels while a prompt is open."""


class _RingBuffer(object):
    """Keeps the last N output lines so failures can be explained."""

    def __init__(self, size):
        self._size = size
        self._lines = []
        self._lock = threading.Lock()

    def add(self, line):
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > self._size:
                del self._lines[: len(self._lines) - self._size]

    def text(self):
        with self._lock:
            return "\n".join(self._lines)


class _ProgressState(object):
    """Converts repeating 0-100% Demucs bars into a single 0-1 fraction.

    Bagged models such as ``htdemucs_ft`` print one bar per sub-model, so the
    percentage resets several times per track.
    """

    def __init__(self, total_passes):
        self.total_passes = max(1, int(total_passes))
        self.completed_passes = 0
        self.last_percent = -1
        self._lock = threading.Lock()

    def update(self, percent):
        percent = max(0, min(100, percent))
        with self._lock:
            if percent < self.last_percent - 5:
                # The bar restarted: the previous pass finished.
                self.completed_passes = min(self.completed_passes + 1, self.total_passes - 1)
            self.last_percent = percent
            done = self.completed_passes + (percent / 100.0)
            return max(0.0, min(1.0, done / float(self.total_passes)))


def drain(message_queue, limit=200):
    """Pull up to ``limit`` messages off the queue without blocking."""
    messages = []
    for _ in range(limit):
        try:
            messages.append(message_queue.get_nowait())
        except queue.Empty:
            break
    return messages
