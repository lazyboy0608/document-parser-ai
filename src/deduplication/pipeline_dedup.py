from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from uuid import uuid4

from src.utils.logger import logger
from src.utils.state_tracker import StateTracker
from src.deduplication.exact_dedup import ExactDeduplicator
from src.deduplication.simhash_dedup import SimHashDeduplicator
from src.deduplication.diff_analyzer import DocumentDiffAnalyzer
from src.ingestion.document_loader import DocumentLoader
from src.clustering.file_organizer import FileOrganizer


@dataclass
class DedupResult:
    """Kết quả kiểm tra trùng lặp từ DeduplicationPipeline."""
    is_duplicate: bool
    duplicate_type: Optional[str] = None      # 'exact' hoặc 'semantic'
    matched_orig_path: Optional[str] = None   # Đường dẫn tệp gốc đối chiếu
    matched_orig_name: Optional[str] = None   # Tên tệp gốc đối chiếu
    group_id: Optional[str] = None            # Mã nhóm trùng lặp (ví dụ: GRP-baocao-1a2b)
    sim_pct: float = 100.0                    # % tương đồng
    simhash_dist: int = 0                     # Khoảng cách Hamming
    file_hash: str = ""                       # SHA-256
    simhash_hex: str = ""                     # SimHash 64-bit
    diff_info: Optional[Dict[str, Any]] = None # Thông tin đối chiếu sai khác
    doc_data: Optional[Dict[str, Any]] = None  # Dữ liệu văn bản đã nạp (chuyển tiếp sang AI nếu không trùng)
    target_path: Optional[Path] = None        # Vị trí tệp sau khi tổ chức
    final_path: Optional[Path] = None         # Đường dẫn cuối cùng ghi nhận trong DB
    inherited_metadata: Optional[Dict[str, Any]] = None # Metadata kế thừa từ bản gốc


