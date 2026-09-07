# SeparateAudio

Offline audio stem separation for Windows. Drop a song in, get `vocals.wav`,
`drums.wav`, `bass.wav` and `other.wav` out.

Everything runs on this PC. There are no accounts, no API keys, no uploads, no
Docker and no usage limits. After the one-time setup the app works with the
network switched off.

---

## Contents

1. [What it does](#what-it-does)
2. [What was installed](#what-was-installed)
3. [Setting up on another PC](#setting-up-on-another-pc)
4. [Launching the app](#launching-the-app)
5. [Normal workflow](#normal-workflow)
6. [Folders](#folders)
7. [Supported input formats](#supported-input-formats)
8. [Output naming convention](#output-naming-convention)
9. [Model and device selection](#model-and-device-selection)
10. [Existing output](#existing-output)
11. [Cancelling](#cancelling)
12. [Configuration](#configuration)
13. [Logs](#logs)
14. [Troubleshooting](#troubleshooting)
15. [Reinstalling the dependencies](#reinstalling-the-dependencies)
16. [Project layout](#project-layout)

---

## What it does

SeparateAudio is a Tkinter desktop front end for
[Demucs](https://github.com/adefossez/demucs), a neural network that splits a
mixed recording back into its component parts. The app scans an input folder,
lets you pick files, runs Demucs on each one in a background process, validates
the result and files the stems into a folder named after the song.

The interface stays responsive throughout: Demucs never runs on the Tkinter main
thread. A worker thread drives the subprocess and reports progress through a
queue that the GUI drains from an `after()` callback.

---

## What was installed

The setup performed on this machine:

| Component | Version / detail | Location |
|---|---|---|
| Miniconda | conda 26.7.1, installed "Just Me", silently, **not** added to `PATH` | `%USERPROFILE%\miniconda3` |
| Conda environment | `separateaudio` (isolated) | `%USERPROFILE%\miniconda3\envs\separateaudio` |
| Python | 3.11.16 | inside the environment |
| PyTorch | 2.5.1 (CUDA 12.4 build) | inside the environment |
| torchaudio | 2.5.1 | inside the environment |
| Demucs | 4.0.1 | inside the environment |
| FFmpeg | conda-forge build | `...\envs\separateaudio\Library\bin` |
| NumPy | 1.26.4 (pinned below 2.0 for Demucs compatibility) | inside the environment |
| Tkinter | bundled with the environment's Python | inside the environment |

Two deliberate choices are worth knowing about:

* **conda-forge only.** Packages come from the `conda-forge` channel with
  `--override-channels`. Anaconda's own default channels now require accepting a
  Terms of Service agreement; conda-forge does not, and it carries no
  commercial-use restrictions. Nothing was accepted on your behalf.
* **PyTorch is pinned to 2.5.1.** PyTorch 2.6 changed the default of
  `torch.load` to `weights_only=True`, which stops Demucs 4.0.1 from loading its
  own checkpoints. 2.5.1 is the newest release that works unmodified.

The Anaconda installer (`Anaconda3-2026.07-1-Windows-x86_64.exe`) was deleted
only after the environment was created and verified end to end. Nothing else on
the system was modified, and no existing Python installation was touched.

### Migrated from Anaconda to Miniconda

This machine originally ran full Anaconda 2026.07-1. It was replaced with
Miniconda, which cut the footprint from about 12 GB to 5.9 GB — SeparateAudio
used none of the extra packages Anaconda bundles.

The replacement was built and verified *before* the old installation was
removed, so there was never a point where nothing worked. In order: Miniconda
was installed alongside Anaconda, the `separateaudio` environment was rebuilt
inside it, that environment passed the self-tests and separated a real track,
and only then was Anaconda uninstalled through its own
`Uninstall-Anaconda3.exe`.

Nothing outside Anaconda was touched. `PATH` was left alone (Miniconda was
installed with `/AddToPath=0 /RegisterPython=0`), and the separate Python 3.11.16
installation at `%LOCALAPPDATA%\Programs\Python` is still what `python` resolves
to on the command line.

Two things deliberately survived the migration:

* **The model weights.** They live in `%USERPROFILE%\.cache\torch`, outside any
  Conda installation, so all 402 MB were reused and nothing was re-downloaded.
* **`config.json` needed no edit.** Its `conda_root` and `env_python` are empty,
  meaning "auto-detect", so the app found Miniconda on its own. This is the same
  mechanism that lets you move or reinstall Conda without reconfiguring anything.

Anaconda's uninstaller left one empty `%USERPROFILE%\.anaconda\keyring` file
behind (0 bytes). It was removed afterwards, to the Recycle Bin rather than
permanently, once it was confirmed to hold nothing and to be untouched since
Miniconda was installed. Conda continued to work normally with it gone.

---

## Setting up on another PC

Nothing in the app is tied to this machine. Install Conda on the new PC, copy the
`SeparateAudio` folder anywhere you like, run `setup_env.bat`, and you are done.
All paths are worked out from wherever `App\` actually sits, so the folder can
live on any drive at any depth.

> The concrete paths shown elsewhere in this README (`%USERPROFILE%\...`) are this
> machine's. On another PC, substitute wherever you put the folder and wherever
> Conda installed itself — you never have to edit anything to make that work.

### 1. What the new PC needs

| Requirement | Detail |
|---|---|
| Windows | 10 or 11, 64-bit |
| Free disk space | About 6.3 GB with Miniconda, 12.4 GB with full Anaconda (measured table below) |
| Internet | Once, for setup — roughly 3 GB of downloads. Afterwards the app never needs the network again |
| Conda | Miniconda, Miniforge **or** Anaconda. Any one of them |
| NVIDIA GPU | **Optional.** Purely for speed. Needs driver 527 or newer for the CUDA 12.4 build |

That is the whole list. There is no separate Python install to manage, no Visual
Studio build tools, and **no CUDA Toolkit** — PyTorch ships its own CUDA runtime.
No administrator rights are needed if you install Conda with "Just Me".

### 2. Install Conda

**Miniconda is the better choice on a fresh PC.** The installer is 126 MB and
the base install measures 456 MB, against about 12 GB for full Anaconda —
SeparateAudio uses none of the extra packages Anaconda bundles. This machine
started on full Anaconda and was migrated to Miniconda for exactly that reason.

* Miniconda — <https://www.anaconda.com/download/success> (scroll to the
  Miniconda section)
* Miniforge — <https://github.com/conda-forge/miniforge> if you would rather use
  a fully community-run build with conda-forge already as the default channel

Choose **Just Me** during installation; the default location is fine, and you do
not need to add it to `PATH`.

You do not have to tell SeparateAudio where it went. `setup_env.bat` and
`launch.bat` both search `CONDA_EXE`, `PATH`,
`%USERPROFILE%\.conda\environments.txt`, the Windows registry, and the usual
folders for `anaconda3`, `miniconda3`, `miniforge3` and `mambaforge`.

### 3. Copy the project folder

Copy these five items to the new PC:

```
App\   config.json   launch.bat   setup_env.bat   README.md
```

`Input\`, `Output\` and `Logs\` are recreated automatically on first launch, so
there is no need to copy them — and you probably do not want the old logs.

> **Moving between machines is handled.** `config.json` stores absolute paths. If
> it still points at the previous PC's `Input`/`Output` folders, the app notices
> on startup, re-points them at the copied project, and records the change in the
> log. A folder you deliberately chose yourself — say `D:\Stems` — is left exactly
> as you set it, even if it does not exist yet.

### 4. Run `setup_env.bat`

Double-click it. It will:

1. locate the Conda installation,
2. create the isolated `separateaudio` environment with Python 3.11,
3. install FFmpeg from conda-forge,
4. check for an NVIDIA GPU and install the matching PyTorch 2.5.1 build — the
   CUDA 12.4 one if `nvidia-smi` is present, the CPU-only one otherwise, falling
   back to CPU automatically if the CUDA download fails,
5. install Demucs 4.0.1 and its audio dependencies,
6. verify the result, and refuse to report success if anything is missing.

Budget 10–30 minutes, nearly all of it downloading PyTorch (2.5 GB for the CUDA
build). Re-running it later is safe and quick: an existing environment is reused
and repaired rather than rebuilt.

### 5. Run `launch.bat`

On the first separation Demucs fetches the model weights into
`%USERPROFILE%\.cache\torch\hub\checkpoints` — 320 MB for `htdemucs_ft`, 80 MB
for `htdemucs`. That is the last thing it ever downloads. Use
**Tools > Download Selected Model** to fetch them deliberately instead of
waiting for the first run.

### Disk space, measured on this machine

| Item | Size |
|---|---|
| Miniconda base, without environments or package cache | 456 MB |
| `separateaudio` environment (CUDA build) | 5.1 GB |
| Conda package cache (`miniconda3\pkgs`) | 1.2 GB |
| **Whole `miniconda3` tree, as it actually sits on disk** | **5.9 GB** |
| Model weights — `htdemucs` | 80 MB |
| Model weights — `htdemucs_ft` | 320 MB (a bag of 4 × 80 MB) |
| The project folder itself | 543 KB |

The parts do not add up to the total because Conda hard-links packages from
`pkgs` into each environment, so the same bytes are counted twice by anything
that measures the folders separately.

So **about 6.3 GB all-in** with Miniconda — the 5.9 GB tree plus 402 MB of model
weights — against roughly **12.4 GB for the same setup on full Anaconda**, which
is what this machine ran before the migration. Add room for the stems you
produce: uncompressed WAV runs about **42 MB per minute of audio** for four
stems.

You can reclaim the 1.2 GB package cache at any time with
`%USERPROFILE%\miniconda3\Scripts\conda.exe clean --all`. It only holds installer
archives; removing them does not affect the environment, though it does mean a
future reinstall re-downloads instead of unpacking locally.

A PC with no NVIDIA GPU gets a noticeably smaller environment — the CPU-only
PyTorch wheel is around 200 MB against 2.5 GB for the CUDA one.

### A PC with no NVIDIA GPU

Fully supported, and there is nothing to configure. `setup_env.bat` finds no
`nvidia-smi`, installs the CPU build, and the **Device** dropdown then offers
only *Auto* and *CPU* — the CUDA entry is hidden because it was never detected.

Expect a few minutes per track with `htdemucs_ft` on a typical desktop CPU.
Switching the model to `htdemucs` gives roughly a 4× speed-up for a modest
quality cost.

### Installing where the target PC has no internet

Everything can be prepared on a connected machine and carried across:

1. Install Conda on **both** machines.
2. On the connected PC, run `setup_env.bat` as normal, then package the finished
   environment:
   ```bat
   <conda root>\Scripts\conda.exe install -y --override-channels -c conda-forge conda-pack
   <conda root>\Scripts\conda-pack.exe -n separateaudio -o separateaudio.tar.gz
   ```
3. On the offline PC, extract that archive into
   `<conda root>\envs\separateaudio` and run
   `<conda root>\envs\separateaudio\Scripts\conda-unpack.exe` to fix the
   internal paths.
4. Copy `%USERPROFILE%\.cache\torch\hub\checkpoints` across as well, so the
   model weights do not need downloading.

`conda-pack` exists precisely for this and handles both the conda and pip
packages, which a plain `conda list --explicit` export would not. If the
environment ends up somewhere unusual, set `env_python` in `config.json` to its
`python.exe`; the app will use it directly and note that in the log.

### Verifying a fresh install

```bat
<conda root>\envs\separateaudio\python.exe -m App.selftest
```
Run from the project folder. Prints `154 passed, 0 failed` in about a second and
needs neither Demucs nor audio files.

```bat
<conda root>\envs\separateaudio\python.exe -I App\_probe.py htdemucs_ft
```
Prints a JSON report of Python, Demucs, PyTorch, CUDA, FFmpeg and model-cache
status, and exits non-zero if anything essential is missing.

Inside the app the same information is under **Help > Environment Details**, and
the **Environment** line at the bottom of the window turns green only when
processing is actually possible.

---

## Launching the app

Double-click:

```
%USERPROFILE%\Desktop\SeparateAudio\launch.bat
```

`launch.bat` finds the `separateaudio` environment on its own — you never need to
run `conda activate`. It looks in `CONDA_EXE`, the usual Anaconda/Miniconda
install locations, `%USERPROFILE%\.conda\environments.txt` and finally any
`conda` on `PATH`, so the app keeps working if Anaconda is moved or reinstalled
elsewhere.

To start it from a terminal instead:

```bat
%USERPROFILE%\miniconda3\envs\separateaudio\python.exe %USERPROFILE%\Desktop\SeparateAudio\App\main.py
```

---

## Normal workflow

1. Put audio files in `%USERPROFILE%\Desktop\SeparateAudio\Input`.
2. Run `launch.bat`.
3. Wait for **Environment** at the bottom to turn green and read *Ready*.
4. Select files in the list, or just press **Start All**.
5. Collect the stems from `%USERPROFILE%\Desktop\SeparateAudio\Output`.

Press **Refresh** if you add files while the app is already open.

---

## Folders

| Purpose | Path |
|---|---|
| Input | `%USERPROFILE%\Desktop\SeparateAudio\Input` |
| Output | `%USERPROFILE%\Desktop\SeparateAudio\Output` |
| Logs | `%USERPROFILE%\Desktop\SeparateAudio\Logs` |
| Source code | `%USERPROFILE%\Desktop\SeparateAudio\App` |

All four are created automatically if missing. You can point Input and Output
somewhere else with the **Browse...** buttons; the choice is remembered.

**Your input files are never modified, renamed, moved or deleted.** The app only
ever reads them.

---

## Supported input formats

`.wav` `.mp3` `.flac` `.ogg` `.m4a` `.aac`

Also accepted: `.opus` `.wma` `.aiff` `.aif` `.alac` `.mp4`

Only files with these extensions appear in the list. Tick **Include subfolders**
to scan recursively.

Stems are always written as **WAV**, whatever the input was. A lossless source is
never re-encoded to a lossy format on the way through — Demucs decodes the
original and writes uncompressed WAV.

---

## Output naming convention

Each input file gets its own folder, named after the file without its extension:

```
Input\My Song.mp3
  ->
Output\My Song\
    vocals.wav
    drums.wav
    bass.wav
    other.wav
```

Two different songs never share a folder. Names are sanitised for Windows:

* `< > : " / \ | ? *` and control characters become `_`
* trailing dots and spaces are removed
* reserved device names (`CON`, `NUL`, `COM1`, `LPT9`, ...) get a leading `_`
* names longer than 110 characters are truncated, keeping the full path well
  inside the 260-character limit
* a name that would end up empty becomes `untitled`

If two selected files would end up with the same folder name — most often two
tracks called `intro.wav` in different album subfolders, when **Include
subfolders** is ticked — the second gets `intro (2)`, the third `intro (3)`, and
so on. They never overwrite each other, and the log records which file went
where.

Models that produce more stems (for example `htdemucs_6s`, which adds `guitar`
and `piano`) write those into the same folder using the same lowercase naming.

Demucs writes into a temporary workspace first. Stems are only moved into the
song folder after they have been verified to exist and be non-empty, so a failed
run cannot leave a half-populated folder that looks complete.

---

## Model and device selection

**Model** — default `htdemucs_ft`. This is the fine-tuned Hybrid Transformer
model: the best quality Demucs offers, and roughly four times slower than plain
`htdemucs` because it is a bag of four models. The dropdown also offers
`htdemucs`, `htdemucs_6s`, `hdemucs_mmi`, `mdx`, `mdx_extra`, `mdx_q` and
`mdx_extra_q`, and you can type any other Demucs model name.

**Device**

| Setting | Behaviour |
|---|---|
| Auto | Uses the GPU when a working CUDA device is present, otherwise the CPU |
| CPU | Always the CPU |
| CUDA | Only offered when a CUDA GPU was actually detected |

CUDA detection is done by asking PyTorch inside the environment, in a guarded
subprocess. A broken driver reports "not available" rather than crashing the app,
and processing falls back to the CPU. **An NVIDIA GPU is not required.**

This machine has an NVIDIA GeForce RTX 2060 SUPER, so Auto uses the GPU. On the
CPU a four-minute track with `htdemucs_ft` typically takes several minutes; on
this GPU it is a small fraction of that.

The first run with a given model downloads its weights (about 320 MB for
`htdemucs_ft`) into `%USERPROFILE%\.cache\torch\hub\checkpoints`. After that the
app is fully offline. You can pre-fetch them from **Tools > Download Selected
Model**.

---

## Existing output

If a song's output folder already exists and contains files, nothing is
overwritten silently. What happens next depends on **If output exists**:

| Setting | Behaviour |
|---|---|
| Ask me each time (default) | A dialog offers Skip / New timestamped folder / Overwrite / Stop everything, with an "apply to all remaining" option |
| Skip the file | Leaves the existing folder untouched and moves on |
| Overwrite the stems | Replaces only the stem files it writes; every other file in that folder is left alone |
| Create a new timestamped folder | Writes to `My Song_20260907_184530` and leaves the original intact |

Choosing Overwrite in the dialog asks for a second confirmation. Existing output
is never deleted without an explicit confirmation, and only stem files are ever
replaced — the whole folder is never wiped.

---

## Cancelling

**Stop / Cancel** asks for confirmation, then:

* stops starting new files,
* terminates the running Demucs process and its children,
* keeps every file that already finished,
* moves anything partially written into `<song name>_incomplete_<timestamp>`,
* sets the status to *Cancelled*.

The GUI itself is never killed — it stays open and usable. Closing the window
during a run offers the same clean cancellation.

---

## Configuration

`%USERPROFILE%\Desktop\SeparateAudio\config.json`

```json
{
  "input_dir": "%USERPROFILE%\\Desktop\\SeparateAudio\\Input",
  "output_dir": "%USERPROFILE%\\Desktop\\SeparateAudio\\Output",
  "model": "htdemucs_ft",
  "device": "auto",
  "overwrite_policy": "prompt"
}
```

Loaded at startup and saved whenever you change a setting or close the app.

Optional extra keys:

| Key | Default | Meaning |
|---|---|---|
| `conda_root` | `""` | Force a specific Conda installation instead of auto-detecting |
| `env_python` | `""` | Force a specific interpreter (skips all discovery) |
| `env_name` | `"separateaudio"` | Name of the Conda environment to use |
| `keep_logs` | `30` | How many run logs to keep before the oldest are pruned |
| `extra_demucs_args` | `[]` | Extra CLI arguments passed to Demucs, e.g. `["--shifts", "2"]` |
| `window_geometry` | `""` | Remembered window size and position |

Arguments that would change the output format or location (`--mp3`, `--flac`,
`--mp3-bitrate`, `-o`, `--out`, `--filename`) are ignored and noted in the log:
stems are always WAV, always in the song's own folder. Anything else, such as
`--shifts`, is passed straight through.

**`--two-stems` is fully supported.** Demucs splits the track into one named
stem and everything else, so

```json
"extra_demucs_args": ["--two-stems=vocals"]
```

produces `vocals.wav` and `no_vocals.wav` — a karaoke split — instead of the
usual four files. Any of the model's own stem names works: `drums` gives
`drums.wav` and `no_drums.wav`, and so on. Both `--two-stems=vocals` and
`--two-stems vocals` are accepted.

The app adjusts what it expects to find so a two-stem run is verified against
the two files it will actually write. It also halves the disk-space estimate,
since only two stems are produced. Runtime is unchanged: Demucs still runs the
model in full and merges the other sources afterwards, so a four-pass bag like
`htdemucs_ft` still takes four passes.

`device` accepts `auto`, `cpu` or `cuda`. `overwrite_policy` accepts `prompt`,
`skip`, `overwrite` or `rename`. If the file is corrupt or a value is invalid,
the app logs it, backs the file up as `config.json.corrupt-<timestamp>.bak` and
carries on with defaults — a bad config can never stop the app from starting.

---

## Logs

One log file per launch:

```
%USERPROFILE%\Desktop\SeparateAudio\Logs\separateaudio_YYYYMMDD_HHMMSS.log
```

Each records startup, the full environment check (Python, Demucs, PyTorch,
FFmpeg and CUDA versions), the model and device in use, the exact Demucs command
line, every input file with start and end times, the output path, success or
failure, cancellations, and full tracebacks for anything unexpected.

The **Activity** pane in the app shows a readable summary of the same events.
Raw tracebacks go to the log file, never to a message box — error dialogs explain
what to do instead. The 30 most recent logs are kept; older ones are pruned, and
only files matching the app's own naming pattern are ever removed.

Open them from **Tools > Open Logs Folder**.

---

## Troubleshooting

**"Environment: Not ready" / the Start buttons are greyed out**
Press **Details** for the full report — it names exactly what is missing. In
almost every case the fix is to run `setup_env.bat`.

**"No Conda installation was found"**
Anaconda or Miniconda was moved or uninstalled. Reinstall it, or set
`conda_root` in `config.json` to its folder.

**"The Conda environment 'separateaudio' was not found"**
Run `setup_env.bat`. It is safe to re-run at any time.

**"FFmpeg was not found"**
```bat
%USERPROFILE%\miniconda3\Scripts\conda.exe install -n separateaudio -y --override-channels -c conda-forge ffmpeg
```

**The first run stalls at 0%**
It is downloading the model weights (about 320 MB). This happens once. Use
**Tools > Download Selected Model** to do it deliberately with a progress
indicator.

**"The GPU ran out of memory"**
Set **Device** to CPU and try again, or use the lighter `htdemucs` model. An
8 GB card can run out on very long tracks.

**A file fails with "corrupt or unsupported encoding"**
Check it plays in a media player. Converting it to WAV first usually fixes it:
```bat
%USERPROFILE%\miniconda3\envs\separateaudio\Library\bin\ffmpeg.exe -i "broken.m4a" "fixed.wav"
```

**"Not enough free disk space"**
Stems are uncompressed WAV — roughly 42 MB per minute of audio for four stems.
Free some space or point Output at another drive.

**Everything is slow**
`htdemucs_ft` runs four models per track. Switch the model to `htdemucs` for
roughly a 4x speed-up at a small quality cost.

**The app will not start at all**
Run it from a terminal to see the error:
```bat
%USERPROFILE%\miniconda3\envs\separateaudio\python.exe %USERPROFILE%\Desktop\SeparateAudio\App\main.py
```

**Checking the app's own logic**
```bat
%USERPROFILE%\miniconda3\envs\separateaudio\python.exe -m App.selftest
```
Run it from the project folder. It exercises naming, scanning, output
resolution, verification, config handling, argument filtering, logging
resilience and progress maths without needing Demucs — 154 checks that finish
in about a second.

**Checking the environment from a terminal**
```bat
%USERPROFILE%\miniconda3\envs\separateaudio\python.exe -I App\_probe.py htdemucs_ft
```
Prints a JSON report of Python, Demucs, PyTorch, CUDA, FFmpeg and model-cache
status, and exits non-zero if anything essential is missing.

---

## Reinstalling the dependencies

Re-run `setup_env.bat`. It reuses an existing environment and repairs what is
missing.

To start completely fresh:

```bat
%USERPROFILE%\miniconda3\Scripts\conda.exe env remove -n separateaudio -y
```

then run `setup_env.bat` again. This removes only the `separateaudio`
environment; the rest of your Conda installation and any other environments are
untouched.

To upgrade or repair individual pieces:

```bat
%USERPROFILE%\miniconda3\envs\separateaudio\python.exe -m pip install --force-reinstall demucs==4.0.1
%USERPROFILE%\miniconda3\envs\separateaudio\python.exe -m pip install --force-reinstall torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu124
```

Use `.../whl/cpu` instead of `.../whl/cu124` on a machine without an NVIDIA GPU.

---

## Project layout

```
%USERPROFILE%\Desktop\SeparateAudio\
├── App\                  application source
│   ├── main.py           entry point
│   ├── gui.py            Tkinter interface
│   ├── processor.py      Demucs worker thread and subprocess handling
│   ├── envcheck.py       Conda / Demucs / FFmpeg / CUDA detection
│   ├── audiofiles.py     scanning, sanitising, output resolution, verification
│   ├── config.py         config.json load and save
│   ├── logging_setup.py  per-run logging with pruning
│   ├── procutil.py       Windows subprocess helpers
│   ├── paths.py          project locations
│   ├── _probe.py         runs inside the Conda env to report versions
│   └── selftest.py       offline test suite
├── Input\                your audio files
├── Output\               generated stems, one folder per song
├── Logs\                 one log per run
├── config.json           settings
├── launch.bat            starts the GUI in the Conda environment
├── setup_env.bat         creates or repairs the environment
└── README.md             this file
```

Source, input and generated output are kept strictly separate.
