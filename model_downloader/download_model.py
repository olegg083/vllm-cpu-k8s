#!/usr/bin/env python3
"""Скачивание весов модели с Hugging Face в примонтированный PVC.
"""

import json
import os
import sys
import time
from pathlib import Path

MARKER_NAME = ".download_complete"
MAX_ATTEMPTS = 5


def log(msg: str) -> None:
    print(f"[download_model] {msg}", flush=True)


def model_files_present(model_dir: Path) -> bool:
    return (model_dir / "config.json").is_file() and (
        any(model_dir.glob("*.safetensors")) or any(model_dir.glob("pytorch_model*.bin"))
    )


def read_marker(marker: Path) -> dict | None:
    if not marker.is_file():
        return None
    try:
        return json.loads(marker.read_text())
    except (OSError, ValueError):
        return None


def main() -> int:
    model_id = os.environ.get("MODEL_ID", "").strip()
    if not model_id:
        log("ERROR: переменная MODEL_ID не задана")
        return 2

    revision = os.environ.get("MODEL_REVISION", "").strip() or "main"
    model_dir = Path(
        os.environ.get("MODEL_DIR") or f"/models/{model_id.replace('/', '--')}"
    )
    token = os.environ.get("HF_TOKEN", "").strip() or None

    expected = {"model_id": model_id, "revision": revision}
    marker = model_dir / MARKER_NAME
    current = read_marker(marker)

    if current is not None and model_files_present(model_dir):
        if current == expected:
            log(f"Модель {model_id}@{revision} уже в {model_dir}, сеть не нужна.")
            return 0
        log(
            f"ERROR: в {model_dir} лежит другая модель/ревизия: {current}, "
            f"а запрошено {expected}. Укажите другой MODEL_DIR или очистите PVC."
        )
        return 3

    # Импорт после быстрого пути
    from huggingface_hub import snapshot_download

    model_dir.mkdir(parents=True, exist_ok=True)
    log(f"Скачиваю {model_id}@{revision} -> {model_dir} (токен: {'есть' if token else 'нет'})")

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            snapshot_download(
                repo_id=model_id,
                revision=None if revision == "main" else revision,
                local_dir=str(model_dir),
                token=token,
                max_workers=4,
                ignore_patterns=["*.msgpack", "*.h5", "*.onnx", "*.gguf", "coreml/*", "original/*"],
            )
            if not model_files_present(model_dir):
                raise RuntimeError("после загрузки нет config.json или файлов весов")
            marker.write_text(json.dumps(expected))
            log("Готово: модель скачана, маркер записан.")
            return 0
        except Exception as exc:
            last_error = exc
            log(f"Попытка {attempt}/{MAX_ATTEMPTS} не удалась: {type(exc).__name__}: {exc}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(2**attempt, 30))

    log(
        f"ERROR: не удалось скачать модель: {type(last_error).__name__}: {last_error}. "
        "Для gated-моделей проверьте HF_TOKEN и принятие лицензии на huggingface.co."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
