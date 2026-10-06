# -*- coding: utf-8 -*-
"""
MCP Server for Document Parser AI (Dầu khí / Doanh nghiệp)
Cung cấp các công cụ MCP chuẩn hóa cho AI IDE (Antigravity, Claude Desktop, Cursor...)
qua giao thức Model Context Protocol (stdio transport), tích hợp quản lý trạng thái SQLite.
"""

import sys
import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, Any, Optional, List

# Đảm bảo thư mục gốc dự án nằm trong sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings
from src.ingestion.document_loader import DocumentLoader
from src.pipeline import DocumentPipeline

# Tương thích cả mcp 2.x (MCPServer) và mcp 1.x (FastMCP)
try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

# Cấu hình loguru: BẮT BUỘC log ra sys.stderr sau khi nạp toàn bộ module dự án
# (Do src.utils.logger mặc định add sys.stdout làm phá hỏng JSON-RPC)
from loguru import logger
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    level="INFO"
)

# Khởi tạo MCP Server
mcp = MCPServer(
    name="document-parser-ai",
    version="1.1.0",
    description="MCP Server cho hệ thống trích xuất, phân loại và tổng hợp tài liệu Dầu khí tự động (OCR + VLM + LLM + SQLite State Tracking)."
)


# =====================================================================
# HỖ TRỢ KẾT NỐI SQLITE READ-ONLY AN TOÀN
# =====================================================================

def _get_active_db_path() -> Path:
    """Xác định đường dẫn file database SQLite thực tế đang có dữ liệu."""
    db_file = settings.CACHE_DIR / "pipeline_state.db"
    if not db_file.exists():
        alt_db = settings.CACHE_DIR / "pipeline_state.sqlite"
        if alt_db.exists() and alt_db.stat().st_size > 0:
            return alt_db
    return db_file


def _get_readonly_db() -> sqlite3.Connection:
    """Mở kết nối SQLite ở chế độ READ-ONLY thông qua URI để không bao giờ gây lock DB."""
    db_path = _get_active_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"Không tìm thấy file SQLite tại {db_path}")
    uri_path = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri_path, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# =====================================================================
# CÁC MCP TOOL: HỆ THỐNG & XỬ LÝ TÀI LIỆU
# =====================================================================

@mcp.tool(
    name="get_system_info",
    description="Lấy thông tin cấu hình hệ thống: mô hình LLM, VLM, OCR, định dạng tệp hỗ trợ và thư mục làm việc."
)
def get_system_info() -> Dict[str, Any]:
    """Trả về thông tin trạng thái và cấu hình của hệ thống Document Parser."""
    db_path = _get_active_db_path()
    return {
        "status": "online",
        "llm_model": settings.OLLAMA_MODEL,
        "vlm_model": settings.VLM_MODEL,
        "ollama_base_url": settings.OLLAMA_BASE_URL,
        "ocr_language": settings.OCR_LANG,
        "supported_extensions": sorted(list(DocumentLoader.SUPPORTED_EXTENSIONS)),
        "default_input_dir": str(settings.INPUT_DIR),
        "default_output_dir": str(settings.OUTPUT_DIR),
        "cache_dir": str(settings.CACHE_DIR),
        "sqlite_db_path": str(db_path),
        "sqlite_db_exists": db_path.exists(),
        "sqlite_db_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
    }


@mcp.tool(
    name="extract_raw_text",
    description="Trích xuất nhanh nội dung văn bản thô từ tệp tài liệu (PDF, Word docx/doc, TXT, hoặc OCR ảnh) mà không chạy qua LLM."
)
def extract_raw_text(file_path: str) -> Dict[str, Any]:
    """
    Trích xuất văn bản từ tài liệu:
    - file_path: Đường dẫn tuyệt đối hoặc tương đối tới tệp tài liệu.
    """
    path = Path(file_path).resolve()
    if not path.exists():
        return {"error": f"Tệp không tồn tại: {file_path}", "success": False}
    
    if path.suffix.lower() not in DocumentLoader.SUPPORTED_EXTENSIONS:
        return {
            "error": f"Định dạng {path.suffix} không được hỗ trợ. Các định dạng cho phép: {list(DocumentLoader.SUPPORTED_EXTENSIONS)}",
            "success": False
        }

    try:
        doc_data = DocumentLoader.load_document(path)
        text_content = doc_data.get("text", "")
        return {
            "success": True,
            "file_name": path.name,
            "file_path": str(path),
            "file_ext": path.suffix.lower(),
            "is_scanned": doc_data.get("is_scanned", False),
            "total_chars": len(text_content),
            "text_preview": text_content[:2000] + ("..." if len(text_content) > 2000 else ""),
            "full_text": text_content,
        }
    except Exception as e:
        logger.error(f"Lỗi khi trích xuất text từ {file_path}: {e}")
        return {"error": str(e), "success": False}


