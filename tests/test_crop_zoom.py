import sys
from pathlib import Path
import base64
import io
import json
import ollama
from PIL import Image, ImageOps, ImageEnhance

# Setup root path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")

def prepare_b64(img_input, max_dim=1024):
    if isinstance(img_input, (str, Path)):
        im = Image.open(str(img_input)).convert("RGB")
    else:
        im = img_input.convert("RGB")
    w, h = im.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        im = im.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def create_high_res_roi_crop(img_path: Path):
    im = Image.open(str(img_path))
    w, h = im.size
    roi_box = (int(0.35 * w), int(0.45 * h), int(0.98 * w), int(0.88 * h))
    crop = im.crop(roi_box)
    crop_gray = ImageOps.autocontrast(crop.convert("L"), cutoff=2)
    crop_sharp = ImageEnhance.Sharpness(crop_gray).enhance(1.5).convert("RGB")
    return prepare_b64(crop_sharp, max_dim=1400)

def main():
    imgs = [
        prepare_b64("data/cache/test_render/page_1.png", max_dim=1024),
        prepare_b64("data/cache/test_render/page_2.png", max_dim=1024),
        prepare_b64("data/cache/test_render/page_3.png", max_dim=1024),
        create_high_res_roi_crop(Path("data/cache/test_render/page_2.png"))
    ]

    client = ollama.Client()

    prompt = """Bạn là chuyên gia thẩm định và trích xuất dữ liệu tài liệu báo cáo kỹ thuật địa chất Dầu khí.
Dưới đây là hình ảnh các trang đầu của tài liệu scan:
- Trang 1: Bìa
- Trang 2: Trang phê duyệt và tác giả
- Trang 3: Danh mục phụ bản
- Ảnh 4: Ảnh zoom cận cảnh độ nét cao (Crop-Zoom) góc Tác giả và Chủ biên ở trang 2.

Nhiệm vụ: Hãy quan sát kỹ toàn bộ hình ảnh, đặc biệt quan sát ảnh cận cảnh số 4 để đọc thật chính xác họ tên từng tác giả và chủ biên, trích xuất 10 trường JSON theo cấu trúc sau:
{
  "well_link": "Tên hoặc số hiệu giếng khoan / lỗ khoan (ví dụ: LK-101)",
  "contract_name": "Tên đơn vị / liên đoàn / đề án (ví dụ: LIÊN ĐOÀN DẦU KHÍ 36)",
  "title": "Tiêu đề đầy đủ nhất của báo cáo (ví dụ: KẾT QUẢ NGHIÊN CỨU ĐỊA CHẤT LỖ KHOAN 101 KIẾN XƯƠNG THÁI BÌNH)",
  "public_date": "Năm hoặc ngày tháng (ví dụ: 1975)",
  "originator": "Cơ quan ban hành (ví dụ: TỔNG CỤC DẦU MỎ VÀ KHÍ ĐỐT VIỆT NAM)",
  "contractor": "Nhà thầu thi công (nếu có, không có ghi N/A)",
  "author": "Danh sách họ tên chính xác của các tác giả và chủ biên ngăn cách bằng dấu chấm phẩy (;). Quan sát kỹ ảnh cận cảnh số 4 để nhận diện đúng từng chữ cái và dấu thanh tiếng Việt, không tự suy đoán nếu nét chữ thể hiện khác.",
  "language": "Vietnamese, English, hoặc Russian (quy chuẩn bằng tiếng Anh)",
  "encl": "Số lượng phụ bản (ví dụ: 8)",
  "encl_name": "Tên các phụ bản ngăn cách bằng dấu chấm phẩy (;)"
}

Chỉ trả về DUY NHẤT một chuỗi JSON hợp lệ."""

    print("Sending images (including High-Res Author Crop) to Qwen2.5-VL...")
    resp = client.chat(
        model="qwen2.5vl:3b",
        messages=[{"role": "user", "content": prompt, "images": imgs}],
        format="json",
        options={"num_ctx": 16384, "temperature": 0.0}
    )
    print("\n=== RAW RESPONSE ===")
    content = resp.get("message", {}).get("content", "")
    print(content)

if __name__ == "__main__":
    main()
