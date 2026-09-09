# FixDrops

A Windows-oriented GUI for selective dropped-frame repair.

**[Русская версия](README.ru.md)**

FixDrops analyzes video presentation timestamps (PTS), maps source images onto a constant-frame-rate timeline, and selectively interpolates empty frame slots.

It supports OpenCV DIS optical flow and an external `rife-ncnn-vulkan` executable, with NVIDIA NVENC or CPU encoding.

> **Experimental software:** Keep an independent backup of important footage. Verify orientation, audio/video synchronization, and repaired sections before using the output for production or multicamera editing.

![Uploading image.png…]()

## Features

- Nominal source rates limited to **24, 30, and 60 FPS**.
- Automatic nominal-FPS selection based on median frame intervals.
- Manual source-FPS override.
- Preliminary metadata-based quality labels before full analysis.
- Selective interpolation of empty output slots.
- Optional interpolation at frame-assignment collisions.
- Optional half-rate output without intentionally changing playback speed.
- RIFE or OpenCV DIS interpolation.
- NVIDIA NVENC or CPU encoding.
- HEVC and H.264 output.
- Source-bitrate targeting or an explicit bitrate.
- Ordinary 90°/180°/270° rotation metadata applied to the images.
- Multiple-file processing and drag-and-drop.
- Analysis or repair of selected files or the entire list.
- Workload-based progress and approximate time estimates.
- Persistent file list, selection, processing history, and last reported progress.
- Force stop and stop-after-current controls.
- Original-file preservation in `original(dropped)`.
- JSON reports with relative `HH:MM:SS:FF` timecodes.

## What this tool detects

The repair algorithm relies on timestamps.

It is suitable when missing frames leave unusually large gaps between adjacent PTS values.

It does **not** detect all cases of:
- Missing frames replaced with duplicate images and regular timestamps.
- Missing frames followed by timestamp rewriting.
- Genuine variable-rate capture that resembles dropped frames.

An empty output-grid slot is not necessarily a physical camera drop.

## Requirements

- Windows is the primary target.
- Python **3.10 or newer**.
- FFmpeg and FFprobe available in `PATH`.
- Python dependencies from `requirements.txt`.
- A compatible NVIDIA GPU and driver for NVENC, or CPU encoding.
- A Vulkan-capable GPU and compatible RIFE distribution for GPU RIFE interpolation.

The current processing path targets progressive, square-pixel, **8-bit SDR video**.

HDR, interlaced video, non-square pixels, arbitrary rotation angles, and detected mirrored display matrices are not supported.

### Hardware usage

| Operation | Hardware |
|---|---|
| Timestamp analysis and decoding | CPU |
| OpenCV DIS optical flow | CPU |
| RIFE interpolation | External `rife-ncnn-vulkan`, typically GPU/Vulkan |
| Encoding with NVENC | NVIDIA video encoder |
| CPU encoding | CPU |

RIFE does not require a PyTorch/CUDA environment in this integration.

## Installation

Install Python dependencies:

```bash
python -m pip install -r requirements.txt
```

Install FFmpeg separately and verify:

```bash
ffmpeg -version
ffprobe -version
```

Start the application:

```bash
python fixdrops_gui.py
```

Keep these files together:

```text
fix_drops.py
fixdrops_gui.py
fixdrops_gui_impl.py
fixdrops_worker.py
fixdrops_worker_impl.py
```

The `_impl.py` files are required, not obsolete backups.

## RIFE setup

The application uses the external project:

https://github.com/nihui/rife-ncnn-vulkan

If your package does not include it, obtain a compatible Windows release from:

https://github.com/nihui/rife-ncnn-vulkan/releases

Extract the complete distribution, preserving its accompanying files and license notices.

In the GUI:

1. Select **Engine → RIFE**.
2. Set **RIFE executable** to `rife-ncnn-vulkan.exe`.
3. Set **RIFE model folder** to a compatible RIFE v4 model directory.
4. Select the appropriate **GPU ID**.
5. Run **Benchmark** on a short clip.

Example paths:

```text
third_party/rife-ncnn-vulkan/rife-ncnn-vulkan.exe
third_party/rife-ncnn-vulkan/rife-v4.6/
```

The model directory must contain actual model files, not just an empty folder.

### RIFE implementation notes

- Arbitrary timesteps are needed to fill gaps at the correct target times.
- The current integration starts a separate RIFE process for each synthesized image.
- Reference PNGs are reused for repeated interpolation of the same frame pair.
- Model-loading, process-startup, and disk I/O overhead can be substantial.
- Full-resolution inference may exceed available GPU memory.
- This integration does not implement automatic tiling or resolution reduction.
- The worker handles the observed decimal-separator issue by trying comma/dot timestep formatting when RIFE reports an invalid timestep.

