import hashlib
import json
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from fractions import Fraction
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    RootClass = TkinterDnD.Tk
except ImportError:
    DND_FILES = None
    RootClass = tk.Tk


APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home()))
) / "FixDrops"

CONFIG_FILE = CONFIG_DIR / "gui.json"
BENCH_FILE = CONFIG_DIR / "benchmarks_gui_v2.json"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv"}

RATES = [
    Fraction(24000, 1001), Fraction(24), Fraction(25),
    Fraction(30000, 1001), Fraction(30), Fraction(50),
    Fraction(60000, 1001), Fraction(60),
    Fraction(120000, 1001), Fraction(120),
]


class Cancelled(Exception):
    pass


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def benchmark_key(settings):
    selected = {
        name: settings.get(name)
        for name in (
            "engine", "rife_exe", "rife_model", "gpu",
            "flow_width", "encoder", "codec", "bitrate",
        )
    }

    for name in ("rife_exe", "rife_model"):
        value = selected.get(name)
        if value:
            try:
                selected[name + "_mtime"] = Path(value).stat().st_mtime
            except OSError:
                pass

    text = json.dumps(selected, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def finite_float(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def fps_float(value):
    try:
        return float(Fraction(value))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def human_time(seconds):
    if seconds is None or not math.isfinite(seconds):
        return "—"

    seconds = max(1, round(seconds))

    if seconds < 60:
        return f"~{seconds}s"

    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"~{minutes}m {seconds:02d}s"

    hours, minutes = divmod(minutes, 60)
    return f"~{hours}h {minutes:02d}m"


def quick_probe(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_streams", "-show_format",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ffprobe error")

    info = json.loads(result.stdout)
    streams = info.get("streams", [])

    if not streams:
        raise RuntimeError("No video stream")

    stream = streams[0]
    duration = finite_float(stream.get("duration"))

    if duration <= 0:
        duration = finite_float(
            info.get("format", {}).get("duration")
        )

    return {
        "avg": fps_float(stream.get("avg_frame_rate", "0/0")),
        "duration": duration,
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "frames": finite_float(stream.get("nb_frames")),
    }


class App(RootClass):
    def __init__(self):
        super().__init__()

        self.title("FixDrops")
        self.geometry("1320x850")
        self.minsize(1050, 650)

        self.events = queue.Queue()
        self.rows = {}
        self.known_paths = {}

        self.busy = False
        self.stop_after = threading.Event()
        self.cancel_now = threading.Event()

        # Защищает запуск/завершение текущего worker.
        self.process_lock = threading.Lock()
        self.current_process = None

        self.auto_benchmark_offered = False
        self.benchmarks = load_json(BENCH_FILE, {})

        config = load_json(CONFIG_FILE, {})

        self.engine = tk.StringVar(
            value=config.get("engine", "DIS medium")
        )
        self.rife_exe = tk.StringVar(
            value=config.get("rife_exe", "")
        )
        self.rife_model = tk.StringVar(
            value=config.get("rife_model", "")
        )
        self.gpu = tk.StringVar(value=config.get("gpu", "0"))
        self.source_fps = tk.StringVar(
            value=config.get("source_fps", "auto")
        )
        self.collision = tk.StringVar(
            value=config.get("collision", "nearest")
        )
        self.encoder = tk.StringVar(
            value=config.get("encoder", "nvenc")
        )
        self.codec = tk.StringVar(
            value=config.get("codec", "hevc")
        )
        self.flow_width = tk.StringVar(
            value=str(config.get("flow_width", 960))
        )
        self.bitrate = tk.StringVar(
            value=str(config.get("bitrate", ""))
        )
        self.half = tk.BooleanVar(
            value=config.get("half", False)
        )
        self.footer = tk.StringVar(value="Ready")

        self.build_ui()

        for variable in (
            self.engine, self.rife_exe, self.rife_model,
            self.gpu, self.source_fps, self.collision,
            self.encoder, self.codec, self.flow_width,
            self.bitrate, self.half,
        ):
            variable.trace_add("write", self.settings_changed)

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.poll_events)

    def build_ui(self):
        toolbar = ttk.Frame(self, padding=8)
        toolbar.pack(fill="x")

        for title, command in (
            ("Add files", self.add_files),
            ("Add folder", self.add_folder),
            ("Remove selected", self.remove_selected),
            ("Benchmark", self.start_benchmark),
        ):
            ttk.Button(
                toolbar, text=title, command=command
            ).pack(side="left", padx=3)

        ttk.Label(
            toolbar,
            text="Drag files/folders here • Ctrl/Shift to select",
        ).pack(side="left", padx=15)

        settings_box = ttk.LabelFrame(
            self, text="Settings", padding=8
        )
        settings_box.pack(fill="x", padx=8)

        def combo(parent, title, variable, values, width=12):
            ttk.Label(parent, text=title).pack(
                side="left", padx=(7, 3)
            )
            ttk.Combobox(
                parent,
                textvariable=variable,
                values=values,
                width=width,
                state="readonly",
            ).pack(side="left", padx=3)

        line = ttk.Frame(settings_box)
        line.pack(fill="x", pady=3)

        combo(
            line, "Engine", self.engine,
            ["DIS fast", "DIS medium", "RIFE"],
        )
        combo(
            line, "Source FPS", self.source_fps,
            [
                "auto", "24", "24000/1001", "25",
                "30", "30000/1001", "50",
                "60", "60000/1001", "120", "120000/1001",
            ],
        )
        combo(
            line, "Collisions", self.collision,
            ["nearest", "interpolate"],
        )
        combo(
            line, "Encoder", self.encoder, ["nvenc", "cpu"], 8
        )
        combo(
            line, "Codec", self.codec, ["hevc", "h264"], 7
        )

        ttk.Checkbutton(
            line, text="Half FPS", variable=self.half
        ).pack(side="left", padx=10)

        line = ttk.Frame(settings_box)
        line.pack(fill="x", pady=3)

        for title, variable, width in (
            ("Flow width", self.flow_width, 7),
            ("Bitrate Mbps (blank = source)", self.bitrate, 8),
            ("RIFE GPU ID", self.gpu, 5),
        ):
            ttk.Label(line, text=title).pack(
                side="left", padx=(8, 5)
            )
            ttk.Entry(
                line, textvariable=variable, width=width
            ).pack(side="left", padx=(0, 12))

        for title, variable, command in (
            ("RIFE executable", self.rife_exe, self.choose_exe),
            ("RIFE model folder", self.rife_model, self.choose_model),
        ):
            line = ttk.Frame(settings_box)
            line.pack(fill="x", pady=3)

            ttk.Label(line, text=title, width=18).pack(side="left")
            ttk.Entry(
                line, textvariable=variable
            ).pack(side="left", fill="x", expand=True, padx=6)
            ttk.Button(
                line, text="Browse", command=command
            ).pack(side="left")

        table_frame = ttk.Frame(self, padding=8)
        table_frame.pack(fill="both", expand=True)

        columns = (
            "file", "fps", "target", "difference",
            "quality", "analysis", "total", "state",
        )

        self.table = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
        )

        headers = [
            ("file", "File", 290),
            ("fps", "Avg FPS", 80),
            ("target", "Nearest / forced", 110),
            ("difference", "Difference", 90),
            ("quality", "Preliminary status", 130),
            ("analysis", "Analysis ETA", 105),
            ("total", "Repair + analysis ETA", 150),
            ("state", "Job", 105),
        ]

        for column, title, width in headers:
            self.table.heading(column, text=title)
            self.table.column(column, width=width, minwidth=65)

        scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical",
            command=self.table.yview,
        )
        self.table.configure(yscrollcommand=scrollbar.set)

        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        if DND_FILES:
            self.table.drop_target_register(DND_FILES)
            self.table.dnd_bind(
                "<<Drop>>",
                lambda event: self.add_paths(
                    self.tk.splitlist(event.data)
                ),
            )

        actions = ttk.Frame(self, padding=8)
        actions.pack(fill="x")

        for title, action, selected in (
            ("Analyze selected", "analyze", True),
            ("Analyze all", "analyze", False),
            ("Repair selected", "repair", True),
            ("Repair all", "repair", False),
        ):
            ttk.Button(
                actions,
                text=title,
                command=lambda a=action, s=selected: self.start_jobs(a, s),
            ).pack(side="left", padx=3)

        ttk.Button(
            actions,
            text="Stop after current",
            command=self.request_stop_after,
        ).pack(side="right", padx=3)

        tk.Button(
            actions,
            text="STOP NOW",
            bg="#b3261e",
            fg="white",
            activebackground="#8c1d18",
            activeforeground="white",
            command=self.request_stop_now,
        ).pack(side="right", padx=8)

        ttk.Label(
            self,
            text=(
                "Excellent = 0%; Very good ≤ 0.3%; Good ≤ 2%; "
                "Fair ≤ 5%; Poor ≤ 10%; Very poor > 10%. "
                "Metadata estimate only."
            ),
            padding=(10, 0, 10, 4),
        ).pack(fill="x")

        self.log = tk.Text(
            self, height=12, wrap="word", state="disabled"
        )
        self.log.pack(fill="both", padx=8, pady=4)

        ttk.Label(
            self, textvariable=self.footer, padding=8
        ).pack(fill="x")

    def settings(self):
        return {
            "engine": self.engine.get(),
            "rife_exe": self.rife_exe.get().strip(),
            "rife_model": self.rife_model.get().strip(),
            "gpu": self.gpu.get().strip() or "0",
            "source_fps": self.source_fps.get(),
            "collision": self.collision.get(),
            "encoder": self.encoder.get(),
            "codec": self.codec.get(),
            "flow_width": int(self.flow_width.get()),
            "bitrate": self.bitrate.get().strip(),
            "half": self.half.get(),
        }

    def settings_changed(self, *_):
        try:
            settings = self.settings()
            save_json(CONFIG_FILE, settings)
            self.refresh_all()
        except (ValueError, OSError):
            pass

    def choose_exe(self):
        path = filedialog.askopenfilename(
            title="Select rife-ncnn-vulkan",
            filetypes=[("Executable", "*.exe"), ("All files", "*")],
        )
        if path:
            self.rife_exe.set(path)

    def choose_model(self):
        path = filedialog.askdirectory(
            title="Select RIFE v4 model folder"
        )
        if path:
            self.rife_model.set(path)

    def add_files(self):
        self.add_paths(filedialog.askopenfilenames(
            filetypes=[
                ("Video", "*.mp4 *.mov *.mkv"),
                ("All files", "*"),
            ]
        ))

    def add_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.add_paths([path])

    def add_paths(self, values):
        if self.busy:
            messagebox.showinfo(
                "Busy", "Wait for the current queue to finish."
            )
            return

        paths = []
        for value in values:
            path = Path(value).expanduser().resolve()
            if path.is_dir():
                if path.name == "original(dropped)":
                    continue
                paths.extend(sorted(path.iterdir()))
            else:
                paths.append(path)

        for path in paths:
            if not path.is_file():
                continue
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if path.name.startswith("."):
                continue
            if path.parent.name == "original(dropped)":
                continue

            key = os.path.normcase(str(path))
            if key in self.known_paths:
                continue

            iid = self.table.insert(
                "", "end",
                values=(
                    path.name, "…", "…", "…",
                    "…", "—", "—", "Reading",
                ),
            )

            self.rows[iid] = {
                "path": path,
                "meta": None,
                "state": "Reading",
            }
            self.known_paths[key] = iid

            threading.Thread(
                target=self.probe_thread,
                args=(iid, path),
                daemon=True,
            ).start()

    def probe_thread(self, iid, path):
        try:
            self.events.put(("metadata", iid, quick_probe(path)))
        except Exception as error:
            self.events.put(("probe_error", iid, str(error)))

    def preliminary(self, metadata, settings):
        average = metadata["avg"]

        if average <= 0:
            return None, None, "Unknown"

        if settings["source_fps"] == "auto":
            nominal = min(
                RATES,
                key=lambda rate: abs(float(rate) - average),
            )
        else:
            nominal = Fraction(settings["source_fps"])

        difference = (
            abs(average - float(nominal)) / float(nominal) * 100
        )

        # Только вычислительный допуск, не "допустимые дропы".
        if difference <= 1e-9:
            status = "Excellent"
        elif difference <= 0.3:
            status = "Very good"
        elif difference <= 2:
            status = "Good"
        elif difference <= 5:
            status = "Fair"
        elif difference <= 10:
            status = "Poor"
        else:
            status = "Very poor"

        return nominal, difference, status

    def estimates(self, meta, nominal, settings):
        bench = self.benchmarks.get(benchmark_key(settings))

        if not bench or nominal is None or meta["duration"] <= 0:
            return None, None

        pixels = meta["width"] * meta["height"]
        base = bench["width"] * bench["height"]
        scale = max(0.1, pixels / max(1, base))

        duration = meta["duration"]
        input_count = meta["frames"] or duration * meta["avg"]

        if input_count <= 0:
            return None, None

        output_fps = float(nominal) / (
            2 if settings["half"] else 1
        )
        output_count = max(1, math.ceil(duration * output_fps))

        deficit = max(0, 1 - meta["avg"] / float(nominal))
        interpolated = output_count * deficit

        if settings["collision"] == "interpolate":
            interpolated += output_count * 0.01

        analysis = (
            0.5 + input_count * bench["decode_per_frame"] * scale
        )

        total = (
            analysis
            + input_count * (
                bench["decode_per_frame"]
                + bench["convert_per_frame"]
            ) * scale
            + output_count * bench["encode_per_frame"] * scale
            + interpolated * bench["interpolate_per_frame"] * scale
            + 2
        )

        return analysis, total

    def refresh_row(self, iid):
        row = self.rows.get(iid)
        if not row or row["meta"] is None:
            return

        try:
            settings = self.settings()
            nominal, difference, quality = self.preliminary(
                row["meta"], settings
            )
            analysis, total = self.estimates(
                row["meta"], nominal, settings
            )
        except (ValueError, ZeroDivisionError):
            return

        difference_text = "?"
        if difference is not None:
            if difference <= 1e-9:
                difference_text = "0%"
            elif difference < 0.0001:
                difference_text = "<0.0001%"
            else:
                difference_text = f"{difference:.4f}%"

        self.table.item(iid, values=(
            row["path"].name,
            f"{row['meta']['avg']:.4f}",
            str(nominal) if nominal else "?",
            difference_text,
            quality,
            human_time(analysis),
            human_time(total),
            row["state"],
        ))

    def refresh_all(self):
        for iid in list(self.rows):
            self.refresh_row(iid)

    def remove_selected(self):
        if self.busy:
            return

        for iid in self.table.selection():
            row = self.rows.pop(iid, None)
            if row:
                self.known_paths.pop(
                    os.path.normcase(str(row["path"])), None
                )
            self.table.delete(iid)

    def validate_settings(self, needs_engine):
        settings = self.settings()

        if settings["flow_width"] < 64:
            raise ValueError("Flow width must be at least 64")

        if settings["bitrate"]:
            bitrate = float(settings["bitrate"])
            if not math.isfinite(bitrate) or bitrate <= 0:
                raise ValueError("Bitrate must be positive")

        for name in ("ffmpeg", "ffprobe"):
            if not shutil.which(name):
                raise ValueError(f"{name} is not available in PATH")

        for filename in ("fix_drops.py", "fixdrops_worker.py"):
            if not (APP_DIR / filename).is_file():
                raise ValueError(f"Missing file: {filename}")

        if needs_engine and settings["engine"] == "RIFE":
            if not Path(settings["rife_exe"]).is_file():
                raise ValueError("Select rife-ncnn-vulkan executable")
            if not Path(settings["rife_model"]).is_dir():
                raise ValueError("Select a RIFE v4 model folder")

        return settings

    def start_benchmark(self):
        if self.busy:
            return

        candidates = list(self.table.selection()) or list(self.rows)
        ready = [
            iid for iid in candidates
            if self.rows[iid]["meta"] is not None
        ]

        if not ready:
            messagebox.showinfo(
                "Benchmark", "Add a video and wait for metadata."
            )
            return

        self.launch([ready[0]], "benchmark")

    def start_jobs(self, action, selected):
        if self.busy:
            return

        ids = (
            list(self.table.selection())
            if selected else list(self.rows)
        )
        ids = [
            iid for iid in ids
            if self.rows[iid]["meta"] is not None
        ]

        if not ids:
            messagebox.showinfo("Queue", "No ready files selected.")
            return

        if action == "repair":
            if not messagebox.askyesno(
                "Repair",
                f"Repair {len(ids)} file(s)?\n\n"
                "Originals will be moved to original(dropped).\n"
                "Repaired files will receive the original names.\n"
                "Existing backup files will never be overwritten.",
            ):
                return

        self.launch(ids, action)

    def launch(self, ids, action):
        try:
            settings = self.validate_settings(
                action in ("repair", "benchmark")
            )
        except Exception as error:
            messagebox.showerror("Settings", str(error))
            return

        self.busy = True
        self.stop_after.clear()
        self.cancel_now.clear()

        self.footer.set(f"{action.capitalize()}: {len(ids)} file(s)")

        jobs = [
            (iid, self.rows[iid]["path"])
            for iid in ids
        ]

        # Обычный поток, чтобы GUI не завершился незаметно
        # до окончания возврата оригинала.
        threading.Thread(
            target=self.queue_thread,
            args=(jobs, action, settings),
            daemon=False,
        ).start()

    def run_worker(self, job):
        if self.cancel_now.is_set():
            raise Cancelled()

        workdir = Path(job["workdir"])
        job_path = workdir / "job.json"
        job_path.write_text(
            json.dumps(job, ensure_ascii=False),
            encoding="utf-8",
        )

        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUNBUFFERED"] = "1"

        # Временные файлы core тоже остаются внутри каталога задания.
        for name in ("TEMP", "TMP", "TMPDIR"):
            environment[name] = str(workdir)

        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True

        with self.process_lock:
            if self.cancel_now.is_set():
                raise Cancelled()

            process = subprocess.Popen(
                [
                    sys.executable, "-u",
                    str(APP_DIR / "fixdrops_worker.py"),
                    str(job_path),
                ],
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                **kwargs,
            )
            self.current_process = process

        try:
            for line in process.stdout:
                if line.startswith("@@BENCH "):
                    result = json.loads(line[len("@@BENCH "):])
                    key = benchmark_key(job["settings"])
                    self.benchmarks[key] = result
                    save_json(BENCH_FILE, self.benchmarks)
                    self.events.put(("refresh",))
                else:
                    self.events.put(("log", line))

            return_code = process.wait()

        finally:
            with self.process_lock:
                if self.current_process is process:
                    self.current_process = None

            if process.stdout:
                process.stdout.close()

        if self.cancel_now.is_set():
            raise Cancelled()

        if return_code:
            raise RuntimeError(
                f"Worker exited with code {return_code}. "
                "See the log above."
            )

    def run_benchmark(self, source, settings):
        with tempfile.TemporaryDirectory(
            prefix="fixdrops_benchmark_"
        ) as folder:
            self.run_worker({
                "action": "benchmark",
                "source": str(source),
                "settings": settings,
                "workdir": folder,
            })

    def run_analysis(self, source, settings):
        # Само видео при анализе не создаётся.
        output = source.with_name(
            source.stem + ".analysis" + source.suffix
        )

        with tempfile.TemporaryDirectory(
            prefix="fixdrops_analysis_"
        ) as folder:
            self.run_worker({
                "action": "analyze",
                "source": str(source),
                "output": str(output),
                "settings": settings,
                "workdir": folder,
            })

    def run_repair(self, source, settings):
        backup_dir = source.parent / "original(dropped)"
        backup = backup_dir / source.name
        final_report = source.with_name(
            source.name + ".drops.json"
        )

        if backup.exists():
            raise RuntimeError(
                f"Backup already exists:\n{backup}\n"
                "Processing blocked to protect the original."
            )

        if final_report.exists():
            raise RuntimeError(
                f"Report already exists:\n{final_report}\n"
                "Rename or remove it before retrying."
            )

        key = benchmark_key(settings)
        if key not in self.benchmarks:
            self.events.put(("log", "\nRunning initial benchmark...\n"))
            self.run_benchmark(source, settings)

        if self.cancel_now.is_set():
            raise Cancelled()

        workdir = Path(tempfile.mkdtemp(
            prefix=".fixdrops_job_",
            dir=str(source.parent),
        ))

        staging = workdir / ("result" + source.suffix)
        stage_report = staging.with_name(
            staging.name + ".drops.json"
        )

        moved = False
        committed = False

        try:
            backup_dir.mkdir(exist_ok=True)

            if self.cancel_now.is_set():
                raise Cancelled()

            source.rename(backup)
            moved = True

            self.events.put((
                "log",
                f"\nOriginal moved to:\n{backup}\n",
            ))

            self.run_worker({
                "action": "repair",
                "source": str(backup),
                "output": str(staging),
                "settings": settings,
                "workdir": str(workdir),
            })

            if self.cancel_now.is_set():
                raise Cancelled()

            if not staging.is_file():
                raise RuntimeError("Output video was not created")

            report = load_json(stage_report, {})

            if report.get("timing_check") != "video_start_ok":
                raise RuntimeError(
                    "Output did not pass the core timing check."
                )

            if source.exists():
                raise RuntimeError(
                    "Another file appeared at the original path. "
                    "Nothing will be overwritten."
                )

            # Файл уже полностью закодирован и проверен.
            staging.rename(source)
            committed = True

            try:
                report["input"] = str(backup)
                report["output"] = str(source)
                save_json(final_report, report)
            except Exception as error:
                self.events.put((
                    "log",
                    f"\nVideo saved, but report write failed: {error}\n",
                ))

            self.events.put((
                "log", f"\nRepaired video saved:\n{source}\n"
            ))

        finally:
            if moved and not committed:
                if backup.exists() and not source.exists():
                    try:
                        backup.rename(source)
                        self.events.put((
                            "log",
                            "\nOriginal restored to its initial location.\n",
                        ))
                    except OSError as error:
                        self.events.put((
                            "log",
                            "\nWARNING: automatic restore failed.\n"
                            f"Original is here: {backup}\n{error}\n",
                        ))
                else:
                    self.events.put((
                        "log",
                        "\nWARNING: original was not automatically restored.\n"
                        f"Check: {source}\nBackup: {backup}\n",
                    ))

            # После завершения worker/его дерева удаляем частичный результат.
            try:
                shutil.rmtree(workdir)
            except OSError as error:
                self.events.put((
                    "log",
                    f"\nTemporary directory remains:\n{workdir}\n{error}\n",
                ))

    def queue_thread(self, jobs, action, settings):
        try:
            for iid, source in jobs:
                if self.stop_after.is_set() or self.cancel_now.is_set():
                    break

                self.events.put(("state", iid, action.capitalize()))
                self.events.put((
                    "log", f"\n{'=' * 65}\n{source}\n"
                ))

                try:
                    if action == "benchmark":
                        self.run_benchmark(source, settings)
                    elif action == "analyze":
                        self.run_analysis(source, settings)
                    else:
                        self.run_repair(source, settings)

                    self.events.put(("state", iid, "Done"))

                except Cancelled:
                    self.events.put(("state", iid, "Stopped"))
                    self.events.put((
                        "log",
                        "\nStopped by user. Remaining queue cancelled.\n",
                    ))
                    break

                except Exception as error:
                    self.events.put(("state", iid, "Error"))
                    self.events.put((
                        "log", f"\nERROR: {error}\n"
                    ))

        finally:
            self.events.put(("queue_done",))

    def request_stop_after(self):
        if self.busy:
            self.stop_after.set()
            self.footer.set("Will stop after the current file.")

    def request_stop_now(self):
        if not self.busy:
            return

        self.cancel_now.set()
        self.stop_after.set()
        self.footer.set(
            "Stopping process tree... Waiting for original-file recovery."
        )

        threading.Thread(
            target=self.kill_current_tree,
            daemon=True,
        ).start()

    def kill_current_tree(self):
        # Пока держим lock, worker не может быть заменён следующим.
        with self.process_lock:
            process = self.current_process
            if process is None:
                return

            if os.name == "nt":
                result = subprocess.run(
                    [
                        "taskkill",
                        "/PID", str(process.pid),
                        "/T",
                        "/F",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )

                if result.returncode and process.poll() is None:
                    self.events.put((
                        "log",
                        "\nWARNING: taskkill failed. "
                        "Waiting for the worker to exit.\n"
                        + result.stdout + result.stderr,
                    ))
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    self.events.put((
                        "log", f"\nUnable to stop process group: {error}\n"
                    ))

    def maybe_offer_benchmark(self):
        if self.busy or self.auto_benchmark_offered:
            return

        try:
            settings = self.settings()
        except ValueError:
            return

        if benchmark_key(settings) in self.benchmarks:
            return

        ready = [
            iid for iid, row in self.rows.items()
            if row["meta"] is not None
        ]
        if not ready:
            return

        self.auto_benchmark_offered = True

        if messagebox.askyesno(
            "Initial benchmark",
            "No benchmark is saved for these settings.\n\n"
            "Run it now using the first video?\n"
            "The original file will not be changed.",
        ):
            self.launch([ready[0]], "benchmark")

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)

        if int(self.log.index("end-1c").split(".")[0]) > 3000:
            self.log.delete("1.0", "1000.0")

        self.log.see("end")
        self.log.configure(state="disabled")

    def poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]

                if kind == "metadata":
                    _, iid, metadata = event
                    if iid in self.rows:
                        self.rows[iid]["meta"] = metadata
                        self.rows[iid]["state"] = "Ready"
                        self.refresh_row(iid)
                        self.after(200, self.maybe_offer_benchmark)

                elif kind == "probe_error":
                    _, iid, error = event
                    if iid in self.rows:
                        self.table.set(iid, "state", "Probe error")
                    self.append_log(error + "\n")

                elif kind == "state":
                    _, iid, state = event
                    if iid in self.rows:
                        self.rows[iid]["state"] = state
                        self.refresh_row(iid)

                elif kind == "log":
                    self.append_log(event[1])

                elif kind == "refresh":
                    self.refresh_all()

                elif kind == "queue_done":
                    self.busy = False
                    self.refresh_all()
                    self.footer.set(
                        "Stopped; recovery finished."
                        if self.cancel_now.is_set()
                        else "Queue finished."
                    )

        except queue.Empty:
            pass

        self.after(100, self.poll_events)

    def close(self):
        if self.busy:
            if messagebox.askyesno(
                "Processing",
                "Force-stop the current job?\n\n"
                "The window will remain open until file recovery finishes.\n"
                "Then close it again.",
            ):
                self.request_stop_now()
            return

        try:
            save_json(CONFIG_FILE, self.settings())
        except Exception:
            pass

        self.destroy()


if __name__ == "__main__":
    App().mainloop()