import base64
import io
import json
from pathlib import Path
from typing import List, Union, Optional, Dict, Any
import ollama
from PIL import Image, ImageOps, ImageEnhance

from config.settings import settings
from .schemas import DocumentMetadata
from .domain_cleaner import clean_metadata_fields
from src.utils.logger import logger


VLM_MAIN_PROMPT = """Bạn là chuyên gia thẩm định và trích xuất dữ liệu tài liệu báo cáo kỹ thuật địa chất, khoan, khai thác Dầu khí.
Dưới đây là hình ảnh các trang đầu (trang bìa, trang phê duyệt có con dấu/chữ ký) của tài liệu.

Nhiệm vụ: Hãy quan sát kỹ hình ảnh và trích xuất các trường dữ liệu JSON theo định dạng sau:
{
  "well_link": "Tên/số hiệu giếng khoan hoặc số hiệu Lô/Block dầu khí nếu là báo cáo tổng quan Lô/Block (ví dụ: <SỐ_HIỆU_GIẾNG>, <TÊN_BLOCK>). Nếu không có giếng hoặc Lô thì ghi 'N/A'",
  "contract_name": "Tên hợp đồng, đề án, hoặc tên công ty/đơn vị chủ trì ký kết hợp đồng PSC (ví dụ: <TÊN_HỢP_ĐỒNG_HOẶC_CÔNG_TY>). Nếu không có ghi 'N/A'",
  "title": "Tiêu đề chính xác và đầy đủ nhất của báo cáo ghi trên trang bìa. Tuyệt đối không ghi sai chính tả.",
  "public_date": "Ngày tháng hoặc năm lập báo cáo (ví dụ: <NGÀY_THÁNG_NĂM>). Nếu không rõ ghi 'Chưa rõ'",
  "originator": "Đơn vị ban hành, công ty chủ sở hữu hoặc công ty lập báo cáo (ví dụ: <TÊN_CÔNG_TY_BAN_HÀNH>). Chỉ lấy đơn vị thực tế có ghi trong tài liệu, TUYỆT ĐỐI KHÔNG tự bịa đặt đơn vị khác nếu tài liệu không nhắc tới.",
  "contractor": "Nhà thầu thi công / đơn vị dịch vụ kỹ thuật / đơn vị thực hiện (nếu có ghi rõ). Nếu không có ghi 'N/A'",
  "author": "Họ và tên tác giả cá nhân hoặc nhóm biên soạn báo cáo. Nếu trên trang bìa/phê duyệt KHÔNG có tên tác giả cá nhân thì ghi 'None' hoặc 'N/A'. TUYỆT ĐỐI KHÔNG tự gán tên tác giả của tài liệu khác vào.",
  "language": "Tên ngôn ngữ chính của tài liệu viết bằng TIẾNG ANH chuẩn quốc tế (ví dụ: 'English', 'Vietnamese', 'Russian', 'French'...)"
}

LƯU Ý QUAN TRỌNG:
- Trả về DUY NHẤT một chuỗi JSON hợp lệ.
- Không thêm bất kỳ lời dẫn giải hay markdown thừa nào ngoài JSON.
- Tuyệt đối trung thực với nội dung hiển thị trong hình ảnh, không suy diễn thông tin từ tài liệu khác.
"""

VLM_ENCL_PROMPT = """Hình ảnh này là trang danh mục 'PHỤ BẢN KÈM THEO' (Enclosures / Danh mục bản vẽ đính kèm) của báo cáo dầu khí.
Hãy quan sát kỹ danh sách ở cột 'Tên phụ bản' hoặc phần liệt kê các Enclosures:
1. Đếm tổng số lượng phụ bản thực tế (ví dụ: '3', '8', '13'). Nếu không có phụ bản nào ghi 'No'.
2. Liệt kê đầy đủ tên từng phụ bản theo thứ tự các mục được đánh số (từ mục 1 đến hết), ngăn cách nhau bằng dấu chấm phẩy (;). 
   - Chỉ lấy phần tên nội dung của phụ bản (ví dụ: 'Regional Geological Map', 'Generalised Stratigraphy', 'Time contour map of Red horizon'...).
   - Không lấy số hiệu bản vẽ hay tỉ lệ bản vẽ vào trong tên phụ bản.

Trả về DUY NHẤT định dạng JSON sau:
{
  "encl": "Số lượng phụ bản",
  "encl_name": "Tên phụ bản 1; Tên phụ bản 2; Tên phụ bản 3; ..."
}
"""

VLM_AUTHOR_PROMPT = """Hình ảnh là ảnh chụp vùng chữ ký, phê duyệt hoặc người thực hiện báo cáo.
Hãy quan sát kỹ và đọc chính xác họ và tên của các tác giả, người lập báo cáo, người kiểm tra hoặc chủ biên trong ảnh:
- Trả về danh sách họ tên các tác giả ngăn cách bởi dấu chấm phẩy (;).
- Nếu trong ảnh KHÔNG có tên tác giả cá nhân hoặc chỉ là chữ ký không rõ tên / con dấu cơ quan thì trả về: None
- TUYỆT ĐỐI KHÔNG tự suy diễn hoặc bịa đặt tên người nếu không thấy chữ trong ảnh.
Chỉ trả về chuỗi tên tác giả (hoặc None), không kèm bất kỳ lời dẫn giải nào khác.
"""