This is a functional integration, not a persistent, maximum-throughput RIFE pipeline.

## Basic workflow

1. Add files or a folder, or drag them into the file list.
2. Review the preliminary FPS information.
3. Select the interpolation engine and source FPS.
4. Run a benchmark for the selected settings.
5. Use **Analyze selected**, **Analyze all**, **Repair selected**, or **Repair all**.
6. Check the result in your video editor.

Folder addition is non-recursive. The `original(dropped)` folder is excluded from normal folder import.

Paths containing spaces are supported.

## Preliminary status

Before full analysis, the GUI compares metadata average FPS with the nearest allowed nominal rate, or with the manually selected rate.

| Relative difference | Label |
|---|---|
| Zero, within floating-point tolerance | `Excellent` |
| Greater than zero, up to 0.3% | `Very good` |
| Greater than 0.3%, up to 2% | `Good` |
| Greater than 2%, up to 5% | `Fair` |
| Greater than 5%, up to 10% | `Poor` |
| Greater than 10% | `Very poor` |

**These are heuristic labels, not a verified assessment of image quality or dropped frames.**

For example, a reported average of 30 FPS might represent a normal 30 FPS source or a heavily damaged higher-rate source. Select the known nominal rate manually when possible.

### Allowed rates

Automatic analysis selects only:

```text
24 / 30 / 60
```

Fractional rates are not preserved as separate output standards:

```text
23.976 → 24
29.97  → 30
59.94  → 60
```

This changes the selected output grid, not the underlying source PTS.

If the median-based estimate is more than 5% away from the nearest allowed nominal rate, automatic analysis requests a manual override.

## Main settings

| Setting | Purpose |
|---|---|
| Engine | `DIS fast`, `DIS medium`, or `RIFE` |
| Source FPS | `auto`, `24`, `30`, or `60` |
| Collisions | Choose the nearest original image or interpolate the target moment |
| Half FPS | Output at half the nominal rate |
| Encoder | `nvenc` or `cpu` |
| Codec | `hevc` or `h264` |
| Flow width | Resolution used for DIS motion estimation |
| Bitrate Mbps | Explicit video bitrate; blank targets source bitrate |
| RIFE executable | Path to the external executable |
| RIFE model folder | Path to the model directory |
| RIFE GPU ID | Device index used by RIFE |

`Flow width` affects DIS, not RIFE.

Half-rate examples:

```text
60 → 30
30 → 15
24 → 12
```

The bitrate is a VBR target. Actual average bitrate and file size may differ from the source.

## Progress and benchmark

The initial benchmark estimates:
- Decoding cost.
- Image conversion cost.
- Interpolation cost.
- Encoding cost.

Before analysis, the predicted repair workload is based on metadata.

After analysis, the worker builds a frame-assignment plan and counts:
- Output/encoding frames.
- Empty slots.
- Collision slots.
- Planned interpolator calls.

Progress then weights ordinary frame processing and interpolation separately. A long section without drops does not have the same estimated cost as a section with many RIFE calls.

### Accuracy

The display uses integer percentages, but **this does not imply ±1% time-estimation accuracy**.

- Disk speed, model startup, GPU load, and scene complexity affect runtime.
- Scene-cut and edge handling may produce holds instead of synthesized images.
- An interpolator call is not necessarily a neural inference.
- The prediction may change after analysis and as runtime measurements become available.
- Queue progress represents completed files plus a fraction of the current file, not a precise time-weighted estimate for the entire queue.

A saved benchmark can be rerun after changing hardware, drivers, models, or relevant settings.

## Original-file handling

Before actual repair, the original is moved into:

```text
original(dropped)/
```

A temporary output is generated and checked. After successful completion, it receives the original filename.

Example:

```text
Videos/
├── clip.mp4
├── clip.mp4.drops.json
└── original(dropped)/
    └── clip.mp4
```

The top-level `clip.mp4` is the repaired result.

Existing backup files are never intentionally overwritten. Repair is blocked if a same-named backup already exists.

> Moving an original into a subfolder is not an independent backup. Keep a separate copy for important footage.

## Stopping processing

### Stop after current

Finishes the current file and cancels the remaining queue.

### STOP NOW

Requests termination of the worker process tree, including its FFmpeg/RIFE child processes, then attempts to restore the original if its original path is free.

On Windows, process-tree termination uses `taskkill /T /F`.