@mcp.tool(
    name="process_single_document",
    description="Xử lý toàn diện một tài liệu (OCR -> Lọc trùng lặp -> Trích xuất LLM/VLM -> Chuẩn hóa Dầu khí -> Phân loại & Gom thư mục)."
)
def process_single_document(file_path: str, mode: str = "copy", output_dir: Optional[str] = None) -> Dict[str, Any]:
    """
    Chạy pipeline xử lý đầy đủ trên một tài liệu cụ thể:
    - file_path: Đường dẫn tới file cần xử lý.
    - mode: 'copy' (sao chép tài liệu vào thư mục đích) hoặc 'move' (di chuyển tệp gốc). Mặc định 'copy'.
    - output_dir: Thư mục lưu kết quả phân loại (tùy chọn, mặc định lấy theo cấu hình).
    """
    path = Path(file_path).resolve()
    if not path.exists():
        return {"error": f"Tệp không tồn tại: {file_path}", "success": False}

    target_out = Path(output_dir).resolve() if output_dir else settings.OUTPUT_DIR
    target_out.mkdir(parents=True, exist_ok=True)

    try:
        pipeline = DocumentPipeline(output_dir=target_out, mode=mode)
        result = pipeline.process_single_file(path)
        if result is None:
            return {
                "success": False,
                "error": f"Không thể xử lý tệp {path.name} (có thể do định dạng không hỗ trợ hoặc tệp rỗng)."
            }
        
        return {
            "success": True,
            "result": result
        }
    except Exception as e:
        logger.exception(f"Lỗi khi xử lý tài liệu {file_path}: {e}")
        return {"error": str(e), "success": False}


@mcp.tool(
    name="batch_process_directory",
    description="Quét và xử lý toàn bộ tài liệu trong một thư mục hàng loạt, gom nhóm phân loại và xuất báo cáo Excel."
)
def batch_process_directory(input_dir: str, output_dir: Optional[str] = None, mode: str = "copy") -> Dict[str, Any]:
    """
    Chạy hàng loạt pipeline trên thư mục:
    - input_dir: Thư mục chứa tài liệu cần quét.
    - output_dir: Thư mục đích lưu tài liệu phân loại và tệp Excel tổng hợp.
    - mode: 'copy' hoặc 'move'.
    """
    inp_path = Path(input_dir).resolve()
    if not inp_path.exists() or not inp_path.is_dir():
        return {"error": f"Thư mục đầu vào không tồn tại: {input_dir}", "success": False}

    out_path = Path(output_dir).resolve() if output_dir else settings.OUTPUT_DIR
    out_path.mkdir(parents=True, exist_ok=True)

    try:
        pipeline = DocumentPipeline(output_dir=out_path, mode=mode)
        processed_files = pipeline.run(input_dir=inp_path)
        return {
            "success": True,
            "total_processed": len(processed_files),
            "output_directory": str(out_path),
            "processed_items": [
                {
                    "file_name": item.get("file_name"),
                    "category": (item.get("metadata") or {}).get("contract_name", "CHUA_XAC_DINH"),
                    "confidence": (item.get("metadata") or {}).get("confidence_score", 0.0),
                    "target_path": item.get("file_path")
                }
                for item in processed_files
            ]
        }
    except Exception as e:
        logger.exception(f"Lỗi xử lý hàng loạt thư mục {input_dir}: {e}")
        return {"error": str(e), "success": False}


# =====================================================================
# CÁC MCP TOOL: TRUY VẤN VÀ GIÁM SÁT SQLITE (PIPELINE STATE)
# =====================================================================

