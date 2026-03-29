"""
Prometheus — Logging
Централизованная настройка логирования.
Вызывается один раз в main.py до всех импортов.
"""
from __future__ import annotations
import logging
import logging.handlers
import os
from pathlib import Path


def setup(logs_dir: str = "/app/logs", level: str = "INFO") -> None:
    """Настроить logging для всего приложения."""
    Path(logs_dir).mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(fmt)

    # File handler с ротацией (10 MB, 5 файлов)
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(logs_dir, "prometheus.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(console)
    root.addHandler(file_handler)

    # Заглушить шумные библиотеки
    for noisy in ("urllib3", "requests", "httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