If the result has already been committed, the completed output may remain while the original stays in `original(dropped)`.

Do not close the application forcibly during recovery. After a crash or power loss, inspect:
- The original file location.
- `original(dropped)`.
- Remaining `.fixdrops_job_*` directories.

There is no automatic resume from an intermediate video frame.

## Session and history

State is saved automatically:
- Approximately every two seconds when changed.
- After completed jobs.
- On normal close.
- When the list is cleared.

**Save session is optional** and only forces an immediate save.

Stored state includes:
- File list.
- Selected rows.
- Processing status.
- Last reported progress.
- File size and modification time.

### Saved status labels

| Label | Meaning |
|---|---|
| `✓ Fixed` | This application recorded successful repair |
| `✓ Zero FPS diff` | Metadata FPS matches the selected nominal rate |
| `Analyzed` | Analysis completed |
| `Stopped` | Stopped during this session |
| `Interrupted` | A previously active job was not recorded as completed |
| `Error` | Processing failed |
| `File changed` | Size or modification time differs from the saved record |

**`✓ Zero FPS diff` is not proof that the video has no drops.**

History is keyed by full path and checked against file size/modification time. This is not a content hash and does not detect every possible file change.

Interrupted files restart from the beginning. Their saved percentage is historical information, not a resumable checkpoint.

### Clear list

Clears the visible and saved file list without deleting:
- Media files.
- Original backups.
- Processing history.

Re-adding an unchanged file can restore its saved status.

## Local application data

By default on Windows:

```text
%LOCALAPPDATA%\FixDrops\
├── gui.json
├── benchmarks_gui_v2.json
└── session_v3.json
```

Session files and reports contain local paths and filenames. Review them before sharing publicly.

## Timing and audio

Output positions remain anchored to source timestamps.

The temporary encoded stream's first PTS is measured and compensated before final muxing.

Audio is copied directly from the original without intentional re-encoding, stretching, or trimming.

However:

- Individual source images may be moved slightly in presentation time when mapped to CFR.
- All output images undergo color conversion and video re-encoding.
- The video end is rounded up to a whole output frame.
- Complex container edit lists and editor-specific behavior are not exhaustively validated.
- Existing drift between independently recorded cameras is not corrected.

The automatic check verifies the output video start and, when available, the reported frame count. It is not a complete audiovisual synchronization test.

## Reports and timecodes

Reports use relative:

```text
HH:MM:SS:FF
```

They start at the first source video frame and use the selected nominal source rate.

They are not embedded camera timecodes. Camera timecode/data streams and general source metadata are not preserved by this version.

Reports include timing statistics, suspected gaps, processing counters, and timing-check results.

## Audio sample verification

To compare the first decoded audio stream:

```bash
ffmpeg -v error -i "original(dropped)/clip.mp4" -map 0:a:0 -vn -c:a pcm_s32le -f hash -hash sha256 -
```

```bash
ffmpeg -v error -i clip.mp4 -map 0:a:0 -vn -c:a pcm_s32le -f hash -hash sha256 -
```

Matching hashes indicate matching decoded sample sequences in this comparison.

Hashes do not verify the audio's temporal placement relative to video.

## Troubleshooting

### RIFE executable is not selected

Select the actual `rife-ncnn-vulkan.exe` file, not its directory.

### RIFE model fails to load

Verify the model files, model compatibility, and extracted distribution structure.

### RIFE runs out of GPU memory

Test a smaller clip or a lower-resolution source. This version does not automatically tile RIFE inference.

### NVENC fails

Check the NVIDIA driver and FFmpeg build, or select CPU encoding.

### Repair is blocked by an existing backup

Inspect `original(dropped)` before changing anything. The block protects the original from accidental replacement.

### Timing verification fails

Do not use the result for timing-critical work until the discrepancy is understood. Keep the log and JSON report.

## Third-party software and licensing

FixDrops is an independent project and is not an official RIFE, ncnn, or FFmpeg release.

The FixDrops project code is distributed under the [MIT License](LICENSE).

Third-party executables, libraries, and model weights retain their own licenses. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and the notices included with each distribution.

The project's MIT license does not automatically cover bundled RIFE model weights or an FFmpeg binary.

## Reporting issues

Include:
- The exact command or GUI settings.
- OS, Python, FFmpeg, and dependency versions.
- RIFE executable/model version, if used.
- Input resolution, codec, nominal FPS, and orientation.
- The relevant log and JSON report.
- A short sample, if you have permission to share it.

Remove private paths or other sensitive information before posting.
