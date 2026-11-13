from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

class ScrapeRequest(BaseModel):
    query: str = Field(default="Law firms", description="Target industry keyword or practice area")
    location: str = Field(default="Fulton County, Atlanta, GA", description="Geographic location")
    max_results: int = Field(default=25, ge=5, le=200, description="Target lead count")
    session_id: Optional[str] = Field(default=None, description="Client session identifier")

class LawFirm(BaseModel):
    id: Optional[int] = None
    run_id: Optional[str] = None
    search_query: str
    name: str
    category: Optional[str] = None
    relevance: str = "high" # high or low
    relevance_reason: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    email: Optional[str] = None
    email_type: Optional[str] = None # generic, personal, freemail
    mx_valid: Optional[bool] = None
    secondary_emails: Optional[List[Dict[str, Any]]] = []
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    google_maps_url: Optional[str] = None
    place_id: Optional[str] = None
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    enrichment_status: str = "pending" # pending, email_found, email_not_found, no_website, failed, already_exists
    scraped_at: Optional[str] = None

class SearchStats(BaseModel):
    total_leads: int
    with_email: int
    with_phone: int
    with_website: int
    email_success_rate: float
    high_relevance_count: int
    low_relevance_count: int
