import argparse
import copy
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import av
import cv2
import numpy as np

import fix_drops as core


ORIGINAL_VALIDATE = core.validate_video_format
ORIGINAL_DECODE = core.decode_frames


def emit(name, value):
    print(
        "@@" + name + " " + json.dumps(value, ensure_ascii=False),
        flush=True,
    )


def format_timestep_variants(alpha):
    # Эта сборка ожидает запятую, другие сборки - точку.
    # Пробуем сначала запятую, потом точку.
    dot = f"{alpha:.9f}"
    comma = dot.replace(".", ",")
    return [comma, dot]


def matrix_is_mirrored(side_data):
    text = side_data.get("displaymatrix")
    if not text:
        return False
    rows = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        values = re.findall(r"-?\d+", line.split(":", 1)[1])
        if len(values) >= 3:
            rows.append([int(value) for value in values[:3]])
    if len(rows) < 2:
        return False
    a, b = rows[0][:2]
    c, d = rows[1][:2]
    return a * d - b * c < 0


def rotation_degrees(metadata):
    raw_rotation = metadata.get("tags", {}).get("rotate", 0) or 0
    for side_data in metadata.get("side_data_list", []):
        if matrix_is_mirrored(side_data):
            raise RuntimeError(
                "Обнаружена зеркальная display matrix. "
                "Поддерживаются только повороты 0/90/180/270."
            )
        if "rotation" in side_data:
            raw_rotation = side_data["rotation"]
    try:
        value = float(raw_rotation)
    except (ValueError, TypeError):
        raise RuntimeError(f"Некорректное значение поворота: {raw_rotation!r}")
    if not np.isfinite(value):
        raise RuntimeError("Некорректный угол поворота")
    normalized = value % 360
    nearest = (round(normalized / 90) * 90) % 360
    error = abs((normalized - nearest + 180) % 360 - 180)
    if error > 0.1:
        raise RuntimeError(
            f"Поворот {value:.3f}° не поддерживается. Допустимы 0/90/180/270°."
        )
    return int(nearest)


def rotate_image(image, angle):
    if angle == 90:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if angle == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if angle == 270:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    return image


def validate_with_rotation(metadata):
    rotation_degrees(metadata)
    cleaned = copy.deepcopy(metadata)
    if "tags" in cleaned:
        cleaned["tags"].pop("rotate", None)
    for side_data in cleaned.get("side_data_list", []):
        if "rotation" in side_data:
            side_data["rotation"] = 0
    ORIGINAL_VALIDATE(cleaned)


def decode_with_rotation(path):
    metadata = core.probe_video(path)
    angle = rotation_degrees(metadata)
    if angle:
        desc = {90: "90° CCW", 180: "180°", 270: "90° CW"}[angle]
        print(f"\nПрименяю ориентацию к изображениям: {desc}.", flush=True)
    frames = ORIGINAL_DECODE(path)
    try:
        for pts, image in frames:
            yield pts, rotate_image(image, angle)
    finally:
        frames.close()


core.validate_video_format = validate_with_rotation
core.decode_frames = decode_with_rotation


