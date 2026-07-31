import math


def page_meta(page: int, page_size: int, total: int) -> dict:
    total_pages = max(1, math.ceil(total / page_size))
    return {"page": page, "page_size": page_size, "total": total, "total_pages": total_pages}
