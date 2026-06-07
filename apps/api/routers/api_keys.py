"""Router for API key management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db
from apps.api.schemas.api_keys import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyListResponse,
)
from apps.api.services.api_key_service import ApiKeyService

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


@router.post("", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
def create_api_key(
    data: ApiKeyCreateRequest,
    db: Session = Depends(get_db),
) -> ApiKeyCreateResponse:
    """Create a new API key. The raw key is returned once in the response."""
    return ApiKeyService(db).create(data)


@router.get("", response_model=ApiKeyListResponse)
def list_api_keys(
    db: Session = Depends(get_db),
) -> ApiKeyListResponse:
    """List all API keys (only metadata is returned, not raw keys)."""
    return ApiKeyService(db).list_all()


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_key(
    key_id: str = Path(..., description="API key public ID"),
    db: Session = Depends(get_db),
) -> None:
    """Revoke an API key. The key can no longer be used for authentication."""
    ApiKeyService(db).revoke(key_id)
