import sys
import io
from pathlib import Path

# Đảm bảo UTF-8 cho Windows console
if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.ingestion.pdf_parser import extract_text_from_pdf


def create_sample_text_pdf(pdf_path: Path):
    """Tạo file PDF dạng số có sẵn text layer chuẩn."""
    doc = fitz.open()
    page = doc.new_page()
    text = (
        "BO THONG TIN VA TRUYEN THONG\n"
        "BAO CAO TONG KET DU AN XU LY DU LIEU NLP NAM 2026\n"
        "Ngay ban hanh: 25/09/2026\n"
        "Co quan chu quan: Vien Cong nghe Thong tin"
    )
    page.insert_text((50, 72), text, fontsize=14)
    doc.save(str(pdf_path))
    doc.close()


def create_sample_scanned_pdf(pdf_path: Path):
    """
    Tạo file PDF mô phỏng tài liệu scan (chỉ là ảnh, hoàn toàn không có text layer).
    """
    width, height = 800, 1000
    image = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)
    
    # Nội dung tài liệu scan
    lines = [
        "BO GIAO DUC VA DAO TAO",
        "QUYET DINH VE VIEC BAN HANH QUY CHE MOI",
        "Nha xuat ban Giao Duc Viet Nam",
        "Ngay 25 thang 09 nam 2026"
    ]
    y = 80
    for line in lines:
        draw.text((60, y), line, fill=(0, 0, 0))
        y += 50

    temp_img_path = pdf_path.parent / "temp_scanned_page.png"
    image.save(temp_img_path)

    # Chèn ảnh vào PDF dưới dạng trang scan thuần túy (không có text)
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.insert_image(page.rect, filename=str(temp_img_path))
    doc.save(str(pdf_path))
    doc.close()

    if temp_img_path.exists():
        temp_img_path.unlink()


def main():
    test_dir = BASE_DIR / "data" / "cache" / "test_docs"
    test_dir.mkdir(parents=True, exist_ok=True)

    text_pdf_path = test_dir / "sample_digital.pdf"
    scanned_pdf_path = test_dir / "sample_scanned.pdf"

    print(">>> 1. Đang tạo các tệp PDF thử nghiệm (PDF số & PDF scan)...")
    create_sample_text_pdf(text_pdf_path)
    create_sample_scanned_pdf(scanned_pdf_path)

    print("\n>>> 2. Thử nghiệm đọc PDF kỹ thuật số (có text layer)...")
    text1, is_scanned1 = extract_text_from_pdf(text_pdf_path)
    print(f"[Kết quả] is_scanned: {is_scanned1}")
    print(f"[Nội dung trích xuất]:\n{text1.strip()}")
    assert not is_scanned1, "Lỗi: PDF số bị nhận nhầm thành PDF scan!"
    assert "BAO CAO TONG KET" in text1, "Lỗi: Không trích xuất được text!"

    print("\n>>> 3. Thử nghiệm đọc PDF dạng SCAN (chạy qua PaddleOCR)...")
    text2, is_scanned2 = extract_text_from_pdf(scanned_pdf_path, ocr_lang="en")
    print(f"[Kết quả] is_scanned: {is_scanned2}")
    print(f"[Nội dung OCR trích xuất]:\n{text2.strip()}")
    assert is_scanned2, "Lỗi: PDF scan không được phát hiện đúng!"
    assert len(text2.strip()) > 0, "Lỗi: OCR không bóc tách được chữ nào!"

    print("\n✅ KIỂM THỬ THÀNH CÔNG: Module phân biệt PDF thường và PDF Scan hoạt động hoàn hảo!")


if __name__ == "__main__":
    main()
