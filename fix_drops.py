#!/usr/bin/env python3

import argparse
import json
import math
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

import av
import cv2
import numpy as np


STANDARD_RATES = [
    Fraction(24000, 1001),
    Fraction(24),
    Fraction(25),
    Fraction(30000, 1001),
    Fraction(30),
    Fraction(50),
    Fraction(60000, 1001),
    Fraction(60),
    Fraction(120000, 1001),
    Fraction(120),
]


def timecode(seconds, rate):
    """Относительный NDF-таймкод, отсчёт от первого видеокадра."""
    fps = float(rate)
    nominal = max(1, math.floor(fps + 0.5))
    sign = "-" if seconds < 0 else ""

    frame_number = math.floor(abs(seconds) * fps + 1e-7)
    total_seconds, frame = divmod(frame_number, nominal)
    total_minutes, second = divmod(total_seconds, 60)
    hour, minute = divmod(total_minutes, 60)

    return (
        f"{sign}{hour:02d}:{minute:02d}:"
        f"{second:02d}:{frame:02d}"
    )


def parse_rate(value):
    rate = Fraction(value)
    if rate <= 0 or not math.isfinite(float(rate)):
        raise ValueError("FPS должен быть положительным")
    return rate


def save_report(path, report):
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def probe_video(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_streams",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Ошибка ffprobe")

    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise RuntimeError("Видеопоток не найден")

    return streams[0]


def scan_timestamps(path):
    """Первый проход: PTS всех отображаемых видеокадров."""
    timestamps = []
    last_duration = None

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
                    "Временные метки повторяются или идут назад. "
                    "Такой таймлайн этот скрипт не обрабатывает."
                )

            timestamps.append(pts)

            duration = getattr(frame, "duration", None)
            last_duration = (
                float(duration * frame.time_base)
                if duration and duration > 0
                else None
            )

            if len(timestamps) % 1000 == 0:
                print(
                    f"\rПроанализировано кадров: {len(timestamps)}",
                    end="",
                    flush=True,
                )

    print(f"\rПроанализировано кадров: {len(timestamps)}")

    if len(timestamps) < 3:
        raise RuntimeError("Для анализа нужно минимум три кадра")

    return timestamps, last_duration


def first_video_pts(path):
    """
    Читает PTS первого отображаемого кадра.
    Нужен для компенсации сдвига промежуточного контейнера.
    """
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise RuntimeError(f"В файле нет видео: {path}")

        stream = container.streams.video[0]

        for frame in container.decode(stream):
            if frame.pts is None or frame.time_base is None:
                raise RuntimeError("У первого видеокадра нет PTS")

            return float(frame.pts * frame.time_base)

    raise RuntimeError(f"Видеопоток пуст: {path}")


def decode_frames(path):
    """Второй проход: последовательное декодирование изображений."""
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"

        for frame in container.decode(stream):
            if frame.pts is None or frame.time_base is None:
                raise RuntimeError("При обработке обнаружен кадр без PTS")

            yield (
                float(frame.pts * frame.time_base),
                frame.to_ndarray(format="bgr24"),
            )


def validate_video_format(metadata):
    transfer = metadata.get("color_transfer", "")
    pixel_format = metadata.get("pix_fmt", "")

    if transfer in ("smpte2084", "arib-std-b67"):
        raise RuntimeError(
            "HDR этим вариантом не поддерживается. "
            "Нужен отдельный цветовой тракт."
        )

    if pixel_format not in ("yuv420p", "yuvj420p", "nv12"):
        raise RuntimeError(
            f"Неподдерживаемый формат {pixel_format!r}. "
            "Этот вариант рассчитан на SDR 8-bit."
        )

    if metadata.get("field_order") not in (
        None, "unknown", "progressive"
    ):
        raise RuntimeError("Чересстрочное видео не поддерживается")

    rotation = float(
        metadata.get("tags", {}).get("rotate", 0) or 0
    )

    for side_data in metadata.get("side_data_list", []):
        if "rotation" in side_data:
            rotation = float(side_data["rotation"])

    if abs(math.remainder(rotation, 360)) > 0.01:
        raise RuntimeError(
            "У видео есть поворот через метаданные. "
            "Этот вариант не обрабатывает display matrix."
        )

    sar = metadata.get("sample_aspect_ratio")
    if sar not in (None, "N/A", "0:1", "1:1"):
        raise RuntimeError("Поддерживаются только квадратные пиксели")