@mcp.tool(
    name="get_pipeline_stats",
    description="Thống kê tổng quan cơ sở dữ liệu SQLite pipeline_state.db: tổng số file đã quét, tỉ lệ hoàn thành/lỗi/trùng lặp, điểm tin cậy trung bình, phân bố theo hợp đồng."
)
def get_pipeline_stats() -> Dict[str, Any]:
    """Trả về bức tranh thống kê toàn cảnh tiến trình xử lý từ SQLite."""
    try:
        with _get_readonly_db() as conn:
            cur = conn.cursor()
            
            # 1. Tổng số bản ghi
            cur.execute("SELECT count(*) FROM processed_files")
            total_records = cur.fetchone()[0]

            # 2. Phân bố theo trạng thái (COMPLETED, FAILED, ...)
            cur.execute("SELECT status, count(*) FROM processed_files GROUP BY status")
            status_dist = {row[0]: row[1] for row in cur.fetchall()}

            # 3. Thống kê trùng lặp
            cur.execute("SELECT count(*) FROM processed_files WHERE is_duplicate = 1")
            duplicate_count = cur.fetchone()[0]

            # 4. Thời gian cập nhật gần nhất
            cur.execute("SELECT max(updated_at) FROM processed_files")
            latest_update = cur.fetchone()[0]

            # 5. Phân tích chi tiết metadata: Độ tin cậy trung bình, số file cảnh báo, phân bố hợp đồng
            cur.execute("SELECT metadata_json FROM processed_files WHERE status = 'COMPLETED' AND metadata_json IS NOT NULL")
            rows = cur.fetchall()
            
            conf_scores = []
            low_conf_count = 0
            contract_counts = {}
            originator_counts = {}

            for (meta_str,) in rows:
                try:
                    meta = json.loads(meta_str)
                    conf = meta.get("confidence_score")
                    if isinstance(conf, (int, float)):
                        conf_scores.append(conf)
                        if conf < 0.70:
                            low_conf_count += 1
                    
                    c_name = meta.get("contract_name") or "Chưa xác định"
                    contract_counts[c_name] = contract_counts.get(c_name, 0) + 1

                    orig = meta.get("originator") or "Chưa rõ"
                    originator_counts[orig] = originator_counts.get(orig, 0) + 1
                except Exception:
                    continue

            avg_conf = round(sum(conf_scores) / len(conf_scores), 4) if conf_scores else 0.0

            db_path = _get_active_db_path()
            return {
                "success": True,
                "db_file": str(db_path),
                "db_size_kb": round(db_path.stat().st_size / 1024, 2) if db_path.exists() else 0,
                "total_records": total_records,
                "status_distribution": status_dist,
                "duplicate_files_count": duplicate_count,
                "unique_processed_count": total_records - duplicate_count,
                "quality_metrics": {
                    "average_confidence_score": avg_conf,
                    "low_confidence_count (<0.70)": low_conf_count,
                    "evaluated_files_count": len(conf_scores)
                },
                "top_contracts": dict(sorted(contract_counts.items(), key=lambda x: x[1], reverse=True)[:10]),
                "top_originators": dict(sorted(originator_counts.items(), key=lambda x: x[1], reverse=True)[:10]),
                "latest_update": latest_update
            }
    except Exception as e:
        logger.exception(f"Lỗi khi thống kê SQLite: {e}")
        return {"error": str(e), "success": False}


@mcp.tool(
    name="get_document_state",
    description="Tra cứu chi tiết trạng thái, phân loại, mã băm, điểm tin cậy và cảnh báo của 1 tài liệu trong SQLite theo tên tệp hoặc đường dẫn."
)
def get_document_state(file_identifier: str) -> Dict[str, Any]:
    """
    Tra cứu chi tiết 1 tệp tài liệu:
    - file_identifier: Tên tệp (vd '711-MSP7.doc') hoặc đường dẫn đầy đủ tới tệp.
    """
    try:
        with _get_readonly_db() as conn:
            cur = conn.cursor()
            
            # Tìm kiếm chính xác đường dẫn trước
            cur.execute("""
                SELECT file_path, file_hash, status, is_duplicate, metadata_json, updated_at
                FROM processed_files
                WHERE file_path = ? OR file_path LIKE ?
                LIMIT 1
            """, (file_identifier, f"%{file_identifier}%"))
            row = cur.fetchone()

            if not row:
                return {
                    "success": False,
                    "message": f"Không tìm thấy thông tin cho tài liệu: '{file_identifier}' trong database."
                }

            meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
            return {
                "success": True,
                "file_path": row["file_path"],
                "file_name": Path(row["file_path"]).name,
                "file_hash": row["file_hash"],
                "status": row["status"],
                "is_duplicate": bool(row["is_duplicate"]),
                "updated_at": row["updated_at"],
                "metadata": meta,
                "summary": {
                    "contract_name": meta.get("contract_name"),
                    "originator": meta.get("originator"),
                    "public_date": meta.get("public_date"),
                    "confidence_score": meta.get("confidence_score"),
                    "field_warnings": meta.get("field_warnings", [])
                }
            }
    except Exception as e:
        logger.exception(f"Lỗi khi tra cứu tài liệu {file_identifier}: {e}")
        return {"error": str(e), "success": False}


