"""Restore the runtime assets needed to build the published desktop source."""
import argparse
from hashlib import file_digest
from pathlib import Path, PurePosixPath
import re
from shutil import copyfileobj
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
ASSETS = {"runtime_bundle", "career_data", "decision_models", "native_ui", "naval_assets",
          "ordinary_profiles", "presentation_fonts"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".dpapi", ".wowsreplay", ".wowsbattle"}


def restore(archive, sha256, root=ROOT):
    if not re.fullmatch(r"[a-fA-F0-9]{64}", sha256):
        raise ValueError("请输入发行页中的 64 位 SHA-256 校验值。")
    with archive.open("rb") as stream:
        if file_digest(stream, "sha256").hexdigest() != sha256.lower():
            raise ValueError("便携包的 SHA-256 校验失败。")
    root = root.resolve()
    with ZipFile(archive) as package:
        targets = {}
        for entry in package.infolist():
            path = PurePosixPath(entry.filename)
            if entry.is_dir() or path.parts[:2] != ("WOWSTraining", "_internal") or len(path.parts) < 4:
                continue
            relative = path.parts[2:]
            if relative[0] not in ASSETS:
                continue
            target = root.joinpath(*relative)
            if (".." in relative or "\\" in entry.filename or any(":" in part for part in relative)
                    or not target.resolve().is_relative_to(root) or target in targets
                    or target.suffix.lower() in FORBIDDEN_SUFFIXES):
                raise ValueError("便携包包含不适用的运行资源路径。")
            targets[target] = entry
        if set(path.relative_to(root).parts[0] for path in targets) != ASSETS:
            raise ValueError("便携包缺少运行资源。")
        for target, entry in targets.items():
            if target.is_file():
                with target.open("rb") as current, package.open(entry) as incoming:
                    if file_digest(current, "sha256").digest() != file_digest(incoming, "sha256").digest():
                        raise ValueError(f"已有资源与发行版不同，请在干净的源码目录还原。{target.relative_to(root)}")
        for target, entry in targets.items():
            if target.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(entry) as incoming, target.open("xb") as destination:
                copyfileobj(incoming, destination)
    return len(targets)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        count = restore(args.archive, args.sha256)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(f"运行资源已核对并还原，共 {count} 个文件。")