class Interpolator:
    """
    Приближённая двунаправленная интерполяция DIS Optical Flow.
    Расчёт потока — на уменьшенных изображениях.
    Варпинг — в исходном разрешении.

    Это не RIFE. На перекрытиях объектов и резком движении
    возможны двоение и деформации.
    """

    def __init__(self, flow_width, flow_preset, scene_threshold):
        presets = {
            "fast": cv2.DISOPTICAL_FLOW_PRESET_FAST,
            "medium": cv2.DISOPTICAL_FLOW_PRESET_MEDIUM,
        }

        self.dis = cv2.DISOpticalFlow_create(
            presets[flow_preset]
        )
        self.flow_width = flow_width
        self.scene_threshold = scene_threshold

        self.cache_key = None
        self.cache = None
        self.grid_shape = None
        self.grid_x = None
        self.grid_y = None

    def prepare(self, left, right):
        key = (left[0], right[0])

        if key == self.cache_key:
            return self.cache

        a = left[1]
        b = right[1]
        h, w = a.shape[:2]

        scale = min(1.0, self.flow_width / w)
        sw = max(16, round(w * scale))
        sh = max(16, round(h * scale))

        small_a = cv2.resize(
            a, (sw, sh), interpolation=cv2.INTER_AREA
        )
        small_b = cv2.resize(
            b, (sw, sh), interpolation=cv2.INTER_AREA
        )

        ga = cv2.cvtColor(small_a, cv2.COLOR_BGR2GRAY)
        gb = cv2.cvtColor(small_b, cv2.COLOR_BGR2GRAY)

        # Простой предохранитель на сменах сцен.
        # Не гарантирует обнаружение всех склеек.
        ha = cv2.calcHist([ga], [0], None, [64], [0, 256])
        hb = cv2.calcHist([gb], [0], None, [64], [0, 256])

        cv2.normalize(ha, ha, 1, 0, cv2.NORM_L1)
        cv2.normalize(hb, hb, 1, 0, cv2.NORM_L1)

        distance = cv2.compareHist(
            ha, hb, cv2.HISTCMP_BHATTACHARYYA
        )

        if distance > self.scene_threshold:
            result = (True, None, None)
        else:
            forward = self.dis.calc(ga, gb, None)
            backward = self.dis.calc(gb, ga, None)
            result = (False, forward, backward)

        self.cache_key = key
        self.cache = result
        return result

    def warp(self, image, flow, fraction):
        h, w = image.shape[:2]
        fh, fw = flow.shape[:2]

        if self.grid_shape != (h, w):
            self.grid_x = np.arange(
                w, dtype=np.float32
            )[None, :]
            self.grid_y = np.arange(
                h, dtype=np.float32
            )[:, None]
            self.grid_shape = (h, w)

        map_x = cv2.resize(flow[:, :, 0], (w, h))
        map_y = cv2.resize(flow[:, :, 1], (w, h))

        map_x *= -fraction * w / fw
        map_y *= -fraction * h / fh

        map_x += self.grid_x
        map_y += self.grid_y

        return cv2.remap(
            image,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )

    def render(self, left, right, target):
        if left is None and right is None:
            raise RuntimeError(
                "Нет опорных кадров для интерполяции"
            )

        if left is None:
            return right[1], "edge_hold"

        if right is None:
            return left[1], "edge_hold"

        if right[0] <= left[0]:
            return left[1], "original"

        alpha = (
            (target - left[0])
            / (right[0] - left[0])
        )

        if alpha <= 1e-6:
            return left[1], "original"

        if alpha >= 1 - 1e-6:
            return right[1], "original"

        is_cut, forward, backward = self.prepare(left, right)

        if is_cut:
            return left[1], "scene_hold"

        warped_a = self.warp(left[1], forward, alpha)
        warped_b = self.warp(right[1], backward, 1 - alpha)

        image = cv2.addWeighted(
            warped_a, 1 - alpha,
            warped_b, alpha,
            0,
        )

        return image, "interpolated"


def write_frame(pipe, image):
    data = memoryview(
        np.ascontiguousarray(image)
    ).cast("B")

    while len(data):
        written = pipe.write(data)
        if not written:
            raise BrokenPipeError(
                "FFmpeg перестал принимать кадры"
            )
        data = data[written:]