class DeduplicationPipeline:
    """
    TẦNG 1: Sàng lọc trùng lặp siêu tốc thuần CPU (~0.05s).
    - Không nạp bất kỳ thư viện LLM/VLM/PyTorch nào -> giải phóng 100% tài nguyên CPU/RAM.
    - Kết hợp 2 bộ lọc: Exact Hash SHA-256 (100%) và Persistent SimHash 64-bit (>=95%).
    - Quản lý nhóm trùng lặp 2 chiều (Bi-directional Duplicate Grouping):
      + Khi phát hiện tệp nạp sau trùng với tệp gốc nạp trước:
      + Tạo/gán mã nhóm group_id chung.
      + Cập nhật ngược (Reverse Link) ngay lập tức cho tệp gốc trong SQLite.
      + Kế thừa metadata từ tệp gốc cho tệp trùng.
      + Short-circuit dừng ngay lập tức, chặn hoàn toàn việc gọi mô hình AI.
    """

    def __init__(
        self,
        state_tracker: StateTracker,
        file_organizer: FileOrganizer,
        mode: str = "move",
        simhash_threshold_bits: int = 3,
    ):
        self.state_tracker = state_tracker
        self.file_organizer = file_organizer
        self.mode = mode

        self.exact_dedup = ExactDeduplicator()
        self.simhash_dedup = SimHashDeduplicator(threshold_bits=simhash_threshold_bits)
        self.diff_analyzer = DocumentDiffAnalyzer()

        # Nạp dữ liệu băm lịch sử từ SQLite
        self.active_simhashes: Dict[str, str] = self.state_tracker.get_active_simhashes()
        for fhash, fp in self.state_tracker.get_active_exact_hashes().items():
            self.exact_dedup.seen_hashes.add(fhash)
            self.exact_dedup.hash_to_path[fhash] = fp

    def generate_group_id(self, orig_name: str) -> str:
        """Tạo mã nhóm nhận diện gia đình tài liệu trùng lặp duy nhất."""
        clean_stem = Path(orig_name).stem.replace(" ", "_")[:12]
        return f"GRP-{clean_stem}-{uuid4().hex[:4].upper()}"

    def get_or_create_group_id(self, matched_orig_path: str, orig_name: str) -> str:
        """Lấy group_id đã có của tệp gốc hoặc khởi tạo mã nhóm mới nếu đây là lần đầu phát hiện bản trùng."""
        all_completed = self.state_tracker.get_all_completed()
        norm_matched = self.state_tracker._norm(matched_orig_path)

        orig_record = all_completed.get(norm_matched)
        if not orig_record:
            # Tra cứu phụ theo tên tệp
            for fp, rec in all_completed.items():
                if Path(fp).name == orig_name:
                    orig_record = rec
                    break

        if orig_record:
            existing_gid = orig_record.get("duplicate_group_id") or orig_record.get("metadata", {}).get("duplicate_group_id")
            if existing_gid:
                return existing_gid

        return self.generate_group_id(orig_name)

    def process(self, file_path: Path, date_str: Optional[str] = None) -> DedupResult:
        """
        Thực thi kiểm tra trùng lặp trên tệp đầu vào.
        Returns:
            DedupResult chứa thông tin chi tiết. Nếu is_duplicate=True, pipeline AI có thể bỏ qua ngay.
        """
        # 1. Tính mã băm Exact Hash SHA-256
        file_hash = self.exact_dedup.calculate_file_hash(file_path)
        is_exact_dup, matched_exact = self.exact_dedup.find_duplicate(file_path)

        if is_exact_dup:
            orig_name = Path(matched_exact).name if matched_exact else "tệp gốc đã nạp trước"
            logger.warning(
                f"-> [TẦNG 1 DEDUP] Phát hiện trùng lặp tuyệt đối 100% (cùng SHA-256 với {orig_name}): {file_path.name}"
            )
            group_id = self.get_or_create_group_id(matched_exact or str(file_path), orig_name)

            # Cập nhật ngược (Reverse Link) ngay cho tệp gốc
            if matched_exact:
                self.state_tracker.update_duplicate_reverse_link(
                    orig_file_identifier=matched_exact,
                    dup_file_name=file_path.name,
                    group_id=group_id
                )

            # Kế thừa metadata từ bản gốc nếu có
            orig_meta = {}
            if matched_exact:
                all_completed = self.state_tracker.get_all_completed()
                norm_matched = self.state_tracker._norm(matched_exact)
                if norm_matched in all_completed:
                    orig_meta = all_completed[norm_matched].get("metadata", {})

            dup_meta = {
                "title": orig_meta.get("title") or "Tài liệu trùng lặp",
                "contract_name": orig_meta.get("contract_name") or "N/A",
                "well_link": orig_meta.get("well_link") or "N/A",
                "public_date": orig_meta.get("public_date") or "N/A",
                "originator": orig_meta.get("originator") or "N/A",
                "contractor": orig_meta.get("contractor") or "N/A",
                "author": orig_meta.get("author") or "N/A",
                "language": orig_meta.get("language") or "N/A",
                "document_type": orig_meta.get("document_type") or "Tài liệu trùng lặp",
                "encl": orig_meta.get("encl") or "No",
                "encl_name": orig_meta.get("encl_name") or "None",
                "confidence_score": float(orig_meta.get("confidence_score", 0.0) or 0.0),
                "duplicate_type": "exact",
                "duplicate_of": orig_name,
                "duplicate_group_id": group_id,
                "duplicate_status": f"Trùng khớp tuyệt đối 100% (với {orig_name})",
            }

            # Di chuyển tệp vào thư mục 'Trùng lặp'
            target_path = self.file_organizer.organize(
                source_file=file_path,
                is_duplicate=True,
                mode=self.mode,
                date_str=date_str,
            )
            if not target_path:
                logger.error(f"Không thể di chuyển file trùng lặp {file_path.name}.")
                return DedupResult(is_duplicate=True, file_hash=file_hash)

            final_path = target_path if self.mode == "move" else file_path
            if self.mode == "move" and target_path != file_path:
                self.state_tracker.delete_state(str(file_path))

            self.state_tracker.save_state(
                str(final_path),
                file_hash,
                "COMPLETED",
                is_duplicate=True,
                duplicate_of=orig_name,
                metadata=dup_meta,
                duplicate_group_id=group_id
            )

            return DedupResult(
                is_duplicate=True,
                duplicate_type="exact",
                matched_orig_path=matched_exact,
                matched_orig_name=orig_name,
                group_id=group_id,
                sim_pct=100.0,
                simhash_dist=0,
                file_hash=file_hash,
                target_path=target_path,
                final_path=final_path,
                inherited_metadata=dup_meta,
            )

        # 2. Nạp văn bản tài liệu để tính SimHash
        try:
            doc_data = DocumentLoader.load_document(file_path)
            raw_text = doc_data.get("text", "")
        except Exception as e:
            logger.error(f"Lỗi khi nạp file {file_path.name}: {e}")
            self.state_tracker.save_state(str(file_path), file_hash, "ERROR")
            return DedupResult(is_duplicate=False, file_hash=file_hash)

        # 3. Tính SimHash 64-bit và so sánh khoảng cách Hamming
        simhash_hex = self.simhash_dedup.compute_simhash(raw_text)
        is_semantic_dup, matched_orig, simhash_dist = self.simhash_dedup.find_near_duplicate(
            simhash_hex, self.active_simhashes
        )
        sim_pct = self.simhash_dedup.similarity_percentage(simhash_dist)

        if is_semantic_dup and matched_orig:
            semantic_orig_name = Path(matched_orig).name
            logger.warning(
                f"-> [TẦNG 1 DEDUP - SIMHASH] Gần trùng lặp nội dung ({sim_pct}% tương đồng, lệch {simhash_dist}/64 bits) "
                f"với tài liệu gốc: {semantic_orig_name}"
            )
            group_id = self.get_or_create_group_id(matched_orig, semantic_orig_name)

            # Cập nhật ngược (Reverse Link) ngay cho tệp gốc
            self.state_tracker.update_duplicate_reverse_link(
                orig_file_identifier=matched_orig,
                dup_file_name=file_path.name,
                group_id=group_id
            )

            # Phân tích sai khác (Diff) với full_text=True
            diff_info = None
            try:
                orig_file_path = Path(matched_orig)
                orig_raw_text = ""
                if orig_file_path.exists():
                    orig_data = DocumentLoader.load_document(orig_file_path, full_text=True)
                    orig_raw_text = orig_data.get("text", "")

                dup_data = DocumentLoader.load_document(file_path, full_text=True)
                dup_raw_text = dup_data.get("text", "") or raw_text

                diff_info = self.diff_analyzer.analyze_diff(
                    orig_text=orig_raw_text,
                    dup_text=dup_raw_text,
                    orig_name=orig_file_path.name,
                    dup_name=file_path.name,
                    sim_pct=sim_pct,
                )
            except Exception as diff_err:
                logger.warning(f"Lỗi khi đối chiếu sai khác tệp trùng lặp: {diff_err}")

            # Kế thừa metadata từ tệp gốc
            all_completed = self.state_tracker.get_all_completed()
            norm_matched = self.state_tracker._norm(matched_orig)
            orig_meta = all_completed.get(norm_matched, {}).get("metadata", {})

            dup_meta = {
                "title": orig_meta.get("title") or "Tài liệu gần trùng lặp",
                "contract_name": orig_meta.get("contract_name") or "N/A",
                "well_link": orig_meta.get("well_link") or "N/A",
                "public_date": orig_meta.get("public_date") or "N/A",
                "originator": orig_meta.get("originator") or "N/A",
                "contractor": orig_meta.get("contractor") or "N/A",
                "author": orig_meta.get("author") or "N/A",
                "language": orig_meta.get("language") or "N/A",
                "document_type": orig_meta.get("document_type") or "Tài liệu trùng lặp",
                "encl": orig_meta.get("encl") or "No",
                "encl_name": orig_meta.get("encl_name") or "None",
                "confidence_score": float(orig_meta.get("confidence_score", 0.0) or 0.0),
                "duplicate_type": "semantic",
                "duplicate_of": semantic_orig_name,
                "duplicate_group_id": group_id,
                "duplicate_status": f"Gần trùng lặp (với {semantic_orig_name})",
                "diff_info": diff_info,
            }

            # Di chuyển tệp vào thư mục 'Trùng lặp'
            target_path = self.file_organizer.organize(
                source_file=file_path,
                is_duplicate=True,
                mode=self.mode,
                date_str=date_str,
            )
            if not target_path:
                logger.error(f"Không thể di chuyển file trùng lặp {file_path.name}.")
                return DedupResult(is_duplicate=True, file_hash=file_hash, simhash_hex=simhash_hex)

            final_path = target_path if self.mode == "move" else file_path
            if self.mode == "move" and target_path != file_path:
                self.state_tracker.delete_state(str(file_path))

            self.state_tracker.save_state(
                str(final_path),
                file_hash,
                "COMPLETED",
                is_duplicate=True,
                duplicate_of=semantic_orig_name,
                metadata=dup_meta,
                simhash=simhash_hex,
                duplicate_group_id=group_id
            )

            return DedupResult(
                is_duplicate=True,
                duplicate_type="semantic",
                matched_orig_path=matched_orig,
                matched_orig_name=semantic_orig_name,
                group_id=group_id,
                sim_pct=sim_pct,
                simhash_dist=simhash_dist,
                file_hash=file_hash,
                simhash_hex=simhash_hex,
                diff_info=diff_info,
                target_path=target_path,
                final_path=final_path,
                inherited_metadata=dup_meta,
            )

        # 4. Tệp hoàn toàn MỚI ĐỘC NHẤT -> Chuyển tiếp sang Tầng 2 AI
        return DedupResult(
            is_duplicate=False,
            file_hash=file_hash,
            simhash_hex=simhash_hex,
            doc_data=doc_data,
        )

    def register_new_unique_file(self, final_path: Path, file_hash: str, simhash_hex: str):
        """Đăng ký tệp mới độc nhất vào danh sách tra cứu để các tệp nạp sau đối chiếu."""
        self.active_simhashes[str(final_path)] = simhash_hex
        self.exact_dedup.register_file(final_path, file_hash)

    def unregister_file(self, file_path: Path, file_hash: Optional[str] = None):
        """Xóa tệp khỏi bộ nhớ đệm băm (Exact Hash và SimHash) khi tệp bị xóa khỏi đĩa."""
        norm_p = self.state_tracker._norm(str(file_path))
        self.active_simhashes.pop(norm_p, None)
        self.active_simhashes.pop(str(file_path), None)
        target_name = file_path.name
        for p in list(self.active_simhashes.keys()):
            if Path(p).name == target_name:
                self.active_simhashes.pop(p, None)
        self.exact_dedup.unregister_file(file_path, file_hash=file_hash)

    def reload_hashes_from_db(self):
        """Tải lại toàn bộ danh bạ mã băm từ SQLite để đồng bộ tuyệt đối với đĩa vật lý."""
        self.active_simhashes = self.state_tracker.get_active_simhashes()
        self.exact_dedup.seen_hashes.clear()
        self.exact_dedup.hash_to_path.clear()
        for fhash, fp in self.state_tracker.get_active_exact_hashes().items():
            self.exact_dedup.seen_hashes.add(fhash)
            self.exact_dedup.hash_to_path[fhash] = fp
