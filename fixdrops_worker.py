import hashlib
import json
import math
import os
import sys
import time
from fractions import Fraction
from pathlib import Path

import av

import fixdrops_worker_impl as worker


core = worker.core

core.STANDARD_RATES = [
    Fraction(24),
    Fraction(30),
    Fraction(60),
]

CONFIG_DIR = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home()))
) / "FixDrops"

BENCH_FILE = CONFIG_DIR / "benchmarks_gui_v2.json"


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


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


def emit(data):
    print(
        "@@PROGRESS " + json.dumps(data, ensure_ascii=False),
        flush=True,
    )


class Progress:
    def __init__(self, job):
        self.job = job
        self.settings = job["settings"]
        self.analysis_only = job["action"] == "analyze"

        self.started = time.monotonic()
        self.last_emit = 0.0
        self.phase = "Analysis"
        self.planned = False

        self.input_count = 0
        self.scanned = 0
        self.output_count = 0
        self.written = 0
        self.interpolation_count = 0
        self.interpolation_done = 0
        self.synthesized = 0

        self.scan_seconds = 0.0
        self.interpolation_seconds = 0.0
        self.benchmark_available = False

        metadata = core.probe_video(job["source"])

        duration = self.number(metadata.get("duration"))
        average = 0.0

        try:
            average = float(Fraction(metadata.get("avg_frame_rate", "0/1")))
        except (ValueError, ZeroDivisionError, TypeError):
            pass

        self.input_count = int(self.number(metadata.get("nb_frames")))
        if self.input_count <= 0:
            self.input_count = max(1, round(duration * average))

        source_fps = self.settings.get("source_fps", "auto")
        if source_fps == "auto":
            nominal = min(
                (24, 30, 60),
                key=lambda value: abs(value - average),
            )
        else:
            nominal = float(Fraction(source_fps))

        rate = nominal / (2 if self.settings.get("half") else 1)
        self.output_count = max(1, math.ceil(duration * rate))

        deficit = max(0.0, 1 - average / nominal)
        self.interpolation_count = math.ceil(self.output_count * deficit)

        if self.settings.get("collision") == "interpolate":
            self.interpolation_count += math.ceil(self.output_count * 0.01)

        cache = load_json(BENCH_FILE, {})
        bench = cache.get(benchmark_key(self.settings))

        if bench:
            self.benchmark_available = True
            pixels = (
                int(metadata.get("width", 0))
                * int(metadata.get("height", 0))
            )
            base_pixels = max(1, bench["width"] * bench["height"])
            scale = max(0.1, pixels / base_pixels)

            self.scan_cost = max(
                0.00001, bench["decode_per_frame"] * scale
            )
            self.source_cost = max(
                0.00001,
                (
                    bench["decode_per_frame"]
                    + bench["convert_per_frame"]
                ) * scale,
            )
            self.encode_cost = max(
                0.00001, bench["encode_per_frame"] * scale
            )
            self.interpolation_cost = max(
                0.00001, bench["interpolate_per_frame"] * scale
            )
        else:
            # Только относительные единицы работы.
            # Время без benchmark не выдаём.
            self.scan_cost = 1.0
            self.source_cost = 1.0
            self.encode_cost = 1.0
            self.interpolation_cost = 10.0

        self.finish_cost = 2.0 if bench else 1.0
        self.emit(force=True)

    @staticmethod
    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) else 0.0
        except (ValueError, TypeError):
            return 0.0

    def work_values(self):
        if self.analysis_only:
            total = max(1e-9, self.input_count * self.scan_cost)
            done = min(self.scanned, self.input_count) * self.scan_cost
            return done, total

        analysis_work = (
            self.scan_seconds
            if self.planned and self.benchmark_available
            else self.input_count * self.scan_cost
        )

        base_work = (
            self.input_count * self.source_cost
            + self.output_count * self.encode_cost
        )

        interpolation_work = (
            self.interpolation_count * self.interpolation_cost
        )

        total = (
            analysis_work
            + base_work
            + interpolation_work
            + self.finish_cost
        )

        if not self.planned:
            done = min(self.scanned, self.input_count) * self.scan_cost
        else:
            output_fraction = self.written / max(1, self.output_count)

            done = (
                analysis_work
                + base_work * output_fraction
                + self.interpolation_done * self.interpolation_cost
            )

        return done, max(1e-9, total)

    def emit(self, force=False, finished=False):
        now = time.monotonic()

        if not force and now - self.last_emit < 0.2:
            return

        self.last_emit = now

        done, total = self.work_values()
        percent = min(99, max(0, math.floor(100 * done / total)))
        remaining = None

        if finished:
            percent = 100
            remaining = 0
        elif self.benchmark_available:
            remaining = max(0.0, total - done)

        emit({
            "phase": self.phase,
            "percent": percent,
            "elapsed": now - self.started,
            "remaining": remaining,
            "planned": self.planned,
            "benchmark": self.benchmark_available,
            "scanned": self.scanned,
            "input_frames": self.input_count,
            "written": self.written,
            "output_frames": self.output_count,
            "interpolation_done": self.interpolation_done,
            "interpolation_total": self.interpolation_count,
            "synthesized": self.synthesized,
        })

    def plan(self, pts, last_duration):
        import statistics

        deltas = [b - a for a, b in zip(pts, pts[1:])]
        median_dt = statistics.median(deltas)
        median_fps = 1.0 / median_dt

        selected = self.settings.get("source_fps", "auto")

        if selected == "auto":
            nominal = min(
                core.STANDARD_RATES,
                key=lambda value: abs(float(value) - median_fps),
            )

            if abs(float(nominal) - median_fps) / float(nominal) > 0.05:
                # Основной скрипт затем выдаст штатную ошибку
                # и предложит ручной выбор FPS.
                return
        else:
            nominal = Fraction(selected)

        divisor = 2 if self.settings.get("half") else 1
        rate = float(nominal / divisor)
        step = 1.0 / rate

        tail = last_duration if last_duration and last_duration > 0 else median_dt
        duration = pts[-1] - pts[0] + tail

        count = max(1, math.ceil(duration * rate - 1e-9))

        cursor = 0
        empty = 0
        collisions = 0
        calls = 0

        interpolate_collisions = (
            self.settings.get("collision") == "interpolate"
        )

        # Повторяем правила назначения кадров из текущего core.
        # Изображения здесь не декодируются повторно.
        for index in range(count):
            target = pts[0] + index * step
            right_edge = target + step / 2

            before = cursor

            while cursor < len(pts) and pts[cursor] < right_edge:
                cursor += 1

            candidates = cursor - before
            collision = candidates > divisor

            if candidates == 0:
                empty += 1
                calls += 1
            elif collision:
                collisions += 1
                if interpolate_collisions:
                    calls += 1

        self.input_count = len(pts)
        self.output_count = count
        self.interpolation_count = calls
        self.planned = True
        self.phase = "Analysis complete" if self.analysis_only else "Repair"

        print(
            "\nWORK PLAN:"
            f"\n  Output / encoding frames: {count}"
            f"\n  Empty slots: {empty}"
            f"\n  Collision slots: {collisions}"
            f"\n  Interpolator calls: {calls}"
            "\n  Scene cuts and edge positions may use holds "
            "instead of synthesis.",
            flush=True,
        )

        self.emit(force=True)