class RifeInterpolator:
    def __init__(self, settings, workdir, scene_threshold=0.65):
        self.exe = Path(settings["rife_exe"]).resolve()
        self.model = Path(settings["rife_model"]).resolve()
        self.gpu = str(settings.get("gpu", "0"))
        self.scene_threshold = scene_threshold
        if not self.exe.is_file():
            raise RuntimeError("Не найден rife-ncnn-vulkan.exe")
        if not self.model.is_dir():
            raise RuntimeError("Не найдена папка модели RIFE")
        self.temp = tempfile.TemporaryDirectory(prefix="rife_", dir=str(workdir))
        self.folder = Path(self.temp.name)
        self.a_path = self.folder / "a.png"
        self.b_path = self.folder / "b.png"
        self.out_path = self.folder / "out.png"
        self.key = None
        self.is_cut = False

    def close(self):
        self.temp.cleanup()

    @staticmethod
    def write_png(path, image):
        ok, encoded = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise RuntimeError("Ошибка кодирования опорного PNG")
        encoded.tofile(str(path))

    def prepare(self, left, right):
        key = (left[0], right[0])
        if key == self.key:
            return
        a, b = left[1], right[1]
        ga = cv2.cvtColor(cv2.resize(a, (256, 144)), cv2.COLOR_BGR2GRAY)
        gb = cv2.cvtColor(cv2.resize(b, (256, 144)), cv2.COLOR_BGR2GRAY)
        ha = cv2.calcHist([ga], [0], None, [64], [0, 256])
        hb = cv2.calcHist([gb], [0], None, [64], [0, 256])
        cv2.normalize(ha, ha, 1, 0, cv2.NORM_L1)
        cv2.normalize(hb, hb, 1, 0, cv2.NORM_L1)
        distance = cv2.compareHist(ha, hb, cv2.HISTCMP_BHATTACHARYYA)
        self.is_cut = distance > self.scene_threshold
        if not self.is_cut:
            self.write_png(self.a_path, a)
            self.write_png(self.b_path, b)
        self.key = key

    def render(self, left, right, target):
        if left is None and right is None:
            raise RuntimeError("Нет опорных кадров")
        if left is None:
            return right[1], "edge_hold"
        if right is None:
            return left[1], "edge_hold"
        if right[0] <= left[0]:
            return left[1], "original"
        alpha = (target - left[0]) / (right[0] - left[0])
        if alpha <= 1e-6:
            return left[1], "original"
        if alpha >= 1 - 1e-6:
            return right[1], "original"
        self.prepare(left, right)
        if self.is_cut:
            return left[1], "scene_hold"

        last_stdout = ""
        last_stderr = ""
        for ts in format_timestep_variants(alpha):
            if self.out_path.exists():
                self.out_path.unlink()
            result = subprocess.run(
                [
                    str(self.exe),
                    "-0", str(self.a_path),
                    "-1", str(self.b_path),
                    "-o", str(self.out_path),
                    "-m", str(self.model),
                    "-s", ts,
                    "-g", self.gpu,
                    "-j", "1:1:1",
                ],
                cwd=str(self.exe.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            last_stdout = result.stdout
            last_stderr = result.stderr
            if result.returncode == 0 and self.out_path.is_file():
                data = np.fromfile(str(self.out_path), dtype=np.uint8)
                image = cv2.imdecode(data, cv2.IMREAD_COLOR)
                if image is not None and image.shape == left[1].shape:
                    return image, "interpolated"
            # Если ошибка именно про timestep - пробуем второй формат
            if "invalid timestep" in (last_stdout + last_stderr).lower():
                continue
            else:
                break

        raise RuntimeError(
            "Ошибка RIFE. Требуется сборка с параметром -s и модель RIFE v4.\n"
            + last_stdout[-1500:] + "\n" + last_stderr[-3500:]
        )


def make_interpolator(settings, workdir, threshold=0.65):
    if settings["engine"] == "RIFE":
        return RifeInterpolator(settings, workdir, threshold)
    preset = "fast" if settings["engine"] == "DIS fast" else "medium"
    return core.Interpolator(int(settings["flow_width"]), preset, threshold)


def benchmark(job):
    source = Path(job["source"])
    settings = job["settings"]
    workdir = Path(job["workdir"])
    print("Benchmark: checking video format...", flush=True)
    metadata = core.probe_video(source)
    core.validate_video_format(metadata)
    angle = rotation_degrees(metadata)
    if angle:
        print(f"Rotation metadata: {angle}° CCW. Benchmark will use upright images.", flush=True)
    print("Benchmark: decoding...", flush=True)
    samples = []
    count = 0
    conversion_time = 0.0
    started = time.perf_counter()
    with av.open(str(source)) as container:
        if not container.streams.video:
            raise RuntimeError("Видеопоток не найден")
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        for frame in container.decode(stream):
            count += 1
            if count in (1, 3, 10, 30, 60):
                before = time.perf_counter()
                image = frame.to_ndarray(format="bgr24")
                image = rotate_image(image, angle)
                samples.append(image)
                conversion_time += time.perf_counter() - before
            if count >= 120:
                break
    elapsed = time.perf_counter() - started
    if len(samples) < 2:
        raise RuntimeError("Недостаточно кадров для benchmark")
    height, width = samples[0].shape[:2]
    if width % 2 or height % 2:
        raise RuntimeError("Нужны чётные размеры видео")
    print(f"Benchmark image size: {width}x{height}", flush=True)
    print("Benchmark: interpolation...", flush=True)
    interpolator = make_interpolator(settings, workdir, threshold=1.0)
    started = time.perf_counter()
    try:
        _, kind = interpolator.render((0.0, samples[0]), (1.0, samples[-1]), 0.25)
        if kind != "interpolated":
            raise RuntimeError("Не удалось проверить интерполяцию")
    finally:
        close = getattr(interpolator, "close", None)
        if close:
            close()
    interpolation_time = time.perf_counter() - started
    print("Benchmark: encoding...", flush=True)
    args = argparse.Namespace(
        encoder=settings["encoder"], codec=settings["codec"], nvenc_preset="p4", cq=19
    )
    bitrate_text = settings.get("bitrate", "")
    if bitrate_text:
        bitrate = round(float(bitrate_text) * 1_000_000)
    else:
        try:
            bitrate = int(metadata.get("bit_rate", 0)) or None
        except (TypeError, ValueError):
            bitrate = None
    command = core.make_encoder_command(
        args, width, height, 60, bitrate, metadata, workdir / "benchmark.nut"
    )
    encode_count = 24
    started = time.perf_counter()
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        for index in range(encode_count):
            core.write_frame(process.stdin, samples[index % len(samples)])
        process.stdin.close()
        if process.wait():
            raise RuntimeError("Ошибка кодирования benchmark")
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
    encoding_time = time.perf_counter() - started
    emit("BENCH", {
        "width": width,
        "height": height,
        "decode_per_frame": max(0.00001, (elapsed - conversion_time) / count),
        "convert_per_frame": conversion_time / len(samples),
        "interpolate_per_frame": interpolation_time,
        "encode_per_frame": encoding_time / encode_count,
        "created": time.time(),
        "engine": settings["engine"],
        "rotation_applied_ccw": angle,
    })


def run_core(job):
    settings = job["settings"]
    source = Path(job["source"])
    output = Path(job["output"])
    metadata = core.probe_video(source)
    angle = rotation_degrees(metadata)
    original_class = core.Interpolator
    instances = []

    def factory(flow_width, flow_preset, scene_threshold):
        if settings["engine"] == "RIFE":
            instance = RifeInterpolator(settings, Path(job["workdir"]), scene_threshold)
            instances.append(instance)
            return instance
        return original_class(flow_width, flow_preset, scene_threshold)

    core.Interpolator = factory
    arguments = [
        "fix_drops.py",
        str(source),
        str(output),
        "--source-fps", settings["source_fps"],
        "--collision-mode", settings["collision"],
        "--flow-width", str(settings["flow_width"]),
        "--flow-preset", "fast" if settings["engine"] == "DIS fast" else "medium",
        "--encoder", settings["encoder"],
        "--codec", settings["codec"],
    ]
    if settings.get("bitrate"):
        arguments += ["--bitrate-mbps", str(settings["bitrate"])]
    if settings.get("half"):
        arguments.append("--half-fps")
    if job["action"] == "analyze":
        arguments.append("--analyze-only")
    previous_argv = sys.argv
    sys.argv = arguments
    try:
        core.main()
    finally:
        sys.argv = previous_argv
        core.Interpolator = original_class
        for instance in instances:
            instance.close()

    report_path = output.with_name(output.name + ".drops.json")
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["source_rotation_ccw"] = angle
        report["rotation_applied_to_pixels"] = job["action"] != "analyze" and angle != 0
        report["interpolation_engine"] = settings["engine"]
        if settings["engine"] == "RIFE":
            report["rife_model"] = str(Path(settings["rife_model"]).resolve())
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if job["action"] != "analyze":
        output_metadata = core.probe_video(output)
        remaining_rotation = rotation_degrees(output_metadata)
        if remaining_rotation != 0:
            raise RuntimeError("В выходном файле сохранился поворот через метаданные.")
        if angle:
            print("\nOrientation check passed: rotation applied to pixels.", flush=True)


def main():
    if len(sys.argv) != 2:
        raise RuntimeError("Worker запускается через GUI")
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if job["action"] == "benchmark":
        benchmark(job)
    else:
        run_core(job)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"\nERROR: {error}", flush=True)
        sys.exit(1)