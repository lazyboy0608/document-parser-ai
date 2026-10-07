from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Ollama
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    OLLAMA_MODEL: str = "qwen2.5:3b"
    VLM_MODEL: str = "qwen2.5vl:3b"
    ENABLE_HYBRID_VLM: bool = True
    OLLAMA_TIMEOUT: int = 180

    # Ollama Performance Tuning (Tối ưu cho CPU Core i3 4 luồng)
    OLLAMA_NUM_THREAD: int = 4
    OLLAMA_NUM_CTX: int = 2048
    OLLAMA_NUM_PREDICT: int = 256

    # OCR Model Options (Ưu tiên Mobile cho môi trường CPU)
    OCR_DET_MODEL: str = "PP-OCRv5_mobile_det"

    # Paths
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    INPUT_DIR: Path = BASE_DIR / "data" / "input_documents"
    OUTPUT_DIR: Path = BASE_DIR / "data" / "output_documents"
    REPORTS_DIR: Path = BASE_DIR / "data" / "reports"
    CACHE_DIR: Path = BASE_DIR / "data" / "cache"

    # Processing Options
    BATCH_SIZE: int = 10
    SIMILARITY_THRESHOLD: float = 0.85
    OCR_LANG: str = "vie+eng"
    HEAD_PAGES: int = 5
    TAIL_PAGES: int = 1
    BACKWARD_SCAN_MAX_PAGES: int = 6

    # Hot Folder / Watcher Options
    WATCH_STABLE_SECONDS: float = 3.0
    WATCH_TIMEOUT_SECONDS: float = 300.0

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

# Ensure directories exist
for folder in [settings.INPUT_DIR, settings.OUTPUT_DIR, settings.REPORTS_DIR, settings.CACHE_DIR]:
    folder.mkdir(parents=True, exist_ok=True)
