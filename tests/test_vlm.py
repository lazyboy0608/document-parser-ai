import sys
from pathlib import Path
import json

# Setup paths
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.extraction.vlm_extractor import VLMExtractor

def main():
    images = [
        Path("data/cache/test_render/page_1.png"),
        Path("data/cache/test_render/page_2.png"),
        Path("data/cache/test_render/page_3.png")
    ]
    
    vlm = VLMExtractor()
    print("Testing VLMExtractor with Qwen2.5-VL...")
    metadata = vlm.extract_from_images(images)
    print("\n=== VLM EXTRACTION SUCCESS ===")
    print(json.dumps(metadata.model_dump(), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
