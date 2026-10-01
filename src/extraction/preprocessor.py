def extract_metadata_context(text: str, head_chars: int = 6000, tail_chars: int = 1500) -> str:
    """
    Rút trích các đoạn văn bản tiềm năng nhất chứa thông tin metadata (Tiêu đề, Nhà xuất bản, Ngày tháng).
    Vì tài liệu có thể dài hàng chục trang nhưng metadata hầu hết nằm ở đầu (trang bìa/header)
    hoặc cuối (footer/thông tin xuất bản/chữ ký).
    Giúp Qwen-2.5:3b xử lý nhanh hơn 10x và tránh tràn context window.
    """
    cleaned_text = text.strip()
    if len(cleaned_text) <= (head_chars + tail_chars):
        return cleaned_text

    head = cleaned_text[:head_chars]
    tail = cleaned_text[-tail_chars:]

    return f"{head}\n\n[...ĐOẠN GIỮA VĂN BẢN ĐÃ ĐƯỢC LƯỢC BỎ...]\n\n{tail}"
