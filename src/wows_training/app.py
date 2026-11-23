"""Desktop entry point and isolated replay worker used by the frozen app."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import traceback


def parse_worker(args):
    from .ordinary_replay import parse_for_import
    try:
        report = parse_for_import(Path(args.input), Path(args.bundle) if args.bundle else None)
        code = 0
    except Exception as exc:
        report = {"status": "failed", "error": str(exc), "input": args.input}
        traceback.print_exc()
        code = 2
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".writing")
    temporary.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(output)
    return code


def statistics_worker(args):
    from .runtime_bundle import parse_portable
    from .timebase import packet_to_battle_time
    from .statistics_store import digest
    try:
        full=parse_portable(Path(args.input),Path(args.bundle) if args.bundle else None)
        if full.get('packet_errors') or full.get('framing_error') or full.get('post_battle_result_errors') or not full.get('roster_matches_header'):
            raise ValueError('当前回放的核心数据未通过校验，统计保持未知。')
        stats=full.get('statistics')
        if not stats:raise ValueError('当前资源包没有经验证的承伤统计定义。')
        for event in stats.get('damage_events',[]):
            event['battle_time']=packet_to_battle_time(full['time_analysis'],event['source']['packet_clock'],packet_offset=event['source']['packet_offset'])
        payload={'extractor_version':'statistics-extract-1','source_sha':full['sha256'],'arena_id':full['arena_id'],
                 'replay_version':full['replay_version'],'metadata':full['metadata'],'roster':full['roster'],
                 'host_absolute_team':full.get('host_absolute_team'),'statistics':stats,'core_verified':True}
        payload['content_fingerprint']=digest(payload)
        code=0
    except Exception as exc:
        payload={'status':'failed','error':str(exc)};code=2
    destination=Path(args.output);destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_suffix('.writing');temporary.write_text(json.dumps(payload,ensure_ascii=False,allow_nan=False),encoding='utf8');temporary.replace(destination)
    return code



def default_data_directory() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    parent = Path(local) if local and Path(local).is_absolute() else Path.home() / "AppData" / "Local"
    return parent / "WOWSTraining"


def main():
    parser = argparse.ArgumentParser(description="WOWS 港口开发部")
    worker = parser.add_mutually_exclusive_group()
    worker.add_argument("--parse-worker", action="store_true", help=argparse.SUPPRESS)
    worker.add_argument("--statistics-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--input", help=argparse.SUPPRESS)
    parser.add_argument("--output", help=argparse.SUPPRESS)
    parser.add_argument("--bundle", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help="选择数据保存目录")
    args = parser.parse_args()
    if args.parse_worker or args.statistics_worker:
        if not (args.input and args.output):
            parser.error("回放工作进程需要提供输入和输出")
        if args.statistics_worker:
            return statistics_worker(args)
        return parse_worker(args)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    from .desktop.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("WOWSTraining")
    app.setApplicationDisplayName("WOWS 港口开发部")
    try:
        window = MainWindow(args.data_dir or default_data_directory(), args.bundle)
    except Exception as exc:
        QMessageBox.critical(None, "无法启动港口开发部", f"{exc}\n\n请完整解压程序目录，保留 _internal 内资源。")
        return 2
    window.show()
    QTimer.singleShot(2000, window, window.start_update_check)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