def install_progress(job):
    progress = Progress(job)

    original_write = core.write_frame
    original_mux_probe = core.first_video_pts

    def scan_timestamps(path):
        timestamps = []
        last_duration = None
        started = time.monotonic()

        with av.open(str(path)) as container:
            if not container.streams.video:
                raise RuntimeError("Видеопоток не найден")

            stream = container.streams.video[0]
            stream.thread_type = "AUTO"

            for frame in container.decode(stream):
                if frame.pts is None or frame.time_base is None:
                    raise RuntimeError("У видеокадра отсутствует PTS")

                pts = float(frame.pts * frame.time_base)

                if not math.isfinite(pts):
                    raise RuntimeError("Некорректный PTS")

                if timestamps and pts <= timestamps[-1]:
                    raise RuntimeError(
                        "Временные метки повторяются или идут назад"
                    )

                timestamps.append(pts)

                duration = getattr(frame, "duration", None)
                last_duration = (
                    float(duration * frame.time_base)
                    if duration and duration > 0 else None
                )

                progress.scanned = len(timestamps)
                progress.input_count = max(
                    progress.input_count, progress.scanned
                )
                progress.emit()

        if len(timestamps) < 3:
            raise RuntimeError("Для анализа нужно минимум три кадра")

        progress.scan_seconds = time.monotonic() - started
        progress.scanned = len(timestamps)
        progress.plan(timestamps, last_duration)

        print(
            f"Проанализировано кадров: {len(timestamps)}",
            flush=True,
        )

        return timestamps, last_duration

    def write_frame(pipe, image):
        result = original_write(pipe, image)

        progress.written += 1
        progress.phase = "Repair"

        if progress.written >= progress.output_count:
            progress.phase = "Encoder flush / mux / verification"

        progress.emit()
        return result

    def first_video_pts(path):
        progress.phase = "Mux / timing verification"
        progress.emit(force=True)
        return original_mux_probe(path)

    def instrument_render(cls):
        original_render = cls.render

        def render(instance, left, right, target):
            started = time.monotonic()
            result = original_render(instance, left, right, target)
            elapsed = time.monotonic() - started

            progress.interpolation_done += 1
            progress.interpolation_seconds += elapsed

            if result[1] == "interpolated":
                progress.synthesized += 1

            # Уточняем стоимость оставшихся вызовов по реальным
            # результатам, постепенно заменяя benchmark.
            if progress.benchmark_available:
                count = progress.interpolation_done
                measured = progress.interpolation_seconds / count

                initial = getattr(
                    progress, "_initial_interpolation_cost",
                    progress.interpolation_cost,
                )
                progress._initial_interpolation_cost = initial

                confidence = min(0.8, count / (count + 5.0))
                progress.interpolation_cost = max(
                    0.00001,
                    initial * (1 - confidence) + measured * confidence,
                )

            progress.emit()
            return result

        cls.render = render

    core.scan_timestamps = scan_timestamps
    core.write_frame = write_frame
    core.first_video_pts = first_video_pts

    instrument_render(core.Interpolator)
    instrument_render(worker.RifeInterpolator)

    return progress


def main():
    if len(sys.argv) != 2:
        raise RuntimeError("Запускай программу через fixdrops_gui.py")

    job = json.loads(
        Path(sys.argv[1]).read_text(encoding="utf-8")
    )

    if str(job["settings"].get("source_fps", "auto")) not in (
        "auto", "24", "30", "60"
    ):
        raise RuntimeError("Допустимый Source FPS: auto / 24 / 30 / 60")

    if job["action"] == "benchmark":
        worker.main()
        return

    progress = install_progress(job)
    worker.main()

    # 100% — только после успешного возврата из worker,
    # включая проверки основного скрипта и ориентации.
    progress.phase = "Completed"
    progress.emit(force=True, finished=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as error:
        print(f"\nERROR: {error}", flush=True)
        sys.exit(1)