def get_target_bitrate(args, metadata):
    if args.bitrate_mbps is not None:
        return round(args.bitrate_mbps * 1_000_000)

    if args.rate_control == "quality":
        return None

    try:
        bitrate = int(metadata.get("bit_rate", 0))
    except (TypeError, ValueError):
        bitrate = 0

    if bitrate <= 0:
        raise RuntimeError(
            "Видеобитрейт отсутствует в метаданных. "
            "Задай --bitrate-mbps вручную "
            "или используй --rate-control quality."
        )

    return bitrate


def make_encoder_command(
    args, width, height, rate, bitrate, metadata, path
):
    command = [
        "ffmpeg", "-hide_banner",
        "-loglevel", "warning", "-y",
        "-f", "rawvideo",
        "-pixel_format", "bgr24",
        "-video_size", f"{width}x{height}",
        "-framerate", str(rate),
        "-i", "pipe:0",
        "-an",
    ]

    if args.encoder == "nvenc":
        command += [
            "-c:v",
            "hevc_nvenc"
            if args.codec == "hevc"
            else "h264_nvenc",
            "-preset", args.nvenc_preset,
            "-tune", "hq",
            "-rc", "vbr",
        ]

        if bitrate is None:
            command += [
                "-cq", str(args.cq),
                "-b:v", "0",
            ]

    else:
        command += [
            "-c:v",
            "libx265"
            if args.codec == "hevc"
            else "libx264",
            "-preset", "medium",
        ]

        if bitrate is None:
            command += ["-crf", str(args.cq)]

    if bitrate is not None:
        command += [
            "-b:v", str(bitrate),
            "-maxrate", str(round(bitrate * 1.5)),
            "-bufsize", str(bitrate * 2),
        ]

    matrix = metadata.get("color_space")
    if matrix not in ("bt709", "bt470bg", "smpte170m"):
        matrix = "bt709"

    command += [
        "-vf",
        "scale=in_range=full:out_range=limited:"
        f"out_color_matrix={matrix},format=yuv420p",
        "-color_range", "tv",
        "-colorspace", matrix,
    ]

    for option, key in (
        ("-color_primaries", "color_primaries"),
        ("-color_trc", "color_transfer"),
    ):
        value = metadata.get(key)

        if value and value not in (
            "unknown", "N/A", "reserved"
        ):
            command += [option, value]

    command += ["-f", "nut", str(path)]
    return command


