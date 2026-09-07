"""The SeparateAudio Tkinter interface.

The GUI thread never blocks on Demucs. Long operations run in worker threads
that publish messages onto a queue; :meth:`SeparateAudioApp._poll` drains that
queue from an ``after()`` callback, which is the only place Tk widgets are
touched.
"""

import os
import queue
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from App import audiofiles, config as config_mod, envcheck, logging_setup, paths, procutil
from App import processor as processor_mod

POLL_INTERVAL_MS = 100

_DEVICE_LABELS = {
    "auto": "Auto (use GPU when available)",
    "cpu": "CPU",
    "cuda": "CUDA (NVIDIA GPU)",
}
_POLICY_LABELS = {
    "prompt": "Ask me each time",
    "skip": "Skip the file",
    "overwrite": "Overwrite the stems",
    "rename": "Create a new timestamped folder",
}
_LABEL_TO_DEVICE = {value: key for key, value in _DEVICE_LABELS.items()}
_LABEL_TO_POLICY = {value: key for key, value in _POLICY_LABELS.items()}


class SeparateAudioApp(object):
    """Main application window."""

    def __init__(self, root, cfg, logger, log_path):
        self.root = root
        self.cfg = cfg
        self.logger = logger
        self.log_path = log_path

        self.messages = queue.Queue()
        self.files = []
        self.report = None
        self.worker = None
        self.env_thread = None
        self.busy_task = None
        self._env_token = 0
        self._total_jobs = 0
        self._folder_job = None
        self._poll_job = None
        self._quitting = False
        self._closing = False

        self._build_variables()
        self._build_menu()
        self._build_widgets()
        self._bind_events()

        self.ui_handler = logging_setup.attach_ui_handler(self._log_from_any_thread)

        self._poll_job = self.root.after(POLL_INTERVAL_MS, self._poll)
        self.refresh_files(announce=False)
        self.start_environment_check()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_variables(self):
        self.var_input = tk.StringVar(value=self.cfg["input_dir"])
        self.var_output = tk.StringVar(value=self.cfg["output_dir"])
        self.var_model = tk.StringVar(value=self.cfg["model"])
        self.var_device = tk.StringVar(
            value=_DEVICE_LABELS.get(self.cfg["device"], _DEVICE_LABELS["auto"])
        )
        self.var_policy = tk.StringVar(
            value=_POLICY_LABELS.get(self.cfg["overwrite_policy"], _POLICY_LABELS["prompt"])
        )
        self.var_recursive = tk.BooleanVar(value=False)

        self.var_current_file = tk.StringVar(value="-")
        self.var_operation = tk.StringVar(value="Idle")
        self.var_status = tk.StringVar(value="Starting up...")
        self.var_env = tk.StringVar(value="Checking environment...")
        self.var_counts = tk.StringVar(value="Completed 0   Failed 0   Skipped 0   Total 0")
        self.var_file_count = tk.StringVar(value="0 files")
        self.var_file_percent = tk.StringVar(value="0%")
        self.var_overall_percent = tk.StringVar(value="0%")

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open Input Folder", command=self.open_input)
        file_menu.add_command(label="Open Output Folder", command=self.open_output)
        file_menu.add_separator()
        file_menu.add_command(label="Refresh File List", command=self.refresh_files)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        tools = tk.Menu(menubar, tearoff=0)
        tools.add_command(label="Re-check Environment", command=self.start_environment_check)
        tools.add_command(label="Download Selected Model", command=self.download_model)
        tools.add_separator()
        tools.add_command(label="Open Logs Folder", command=self.open_logs)
        tools.add_command(label="Open Current Log File", command=self.open_current_log)
        menubar.add_cascade(label="Tools", menu=tools)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Environment Details", command=self.show_env_details)
        help_menu.add_command(label="Open README", command=self.open_readme)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    def _build_widgets(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=3)
        self.root.rowconfigure(4, weight=2)

        pad = dict(padx=6, pady=3)

        # -- Folders ----------------------------------------------------
        folders = ttk.LabelFrame(self.root, text="Folders")
        folders.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        folders.columnconfigure(1, weight=1)

        ttk.Label(folders, text="Input:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(folders, textvariable=self.var_input).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(folders, text="Browse...", command=self.browse_input).grid(
            row=0, column=2, sticky="ew", **pad
        )
        ttk.Button(folders, text="Open Input Folder", command=self.open_input).grid(
            row=0, column=3, sticky="ew", **pad
        )

        ttk.Label(folders, text="Output:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(folders, textvariable=self.var_output).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(folders, text="Browse...", command=self.browse_output).grid(
            row=1, column=2, sticky="ew", **pad
        )
        ttk.Button(folders, text="Open Output Folder", command=self.open_output).grid(
            row=1, column=3, sticky="ew", **pad
        )

        # -- File list --------------------------------------------------
        files = ttk.LabelFrame(self.root, text="Audio files in the input folder")
        files.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        files.columnconfigure(0, weight=1)
        files.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(files)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=(4, 0))
        ttk.Button(toolbar, text="Refresh", command=self.refresh_files).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Select All", command=self.select_all).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Clear Selection", command=self.clear_selection).pack(
            side="left", padx=2
        )
        ttk.Checkbutton(
            toolbar,
            text="Include subfolders",
            variable=self.var_recursive,
            command=self.refresh_files,
        ).pack(side="left", padx=(12, 2))
        ttk.Label(toolbar, textvariable=self.var_file_count).pack(side="right", padx=6)

        list_frame = ttk.Frame(files)
        list_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=4, pady=4)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.listbox = tk.Listbox(
            list_frame,
            selectmode=tk.EXTENDED,
            activestyle="dotbox",
            font=("Consolas", 9),
            exportselection=False,
            height=8,
        )
        self.listbox.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(list_frame, orient="horizontal", command=self.listbox.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.listbox.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        # -- Processing options ----------------------------------------
        options = ttk.LabelFrame(self.root, text="Processing")
        options.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        for column in (1, 3, 5):
            options.columnconfigure(column, weight=1)

        ttk.Label(options, text="Model:").grid(row=0, column=0, sticky="w", **pad)
        self.combo_model = ttk.Combobox(
            options, textvariable=self.var_model, values=list(config_mod.KNOWN_MODELS), width=16
        )
        self.combo_model.grid(row=0, column=1, sticky="ew", **pad)

        ttk.Label(options, text="Device:").grid(row=0, column=2, sticky="w", **pad)
        self.combo_device = ttk.Combobox(
            options,
            textvariable=self.var_device,
            values=[_DEVICE_LABELS["auto"], _DEVICE_LABELS["cpu"]],
            state="readonly",
            width=26,
        )
        self.combo_device.grid(row=0, column=3, sticky="ew", **pad)

        ttk.Label(options, text="If output exists:").grid(row=0, column=4, sticky="w", **pad)
        self.combo_policy = ttk.Combobox(
            options,
            textvariable=self.var_policy,
            values=list(_POLICY_LABELS.values()),
            state="readonly",
            width=28,
        )
        self.combo_policy.grid(row=0, column=5, sticky="ew", **pad)

        buttons = ttk.Frame(options)
        buttons.grid(row=1, column=0, columnspan=6, sticky="ew", padx=4, pady=(2, 6))
        self.btn_start_selected = ttk.Button(
            buttons, text="Start Selected", command=self.start_selected
        )
        self.btn_start_selected.pack(side="left", padx=4)
        self.btn_start_all = ttk.Button(buttons, text="Start All", command=self.start_all)
        self.btn_start_all.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(
            buttons, text="Stop / Cancel", command=self.stop, state="disabled"
        )
        self.btn_stop.pack(side="left", padx=4)

        # -- Status -----------------------------------------------------
        status = ttk.LabelFrame(self.root, text="Status")
        status.grid(row=3, column=0, sticky="ew", padx=8, pady=4)
        status.columnconfigure(1, weight=1)

        ttk.Label(status, text="Current file:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(status, textvariable=self.var_current_file, anchor="w").grid(
            row=0, column=1, columnspan=2, sticky="ew", **pad
        )

        ttk.Label(status, text="Operation:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Label(status, textvariable=self.var_operation, anchor="w").grid(
            row=1, column=1, columnspan=2, sticky="ew", **pad
        )

        ttk.Label(status, text="This file:").grid(row=2, column=0, sticky="w", **pad)
        self.bar_file = ttk.Progressbar(status, orient="horizontal", mode="determinate", maximum=100)
        self.bar_file.grid(row=2, column=1, sticky="ew", **pad)
        ttk.Label(status, textvariable=self.var_file_percent, width=6, anchor="e").grid(
            row=2, column=2, sticky="e", **pad
        )

        ttk.Label(status, text="Overall:").grid(row=3, column=0, sticky="w", **pad)
        self.bar_overall = ttk.Progressbar(
            status, orient="horizontal", mode="determinate", maximum=100
        )
        self.bar_overall.grid(row=3, column=1, sticky="ew", **pad)
        ttk.Label(status, textvariable=self.var_overall_percent, width=6, anchor="e").grid(
            row=3, column=2, sticky="e", **pad
        )

        ttk.Label(status, text="Results:").grid(row=4, column=0, sticky="w", **pad)
        ttk.Label(status, textvariable=self.var_counts, anchor="w").grid(
            row=4, column=1, columnspan=2, sticky="ew", **pad
        )

        ttk.Label(status, text="Status:").grid(row=5, column=0, sticky="w", **pad)
        ttk.Label(status, textvariable=self.var_status, anchor="w").grid(
            row=5, column=1, columnspan=2, sticky="ew", **pad
        )

        env_row = ttk.Frame(status)
        env_row.grid(row=6, column=0, columnspan=3, sticky="ew", padx=4, pady=(2, 6))
        env_row.columnconfigure(1, weight=1)
        ttk.Label(env_row, text="Environment:").grid(row=0, column=0, sticky="w", padx=(2, 6))
        self.label_env = ttk.Label(env_row, textvariable=self.var_env, anchor="w")
        self.label_env.grid(row=0, column=1, sticky="ew")
        ttk.Button(env_row, text="Details", command=self.show_env_details, width=9).grid(
            row=0, column=2, padx=2
        )
        ttk.Button(env_row, text="Re-check", command=self.start_environment_check, width=9).grid(
            row=0, column=3, padx=2
        )

        # -- Activity ---------------------------------------------------
        activity = ttk.LabelFrame(self.root, text="Activity")
        activity.grid(row=4, column=0, sticky="nsew", padx=8, pady=(4, 8))
        activity.columnconfigure(0, weight=1)
        activity.rowconfigure(0, weight=1)
        self.text_log = ScrolledText(
            activity, height=8, wrap="word", font=("Consolas", 9), state="disabled"
        )
        self.text_log.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.text_log.tag_configure("error", foreground="#b00020")
        self.text_log.tag_configure("warning", foreground="#a06000")

        footer = ttk.Frame(self.root)
        footer.grid(row=5, column=0, sticky="ew", padx=8, pady=(0, 6))
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            text="Log: %s" % (self.log_path or "not available"),
            anchor="w",
            foreground="#555555",
        ).grid(row=0, column=0, sticky="ew")

    def _bind_events(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.combo_model.bind("<<ComboboxSelected>>", lambda _e: self.on_model_changed())
        self.combo_model.bind("<Return>", lambda _e: self.on_model_changed())
        self.combo_model.bind("<FocusOut>", lambda _e: self.on_model_changed())
        self.combo_device.bind("<<ComboboxSelected>>", lambda _e: self.save_settings())
        self.combo_policy.bind("<<ComboboxSelected>>", lambda _e: self.save_settings())
        self.listbox.bind("<Double-Button-1>", lambda _e: self.start_selected())
        self.var_input.trace_add("write", lambda *_a: self._folder_edited())
        self.var_output.trace_add("write", lambda *_a: self._folder_edited())

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def _folder_edited(self):
        # Debounce: only persist once typing has settled.
        if getattr(self, "_folder_job", None):
            try:
                self.root.after_cancel(self._folder_job)
            except Exception:  # noqa: BLE001
                pass
        self._folder_job = self.root.after(700, self._commit_folders)

    def _commit_folders(self):
        self._folder_job = None
        new_input = self.var_input.get().strip()
        new_output = self.var_output.get().strip()
        changed = False
        if new_input and new_input != self.cfg["input_dir"]:
            self.cfg["input_dir"] = os.path.abspath(new_input)
            changed = True
        if new_output and new_output != self.cfg["output_dir"]:
            self.cfg["output_dir"] = os.path.abspath(new_output)
            changed = True
        if changed:
            config_mod.save(self.cfg, logger=self.logger)
            self.refresh_files(announce=False)

    def save_settings(self):
        self.cfg["model"] = self.var_model.get().strip() or "htdemucs_ft"
        self.cfg["device"] = _LABEL_TO_DEVICE.get(self.var_device.get(), "auto")
        self.cfg["overwrite_policy"] = _LABEL_TO_POLICY.get(self.var_policy.get(), "prompt")
        self.cfg["input_dir"] = os.path.abspath(self.var_input.get().strip() or paths.INPUT_DIR)
        self.cfg["output_dir"] = os.path.abspath(self.var_output.get().strip() or paths.OUTPUT_DIR)
        try:
            self.cfg["window_geometry"] = self.root.winfo_geometry()
        except Exception:  # noqa: BLE001
            pass
        config_mod.save(self.cfg, logger=self.logger)

    def on_model_changed(self):
        model = self.var_model.get().strip()
        if not model or model == self.cfg.get("model"):
            return
        self.save_settings()
        self.logger.info("Model changed to %s", model)
        self.start_environment_check()

    # ------------------------------------------------------------------
    # Folder actions
    # ------------------------------------------------------------------
    def browse_input(self):
        chosen = filedialog.askdirectory(
            title="Choose the folder containing your audio files",
            initialdir=self.var_input.get() or paths.INPUT_DIR,
        )
        if chosen:
            self.var_input.set(os.path.normpath(chosen))
            self._commit_folders()

    def browse_output(self):
        chosen = filedialog.askdirectory(
            title="Choose where separated stems should be written",
            initialdir=self.var_output.get() or paths.OUTPUT_DIR,
        )
        if chosen:
            self.var_output.set(os.path.normpath(chosen))
            self._commit_folders()

    def open_input(self):
        procutil.open_in_explorer(self.var_input.get() or paths.INPUT_DIR, self.logger)

    def open_output(self):
        procutil.open_in_explorer(self.var_output.get() or paths.OUTPUT_DIR, self.logger)

    def open_logs(self):
        procutil.open_in_explorer(paths.LOGS_DIR, self.logger)

    def open_current_log(self):
        if self.log_path and os.path.isfile(self.log_path):
            try:
                os.startfile(self.log_path)  # noqa: S606
                return
            except Exception as exc:  # noqa: BLE001
                self.logger.error("Could not open the log file: %s", exc)
        self.open_logs()

    def open_readme(self):
        readme = os.path.join(paths.ROOT_DIR, "README.md")
        if os.path.isfile(readme):
            try:
                os.startfile(readme)  # noqa: S606
                return
            except Exception as exc:  # noqa: BLE001
                self.logger.error("Could not open README.md: %s", exc)
        messagebox.showinfo("README", "README.md was not found in %s" % paths.ROOT_DIR)

    # ------------------------------------------------------------------
    # File list
    # ------------------------------------------------------------------
    def refresh_files(self, announce=True):
        previous = {
            self.files[i].path
            for i in self.listbox.curselection()
            if i < len(self.files)
        }
        input_dir = self.var_input.get().strip() or paths.INPUT_DIR

        found, error = audiofiles.scan(input_dir, recursive=self.var_recursive.get())
        self.files = found
        self.listbox.delete(0, tk.END)
        for item in found:
            self.listbox.insert(tk.END, item.display())

        for index, item in enumerate(found):
            if item.path in previous:
                self.listbox.selection_set(index)

        self.var_file_count.set("%d file%s" % (len(found), "" if len(found) == 1 else "s"))

        if error:
            self.var_status.set(error)
            self.logger.warning("Scan problem: %s", error)
        elif announce:
            self.var_status.set(
                "Found %d supported audio file%s in %s"
                % (len(found), "" if len(found) == 1 else "s", input_dir)
            )
            self.logger.info("Scanned %s: %d file(s)", input_dir, len(found))
        elif not found:
            self.var_status.set(
                "No supported audio files in %s - drop some in and press Refresh." % input_dir
            )
        self._update_buttons()

    def select_all(self):
        self.listbox.selection_set(0, tk.END)
        self._update_buttons()

    def clear_selection(self):
        self.listbox.selection_clear(0, tk.END)
        self._update_buttons()

    def _selected_paths(self):
        return [self.files[i].path for i in self.listbox.curselection() if i < len(self.files)]

    # ------------------------------------------------------------------
    # Environment
    # ------------------------------------------------------------------
    def start_environment_check(self):
        if self.env_thread is not None and self.env_thread.is_alive():
            return
        self.var_env.set("Checking environment...")
        self.var_status.set("Checking the Conda environment, Demucs, FFmpeg and CUDA...")
        self._set_env_colour("#555555")
        self.report = None
        self._update_buttons()

        snapshot = dict(self.cfg)
        snapshot["model"] = self.var_model.get().strip() or self.cfg["model"]

        # Tag each probe so a slow, superseded one cannot overwrite the result
        # of the check the user is actually waiting for.
        self._env_token += 1
        token = self._env_token

        def work():
            try:
                report = envcheck.probe(snapshot, self.logger)
            except Exception:  # noqa: BLE001
                self.logger.error("Environment check crashed:\n%s", traceback.format_exc())
                report = envcheck.EnvironmentReport()
                report.problems.append(
                    "The environment check failed unexpectedly. See the log for details."
                )
            self.messages.put({"type": "env_report", "report": report, "token": token})

        self.env_thread = threading.Thread(target=work, name="env-check", daemon=True)
        self.env_thread.start()

    def _apply_env_report(self, report):
        self.report = report
        self.var_env.set(report.summary_line())
        self._set_env_colour("#106010" if report.ready else "#b00020")

        devices = [_DEVICE_LABELS["auto"], _DEVICE_LABELS["cpu"]]
        if report.cuda_available:
            devices.insert(1, _DEVICE_LABELS["cuda"])
        elif _LABEL_TO_DEVICE.get(self.var_device.get()) == "cuda":
            self.var_device.set(_DEVICE_LABELS["auto"])
        self.combo_device.configure(values=devices)

        # The Environment label always updates, but the status line belongs to
        # whatever the user is currently doing: a probe finishing late must not
        # overwrite "Cancelling..." or a just-printed batch summary.
        idle = self.worker is None and not self.busy_task and not self._closing
        if idle:
            if report.ready:
                self.var_status.set(
                    "Ready. %d file%s listed. Separation will run on %s."
                    % (
                        len(self.files),
                        "" if len(self.files) == 1 else "s",
                        "the GPU"
                        if report.resolve_device(self.cfg["device"]) == "cuda"
                        else "the CPU",
                    )
                )
            else:
                self.var_status.set(report.problems[0])
        for note in report.notes:
            self.logger.info("Note: %s", note)
        self._update_buttons()

    def _set_env_colour(self, colour):
        try:
            self.label_env.configure(foreground=colour)
        except Exception:  # noqa: BLE001
            pass

    def show_env_details(self):
        if self.report is None:
            messagebox.showinfo(
                "Environment", "The environment check is still running. Try again in a moment."
            )
            return
        _TextDialog(self.root, "Environment details", self.report.details_text())

    def download_model(self):
        if self.busy_task or (self.worker and self.worker.is_alive()):
            messagebox.showinfo("Busy", "Please wait for the current operation to finish.")
            return
        if self.report is None or not self.report.env_python:
            messagebox.showwarning(
                "Environment not ready",
                "The Conda environment has not been found yet, so the model cannot be "
                "downloaded. Run setup_env.bat first.",
            )
            return

        model = self.var_model.get().strip() or "htdemucs_ft"
        if not messagebox.askyesno(
            "Download model",
            "Download the weights for '%s' now?\n\nThis needs an internet connection "
            "and may take several minutes. Afterwards the app works fully offline." % model,
        ):
            return

        env_python = self.report.env_python
        env_dir = self.report.env_dir
        self.busy_task = "download"
        self.var_operation.set("Downloading model '%s'" % model)
        self.var_status.set("Downloading model weights - this can take a few minutes...")
        self._update_buttons()
        self.bar_file.configure(mode="indeterminate")
        self.bar_file.start(15)

        script = (
            "import sys;"
            "from demucs.pretrained import get_model;"
            "get_model(sys.argv[1]);"
            "print('MODEL_OK')"
        )

        def work():
            # Any escape from this thread would strand busy_task and leave
            # every start control disabled for the rest of the session.
            try:
                code, out, err = procutil.run_capture(
                    [env_python, "-u", "-c", script, model], timeout=3600, env_dir=env_dir
                )
                payload = {
                    "code": code,
                    "ok": code == 0 and "MODEL_OK" in (out or ""),
                    "detail": (err or out or "").strip()[-800:],
                }
            except Exception:  # noqa: BLE001
                payload = {
                    "code": -1,
                    "ok": False,
                    "detail": traceback.format_exc()[-800:],
                }
            payload.update({"type": "download_done", "model": model})
            self.messages.put(payload)

        threading.Thread(target=work, name="model-download", daemon=True).start()

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------
    def start_selected(self):
        paths_to_do = self._selected_paths()
        if not paths_to_do:
            messagebox.showinfo(
                "Nothing selected",
                "Select one or more files in the list first, or use Start All.",
            )
            return
        self._start(paths_to_do)

    def start_all(self):
        if not self.files:
            messagebox.showinfo(
                "No files",
                "No supported audio files were found in:\n%s\n\nAdd some files and "
                "press Refresh." % (self.var_input.get() or paths.INPUT_DIR),
            )
            return
        self._start([item.path for item in self.files])

    def _start(self, file_paths):
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo("Already running", "A separation is already in progress.")
            return
        if self.busy_task:
            messagebox.showinfo("Busy", "Please wait for the current operation to finish.")
            return
        if self.report is None:
            messagebox.showinfo(
                "Please wait", "The environment check has not finished yet."
            )
            return
        if not self.report.ready:
            messagebox.showerror(
                "Environment not ready",
                "Processing is disabled because:\n\n%s\n\nUse Help > Environment Details "
                "for the full report." % "\n\n".join(self.report.problems),
            )
            return

        self.save_settings()

        output_dir = self.cfg["output_dir"]
        if audiofiles.is_inside(output_dir, self.cfg["input_dir"]):
            messagebox.showerror(
                "Output folder",
                "The output folder is inside the input folder:\n\n%s\n\nStems would "
                "then be scanned as input and could overwrite your originals. Choose "
                "an output folder outside the input folder." % output_dir,
            )
            self.logger.error(
                "Refusing to start: output %s is inside input %s",
                output_dir, self.cfg["input_dir"],
            )
            return

        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "Output folder", "Could not create the output folder:\n%s\n\n%s" % (output_dir, exc)
            )
            return

        if self.report.model_cached is False:
            proceed = messagebox.askyesno(
                "Model download required",
                "The weights for '%s' are not cached yet. The first run will download "
                "them (about 320 MB for htdemucs_ft) and needs an internet connection.\n\n"
                "Continue?" % self.cfg["model"],
            )
            if not proceed:
                return

        self._reset_progress(len(file_paths))
        request = processor_mod.JobRequest(file_paths, self.cfg, self.report, self.logger)
        self.logger.info(
            "Starting %d job(s) | model=%s | device=%s | policy=%s",
            len(file_paths), request.model, request.device, request.overwrite_policy,
        )
        self.worker = processor_mod.SeparationWorker(request, self.messages)
        self.worker.start()
        self.var_status.set("Processing %d file(s)..." % len(file_paths))
        self._update_buttons()

    def stop(self):
        # Hold a reference: the confirmation dialog runs a nested event loop,
        # so _poll can clear self.worker while the question is on screen.
        worker = self.worker
        if worker is None or not worker.is_alive():
            return
        if not messagebox.askyesno(
            "Stop processing",
            "Stop after the current step?\n\nFiles that are already finished are kept, "
            "and anything partially written is preserved in a folder marked "
            "'_incomplete_'.",
        ):
            return
        # The worker emits "finished" from inside its own finally block, so it
        # is still is_alive() at that moment - is_alive() alone is not enough.
        # _poll clearing self.worker is the authoritative "batch is done"
        # signal, so compare identity rather than liveness.
        if self.worker is not worker or not worker.is_alive():
            self.logger.info("Cancel ignored: the batch had already finished")
            return
        self.var_status.set("Cancelling...")
        self.var_operation.set("Cancelling - waiting for Demucs to stop")
        self.logger.info("User requested cancellation")
        self.btn_stop.configure(state="disabled")
        threading.Thread(target=worker.cancel, name="cancel", daemon=True).start()

    def _set_counts(self, ok, failed, skipped, cancelled):
        """Render the results line so the parts always add up to Total."""
        text = "Completed %d   Failed %d   Skipped %d" % (ok, failed, skipped)
        if cancelled:
            text += "   Cancelled %d" % cancelled
        text += "   Total %d" % getattr(self, "_total_jobs", 0)
        self.var_counts.set(text)

    def _reset_progress(self, total):
        self.bar_file.configure(mode="determinate")
        self.bar_file.stop()
        self.bar_file["value"] = 0
        self.bar_overall["value"] = 0
        self.var_file_percent.set("0%")
        self.var_overall_percent.set("0%")
        self._total_jobs = total
        self._set_counts(0, 0, 0, 0)
        self.var_current_file.set("-")
        self.var_operation.set("Starting")

    # ------------------------------------------------------------------
    # Queue pump
    # ------------------------------------------------------------------
    def _log_from_any_thread(self, text, level):
        """Called by the logging handler on whatever thread emitted the record."""
        self.messages.put({"type": "log", "text": text, "level": level})

    def _poll(self):
        try:
            for message in processor_mod.drain(self.messages):
                try:
                    self._handle(message)
                except Exception:  # noqa: BLE001 - the UI must survive bad messages
                    self.logger.debug(
                        "Error handling UI message %r:\n%s",
                        message.get("type"), traceback.format_exc(),
                    )
        finally:
            if not self._closing:
                self._poll_job = self.root.after(POLL_INTERVAL_MS, self._poll)
            else:
                self._poll_job = None

    def _handle(self, message):
        kind = message.get("type")

        if kind == "log":
            self._append_log(message["text"], message["level"])
        elif kind == "env_report":
            if message.get("token") == self._env_token:
                self._apply_env_report(message["report"])
            else:
                self.logger.debug("Ignoring a superseded environment report")
        elif kind == "download_done":
            self._handle_download_done(message)
        elif kind == "job_start":
            self.var_current_file.set(
                "%s   (%d of %d)" % (message["name"], message["index"] + 1, message["total"])
            )
            self.bar_file["value"] = 0
            self.var_file_percent.set("0%")
        elif kind == "operation":
            self.var_operation.set(message["text"])
        elif kind == "file_progress":
            value = max(0, min(100, int(message["fraction"] * 100)))
            self.bar_file["value"] = value
            self.var_file_percent.set("%d%%" % value)
        elif kind == "overall":
            value = max(0, min(100, int(message["fraction"] * 100)))
            self.bar_overall["value"] = value
            self.var_overall_percent.set("%d%%" % value)
        elif kind == "counts":
            self._set_counts(
                message.get(processor_mod.OK, 0),
                message.get(processor_mod.FAILED, 0),
                message.get(processor_mod.SKIPPED, 0),
                message.get(processor_mod.CANCELLED, 0),
            )
        elif kind == "job_done":
            self._handle_job_done(message)
        elif kind == "ask_overwrite":
            self._handle_ask_overwrite(message)
        elif kind == "finished":
            self._handle_finished(message)

    def _handle_job_done(self, message):
        status = message["status"]
        name = message["name"]
        if status == processor_mod.OK:
            self.var_operation.set("Finished %s" % name)
        elif status == processor_mod.SKIPPED:
            self.var_operation.set("Skipped %s" % name)
        elif status == processor_mod.CANCELLED:
            self.var_operation.set("Cancelled %s" % name)
        else:
            self.var_operation.set("Failed: %s" % name)
            self._append_log(
                "FAILED  %s - %s" % (name, message.get("error") or "unknown error"), 40
            )

    def _handle_ask_overwrite(self, message):
        action, apply_all = "skip", False
        try:
            dialog = _OverwriteDialog(self.root, message["name"], message["target"])
            action, apply_all = dialog.result
        except Exception:  # noqa: BLE001
            self.logger.error(
                "The overwrite dialog failed; skipping %s to stay safe.\n%s",
                message.get("name"), traceback.format_exc(),
            )
            self.var_status.set(
                "Could not show the overwrite prompt - the file was skipped. See the log."
            )
        finally:
            # The worker is blocked on this event. It must be set on every
            # path, or the batch would hang forever with no way out.
            message["answer"]["action"] = action
            message["answer"]["apply_all"] = apply_all
            message["event"].set()
        self.logger.info(
            "Existing output for %s: user chose '%s'%s",
            message["name"], action, " (for all remaining)" if apply_all else "",
        )

    def _handle_download_done(self, message):
        self.busy_task = None
        self.bar_file.stop()
        self.bar_file.configure(mode="determinate")
        self.bar_file["value"] = 0
        self.var_operation.set("Idle")
        if message["ok"]:
            self.var_status.set("Model '%s' downloaded and cached." % message["model"])
            self.logger.info("Model %s downloaded successfully", message["model"])
            messagebox.showinfo(
                "Model ready",
                "The weights for '%s' are cached. The app can now run offline."
                % message["model"],
            )
            self.start_environment_check()
        else:
            self.var_status.set("Model download failed - see the log for details.")
            self.logger.error(
                "Model download failed (code %s): %s", message["code"], message["detail"]
            )
            messagebox.showerror(
                "Download failed",
                "The model could not be downloaded.\n\nCheck your internet connection "
                "and try again. Details are in the log file.",
            )
        self._update_buttons()

    def _handle_finished(self, message):
        counts = message.get("counts", {})
        ok = counts.get(processor_mod.OK, 0)
        failed = counts.get(processor_mod.FAILED, 0)
        skipped = counts.get(processor_mod.SKIPPED, 0)
        cancelled = counts.get(processor_mod.CANCELLED, 0)

        if message.get("cancelled"):
            self.var_status.set(
                "Cancelled. %d completed, %d failed, %d skipped, %d not processed."
                % (ok, failed, skipped, cancelled)
            )
            self.var_operation.set("Cancelled")
        else:
            self.var_status.set(
                "Finished in %.0f s. %d completed, %d failed, %d skipped."
                % (message.get("elapsed", 0.0), ok, failed, skipped)
            )
            self.var_operation.set("Idle")
            self.bar_overall["value"] = 100
            self.var_overall_percent.set("100%")

        self._set_counts(ok, failed, skipped, cancelled)
        self.var_current_file.set("-")
        self.worker = None
        self._update_buttons()

        if failed and not message.get("cancelled"):
            messagebox.showwarning(
                "Finished with errors",
                "%d file(s) completed, %d failed.\n\nThe Activity pane lists what went "
                "wrong; the log file has full details." % (ok, failed),
            )

    def _append_log(self, text, level):
        tag = None
        if level >= 40:
            tag = "error"
        elif level >= 30:
            tag = "warning"
        self.text_log.configure(state="normal")
        self.text_log.insert(tk.END, text + "\n", tag)
        # Keep the pane bounded so a long batch cannot exhaust memory.
        if int(self.text_log.index("end-1c").split(".")[0]) > 800:
            self.text_log.delete("1.0", "300.0")
        self.text_log.see(tk.END)
        self.text_log.configure(state="disabled")

    # ------------------------------------------------------------------
    # Button state
    # ------------------------------------------------------------------
    def _update_buttons(self):
        running = self.worker is not None and self.worker.is_alive()
        busy = bool(self.busy_task)
        ready = self.report is not None and self.report.ready

        start_state = "normal" if (ready and not running and not busy) else "disabled"
        self.btn_start_selected.configure(state=start_state)
        self.btn_start_all.configure(
            state="normal" if (start_state == "normal" and self.files) else "disabled"
        )
        self.btn_stop.configure(state="normal" if running else "disabled")

        combo_state = "disabled" if (running or busy) else "readonly"
        for combo in (self.combo_device, self.combo_policy):
            combo.configure(state=combo_state)
        self.combo_model.configure(state="disabled" if (running or busy) else "normal")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def on_close(self):
        # A shutdown already in progress must not prompt again if the user
        # clicks the close button a second time while it finishes.
        if self._closing or self._quitting:
            return
        # Same nested-event-loop hazard as stop(): keep our own reference.
        worker = self.worker
        if worker is not None and worker.is_alive():
            if not messagebox.askyesno(
                "Quit",
                "A separation is still running.\n\nStop it and quit? Finished files are "
                "kept and partial output is preserved.",
            ):
                return
            # Same identity check as stop(): _poll may have handled the
            # batch's completion while the confirmation was on screen.
            if self.worker is worker and worker.is_alive():
                self.logger.info("Quit requested while processing - cancelling")
                self.var_status.set("Cancelling before quitting...")
                self.var_operation.set("Stopping Demucs")
                self.btn_stop.configure(state="disabled")
                # Cancelling can take a few seconds (taskkill plus process
                # teardown). Doing it inline would freeze the window, so wait
                # from the event loop instead.
                self._quitting = True
                threading.Thread(target=worker.cancel, name="cancel-on-quit", daemon=True).start()
                self._await_worker(worker, time.time() + 60)
                return

        if self.busy_task and not messagebox.askyesno(
            "Quit",
            "A model download is still running.\n\nQuit anyway? The download will be "
            "stopped and can be restarted later from Tools > Download Selected Model.",
        ):
            return

        self._finish_close()

    def _await_worker(self, worker, deadline):
        """Poll for the worker to finish, then close - without blocking Tk."""
        if worker.is_alive() and time.time() < deadline:
            self.root.after(150, lambda: self._await_worker(worker, deadline))
            return
        if worker.is_alive():
            self.logger.warning(
                "Worker did not stop within the grace period; closing anyway"
            )
        self._finish_close()

    def _finish_close(self):
        self._closing = True
        # Cancel the queued poll so it cannot fire against a destroyed window.
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:  # noqa: BLE001
                pass
            self._poll_job = None
        if self._folder_job is not None:
            try:
                self.root.after_cancel(self._folder_job)
            except Exception:  # noqa: BLE001
                pass
            self._folder_job = None
        try:
            self.save_settings()
        except Exception:  # noqa: BLE001
            pass
        try:
            logging_setup.get_logger().removeHandler(self.ui_handler)
        except Exception:  # noqa: BLE001
            pass
        self.logger.info("Application closed")
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def show_about(self):
        from App import __version__

        messagebox.showinfo(
            "About SeparateAudio",
            "SeparateAudio %s\n\nOffline vocal / drums / bass / other separation "
            "powered by Demucs.\n\nEverything runs locally on this PC - no accounts, "
            "no uploads, no usage limits.\n\nProject folder:\n%s" % (__version__, paths.ROOT_DIR),
        )


