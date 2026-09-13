from pydantic import BaseModel
from typing import List, Any, Optional


from app.schemas.cctv import (
    CCTVCompanySettingBase,
    CCTVCompanySettingCreate,
    CCTVCompanySettingUpdate,
    CCTVCompanySettingResponse,
    CCTVCompanySettingStatusResponse,
)

class PaginationMeta(BaseModel):
    total_items: int
    total_pages: int
    current_page: int
    items_limit: int
    items_count: int


class Pagination(BaseModel):
    items: Optional[List[Any]]
    trips: Optional[List[Any]]
    meta: PaginationMeta
