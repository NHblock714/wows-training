"""Qt background jobs; parsing runs in isolated, cancellable subprocesses."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from PySide6.QtCore import QThread, Signal

from wows_training.library import Library
from wows_training.runtime_bundle import file_hash, load_bundle


class ImportWorker(QThread):
    item_started = Signal(int, str)
    item_finished = Signal(int, str, str, str)
    batch_finished = Signal(object)

    def __init__(self, paths, data_root, bundle_root, parent=None, *, refresh=False):
        super().__init__(parent)
        self.paths, self.data_root, self.bundle_root = list(paths), Path(data_root), Path(bundle_root)
        self.refresh = bool(refresh)
        self.process = None

    def run(self):
        totals = {"imported": 0, "duplicate": 0, "failed": 0, "cancelled": 0, "updated": 0}
        try:
            library = Library(self.data_root)
            work = library.root / "jobs"
            work.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            totals["failed"] = len(self.paths)
            for index, filename in enumerate(self.paths):
                self.item_started.emit(index, str(filename))
                self.item_finished.emit(index, "失败", f"资料库初始化失败，未更新现有分析：{exc}", "")
            self.batch_finished.emit(totals)
            return
        for index, filename in enumerate(self.paths):
            if self.isInterruptionRequested():
                totals["cancelled"] += len(self.paths) - index
                break
            path = Path(filename)
            self.item_started.emit(index, str(path))
            try:
                if path.suffix.lower() != ".wowsreplay" or not path.is_file():
                    raise ValueError("文件不存在或不是 .wowsreplay")
                if path.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError("回放超过当前 64 MiB 限制")
                fingerprint = file_hash(path)
                existing = library.find_replay(fingerprint)
                if existing and not self.refresh:
                    record = library.ingest(library.get_report(fingerprint), path)
                    totals["duplicate"] += 1
                    self.item_finished.emit(index, "重复", "已在资料库中，保留来源路径", record["id"])
                    continue
                if self.refresh and not existing:
                    raise ValueError("该回放尚未入库或文件内容已变化，请先普通导入")
                output = work / (uuid.uuid4().hex + ".json")
                log = output.with_suffix(".log")
                if getattr(sys, "frozen", False):
                    command = [sys.executable, "--parse-worker"]
                else:
                    command = [sys.executable, "-B", "-m", "wows_training.app", "--parse-worker"]
                command += ["--input", str(path.resolve()), "--output", str(output), "--bundle", str(self.bundle_root)]
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                environment = os.environ.copy()
                environment["PYTHONIOENCODING"] = "utf-8"
                if not getattr(sys, "frozen", False):
                    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + environment.get("PYTHONPATH", "")
                with log.open("w", encoding="utf-8") as stream:
                    self.process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, creationflags=flags, env=environment)
                    start = time.monotonic()
                    while self.process.poll() is None:
                        if self.isInterruptionRequested() or time.monotonic() - start > 120:
                            self.process.terminate()
                            self.process.wait(timeout=5)
                            if self.isInterruptionRequested():
                                totals["cancelled"] += len(self.paths) - index
                                self.item_finished.emit(index, "已取消", "当前回放未写入比赛库", "")
                                self.batch_finished.emit(totals)
                                return
                            raise TimeoutError("解析超过 120 秒，其他文件继续处理")
                        self.msleep(80)
                if not output.is_file():
                    raise ValueError(f"解析进程未生成结果，请查看 {log.name}")
                report = json.loads(output.read_text(encoding="utf-8"))
                if report.get("status") == "failed" or self.process.returncode != 0:
                    raise ValueError(report.get("error", "解析失败"))
                if report.get("sha256") != fingerprint or file_hash(path) != fingerprint:
                    raise ValueError("回放在导入期间发生变化，请录制完成后重试")
                if self.isInterruptionRequested():
                    totals["cancelled"] += len(self.paths) - index
                    self.item_finished.emit(index, "已取消", "当前回放未写入比赛库", "")
                    break
                record = library.update_analysis(report, path) if self.refresh else library.ingest(report, path)
                cleanup_note = ""
                try:
                    output.unlink(missing_ok=True)
                except OSError:
                    cleanup_note = "（临时解析结果保留在 jobs）"
                if self.refresh:
                    totals["updated"] += 1
                    self.item_finished.emit(index, "已更新", f"复盘数据已更新至分析版本 {record['analysis_revision']}，旧版与批注已保留" + cleanup_note, record["id"])
                elif record.get("ingest_status") == "duplicate":
                    totals["duplicate"] += 1
                    self.item_finished.emit(index, "重复", "已在资料库中，保留来源路径" + cleanup_note, record["id"])
                else:
                    totals["imported"] += 1
                    self.item_finished.emit(index, "已导入", "基础资料已保存" + cleanup_note, record["id"])
            except Exception as exc:
                totals["failed"] += 1
                self.item_finished.emit(index, "失败", str(exc), "")
            finally:
                self.process = None
        self.batch_finished.emit(totals)


class ExportWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, data_root, bundle_root, replay_shas, output, summary=False, clock=None, parent=None, *, recording_id=None):
        super().__init__(parent)
        self.data_root, self.bundle_root = Path(data_root), Path(bundle_root)
        self.replay_shas, self.output, self.summary, self.clock = list(replay_shas), Path(output), summary, clock
        self.recording_id = recording_id

    def run(self):
        try:
            from wows_training.reporting import export_single, export_summary
            library, bundle = Library(self.data_root), load_bundle(self.bundle_root, verify=False)
            if self.recording_id:
                from wows_training.multiview_store import BattleRecordingStore
                recordings = BattleRecordingStore(library.root)
                record = recordings.get(self.recording_id)
                report = recordings.load_report(self.recording_id)
                battle = library.get_battle(record['battle_id'])
                report['event_key'] = battle['event_key']
                report['battle_annotation'] = {key: battle.get(key) for key in ('label', 'notes')}
                reports = [bundle.presentation_report(report)]
            else:
                reports = [bundle.presentation_report(library.get_report(sha)) for sha in self.replay_shas]
            from wows_training.workbench_store import WorkbenchStore
            from wows_training.workbench_service import attach_identities
            store = WorkbenchStore(library.root)
            reports = [attach_identities(report, store, library.list_mappings()) for report in reports]
            kwargs = {"mappings": library.list_mappings(), "ship_catalog": bundle.ship_catalog()}
            if self.summary:
                result = export_summary(reports, self.output, **kwargs)
            else:
                result = export_single(reports[0], self.output, clock=self.clock, **kwargs)
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