class _OverwriteDialog(tk.Toplevel):
    """Modal 'output already exists' prompt. Blocks the caller until answered."""

    def __init__(self, parent, name, target):
        super().__init__(parent)
        self.title("Output already exists")
        self.resizable(False, False)
        self.transient(parent)
        self.result = ("skip", False)
        self._apply_all = tk.BooleanVar(value=False)

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Stems for this track already exist:",
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w")
        ttk.Label(frame, text=name, wraplength=460).pack(anchor="w", pady=(6, 0))
        ttk.Label(frame, text=target, wraplength=460, foreground="#444444").pack(
            anchor="w", pady=(2, 10)
        )
        ttk.Label(
            frame,
            text="Nothing has been changed yet. Choose what to do:",
            wraplength=460,
        ).pack(anchor="w")

        ttk.Checkbutton(
            frame,
            text="Apply this choice to all remaining files",
            variable=self._apply_all,
        ).pack(anchor="w", pady=(10, 8))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Skip this file", command=lambda: self._choose("skip")).pack(
            side="left", padx=3
        )
        ttk.Button(
            buttons, text="New timestamped folder", command=lambda: self._choose("rename")
        ).pack(side="left", padx=3)
        ttk.Button(
            buttons, text="Overwrite the stems", command=self._confirm_overwrite
        ).pack(side="left", padx=3)
        ttk.Button(buttons, text="Stop everything", command=lambda: self._choose("cancel")).pack(
            side="right", padx=3
        )

        self.protocol("WM_DELETE_WINDOW", lambda: self._choose("skip"))
        self.bind("<Escape>", lambda _e: self._choose("skip"))
        self.update_idletasks()
        self._centre(parent)
        self.grab_set()
        self.focus_force()
        parent.wait_window(self)

    def _centre(self, parent):
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
            self.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:  # noqa: BLE001
            pass

    def _confirm_overwrite(self):
        if messagebox.askyesno(
            "Confirm overwrite",
            "Replace the existing stem files in this folder?\n\nAny other files in the "
            "folder are left untouched. This cannot be undone.",
            parent=self,
        ):
            self._choose("overwrite")

    def _choose(self, action):
        self.result = (action, bool(self._apply_all.get()))
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


