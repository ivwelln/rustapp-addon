import logging
import os


def configure_console_logging() -> None:
    level_name = os.getenv("APP_LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelNamesMapping().get(level_name, logging.INFO)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("app").setLevel(level)
    logging.getLogger("__main__").setLevel(level)
    if level_name not in logging.getLevelNamesMapping():
        logging.getLogger(__name__).warning("Неверный APP_LOG_LEVEL, используется INFO")
