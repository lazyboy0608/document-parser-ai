from loguru import logger
import sys
from pathlib import Path

# Configure loguru format and file rotation
LOG_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    level="INFO"
)
logger.add(
    LOG_DIR / "app_{time:YYYY-MM-DD}.log",
    rotation="10 MB",
    retention="7 days",
    level="DEBUG",
    encoding="utf-8"
)

__all__ = ["logger"]
