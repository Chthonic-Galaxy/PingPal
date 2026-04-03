import logging
import logging.config


def setup_logging(level: str | int) -> None:
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)-8s %(name)-15s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            }
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "class": "logging.FileHandler",
                "formatter": "default",
                "filename": "/tmp/pingpal.log",
                "mode": "w",
            },
        },
        "root": {"level": f"{level}", "handlers": ["stdout", "file"]},
    }
    logging.config.dictConfig(config)
