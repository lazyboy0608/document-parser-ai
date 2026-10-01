from pydantic import BaseModel, Field
from typing import Optional, List, Dict


class DocumentMetadata(BaseModel):
    """
    Cấu trúc dữ liệu chuẩn cho báo cáo và tài liệu kỹ thuật ngành Dầu khí (10 trường + Độ tin cậy).
    """
    well_link: str = Field(
        default="N/A",
        description="Tên giếng, mã giếng khoan, hoặc liên kết giếng (ví dụ: 15-A-1X, Bạch Hổ-12X,...)"
    )
    contract_name: str = Field(
        default="N/A",
        description="Tên hợp đồng, số hợp đồng, hoặc tên Lô/Block dầu khí (ví dụ: BLOCKS 15-1 & 15-2, Lô 09-1,...)"
    )
    title: str = Field(
        default="Chưa rõ",
        description="Tiêu đề chính xác của báo cáo/tài liệu kỹ thuật"
    )
    public_date: str = Field(
        default="Chưa rõ",
        description="Ngày tháng năm phát hành, ban hành hoặc xuất bản báo cáo (YYYY-MM-DD hoặc Month YYYY)"
    )
    originator: str = Field(
        default="Chưa rõ",
        description="Cơ quan, đơn vị chủ quản hoặc đơn vị ban hành tài liệu (ví dụ: Vietsovpetro, PVN, Santos Ltd,...)"
    )
    contractor: str = Field(
        default="N/A",
        description="Nhà thầu thi công, đơn vị kỹ thuật hoặc nhà thầu phụ thực hiện báo cáo"
    )
    author: str = Field(
        default="Chưa rõ",
        description="Tác giả, nhóm tác giả, kỹ sư trưởng hoặc người lập báo cáo"
    )
    language: str = Field(
        default="English",
        description="Ngôn ngữ chính của tài liệu quy chuẩn bằng tiếng Anh (ISO 639)"
    )
    encl: str = Field(
        default="No",
        description="Trạng thái hoặc số lượng tài liệu đính kèm/phụ lục (ví dụ: Yes, No, 13 Enclosures, 3,...)"
    )
    encl_name: str = Field(
        default="None",
        description="Danh sách tiêu đề/tên các tài liệu đính kèm (cách nhau bởi dấu chấm phẩy ';')"
    )
    confidence_score: float = Field(
        default=0.0,
        description="Điểm độ tin cậy tổng thể của mô hình khi trích xuất tài liệu (thang 0.0 - 100.0%)"
    )
    field_confidences: Dict[str, float] = Field(
        default_factory=dict,
        description="Điểm độ tin cậy chi tiết cho từng trường thông tin (thang 0.0 - 100.0%)"
    )
    low_confidence_fields: List[str] = Field(
        default_factory=list,
        description="Danh sách các trường có độ tin cậy thấp (mờ/nhòe/khuyết) cần highlight cảnh báo trong Excel"
    )
    field_warnings: Dict[str, str] = Field(
        default_factory=dict,
        description="Lý do cảnh báo cụ thể trên từng ô được highlight"
    )

