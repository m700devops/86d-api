from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List
from datetime import datetime

# ============== USER MODELS ==============

class UserBase(BaseModel):
    email: EmailStr
    name: Optional[str] = None

class UserCreate(UserBase):
    password: str = Field(..., min_length=8)

class UserResponse(UserBase):
    id: str
    created_at: datetime
    
    class Config:
        from_attributes = True

class UserLogin(BaseModel):
    email: EmailStr
    password: str

# ============== AUTH MODELS ==============

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int = 3600
    user: UserResponse

class RefreshRequest(BaseModel):
    refresh_token: str

class RefreshResponse(BaseModel):
    access_token: str
    expires_in: int = 3600

# ============== PRODUCT MODELS ==============

class ProductBase(BaseModel):
    name: str
    brand: Optional[str] = None
    category: str = Field(..., pattern="^(spirits|beer|wine|other)$")
    size: Optional[str] = None
    upc: Optional[str] = None

class ProductCreate(ProductBase):
    pass

class ProductResponse(ProductBase):
    id: str
    image_url: Optional[str] = None
    scan_count: int = 0
    verified: bool = False
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True

class ProductListResponse(BaseModel):
    products: List[ProductResponse]
    total: int
    limit: int
    offset: int
    has_more: bool

class ProductSearchResponse(BaseModel):
    products: List[ProductResponse]
    query: str
    total: int

class ScanCountResponse(BaseModel):
    scan_count: int

# ============== LOCATION MODELS ==============

class LocationBase(BaseModel):
    name: str
    address: Optional[str] = None
    timezone: str = "America/New_York"

class LocationCreate(LocationBase):
    pass

class LocationResponse(LocationBase):
    id: str
    user_id: str
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True

class LocationListResponse(BaseModel):
    locations: List[LocationResponse]

# ============== PAR LEVEL MODELS ==============

class ParLevelBase(BaseModel):
    product_id: str
    par_quantity: float = Field(..., gt=0)

class ParLevelCreate(ParLevelBase):
    pass

class ParLevelBulkRequest(BaseModel):
    par_levels: List[ParLevelBase]

class ParLevelResponse(BaseModel):
    id: str
    location_id: str
    product_id: str
    product: Optional[ProductResponse] = None
    par_quantity: float
    updated_at: datetime
    
    class Config:
        from_attributes = True

class ParLevelListResponse(BaseModel):
    par_levels: List[ParLevelResponse]

class ParLevelBulkResponse(BaseModel):
    updated: int
    par_levels: List[ParLevelResponse]

# ============== SCAN MODELS ==============

class ScanBase(BaseModel):
    product_id: str
    level: str = Field(..., pattern="^(full|3/4|half|1/4|empty)$")
    quantity: int = Field(default=1, ge=1)
    detection_method: str = Field(..., pattern="^(auto|pen|barcode|manual)$")
    confidence: Optional[float] = Field(None, ge=0, le=1)
    photo_url: Optional[str] = None
    shelf_location: Optional[str] = None
    notes: Optional[str] = None
    idempotency_key: Optional[str] = None

class ScanCreate(ScanBase):
    created_at: Optional[datetime] = None

class ScanResponse(ScanBase):
    id: str
    session_id: str
    level_decimal: float
    product: Optional[ProductResponse] = None
    synced_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True

class ScanBulkRequest(BaseModel):
    scans: List[ScanCreate]

class ScanBulkResponse(BaseModel):
    created: int
    duplicates: int
    scans: List[ScanResponse]

# ============== VOICE NOTE MODELS ==============

class VoiceNoteBase(BaseModel):
    audio_url: Optional[str] = None
    transcript: Optional[str] = None
    linked_product_id: Optional[str] = None
    duration_seconds: Optional[int] = None

class VoiceNoteCreate(VoiceNoteBase):
    pass

class VoiceNoteResponse(VoiceNoteBase):
    id: str
    session_id: str
    processed: bool = False
    created_at: datetime
    
    class Config:
        from_attributes = True

# ============== INVENTORY SESSION MODELS ==============

class InventorySessionBase(BaseModel):
    location_id: str
    device_id: Optional[str] = None
    app_version: Optional[str] = None

class InventorySessionCreate(InventorySessionBase):
    pass

class InventorySessionResponse(BaseModel):
    id: str
    location_id: str
    user_id: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    total_bottles: int = 0
    duration_seconds: Optional[int] = None
    status: str = "in_progress"
    device_id: Optional[str] = None
    app_version: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True

class InventorySessionDetailResponse(BaseModel):
    session: InventorySessionResponse
    scans: List[ScanResponse]
    voice_notes: List[VoiceNoteResponse]

class SessionExistsError(BaseModel):
    error: str = "session_exists"
    message: str
    existing_session: dict
    options: List[str]

class InventoryCompleteResponse(BaseModel):
    session: InventorySessionResponse
    order: dict

# ============== ORDER MODELS ==============

class OrderItem(BaseModel):
    product_id: str
    product_name: str
    category: str
    current_amount: float
    par_level: float
    order_quantity: float
    urgency: str

class OrderResponse(BaseModel):
    id: str
    session_id: str
    location_id: str
    location_name: Optional[str] = None
    items: List[OrderItem]
    variance_alerts: List[dict]
    total_items: int
    estimated_cost: Optional[float] = None
    created_at: datetime
    exported_at: Optional[datetime] = None
    export_format: Optional[str] = None
    export_destination: Optional[str] = None
    
    class Config:
        from_attributes = True

class OrderListResponse(BaseModel):
    orders: List[OrderResponse]
    total: int

class OrderExportRequest(BaseModel):
    format: str = Field(..., pattern="^(json|text|csv)$")
    destination: Optional[str] = None

class OrderExportResponse(BaseModel):
    export: dict

# ============== SYNC MODELS ==============

class SyncScan(BaseModel):
    id: str
    product_id: str
    level: str
    detection_method: str
    idempotency_key: Optional[str] = None
    created_at: datetime

class SyncVoiceNote(BaseModel):
    id: str
    audio_url: Optional[str] = None
    transcript: Optional[str] = None
    linked_product_id: Optional[str] = None
    duration_seconds: Optional[int] = None

class SyncSession(BaseModel):
    id: str
    location_id: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str
    scans: List[SyncScan]
    voice_notes: List[SyncVoiceNote]

class SyncParLevelUpdate(BaseModel):
    location_id: str
    product_id: str
    par_quantity: float
    updated_at: datetime

class SyncRequest(BaseModel):
    device_id: str
    last_sync_at: Optional[datetime] = None
    sessions: List[SyncSession] = []
    par_level_updates: List[SyncParLevelUpdate] = []

class SyncResponse(BaseModel):
    synced_at: datetime
    sessions: dict
    par_levels: dict
    conflicts: List[dict]
    server_updates: dict

class SyncLocationResponse(BaseModel):
    location: LocationResponse
    par_levels: List[ParLevelResponse]
    recent_sessions: List[InventorySessionResponse]
    products: List[ProductResponse]
    synced_at: datetime

# ============== ERROR MODELS ==============

class ErrorResponse(BaseModel):
    error: str
    message: str
    details: Optional[dict] = None

# ============== HEALTH MODELS ==============

class HealthResponse(BaseModel):
    status: str
    database: str
    timestamp: str
    uptime_seconds: Optional[int] = None

class APIInfoResponse(BaseModel):
    name: str
    version: str
    status: str
    docs: str
    endpoints: dict