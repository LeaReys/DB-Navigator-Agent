"""
Единая настройка логирования для всего приложения.
"""

from __future__ import annotations

import logging

# Логгеры-родители для всего нашего кода. Все модули приложения - их потомки.
APP_LOGGERS = ("core", "api")

_LOG_FORMAT = "%(levelname)s: %(message)s"

# Флаг, чтобы повторные вызовы не плодили хендлеры и не сбрасывали уровни.
_configured = False


def setup_logging(
    app_level: int = logging.INFO,
    root_level: int = logging.WARNING,
    force: bool = False,
) -> None:
    """
    Настраивает логирование приложения.
        - force:мпересоздать конфигурацию, даже если уже настроено.
    """
    global _configured
    if _configured and not force:
        return

    logging.basicConfig(level=root_level, format=_LOG_FORMAT, force=force)
    for name in APP_LOGGERS:
        logging.getLogger(name).setLevel(app_level)

    _configured = True
