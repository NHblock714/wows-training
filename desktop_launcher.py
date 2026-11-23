from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from wows_training.app import default_data_directory, main

if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        log_root = default_data_directory()
        if "--data-dir" in sys.argv:
            index = sys.argv.index("--data-dir") + 1
            if index < len(sys.argv):
                log_root = Path(sys.argv[index])
        log_root.mkdir(parents=True, exist_ok=True)
        log = log_root / "startup-error.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        if sys.stderr:
            traceback.print_exc()
        if "--parse-worker" not in sys.argv and "--statistics-worker" not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, f"程序启动失败，请保留完整程序目录。诊断记录：\n{log}", "WOWS 港口开发部", 0x10
            )
        code = 2
    raise SystemExit(code)
