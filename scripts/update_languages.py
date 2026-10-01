import sys
from pathlib import Path
import sqlite3
import json

# Setup root path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.extraction.domain_cleaner import standardize_language
from src.reporting.excel_exporter import ExcelExporter

def main():
    db_path = BASE_DIR / "data" / "cache" / "pipeline_state.db"
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT file_path, is_duplicate, metadata_json FROM processed_files WHERE status = 'COMPLETED'")
    rows = c.fetchall()

    updated_data = []
    print("--- CHUẨN HÓA TRƯỜNG LANGUAGE VỀ TIẾNG ANH ---")
    for file_path, is_dup, meta_json in rows:
        meta = json.loads(meta_json) if meta_json else {}
        old_lang = meta.get("language", "")
        new_lang = standardize_language(old_lang)
        meta["language"] = new_lang
        print(f"  {Path(file_path).name}: '{old_lang}' -> '{new_lang}'")
        
        c.execute("UPDATE processed_files SET metadata_json = ? WHERE file_path = ?", (json.dumps(meta, ensure_ascii=False), file_path))
        
        updated_data.append({
            "file_name": Path(file_path).name,
            "file_path": file_path,
            "is_duplicate": bool(is_dup),
            "metadata": meta
        })

    conn.commit()
    conn.close()

    report_path = BASE_DIR / "data" / "reports" / "tong_hop_tai_lieu.xlsx"
    out_file = ExcelExporter.export(updated_data, report_path)
    print(f"\n✅ Đã cập nhật xong tệp Excel: {out_file}")

if __name__ == "__main__":
    main()