def validate_paths(parser, args):
    args.input = args.input.expanduser().resolve()
    args.output = args.output.expanduser().resolve()

    if not args.input.is_file():
        nearby = sorted(
            path.name
            for path in Path.cwd().iterdir()
            if path.is_file()
            and path.suffix.lower() in (
                ".mp4", ".mov", ".mkv", ".avi", ".m4v"
            )
        )

        listing = "\n".join(
            f"  {name}" for name in nearby[:30]
        ) or "  Видео в текущей папке не найдены."

        parser.error(
            f"\nВходной файл не найден:\n  {args.input}\n"
            f"\nТекущая папка:\n  {Path.cwd()}\n"
            f"\nВидеофайлы в ней:\n{listing}\n"
            "\nПередай настоящее имя или полный путь в кавычках."
        )

    if args.input == args.output:
        parser.error(
            "Входной и выходной файлы должны различаться"
        )

    if args.output.exists() and not args.analyze_only:
        parser.error(
            f"Выходной файл уже существует:\n{args.output}\n"
            "Выбери другое имя или удали старый результат."
        )

    if not args.output.parent.is_dir():
        parser.error(
            "Каталог выходного файла не существует"
        )

    if args.output.suffix.lower() not in (
        ".mp4", ".mov", ".mkv"
    ):
        parser.error(
            "Выходной файл должен быть .mp4, .mov или .mkv"
        )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Выборочное восстановление кадров "
            "с сохранением исходной временной оси."
        )
    )

    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)

    parser.add_argument(
        "--source-fps", default="auto",
        help="auto, 24, 30, 60, 60000/1001 и т. п.",
    )
    parser.add_argument("--half-fps", action="store_true")

    parser.add_argument(
        "--collision-mode",
        choices=["nearest", "interpolate"],
        default="nearest",
    )

    parser.add_argument(
        "--warn-percent", type=float, default=5.0
    )
    parser.add_argument(
        "--long-gap-ms", type=float, default=100.0
    )

    parser.add_argument(
        "--flow-width", type=int, default=960
    )
    parser.add_argument(
        "--flow-preset",
        choices=["fast", "medium"],
        default="medium",
    )
    parser.add_argument(
        "--scene-threshold", type=float, default=0.65
    )

    parser.add_argument(
        "--encoder",
        choices=["nvenc", "cpu"],
        default="nvenc",
    )
    parser.add_argument(
        "--codec",
        choices=["hevc", "h264"],
        default="hevc",
    )
    parser.add_argument(
        "--rate-control",
        choices=["source", "quality"],
        default="source",
    )
    parser.add_argument("--bitrate-mbps", type=float)
    parser.add_argument("--cq", type=int, default=19)
    parser.add_argument(
        "--nvenc-preset",
        choices=["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
        default="p4",
    )

    parser.add_argument(
        "--analyze-only", action="store_true"
    )

    args = parser.parse_args()
    validate_paths(parser, args)

    for program in ("ffmpeg", "ffprobe"):
        if shutil.which(program) is None:
            parser.error(f"{program} не найден в PATH")

    if (
        not math.isfinite(args.warn_percent)
        or args.warn_percent <= 0
        or not math.isfinite(args.long_gap_ms)
        or args.long_gap_ms <= 0
        or args.flow_width < 64
        or not 0 <= args.scene_threshold <= 1
        or not 1 <= args.cq <= 51
    ):
        parser.error(
            "Некорректные параметры качества или порогов"
        )

    if args.bitrate_mbps is not None:
        if (
            not math.isfinite(args.bitrate_mbps)
            or args.bitrate_mbps <= 0
        ):
            parser.error(
                "--bitrate-mbps должен быть положительным"
            )

    print("Вход:", args.input)
    print("Выход:", args.output)
    print("\nПервый проход: анализ PTS...")

    metadata = probe_video(args.input)
    pts, last_duration = scan_timestamps(args.input)

    deltas = [
        b - a for a, b in zip(pts, pts[1:])
    ]

    median_dt = statistics.median(deltas)
    median_fps = 1.0 / median_dt
    average_fps = (
        (len(pts) - 1)
        / (pts[-1] - pts[0])
    )

    difference = (
        abs(average_fps - median_fps)
        / median_fps * 100
    )

    if args.source_fps == "auto":
        nominal = min(
            STANDARD_RATES,
            key=lambda value: abs(
                float(value) - median_fps
            ),
        )

        error = (
            abs(float(nominal) - median_fps)
            / float(nominal)
        )

        if error > 0.05:
            raise RuntimeError(
                f"Медианная оценка: {median_fps:.6f} FPS. "
                "Нет близкой стандартной частоты. "
                "Задай --source-fps вручную."
            )
    else:
        nominal = parse_rate(args.source_fps)

    divisor = 2 if args.half_fps else 1
    output_rate = nominal / divisor
    output_fps = float(output_rate)
    output_step = 1.0 / output_fps

    warnings = []
    gaps = []

    if difference > args.warn_percent:
        warnings.append(
            "Средний FPS отличается от медианной оценки "
            f"на {difference:.2f}% > {args.warn_percent:g}%. "
            "Возможны многочисленные дропы "
            "или переменная частота съёмки."
        )

    if args.source_fps == "auto":
        nearby_rates = [
            str(rate)
            for rate in STANDARD_RATES
            if abs(float(rate) - median_fps)
            / median_fps < 0.003
        ]

        if len(nearby_rates) > 1:
            warnings.append(
                "Близкие стандарты: "
                + ", ".join(nearby_rates)
                + f". Выбран {nominal}. "
                  "Для точного выбора используй --source-fps."
            )

    for index, delta in enumerate(deltas):
        suspected_drop = delta > 1.5 * median_dt
        large = delta * 1000 >= args.long_gap_ms

        if not (suspected_drop or large):
            continue

        start = pts[index] - pts[0]
        end = pts[index + 1] - pts[0]

        missing = (
            max(
                1,
                math.floor(delta / median_dt + 0.5) - 1,
            )
            if suspected_drop
            else 0
        )

        gaps.append({
            "after_frame_index": index,
            "after_seconds": start,
            "end_seconds": end,
            "after_timecode": timecode(start, nominal),
            "end_timecode": timecode(end, nominal),
            "interval_ms": delta * 1000,
            "estimated_missing": missing,
            "large": large,
        })

    large_count = sum(gap["large"] for gap in gaps)

    if large_count:
        warnings.append(
            f"Разрывов от {args.long_gap_ms:g} мс: "
            f"{large_count}. На них возможны "
            "сильные артефакты интерполяции."
        )

    if not last_duration or last_duration <= 0:
        last_duration = median_dt
        warnings.append(
            "Длительность последнего кадра неизвестна; "
            "конец оценён по медианному интервалу."
        )

    source_duration = (
        pts[-1] - pts[0] + last_duration
    )

    output_count = max(
        1,
        math.ceil(
            source_duration * output_fps - 1e-9
        ),
    )

    output_duration = output_count / output_fps

    report = {
        "input": str(args.input),
        "output": str(args.output),
        "timecode_format": "HH:MM:SS:FF, NDF",
        "timecode_origin": "first video frame",
        "timecode_rate": str(nominal),
        "first_video_pts": pts[0],
        "input_frames": len(pts),
        "median_interval_ms": median_dt * 1000,
        "median_fps": median_fps,
        "average_fps": average_fps,
        "difference_percent": difference,
        "nominal_fps": str(nominal),
        "output_fps": str(output_rate),
        "output_frames": output_count,
        "source_video_duration": source_duration,
        "output_video_duration": output_duration,
        "expected_output_video_end": (
            pts[0] + output_duration
        ),
        "warnings": warnings,
        "gaps": gaps,
    }

    report_path = args.output.with_name(
        args.output.name + ".drops.json"
    )
    save_report(report_path, report)

    print(f"\nСредний FPS:          {average_fps:.6f}")
    print(
        f"Медианный интервал:   {median_dt * 1000:.4f} мс"
    )
    print(f"FPS по медиане:       {median_fps:.6f}")
    print(f"Расхождение:          {difference:.2f}%")
    print(f"Номинальный FPS:      {nominal}")
    print(f"Выходной FPS:         {output_rate}")
    print(f"Исходных кадров:      {len(pts)}")
    print(f"Выходных кадров:      {output_count}")
    print(f"Подозрительных мест:  {len(gaps)}")
    print(
        "Оценка пропущенных:   "
        f"{sum(g['estimated_missing'] for g in gaps)}"
    )

    for warning in warnings:
        print(f"\nПРЕДУПРЕЖДЕНИЕ: {warning}")

    for index, gap in enumerate(gaps):
        if index < 40 or gap["large"]:
            mark = (
                " [БОЛЬШОЙ РАЗРЫВ]"
                if gap["large"]
                else ""
            )

            print(
                f"  {gap['after_timecode']} → "
                f"{gap['end_timecode']}: "
                f"{gap['interval_ms']:.2f} мс, "
                f"пропущено ≈ {gap['estimated_missing']}"
                f"{mark}"
            )

    if len(gaps) > 40:
        print("Полный список разрывов записан в JSON.")

    print(f"\nОтчёт: {report_path}")
    print(
        "Округление конца видео: "
        f"{(output_duration - source_duration) * 1000:.3f} мс"
    )
    print(
        "Ожидаемое начало видео: "
        f"{pts[0]:.9f} с"
    )
    print(
        "Ожидаемый конец видео:  "
        f"{pts[0] + output_duration:.9f} с"
    )

    if args.analyze_only:
        return

    validate_video_format(metadata)
    bitrate = get_target_bitrate(args, metadata)

    if bitrate is None:
        print(f"Кодирование: CQ/CRF {args.cq}")
    else:
        print(
            "Целевой видеобитрейт: "
            f"{bitrate / 1_000_000:.2f} Мбит/с"
        )

    stats = {
        "original": 0,
        "interpolated": 0,
        "edge_hold": 0,
        "scene_hold": 0,
        "empty_slots": 0,
        "collision_slots": 0,
    }

    interpolator = Interpolator(
        args.flow_width,
        args.flow_preset,
        args.scene_threshold,
    )

    start_time = time.monotonic()
    frames = decode_frames(args.input)

    try:
        pending = next(frames, None)

        if pending is None:
            raise RuntimeError(
                "Не удалось получить первый кадр"
            )

        height, width = pending[1].shape[:2]

        if width % 2 or height % 2:
            raise RuntimeError(
                "Ширина и высота должны быть чётными"
            )

        with tempfile.TemporaryDirectory(
            prefix="fixdrops_",
            dir=str(args.output.parent),
        ) as temp_dir:
            temporary_video = (
                Path(temp_dir) / "video.nut"
            )

            command = make_encoder_command(
                args,
                width,
                height,
                output_rate,
                bitrate,
                metadata,
                temporary_video,
            )

            print(
                f"\nОбработка {width}x{height}: "
                f"DIS {args.flow_preset}, "
                f"flow-width={args.flow_width}"
            )
            print(
                "Интерполяция — CPU; "
                f"кодирование — {args.encoder}"
            )

            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                bufsize=0,
            )

            previous = None

            try:
                for index in range(output_count):
                    target = (
                        pts[0] + index * output_step
                    )
                    right_edge = (
                        target + output_step / 2
                    )

                    before_bin = previous
                    candidates = []

                    # Кадры назначаются ячейкам сетки
                    # последовательно, с сохранением порядка.
                    while (
                        pending is not None
                        and pending[0] < right_edge
                    ):
                        candidates.append(pending)
                        previous = pending
                        pending = next(frames, None)

                    left = before_bin
                    right = None

                    # Опоры вокруг точного момента target.
                    for item in candidates:
                        if item[0] <= target:
                            left = item

                        if (
                            item[0] >= target
                            and right is None
                        ):
                            right = item

                    if right is None:
                        right = pending

                    if (
                        left is not None
                        and left[0] > target
                    ):
                        left = None

                    # При half-fps два исходных кадра
                    # в ячейке — штатное прореживание.
                    collision = (
                        len(candidates) > divisor
                    )

                    if collision:
                        stats["collision_slots"] += 1

                    if not candidates:
                        stats["empty_slots"] += 1
                        image, kind = interpolator.render(
                            left, right, target
                        )

                    elif (
                        collision
                        and args.collision_mode
                        == "interpolate"
                    ):
                        image, kind = interpolator.render(
                            left, right, target
                        )

                    else:
                        chosen = min(
                            candidates,
                            key=lambda item: abs(
                                item[0] - target
                            ),
                        )
                        image = chosen[1]
                        kind = "original"

                    stats[kind] += 1
                    write_frame(process.stdin, image)

                    if (
                        index % 60 == 0
                        or index + 1 == output_count
                    ):
                        elapsed = max(
                            0.001,
                            time.monotonic() - start_time,
                        )
                        speed = (index + 1) / elapsed

                        print(
                            f"\rКадр {index + 1}/{output_count}"
                            f" | {speed:.1f} кадр/с"
                            " | интерполировано "
                            f"{stats['interpolated']}",
                            end="",
                            flush=True,
                        )

                process.stdin.close()
                return_code = process.wait()

                if return_code:
                    raise RuntimeError(
                        "FFmpeg завершил кодирование "
                        "с ошибкой. Смотри сообщение выше."
                    )

            except BaseException:
                if process.poll() is None:
                    process.kill()
                process.wait()
                raise

            finally:
                if (
                    process.stdin is not None
                    and not process.stdin.closed
                ):
                    process.stdin.close()

            print(
                "\n\nПроверка временных меток "
                "промежуточного видео..."
            )

            # Ключевое исправление:
            # промежуточный контейнер может начинаться
            # не с нулевого PTS.
            temporary_start = first_video_pts(
                temporary_video
            )
            video_offset = pts[0] - temporary_start

            print(
                "Первый PTS исходника:       "
                f"{pts[0]:.9f} с"
            )
            print(
                "Первый PTS промежуточного: "
                f"{temporary_start:.9f} с"
            )
            print(
                "Поправка при сборке:       "
                f"{video_offset:+.9f} с"
            )

            report["temporary_first_video_pts"] = (
                temporary_start
            )
            report["mux_video_offset"] = video_offset
            report["processing"] = stats
            report["target_bitrate"] = bitrate
            save_report(report_path, report)

            print(
                "\nСборка контейнера. "
                "Звук берётся непосредственно из исходника."
            )

            mux_command = [
                "ffmpeg", "-hide_banner",
                "-loglevel", "warning", "-n",
                "-copyts",

                # Исходник — источник аудио с исходными PTS.
                "-i", str(args.input),

                # Компенсируем фактический сдвиг
                # промежуточного видеопотока.
                "-itsoffset", f"{video_offset:.12f}",
                "-i", str(temporary_video),

                "-map", "1:v:0",
                "-map", "0:a?",

                "-map_metadata", "-1",
                "-map_chapters", "-1",
                "-c", "copy",
                "-avoid_negative_ts", "disabled",
            ]

            if args.output.suffix.lower() in (
                ".mp4", ".mov"
            ):
                # Мелкая временная база, кратная FPS:
                # точный шаг кадра + точнее начальное смещение.
                track_timescale = (
                    output_rate.numerator * 1000
                )

                if track_timescale > 2_000_000_000:
                    raise RuntimeError(
                        "Слишком большая временная база. "
                        "Проверь --source-fps."
                    )

                mux_command += [
                    "-movflags", "+faststart",
                    "-video_track_timescale",
                    str(track_timescale),
                    "-use_editlist", "1",
                ]

                if args.codec == "hevc":
                    mux_command += [
                        "-tag:v", "hvc1"
                    ]

            # Намеренно нет -t и -shortest:
            # хвост исходного аудио не обрезаем.
            mux_command += [str(args.output)]

            result = subprocess.run(mux_command)

            if result.returncode:
                raise RuntimeError(
                    "Ошибка сборки контейнера. "
                    "Если аудиокодек несовместим с MP4, "
                    "попробуй выход .mkv."
                )

    finally:
        frames.close()

    # Проверяем, что подтверждённый ранее сдвиг начала
    # действительно устранён в итоговом файле.
    print("\nПроверка начала итогового видео...")

    actual_start = first_video_pts(args.output)
    output_metadata = probe_video(args.output)

    output_time_base = float(
        Fraction(output_metadata["time_base"])
    )

    # У MP4 edit list может иметь миллисекундное округление;
    # у MKV часто также миллисекундная временная база.
    tolerance = max(
        0.0011,
        2 * output_time_base,
    )

    start_error = actual_start - pts[0]

    report["actual_first_video_pts"] = actual_start
    report["first_video_pts_error_ms"] = (
        start_error * 1000
    )
    report["processing_seconds"] = (
        time.monotonic() - start_time
    )

    print(f"Ожидалось: {pts[0]:.9f} с")
    print(f"Получено:  {actual_start:.9f} с")
    print(f"Разница:   {start_error * 1000:+.3f} мс")

    if abs(start_error) > tolerance:
        report["timing_check"] = "FAILED"
        save_report(report_path, report)

        raise RuntimeError(
            "Итоговый контейнер изменил начало видео "
            f"на {start_error * 1000:+.3f} мс. "
            "Файл создан, но проверку синхронизации не прошёл. "
            "Не используй его для мультикамеры без проверки."
        )

    # Если контейнер сообщает количество кадров,
    # сверяем его с запланированным.
    reported_count = output_metadata.get("nb_frames")

    if reported_count not in (None, "N/A"):
        actual_count = int(reported_count)
        report["container_output_frames"] = actual_count

        if actual_count != output_count:
            report["timing_check"] = "FAILED"
            save_report(report_path, report)

            raise RuntimeError(
                "Количество кадров в контейнере "
                f"({actual_count}) не совпадает "
                f"с ожидаемым ({output_count})."
            )

    report["timing_check"] = "video_start_ok"
    save_report(report_path, report)

    print("\nГотово:", args.output)
    print(
        "Исходных изображений на выходе:",
        stats["original"],
    )
    print(
        "Интерполированных:",
        stats["interpolated"],
    )
    print(
        "Повторов на краях:",
        stats["edge_hold"],
    )
    print(
        "Повторов на предполагаемых склейках:",
        stats["scene_hold"],
    )
    print(
        "Пустых позиций сетки:",
        stats["empty_slots"],
    )
    print(
        "Позиций с конфликтами:",
        stats["collision_slots"],
    )
    print("Проверка начала видео: пройдена")
    print(
        "Аудио скопировано без перекодирования; "
        "его декодированные сэмплы автоматически не сравнивались."
    )
    print("Отчёт:", report_path)


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("\nОстановлено.", file=sys.stderr)
        sys.exit(130)

    except Exception as error:
        print(f"\nОшибка: {error}", file=sys.stderr)
        print(
            "При ошибке NVENC попробуй --encoder cpu.\n"
            "Если выходной файл уже создан, перед повтором "
            "выбери другое имя или удали старый результат.",
            file=sys.stderr,
        )
        sys.exit(1)