class _TextDialog(tk.Toplevel):
    """A read-only scrollable text window (used for the environment report)."""

    def __init__(self, parent, title, body):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.geometry("760x520")

        text = ScrolledText(self, wrap="none", font=("Consolas", 9))
        text.pack(fill="both", expand=True, padx=8, pady=8)
        text.insert("1.0", body)
        text.configure(state="disabled")

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(
            buttons, text="Copy to clipboard", command=lambda: self._copy(body)
        ).pack(side="left")
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda _e: self.destroy())
        self.focus_force()

    def _copy(self, body):
        try:
            self.clipboard_clear()
            self.clipboard_append(body)
        except Exception:  # noqa: BLE001
            pass


def main(argv=None):
    """Create the directories, wire up logging and run the GUI."""
    failures = paths.ensure_dirs()

    # Logging is set up before the configuration is read so that a corrupt or
    # invalid config.json is actually recorded in the run log.
    logger, log_path = logging_setup.setup(
        paths.LOGS_DIR, config_mod.OPTIONAL_DEFAULTS["keep_logs"]
    )

    from App import __version__

    logger.info("=" * 72)
    logger.info("SeparateAudio %s starting", __version__)
    logger.info("Project root: %s", paths.ROOT_DIR)
    logger.info("Log file: %s", log_path)
    for path, error in failures:
        logger.error("Could not create directory %s: %s", path, error)

    cfg = config_mod.load(logger=logger)
    if cfg.get("keep_logs") != config_mod.OPTIONAL_DEFAULTS["keep_logs"]:
        logging_setup.prune_old_logs(paths.LOGS_DIR, cfg["keep_logs"], logger)

    # Make sure the configured (possibly custom) folders exist too.
    paths.ensure_dirs(cfg["input_dir"], cfg["output_dir"])
    if not os.path.isfile(paths.CONFIG_PATH):
        config_mod.save(cfg, logger=logger)

    root = tk.Tk()
    root.title("SeparateAudio - offline stem separation")
    root.minsize(900, 720)
    geometry = cfg.get("window_geometry")
    if geometry:
        try:
            root.geometry(geometry)
        except Exception:  # noqa: BLE001
            root.geometry("1020x820")
    else:
        root.geometry("1020x820")

    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:  # noqa: BLE001
        pass

    app = SeparateAudioApp(root, cfg, logger, log_path)

    if failures:
        messagebox.showwarning(
            "Folder problem",
            "Some folders could not be created:\n\n%s"
            % "\n".join("%s (%s)" % item for item in failures),
        )

    try:
        root.mainloop()
    except KeyboardInterrupt:  # pragma: no cover
        app.on_close()
    return 0