class VLMExtractor:
    """
    Module trích xuất siêu trực quan Vision Language Model (VLM) cho tài liệu scan phức tạp.
    Sử dụng Qwen2.5-VL thông qua kiến trúc trích xuất đa tầng tập trung (Modular Targeted Extraction):
    1. Pass 1: Trích xuất metadata tổng thể tài liệu (Well, Contract, Title, Date, Originator, Contractor).
    2. Pass 2: Trích xuất danh mục Phụ bản (Enclosures) từ trang phụ bản chuyên biệt.
    3. Pass 3: Trích xuất danh sách Tác giả từ ảnh Crop-Zoom cận cảnh độ phân giải cao (RoI 300 DPI).
    """
    def __init__(self, model_name: str = settings.VLM_MODEL, base_url: str = settings.OLLAMA_BASE_URL):
        self.model_name = model_name
        self.client = ollama.Client(host=base_url)

    def prepare_image(self, img_input: Union[str, Path, Image.Image], max_dim: int = 1024) -> str:
        """
        Nén ảnh trang tài liệu về kích thước tối ưu để giảm token vision,
        đảm bảo VLM đọc rõ nét từng con chữ mà không vượt ngưỡng n_ctx.
        """
        if isinstance(img_input, Image.Image):
            im = img_input.convert("RGB")
        else:
            im = Image.open(str(img_input)).convert("RGB")

        w, h = im.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            im = im.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    def create_high_res_roi_crop(self, img_path: Union[str, Path]) -> Optional[str]:
        """
        Tự động cắt vùng quan tâm (Region-of-Interest) chứa Tác giả, Chủ biên và Chữ ký
        ở góc dưới trang phê duyệt với độ phân giải cao (High-Res Crop-Zoom)
        kèm bộ lọc tăng cường độ tương phản ký tự máy đánh chữ / vết mực mờ.
        """
        try:
            im = Image.open(str(img_path))
            w, h = im.size
            # Vùng tác giả & chủ biên: x: 42%-96%, y: 54%-86%
            roi_box = (int(0.42 * w), int(0.54 * h), int(0.96 * w), int(0.86 * h))
            crop = im.crop(roi_box)
            if crop.size[0] > 100 and crop.size[1] > 100:
                crop_gray = ImageOps.autocontrast(crop.convert("L"), cutoff=2)
                crop_sharp = ImageEnhance.Sharpness(crop_gray).enhance(1.5).convert("RGB")
                return self.prepare_image(crop_sharp, max_dim=1400)
        except Exception as e:
            logger.warning(f"Không thể tạo crop RoI cho ảnh '{img_path}': {e}")
        return None

    def extract_from_images(self, image_paths: List[Union[str, Path]]) -> DocumentMetadata:
        """
        Thực hiện trích xuất dữ liệu đa tầng tập trung từ danh sách các ảnh trang tài liệu.
        """
        if not image_paths:
            raise ValueError("Danh sách ảnh rỗng, không thể gọi VLM.")

        valid_paths = [Path(p) for p in image_paths if Path(p).exists()]
        if not valid_paths:
            raise FileNotFoundError("Không tìm thấy tệp ảnh nào hợp lệ để gửi tới VLM.")

        # Phân loại các ảnh: Toàn trang vs Ảnh RoI crop
        full_page_paths = sorted([p for p in valid_paths if "_roi_" not in p.name and "_crop" not in p.name])
        roi_paths = [p for p in valid_paths if "_roi_" in p.name or "_crop" in p.name]

        metadata_dict: Dict[str, Any] = {
            "well_link": "N/A",
            "contract_name": "N/A",
            "title": "N/A",
            "public_date": "N/A",
            "originator": "N/A",
            "contractor": "N/A",
            "author": "N/A",
            "language": "Vietnamese",
            "encl": "No",
            "encl_name": "None"
        }

        # -------------------------------------------------------------
        # 1. PASS 1: Trích xuất Metadata tổng thể (Trang bìa & Phê duyệt)
        # -------------------------------------------------------------
        cover_pages = full_page_paths[:2] if len(full_page_paths) >= 2 else full_page_paths
        encoded_cover_imgs = [self.prepare_image(p, max_dim=1024) for p in cover_pages]
        logger.info(f"-> [VLM - Pass 1] Trích xuất thông tin chung từ {len(encoded_cover_imgs)} trang đầu...")
        try:
            resp_main = self.client.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": VLM_MAIN_PROMPT, "images": encoded_cover_imgs}],
                format="json",
                options={"num_ctx": 16384, "temperature": 0.0}
            )
            raw_main = resp_main.get("message", {}).get("content", "").strip()
            if raw_main.startswith("```"):
                raw_main = raw_main.strip("`").removeprefix("json").strip()
            main_data = json.loads(raw_main)
            metadata_dict.update(main_data)
        except Exception as e:
            logger.warning(f"Lỗi khi trích xuất Pass 1 (Thông tin chung): {e}")

        # -------------------------------------------------------------
        # 2. PASS 2: Trích xuất Trang phụ bản (Enclosures)
        # -------------------------------------------------------------
        # Tìm trang phụ bản thông minh trong các trang đầu (từ trang 2 trở đi)
        encl_page = None
        if len(full_page_paths) >= 2:
            # Ưu tiên trang có tên hoặc nội dung phụ bản nếu có
            for p in full_page_paths[1:]:
                # Nếu có từ khóa hoặc là trang thứ 3, 4
                if "encl" in p.name.lower() or "phu_ban" in p.name.lower():
                    encl_page = p
                    break
            # Nếu chưa xác định, thử trang 3 hoặc 4 hoặc trang cuối trong các trang đầu
            if not encl_page:
                if len(full_page_paths) >= 4:
                    encl_page = full_page_paths[3]
                elif len(full_page_paths) >= 3:
                    encl_page = full_page_paths[2]
        
        if encl_page and encl_page.exists():
            logger.info(f"-> [VLM - Pass 2] Trích xuất danh mục phụ bản chuyên biệt từ trang: {encl_page.name}...")
            encoded_encl_img = [self.prepare_image(encl_page, max_dim=1200)]
            try:
                resp_encl = self.client.chat(
                    model=self.model_name,
                    messages=[{"role": "user", "content": VLM_ENCL_PROMPT, "images": encoded_encl_img}],
                    format="json",
                    options={"num_ctx": 16384, "temperature": 0.0}
                )
                raw_encl = resp_encl.get("message", {}).get("content", "").strip()
                if raw_encl.startswith("```"):
                    raw_encl = raw_encl.strip("`").removeprefix("json").strip()
                encl_data = json.loads(raw_encl)
                metadata_dict["encl"] = encl_data.get("encl", metadata_dict.get("encl", "No"))
                metadata_dict["encl_name"] = encl_data.get("encl_name", metadata_dict.get("encl_name", "None"))
            except Exception as e:
                logger.warning(f"Lỗi khi trích xuất Pass 2 (Phụ bản): {e}")

        # -------------------------------------------------------------
        # 3. PASS 3: Trích xuất Tác giả qua Crop-Zoom cận cảnh (Chỉ khi cần thiết)
        # -------------------------------------------------------------
        current_author = str(metadata_dict.get("author", "") or "").strip()
        current_lang = str(metadata_dict.get("language", "") or "").strip().lower()
        needs_author_zoom = current_author in {"", "N/A", "Chưa rõ", "Unknown"} and current_author != "None"

        roi_b64 = None
        if roi_paths and needs_author_zoom:
            roi_b64 = self.prepare_image(roi_paths[0], max_dim=1400)
            logger.info(f"-> [VLM - Pass 3] Sử dụng ảnh Crop-Zoom RoI 300 DPI: {roi_paths[0].name}")
        elif len(full_page_paths) >= 2 and needs_author_zoom and current_lang in {"vietnamese", "vi"}:
            roi_b64 = self.create_high_res_roi_crop(full_page_paths[1])
            if roi_b64:
                logger.info(f"-> [VLM - Pass 3] Tự động tạo ảnh Crop-Zoom RoI từ: {full_page_paths[1].name}")

        if roi_b64:
            try:
                resp_author = self.client.chat(
                    model=self.model_name,
                    messages=[{"role": "user", "content": VLM_AUTHOR_PROMPT, "images": [roi_b64]}],
                    options={"num_ctx": 16384, "temperature": 0.0}
                )
                raw_author = resp_author.get("message", {}).get("content", "").strip()
                clean_author = raw_author.replace("\n", "; ")
                for prefix in ["Dựa trên thông tin", "Danh sách họ tên các tác giả", "Tác giả gồm:", "Tác giả:"]:
                    if prefix.lower() in clean_author.lower():
                        clean_author = clean_author.split(":", 1)[-1].strip()
                clean_author = clean_author.strip("; ")
                if clean_author and clean_author.lower() not in {"none", "n/a", "không có", "unknown", "chưa rõ"}:
                    metadata_dict["author"] = clean_author
                elif clean_author.lower() in {"none", "n/a", "không có"}:
                    metadata_dict["author"] = "None"
            except Exception as e:
                logger.warning(f"Lỗi khi trích xuất Pass 3 (Tác giả RoI): {e}")

        # Chuẩn hóa đối tượng Pydantic và dọn dẹp lỗi chính tả ngành Dầu khí
        metadata = DocumentMetadata(**metadata_dict)
        metadata = clean_metadata_fields(metadata)
        return metadata
