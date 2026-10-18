import logging
import sys
from rich.console import Console
from rich.logging import RichHandler

console = Console()

def setup_logger(name: str = "kotak_trader", level: int = logging.INFO) -> logging.Logger:
    """Configures and returns a rich console logger."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(level)
        handler = RichHandler(
            console=console,
            rich_tracebacks=True,
            show_time=True,
            show_path=False,
            markup=True
        )
        formatter = logging.Formatter("%(message)s", datefmt="[%X]")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False
    return logger

logger = setup_logger()
