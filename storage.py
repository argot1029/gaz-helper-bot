"""Безопасная работа с JSON-файлами в постоянном хранилище."""
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("storage")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Не удалось прочитать %s", path)
    return default


def write_json(path: Path, data: Any) -> None:
    """Атомарная запись: сначала во временный файл, потом замена."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        log.exception("Не удалось сохранить %s", path)
