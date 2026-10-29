import os
# Tắt oneDNN PIR và kiểm tra kết nối để tương thích 100% trên Windows & tăng tốc độ khởi động
os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "False"
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

from pathlib import Path
from typing import Union, List, Dict, Any, Tuple
import numpy as np
from PIL import Image

from config.settings import settings


class OCREngine:
    """
    Module OCR sử dụng PaddleX / PaddleOCR (Det + Rec) trực tiếp.
    Khắc phục triệt để lỗi xung đột oneDNN/PIR trên Windows,
    tự động sắp xếp các dòng chữ theo thứ tự đọc từ trên xuống dưới (top-to-bottom).
    """
    _instance = None

    def __init__(self, lang: str = "vi", use_gpu: bool = False):
        self.lang = lang
        self.use_gpu = use_gpu
        self._det_model = None
        self._rec_model = None

    def _init_models(self):
        if self._det_model is None or self._rec_model is None:
            try:
                from paddlex import create_model
                # Khởi tạo mô hình phát hiện vùng chữ Mobile siêu nhẹ và mô hình nhận diện
                det_model_name = getattr(settings, "OCR_DET_MODEL", "PP-OCRv5_mobile_det")
                self._det_model = create_model(det_model_name)
                if self.lang in {"vi", "vie"}:
                    rec_model_name = "latin_PP-OCRv5_mobile_rec"
                else:
                    rec_model_name = "en_PP-OCRv5_mobile_rec"
                self._rec_model = create_model(rec_model_name)
            except Exception as e:
                # Fallback sang PaddleOCR wrapper nếu có
                try:
                    from paddleocr import PaddleOCR
                    self._legacy_ocr = PaddleOCR(lang=self.lang)
                except Exception:
                    raise RuntimeError(f"Không thể khởi tạo mô hình OCR: {e}")

    @staticmethod
    def _sort_polys_reading_order(polys: Union[List[np.ndarray], np.ndarray]) -> List[np.ndarray]:
        """
        Sắp xếp các bounding box theo thứ tự đọc tự nhiên 2D của tài liệu:
        - Gom các hộp chữ có độ cao Y tương đồng (cùng một dòng ngang) thành một hàng.
        - Trong mỗi hàng, sắp xếp các hộp chữ từ trái qua phải theo trục X (x_min).
        - Tránh lỗi đảo ngược thứ tự các từ cạnh nhau trên cùng một hàng (như Enclosure 2: <tên>).
        """
        if polys is None or len(polys) == 0:
            return []

        boxes = []
        for p in polys:
            y_min = float(np.min(p[:, 1]))
            y_max = float(np.max(p[:, 1]))
            x_min = float(np.min(p[:, 0]))
            x_max = float(np.max(p[:, 0]))
            boxes.append({
                "poly": p,
                "x_min": x_min,
                "y_min": y_min,
                "x_max": x_max,
                "y_max": y_max,
                "y_center": (y_min + y_max) / 2.0,
                "h": y_max - y_min,
            })

        # Sắp xếp sơ bộ theo y_min
        boxes.sort(key=lambda b: (b["y_min"], b["x_min"]))

        # Gom thành các dòng (lines) dựa trên độ trùng lặp chiều dọc
        lines: List[List[Dict[str, Any]]] = []
        for b in boxes:
            placed = False
            for line in lines:
                line_y_center = float(np.mean([item["y_center"] for item in line]))
                line_h = float(np.mean([item["h"] for item in line]))
                # Nếu tâm Y cách nhau dưới 50% chiều cao dòng -> cùng dòng ngang
                if abs(b["y_center"] - line_y_center) < max(12.0, line_h * 0.5):
                    line.append(b)
                    placed = True
                    break
            if not placed:
                lines.append([b])

        # Sắp xếp các dòng theo Y (từ trên xuống), và trong từng dòng sắp xếp theo X (trái sang phải)
        lines.sort(key=lambda l: float(np.mean([item["y_center"] for item in l])))
        sorted_polys = []
        for line in lines:
            line.sort(key=lambda item: item["x_min"])
            for item in line:
                sorted_polys.append(item["poly"])

        return sorted_polys

    def extract_text_with_scores(self, image_input: Union[str, Path, np.ndarray]) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Trích xuất văn bản kèm danh sách điểm số xác suất nhận diện (rec_score) của từng dòng chữ.
        Trả về:
            (full_text, [{"text": str, "score": float}, ...])
        """
        self._init_models()

        # Chuẩn hóa ảnh đầu vào thành numpy array RGB
        if isinstance(image_input, (str, Path)):
            pil_img = Image.open(str(image_input)).convert("RGB")
            img_np = np.array(pil_img)
        elif isinstance(image_input, np.ndarray):
            img_np = image_input
            if img_np.ndim == 2:
                img_np = np.stack([img_np] * 3, axis=-1)
            elif img_np.shape[2] == 4:
                img_np = img_np[:, :, :3]
            img_np = np.ascontiguousarray(img_np)
        else:
            raise ValueError(f"Kiểu dữ liệu ảnh không hợp lệ: {type(image_input)}")

        # 1. Phát hiện các vùng văn bản (Text Detection)
        det_out = list(self._det_model.predict(img_np))
        if not det_out or "dt_polys" not in det_out[0] or len(det_out[0]["dt_polys"]) == 0:
            return "", []

        polys = det_out[0]["dt_polys"]

        # Sắp xếp các hộp văn bản theo thứ tự đọc tự nhiên 2D (trên xuống dưới, trái sang phải)
        sorted_polys = self._sort_polys_reading_order(polys)

        # Cắt từng vùng văn bản
        cropped_boxes = []
        h, w = img_np.shape[:2]
        for poly in sorted_polys:
            x_min = max(0, int(np.min(poly[:, 0])))
            x_max = min(w, int(np.max(poly[:, 0])))
            y_min = max(0, int(np.min(poly[:, 1])))
            y_max = min(h, int(np.max(poly[:, 1])))

            if (x_max - x_min) > 4 and (y_max - y_min) > 4:
                crop = img_np[y_min:y_max, x_min:x_max]
                cropped_boxes.append(crop)

        if len(cropped_boxes) == 0:
            return "", []

        # 2. Nhận diện ký tự từng vùng (Text Recognition)
        rec_out = list(self._rec_model.predict(cropped_boxes))
        extracted_lines: List[str] = []
        ocr_line_items: List[Dict[str, Any]] = []

        for item in rec_out:
            text = item.get("rec_text", "").strip()
            score = float(item.get("rec_score", 0.0))
            if score > 0.4 and text:
                extracted_lines.append(text)
                ocr_line_items.append({"text": text, "score": round(score, 4)})

        return "\n".join(extracted_lines), ocr_line_items

    def extract_text(self, image_input: Union[str, Path, np.ndarray]) -> str:
        """
        Trích xuất văn bản từ đường dẫn ảnh hoặc mảng numpy RGB.
        """
        text, _ = self.extract_text_with_scores(image_input)
        return text


# Singleton instance mặc định
_default_engine = None


def extract_text_from_image(image_input: Union[str, Path, np.ndarray], lang: str = "vi") -> str:
    """
    Hàm tiện ích trích xuất text từ ảnh hoặc trang scan bằng PaddleOCR.
    """
    global _default_engine
    if _default_engine is None or _default_engine.lang != lang:
        _default_engine = OCREngine(lang=lang)
    return _default_engine.extract_text(image_input)


def extract_text_and_scores_from_image(
    image_input: Union[str, Path, np.ndarray], lang: str = "vi"
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Hàm tiện ích trích xuất cả văn bản và danh sách điểm tin cậy (rec_score) từng dòng từ PaddleOCR.
    """
    global _default_engine
    if _default_engine is None or _default_engine.lang != lang:
        _default_engine = OCREngine(lang=lang)
    return _default_engine.extract_text_with_scores(image_input)

