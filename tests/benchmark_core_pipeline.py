import sys
import time
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline import DocumentPipeline
from src.ingestion.document_loader import DocumentLoader
from src.deduplication.simhash_dedup import SimHashDeduplicator
from src.extraction.llm_extractor import LLMExtractor
from src.extraction.confidence_scorer import evaluate_metadata_confidence
from src.extraction.domain_cleaner import clean_metadata_fields


def run_benchmark():
    test_files = [
        Path(r"D:\outputNLP\20261002\Well_Proposal\Report-sample.pdf"),
        Path(r"D:\outputNLP\20261001\Geological_Report\DOAN36_007.pdf"),
        Path(r"D:\outputNLP\20261001\Final_Well_Report\BC_VSP_1805.doc"),
    ]

    # Find the docx file in 20261015
    docx_dir = Path(r"D:\outputNLP\20261015\Seismic_Report")
    if docx_dir.exists():
        for f in docx_dir.glob("*.docx"):
            test_files.append(f)
            break

    print("=" * 80)
    print("🚀 BẮT ĐẦU KIỂM THỬ CHỨC NĂNG CHÍNH CỦA HỆ THỐNG")
    print(f"Tổng số tệp mẫu kiểm thử: {len(test_files)}")
    print("=" * 80)

    results = []

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_output = Path(temp_dir) / "bench_output"
        temp_output.mkdir(parents=True)
        temp_db = Path(temp_dir) / "bench_state.db"

        pipeline = DocumentPipeline(
            output_dir=temp_output,
            mode="copy"  # Use copy to preserve original files
        )
        # Override state tracker db to temp
        pipeline.state_tracker.db_path = temp_db
        pipeline.state_tracker._init_db()

        for idx, file_path in enumerate(test_files, 1):
            if not file_path.exists():
                print(f"⚠️ Không tìm thấy tệp {file_path}, bỏ qua.")
                continue

            print(f"\n[{idx}/{len(test_files)}] Đang đo lường: {file_path.name} ({file_path.stat().st_size / 1024:.1f} KB)")
            t_start = time.perf_counter()

            # 1. Đọc và nạp tài liệu (Loader & OCR)
            t_load_start = time.perf_counter()
            doc_data = DocumentLoader.load_document(file_path)
            t_load = time.perf_counter() - t_load_start
            raw_text = doc_data.get("text", "")
            is_scanned = bool(doc_data.get("is_scanned", False))
            text_len = len(raw_text.strip())

            # 2. Xử lý toàn diện qua Pipeline
            t_pipe_start = time.perf_counter()
            item = pipeline.process_single_file(file_path, date_str="20261019")
            t_total = time.perf_counter() - t_start

            if item is None:
                print(f"❌ Xử lý thất bại cho {file_path.name}")
                continue

            meta = item.get("metadata", {})
            conf_score = float(meta.get("confidence_score", 0.0) or 0.0)
            low_fields = meta.get("low_confidence_fields", [])
            doc_type = meta.get("document_type", "Chưa rõ")

            result_entry = {
                "file_name": file_path.name,
                "file_size_kb": f"{file_path.stat().st_size / 1024:.1f}",
                "load_time": f"{t_load:.2f}s",
                "total_time": f"{t_total:.2f}s",
                "text_length": text_len,
                "is_scanned": "Scan" if is_scanned else "Digital",
                "doc_type": doc_type,
                "confidence_score": f"{conf_score:.1f}%",
                "low_confidence_fields": ", ".join(low_fields) if low_fields else "None",
                "title": (meta.get("title") or "")[:40],
                "originator": meta.get("originator") or "N/A",
                "contract_name": meta.get("contract_name") or "N/A",
                "well_link": meta.get("well_link") or "N/A",
                "public_date": meta.get("public_date") or "N/A",
            }
            results.append(result_entry)

            print(f"   ⏱️ Thời gian đọc tệp (Loader): {t_load:.2f}s")
            print(f"   ⏱️ Tổng thời gian xử lý:       {t_total:.2f}s")
            print(f"   📊 Loại tài liệu:              {result_entry['is_scanned']} | Độ dài text: {text_len} ký tự")
            print(f"   🏷️ Thể loại (Document Type):   {doc_type}")
            print(f"   🎯 Độ tin cậy (Confidence):   {conf_score:.1f}%")
            print(f"   ⚠️ Trường cần chú ý:          {result_entry['low_confidence_fields']}")
            print(f"   📝 Tiêu đề:                   {result_entry['title']}")
            print(f"   🏢 Đơn vị ban hành:           {result_entry['originator']}")
            print(f"   🛢️ Giếng / Well:              {result_entry['well_link']}")

    print("\n" + "=" * 90)
    print("📈 BẢNG TỔNG HỢP KIỂM THỬ HIỆU NĂNG & ĐỘ TIN CẬY HỆ THỐNG")
    print("=" * 90)
    fmt = "{:<30} | {:<10} | {:<10} | {:<10} | {:<16} | {:<12}"
    print(fmt.format("Tên tệp", "Load Time", "Tổng Time", "Loại", "Document Type", "Độ tin cậy"))
    print("-" * 90)
    for r in results:
        print(fmt.format(
            r["file_name"][:30],
            r["load_time"],
            r["total_time"],
            r["is_scanned"],
            r["doc_type"][:16],
            r["confidence_score"]
        ))
    print("=" * 90)


if __name__ == "__main__":
    run_benchmark()
