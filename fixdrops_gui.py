import json
import os
import sys
import time
from fractions import Fraction
from pathlib import Path
from tkinter import messagebox, ttk

import fixdrops_gui_impl as gui


ALLOWED_FPS = ("auto", "24", "30", "60")
gui.RATES = [Fraction(24), Fraction(30), Fraction(60)]

SESSION_FILE = gui.CONFIG_DIR / "session_v3.json"

ACTIVE_STATES = {"Analyze", "Repair", "Benchmark"}
FINAL_STATES = {"Done", "Error", "Stopped"}


def path_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def fingerprint(path):
    """
    Быстрая проверка изменения файла.
    Это не криптографический хеш содержимого.
    """
    try:
        stat = Path(path).stat()
        return {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    except OSError:
        return None


def clock(seconds):
    seconds = max(0, round(seconds))
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def all_widgets(parent):
    for widget in parent.winfo_children():
        yield widget
        yield from all_widgets(widget)


class App(gui.App):
    def __init__(self):
        # Инициализируем поля до базового конструктора.
        self.extra_ready = False
        self.restoring = False
        self.history = {}
        self.last_saved_text = None

        self.job_ids = []
        self.job_actions = {}
        self.observed_states = {}
        self.active_id = None

        self.last_progress = None
        self.last_progress_time = None
        self.file_started = None
        self.line_buffer = ""
        self.pulsing = False
        self.was_busy = False

        saved = gui.load_json(SESSION_FILE, {})
        if not isinstance(saved, dict):
            saved = {}

        history = saved.get("history", {})
        if isinstance(history, dict):
            self.history = history

        # Незавершённая задача предыдущего запуска не считается готовой.
        for record in self.history.values():
            if isinstance(record, dict):
                if record.get("status") in ("running", "queued"):
                    record["status"] = "interrupted"

        config = gui.load_json(gui.CONFIG_FILE, {})
        if config.get("source_fps", "auto") not in ALLOWED_FPS:
            config["source_fps"] = "auto"
            gui.save_json(gui.CONFIG_FILE, config)

        super().__init__()

        # Единственное окно уже создано базовым классом.
        # Ни self.root, ни дополнительный tk.Tk() не нужны.
        for widget in all_widgets(self):
            if (
                isinstance(widget, ttk.Combobox)
                and str(widget.cget("textvariable")) == str(self.source_fps)
            ):
                widget.configure(
                    values=ALLOWED_FPS,
                    state="readonly",
                )

        self.add_history_column()
        self.build_extra_ui()

        self.extra_ready = True
        self.restore_session(saved)

        self.after(500, self.session_tick)
        self.after(250, self.progress_tick)

    def settings(self):
        result = super().settings()
        if result.get("source_fps") not in ALLOWED_FPS:
            result["source_fps"] = "auto"
        return result

    def add_history_column(self):
        columns = list(self.table["columns"])
        if "saved_status" not in columns:
            columns.append("saved_status")
            self.table.configure(columns=columns)

        self.table.heading("saved_status", text="Saved status")
        self.table.column(
            "saved_status",
            width=190,
            minwidth=140,
        )

    def build_extra_ui(self):
        box = ttk.LabelFrame(
            self,
            text="Progress and session",
            padding=8,
        )
        box.pack(
            fill="x",
            padx=8,
            pady=4,
            before=self.log,
        )

        buttons = ttk.Frame(box)
        buttons.pack(fill="x", pady=(0, 6))

        ttk.Button(
            buttons,
            text="Clear list",
            command=self.clear_list,
        ).pack(side="left", padx=3)

        ttk.Button(
            buttons,
            text="Save session",
            command=lambda: self.save_session(force=True),
        ).pack(side="left", padx=3)

        ttk.Label(
            buttons,
            text=(
                "Clear list does not delete media or saved history. "
                "Interrupted files restart from the beginning."
            ),
        ).pack(side="left", padx=12)

        self.file_text = ttk.Label(box, text="No active file")
        self.file_text.pack(fill="x")

        self.phase_text = ttk.Label(box, text="Ready")
        self.phase_text.pack(fill="x", pady=(4, 2))

        self.work_bar = ttk.Progressbar(
            box,
            maximum=100,
            mode="determinate",
        )
        self.work_bar.pack(fill="x")

        self.count_text = ttk.Label(
            box,
            text="Encoding: — | Interpolator calls: — | Synthesized: —",
        )
        self.count_text.pack(fill="x", pady=(4, 2))

        self.time_text = ttk.Label(
            box,
            text="Elapsed: 00:00:00 | Remaining: —",
        )
        self.time_text.pack(fill="x")

        self.queue_text = ttk.Label(box, text="Queue: 0 / 0")
        self.queue_text.pack(fill="x", pady=(8, 2))

        self.queue_bar = ttk.Progressbar(
            box,
            maximum=1,
            mode="determinate",
        )
        self.queue_bar.pack(fill="x")

        self.session_text = ttk.Label(
            box,
            text=f"Session: {SESSION_FILE}",
        )
        self.session_text.pack(fill="x", pady=(6, 0))

    def record_for(self, row):
        key = path_key(row["path"])
        record = self.history.get(key)

        if not isinstance(record, dict):
            record = {}
            self.history[key] = record

        return record

    def restore_session(self, saved):
        self.restoring = True

        try:
            paths = saved.get("files", [])
            if not isinstance(paths, list):
                paths = []

            for value in paths:
                if not isinstance(value, str):
                    continue

                path = Path(value).expanduser().resolve()

                if path.is_file():
                    # Базовая реализация добавляет строку
                    # и запускает быстрый ffprobe в фоне.
                    super().add_paths([str(path)])
                else:
                    key = path_key(path)
                    if key in self.known_paths:
                        continue

                    iid = self.table.insert(
                        "",
                        "end",
                        values=(
                            path.name, "?", "?", "?",
                            "Unknown", "—", "—",
                            "Missing", "Missing file",
                        ),
                    )
                    self.rows[iid] = {
                        "path": path,
                        "meta": None,
                        "state": "Missing",
                    }
                    self.known_paths[key] = iid

            selected_keys = set(saved.get("selected", []))
            selection = [
                iid
                for iid, row in self.rows.items()
                if path_key(row["path"]) in selected_keys
            ]

            if selection:
                self.table.selection_set(selection)

        finally:
            self.restoring = False

        # Статусы применятся также после прихода metadata.
        self.refresh_all()

    def save_session(self, force=False):
        if not self.extra_ready or self.restoring:
            return

        selected = [
            path_key(self.rows[iid]["path"])
            for iid in self.table.selection()
            if iid in self.rows
        ]

        data = {
            "version": 3,
            "files": [
                str(row["path"]) for row in self.rows.values()
            ],
            "selected": selected,
            "history": self.history,
        }

        text = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
        )

        if not force and text == self.last_saved_text:
            return

        try:
            gui.save_json(SESSION_FILE, data)
            self.last_saved_text = text
            self.session_text.configure(
                text=f"Session saved: {SESSION_FILE}"
            )
        except OSError as error:
            self.session_text.configure(
                text=f"Session save failed: {error}"
            )

    def session_tick(self):
        self.save_session()
        self.after(2000, self.session_tick)

    def add_paths(self, paths):
        super().add_paths(paths)
        if self.extra_ready:
            self.save_session()

    def remove_selected(self):
        super().remove_selected()
        if self.extra_ready:
            self.save_session(force=True)

    def clear_list(self):
        if self.busy:
            messagebox.showinfo(
                "Busy",
                "Stop the current queue and wait for file recovery "
                "before clearing the list.",
            )
            return

        if not self.rows:
            return

        if not messagebox.askyesno(
            "Clear list",
            "Clear the current file list?\n\n"
            "Videos and original(dropped) will not be deleted.\n"
            "Saved processing history will remain available "
            "if you add the files again.",
        ):
            return

        for iid in list(self.rows):
            self.table.delete(iid)

        self.rows.clear()
        self.known_paths.clear()

        self.job_ids = []
        self.job_actions = {}
        self.observed_states = {}
        self.active_id = None
        self.last_progress = None
        self.file_started = None

        self.stop_pulse()

        self.work_bar.configure(
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.queue_bar.configure(maximum=1, value=0)

        self.file_text.configure(text="No active file")
        self.phase_text.configure(text="Ready")
        self.queue_text.configure(text="Queue: 0 / 0")
        self.count_text.configure(
            text="Encoding: — | Interpolator calls: — | Synthesized: —"
        )
        self.time_text.configure(
            text="Elapsed: 00:00:00 | Remaining: —"
        )

        self.save_session(force=True)

    def update_saved_status(self, iid):
        row = self.rows.get(iid)
        if not row:
            return

        record = self.record_for(row)
        current_signature = fingerprint(row["path"])

        if current_signature is None:
            self.table.set(iid, "saved_status", "Missing file")
            return

        old_signature = record.get("fingerprint")

        # Не переносим Fixed на другой файл с тем же именем.
        # Во время восстановления исходный путь временно отсутствует,
        # поэтому проверку смены файла делаем только вне активной задачи.
        active = row.get("state") in ACTIVE_STATES

        if (
            not active
            and old_signature is not None
            and old_signature != current_signature
        ):
            record.clear()
            record.update({
                "status": "changed",
                "fingerprint": current_signature,
            })

        if not record.get("fingerprint"):
            record["fingerprint"] = current_signature

        status = record.get("status", "new")

        labels = {
            "fixed": "✓ Fixed",
            "analyzed": "Analyzed",
            "running": "In progress",
            "queued": "Queued",
            "error": "Error",
            "stopped": "Stopped",
            "interrupted": "Interrupted",
            "changed": "File changed",
            "new": "New",
        }

        label = labels.get(status, "New")

        if status in ("running", "stopped", "interrupted", "error"):
            progress = record.get("progress")
            if isinstance(progress, dict):
                percent = progress.get("percent")
                if isinstance(percent, (int, float)):
                    label += f" — {int(percent)}%"

        # Fixed имеет приоритет над оценкой свойств.
        # Нулевое расхождение не означает "анализ не нужен".
        if status not in (
            "fixed", "running", "queued",
            "error", "stopped", "interrupted",
        ):
            meta = row.get("meta")
            if meta:
                try:
                    _, difference, _ = self.preliminary(
                        meta, self.settings()
                    )
                    if difference is not None and difference <= 1e-9:
                        label = "✓ Zero FPS diff"
                        if status == "analyzed":
                            label += " / analyzed"
                except (ValueError, TypeError):
                    pass

        self.table.set(iid, "saved_status", label)

    def refresh_row(self, iid):
        super().refresh_row(iid)

        if not self.extra_ready:
            return

        row = self.rows.get(iid)
        if not row:
            return

        state = row.get("state")
        previous = self.observed_states.get(iid)
        changed = state != previous
        self.observed_states[iid] = state

        action = self.job_actions.get(iid)
        record = self.record_for(row)

        if changed and action:
            if state in ACTIVE_STATES:
                self.active_id = iid
                self.file_started = time.monotonic()
                self.last_progress = None
                self.line_buffer = ""

                record["last_action"] = action
                record["updated"] = time.time()

                # Benchmark не изменяет статус обработки видео.
                if action != "benchmark":
                    record["status"] = "running"
                    record.pop("progress", None)

                position = (
                    self.job_ids.index(iid) + 1
                    if iid in self.job_ids else 1
                )

                self.file_text.configure(
                    text=(
                        f"File {position}/{len(self.job_ids)}: "
                        f"{row['path'].name}"
                    )
                )
                self.start_pulse(state)

            elif state in FINAL_STATES:
                record["last_action"] = action
                record["updated"] = time.time()

                if action != "benchmark":
                    if state == "Done":
                        if action == "repair":
                            # Done поступает после успешного run_repair:
                            # результат уже находится на исходном пути.
                            record["status"] = "fixed"
                            record["fingerprint"] = fingerprint(row["path"])
                            record["progress"] = {"percent": 100}
                        elif record.get("status_before_job") == "fixed":
                            record["status"] = "fixed"
                        else:
                            record["status"] = "analyzed"
                            record["fingerprint"] = fingerprint(row["path"])
                            record["progress"] = {"percent": 100}
                    else:
                        record["status"] = (
                            "stopped" if state == "Stopped" else "error"
                        )

                if iid == self.active_id:
                    self.stop_pulse()

                    if state == "Done":
                        self.work_bar.configure(
                            mode="determinate",
                            maximum=100,
                            value=100,
                        )
                        self.phase_text.configure(text="Completed — 100%")
                    else:
                        self.phase_text.configure(text=state)

                self.save_session(force=True)

        self.update_saved_status(iid)
        self.update_queue_progress()

    def start_jobs(self, action, selected):
        if self.busy:
            return

        ids = (
            list(self.table.selection())
            if selected else list(self.rows)
        )
        ids = [
            iid for iid in ids
            if self.rows[iid].get("meta") is not None
            and self.rows[iid]["path"].is_file()
        ]

        if action == "repair":
            filtered = []
            skipped_fixed = 0

            for iid in ids:
                row = self.rows[iid]
                record = self.record_for(row)

                is_fixed = (
                    record.get("status") == "fixed"
                    and record.get("fingerprint")
                    == fingerprint(row["path"])
                )

                if is_fixed:
                    skipped_fixed += 1
                else:
                    filtered.append(iid)

            ids = filtered

            if skipped_fixed:
                self.append_log(
                    f"\nSkipped already fixed files: {skipped_fixed}\n"
                )

        if not ids:
            messagebox.showinfo(
                "Queue",
                "No eligible files.\n"
                "Already fixed files are skipped for repair.",
            )
            return

        if action == "repair":
            if not messagebox.askyesno(
                "Repair",
                f"Repair {len(ids)} file(s)?\n\n"
                "Originals will be moved to original(dropped).\n"
                "Existing backups will not be overwritten.\n\n"
                "Zero FPS difference alone does not prove "
                "that there are no dropped frames.",
            ):
                return

        self.launch(ids, action)

    def launch(self, ids, action):
        if self.busy:
            return

        self.job_ids = list(ids)
        self.job_actions = {iid: action for iid in ids}

        # Позволяет распознать новый запуск независимо
        # от сохранённого состояния строки.
        for iid in ids:
            self.observed_states.pop(iid, None)
            record = self.record_for(self.rows[iid])
            record["status_before_job"] = record.get("status", "new")

        self.active_id = None
        self.last_progress = None
        self.file_started = None
        self.line_buffer = ""

        self.queue_bar.configure(
            maximum=max(1, len(ids)),
            value=0,
        )
        self.queue_text.configure(text=f"Queue: 0 / {len(ids)}")

        super().launch(ids, action)

        if self.busy:
            self.was_busy = True
            self.start_pulse("Starting...")

            if action != "benchmark":
                for iid in ids:
                    record = self.record_for(self.rows[iid])
                    record["status"] = "queued"
                    record["last_action"] = action
                    self.update_saved_status(iid)

            self.save_session(force=True)
        else:
            self.job_actions.clear()
            self.stop_pulse()
            self.phase_text.configure(text="Not started")

    def start_pulse(self, text):
        if not self.extra_ready:
            return

        if not self.pulsing:
            self.work_bar.configure(mode="indeterminate")
            self.work_bar.start(12)
            self.pulsing = True

        self.phase_text.configure(text=text)

    def stop_pulse(self):
        if self.extra_ready:
            self.work_bar.stop()
            self.pulsing = False

    def update_queue_progress(self):
        if not self.extra_ready:
            return

        finished = sum(
            self.rows.get(iid, {}).get("state") in FINAL_STATES
            for iid in self.job_ids
        )
        successful = sum(
            self.rows.get(iid, {}).get("state") == "Done"
            for iid in self.job_ids
        )
        errors = sum(
            self.rows.get(iid, {}).get("state") == "Error"
            for iid in self.job_ids
        )

        partial = 0.0
        if (
            self.active_id
            and self.rows.get(self.active_id, {}).get("state")
            in ACTIVE_STATES
            and self.last_progress
        ):
            partial = self.last_progress.get("percent", 0) / 100

        self.queue_bar.configure(
            maximum=max(1, len(self.job_ids)),
            value=min(len(self.job_ids), finished + partial),
        )

        self.queue_text.configure(
            text=(
                f"Queue: {finished} / {len(self.job_ids)} finished"
                f" | Successful: {successful} | Errors: {errors}"
            )
        )

    def handle_progress(self, data):
        self.last_progress = data
        self.last_progress_time = time.monotonic()

        row = self.rows.get(self.active_id)
        if row:
            record = self.record_for(row)
            record["progress"] = {
                key: data.get(key)
                for key in (
                    "phase", "percent", "elapsed",
                    "written", "output_frames",
                    "interpolation_done", "interpolation_total",
                    "scanned", "input_frames",
                )
            }
            record["updated"] = time.time()
            self.update_saved_status(self.active_id)

        if self.cancel_now.is_set():
            return

        self.stop_pulse()
        percent = int(data.get("percent", 0))

        self.work_bar.configure(
            mode="determinate",
            maximum=100,
            value=percent,
        )

        estimate_type = (
            "planned workload"
            if data.get("planned")
            else "preliminary estimate"
        )

        self.phase_text.configure(
            text=(
                f"{data.get('phase', 'Processing')} — "
                f"{percent}% ({estimate_type})"
            )
        )

        if data.get("phase") == "Analysis":
            self.count_text.configure(
                text=(
                    f"Scanned: {data.get('scanned', 0):,}"
                    " / approximately "
                    f"{data.get('input_frames', 0):,} frames"
                )
            )
        else:
            self.count_text.configure(
                text=(
                    f"Encoding: {data.get('written', 0):,}"
                    f" / {data.get('output_frames', 0):,}"
                    " | Interpolator calls: "
                    f"{data.get('interpolation_done', 0):,}"
                    f" / {data.get('interpolation_total', 0):,}"
                    " | Synthesized: "
                    f"{data.get('synthesized', 0):,}"
                )
            )

        self.update_queue_progress()

    def append_log(self, text):
        if not self.extra_ready:
            super().append_log(text)
            return

        self.line_buffer += text.replace("\r", "\n")

        while "\n" in self.line_buffer:
            line, self.line_buffer = self.line_buffer.split("\n", 1)

            if line.startswith("@@PROGRESS "):
                try:
                    self.handle_progress(
                        json.loads(line[len("@@PROGRESS "):])
                    )
                except (ValueError, KeyError, TypeError):
                    super().append_log(line + "\n")
                continue

            super().append_log(line + "\n")

            if "benchmark:" in line.lower():
                self.start_pulse(line.strip())

    def request_stop_now(self):
        super().request_stop_now()

        if self.busy:
            self.start_pulse(
                "Stopping process tree / restoring original..."
            )
            self.save_session(force=True)

    def progress_tick(self):
        if self.extra_ready and self.busy:
            now = time.monotonic()
            elapsed = (
                now - self.file_started
                if self.file_started is not None else 0
            )

            remaining_text = "—"

            if self.last_progress:
                remaining = self.last_progress.get("remaining")
                if remaining is not None:
                    since_update = now - self.last_progress_time
                    adjusted = remaining - since_update

                    remaining_text = (
                        "~" + clock(adjusted)
                        if adjusted > 1
                        else "Finishing / estimate updating"
                    )

            self.time_text.configure(
                text=(
                    f"Elapsed: {clock(elapsed)}"
                    f" | Remaining: {remaining_text}"
                )
            )

        elif self.extra_ready and self.was_busy:
            self.was_busy = False
            self.stop_pulse()

            # Задания, которые не успели начаться после Stop,
            # возвращаем в исходное состояние истории.
            for iid in self.job_ids:
                row = self.rows.get(iid)
                if not row:
                    continue

                record = self.record_for(row)
                if record.get("status") == "queued":
                    record["status"] = record.get(
                        "status_before_job", "new"
                    )
                    self.update_saved_status(iid)

            if self.cancel_now.is_set():
                self.phase_text.configure(
                    text="Stopped — check log for original-file recovery"
                )
            elif self.stop_after.is_set():
                self.phase_text.configure(
                    text="Queue stopped after current file"
                )

            self.save_session(force=True)

        self.after(250, self.progress_tick)

    def close(self):
        self.save_session(force=True)
        super().close()


if __name__ == "__main__":
    try:
        App().mainloop()
    except KeyboardInterrupt:
        sys.exit(130)