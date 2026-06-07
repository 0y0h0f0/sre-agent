"""REST endpoints for incident comments and evidence annotations."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db
from apps.api.schemas.comments import (
    AnnotationCreate,
    AnnotationItem,
    AnnotationListResponse,
    CommentCreate,
    CommentItem,
    CommentListResponse,
)
from apps.api.services.comment_service import CommentService

router = APIRouter(prefix="/api", tags=["comments"])


def _service(db: Session = Depends(get_db)) -> CommentService:
    return CommentService(db)


@router.post(
    "/incidents/{incident_id}/comments",
    response_model=CommentItem,
    status_code=201,
)
def create_comment(
    incident_id: str,
    body: CommentCreate,
    svc: CommentService = Depends(_service),
) -> CommentItem:
    return svc.create_comment(incident_id, body)


@router.get(
    "/incidents/{incident_id}/comments",
    response_model=CommentListResponse,
)
def list_comments(
    incident_id: str,
    svc: CommentService = Depends(_service),
) -> CommentListResponse:
    return svc.list_comments(incident_id)


@router.delete("/comments/{comment_id}", status_code=204)
def delete_comment(
    comment_id: str,
    svc: CommentService = Depends(_service),
) -> None:
    svc.delete_comment(comment_id)


@router.post(
    "/evidence/{evidence_id}/annotations",
    response_model=AnnotationItem,
    status_code=201,
)
def create_annotation(
    evidence_id: str,
    body: AnnotationCreate,
    svc: CommentService = Depends(_service),
) -> AnnotationItem:
    return svc.create_annotation(evidence_id, body)


@router.get(
    "/evidence/{evidence_id}/annotations",
    response_model=AnnotationListResponse,
)
def list_annotations(
    evidence_id: str,
    svc: CommentService = Depends(_service),
) -> AnnotationListResponse:
    return svc.list_annotations(evidence_id)