@mcp.tool(
    name="query_pipeline_documents",
    description="Tìm kiếm, lọc danh sách tài liệu từ SQLite theo trạng thái, cờ trùng lặp, tên hợp đồng, ngưỡng điểm tin cậy hoặc từ khóa."
)
def query_pipeline_documents(
    status: Optional[str] = None,
    is_duplicate: Optional[bool] = None,
    contract_name: Optional[str] = None,
    keyword: Optional[str] = None,
    min_confidence: Optional[float] = None,
    max_confidence: Optional[float] = None,
    limit: int = 20,
    offset: int = 0
) -> Dict[str, Any]:
    """
    Lọc danh sách tài liệu từ bảng processed_files:
    - status: 'COMPLETED', 'FAILED', v.v.
    - is_duplicate: True/False
    - contract_name: Tên hợp đồng cần lọc (vd '711-MSP7')
    - keyword: Từ khóa tìm trong đường dẫn hoặc metadata
    - min_confidence / max_confidence: Lọc theo khoảng điểm tin cậy (0.0 - 1.0)
    - limit / offset: Phân trang kết quả
    """
    try:
        with _get_readonly_db() as conn:
            cur = conn.cursor()
            query = "SELECT file_path, file_hash, status, is_duplicate, metadata_json, updated_at FROM processed_files WHERE 1=1"
            params = []

            if status:
                query += " AND status = ?"
                params.append(status.upper())
            
            if is_duplicate is not None:
                query += " AND is_duplicate = ?"
                params.append(1 if is_duplicate else 0)

            if keyword:
                query += " AND (file_path LIKE ? OR metadata_json LIKE ?)"
                params.extend([f"%{keyword}%", f"%{keyword}%"])

            cur.execute(query, params)
            all_rows = cur.fetchall()

            # Lọc theo metadata (contract_name, confidence) trong bộ nhớ
            filtered_items = []
            for r in all_rows:
                meta = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
                
                if contract_name and contract_name.lower() not in (meta.get("contract_name") or "").lower():
                    continue

                conf = meta.get("confidence_score")
                if min_confidence is not None and (conf is None or conf < min_confidence):
                    continue
                if max_confidence is not None and (conf is None or conf > max_confidence):
                    continue

                filtered_items.append({
                    "file_name": Path(r["file_path"]).name,
                    "file_path": r["file_path"],
                    "status": r["status"],
                    "is_duplicate": bool(r["is_duplicate"]),
                    "contract_name": meta.get("contract_name", "Chưa xác định"),
                    "originator": meta.get("originator", "Chưa rõ"),
                    "confidence_score": conf,
                    "updated_at": r["updated_at"]
                })

            total_matched = len(filtered_items)
            paginated = filtered_items[offset : offset + limit]

            return {
                "success": True,
                "total_matched": total_matched,
                "limit": limit,
                "offset": offset,
                "documents": paginated
            }
    except Exception as e:
        logger.exception(f"Lỗi khi lọc danh sách tài liệu: {e}")
        return {"error": str(e), "success": False}


@mcp.tool(
    name="execute_readonly_sql",
    description="Thực thi câu lệnh SQL tùy biến (CHỈ ĐỌC: SELECT, PRAGMA) trên cơ sở dữ liệu pipeline_state.db để kiểm tra bảng dữ liệu trực tiếp."
)
def execute_readonly_sql(sql_query: str) -> Dict[str, Any]:
    """
    Thực thi câu lệnh SQL chỉ đọc:
    - sql_query: Câu lệnh SQL bắt đầu bằng SELECT hoặc PRAGMA (không cho phép sửa đổi dữ liệu).
    """
    clean_sql = sql_query.strip()
    
    # Kiểm tra an toàn: Tuyệt đối không cho phép các lệnh ghi hoặc thay đổi schema
    forbidden_pattern = r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|VACUUM|TRUNCATE)\b"
    if re.search(forbidden_pattern, clean_sql, re.IGNORECASE):
        return {
            "success": False,
            "error": "Câu lệnh bị từ chối: Chỉ cho phép các câu lệnh truy vấn CHỈ ĐỌC (SELECT, PRAGMA, EXPLAIN, WITH)."
        }

    if not re.match(r"^(SELECT|PRAGMA|EXPLAIN|WITH)\b", clean_sql, re.IGNORECASE):
        return {
            "success": False,
            "error": "Câu lệnh phải bắt đầu bằng SELECT, PRAGMA, WITH hoặc EXPLAIN."
        }

    try:
        with _get_readonly_db() as conn:
            cur = conn.cursor()
            cur.execute(clean_sql)
            
            # Giới hạn tối đa 100 dòng để bảo vệ token context
            rows = cur.fetchmany(100)
            col_names = [d[0] for d in cur.description] if cur.description else []
            
            results = []
            for row in rows:
                results.append({col_names[i]: row[i] for i in range(len(col_names))})

            return {
                "success": True,
                "columns": col_names,
                "row_count": len(results),
                "rows": results
            }
    except Exception as e:
        logger.error(f"Lỗi khi thực thi SQL '{sql_query}': {e}")
        return {"error": str(e), "success": False}


if __name__ == "__main__":
    # Khởi chạy MCP Server lắng nghe qua stdio transport
    logger.info("Khởi động Document Parser AI MCP Server qua STDIO...")
    mcp.run(transport="stdio")
