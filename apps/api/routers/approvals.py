"""Approval endpoints — list, approve, reject, batch, email token."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from apps.api.dependencies import (
    ResumeTaskEnqueue,
    get_db,
    get_resume_task_enqueue,
)
from apps.api.schemas.approvals import (
    ApprovalDecisionResponse,
    ApprovalItem,
    ApproveRequest,
    BatchApprovalRequest,
    RejectRequest,
    TokenApprovalRequest,
)
from apps.api.schemas.common import PaginatedResponse
from apps.api.services.approval_service import ApprovalService
from packages.common.errors import AppError, ValidationAppError

router = APIRouter(prefix="/api", tags=["approvals"])
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def _validate_redirect(url: str) -> None:
    """Reject non-relative redirect targets to prevent open-redirect attacks."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc or url.startswith("//"):
        raise ValidationAppError(
            "redirect target must be a relative path",
            details={"url": url},
        )


def _service(db: Session, enqueue_resume: ResumeTaskEnqueue | None) -> ApprovalService:
    return ApprovalService(db, enqueue_resume=enqueue_resume)


@router.get("/approvals", response_model=PaginatedResponse)
def list_approvals(
    status: str | None = None,
    incident_id: str | None = None,
    service: str | None = None,
    risk_level: str | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> PaginatedResponse:
    return _service(db, enqueue_resume).list_approvals(
        status=status,
        incident_id=incident_id,
        service=service,
        risk_level=risk_level,
        page=page,
        page_size=page_size,
    )


@router.get("/approvals/{approval_id}", response_model=ApprovalItem)
def get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> ApprovalItem:
    return _service(db, enqueue_resume).get_approval(approval_id)


@router.get("/incidents/{incident_id}/approvals", response_model=list[ApprovalItem])
def list_incident_approvals(
    incident_id: str,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> list[ApprovalItem]:
    return _service(db, enqueue_resume).list_for_incident(incident_id)


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=ApprovalDecisionResponse,
)
def approve_action(
    approval_id: str,
    request: ApproveRequest,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> ApprovalDecisionResponse:
    return _service(db, enqueue_resume).approve(approval_id, request)


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=ApprovalDecisionResponse,
)
def reject_action(
    approval_id: str,
    request: RejectRequest,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> ApprovalDecisionResponse:
    return _service(db, enqueue_resume).reject(approval_id, request)


@router.post(
    "/approvals/batch",
    response_model=list[ApprovalDecisionResponse],
)
def batch_decide(
    request: BatchApprovalRequest,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> list[ApprovalDecisionResponse]:
    return _service(db, enqueue_resume).batch_decide(request)


@router.post(
    "/approvals/{approval_id}/email-token",
    response_model=dict,
)
def generate_email_token(
    approval_id: str,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> dict[str, str]:
    token = _service(db, enqueue_resume).generate_email_token(approval_id)
    return {"approval_id": approval_id, "email_token": token}


@router.get("/approvals/by-token/{token}")
def get_by_token(
    token: str,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> RedirectResponse:
    """Redirect from email token to the frontend approval page."""
    svc = _service(db, enqueue_resume)
    approval = svc.get_approval_by_token(token)
    return RedirectResponse(
        url=f"/approvals/{approval.approval_id}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Phase 2: email one-click approval confirmation pages (GET → POST)
# ---------------------------------------------------------------------------

_CONFIRMATION_PAGE = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{
    font:16px/1.5 system-ui,sans-serif;
    max-width:480px;margin:60px auto;padding:20px;color:#1e293b
  }}
  h2{{margin:0 0 8px}}
  button{{
    padding:10px 24px;font-size:16px;border-radius:6px;
    border:none;cursor:pointer;color:#fff
  }}
  .approve{{background:#16a34a}} .reject{{background:#dc2626}}
  .detail{{
    background:#f8fafc;padding:12px;border-radius:6px;
    margin:12px 0;border:1px solid #e2e8f0
  }}
  .detail p{{margin:4px 0}}
  label{{display:block;margin:8px 0}}
  input{{
    padding:6px 10px;font-size:15px;border:1px solid #cbd5e1;
    border-radius:4px;width:100%;box-sizing:border-box
  }}
  .links{{margin-top:16px;font-size:14px}}
  #msg{{margin-top:12px;padding:8px 12px;border-radius:6px;display:none}}
  .msg-error{{
    background:#fef2f2;border:1px solid #fecaca;
    color:#991b1b;display:block!important
  }}
  .msg-success{{
    background:#f0fdf4;border:1px solid #bbf7d0;
    color:#166534;display:block!important
  }}
</style></head><body>
<h2>{title}</h2>
<div class="detail">
<p><strong>Action:</strong> {action_type}</p>
<p><strong>Risk:</strong> {risk_level}</p>
</div>
<form id="af" method="post" action="{action_url}">
<label>Approver: <input name="approver" placeholder="your name" required></label>
<button type="submit" class="{btn_class}">{btn_text}</button>
</form>
<div id="msg"></div>
<div class="links">
<a href="/approvals/{approval_id}">Open in console</a>
</div>
<script>
document.getElementById('af').addEventListener('submit',async function(e){{
  e.preventDefault();
  var m=document.getElementById('msg'),b=e.target.querySelector('button');
  m.className='';m.textContent='Submitting...';
  m.style.display='block';b.disabled=true;
  try{{
    var r=await fetch(this.action,{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{approver:this.approver.value}})
    }});
    if(r.redirected){{window.location=r.url;return}}
    if(r.ok){{
      m.className='msg-success';
      m.textContent='Done! Redirecting...';
      setTimeout(function(){{window.location='/approvals'}},1500)
    }}else{{
      var d=await r.json().catch(
        function(){{return{{error:{{message:'Request failed'}}}}}});
      m.className='msg-error';
      m.textContent=(d.error||d).message||'Request failed';
      b.disabled=false
    }}
  }}catch(err){{
    m.className='msg-error';
    m.textContent='Network error: '+err.message;
    b.disabled=false
  }}
}});
</script>
</body></html>"""

_ERROR_PAGE = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Unavailable</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font:16px/1.5 system-ui,sans-serif;max-width:480px;margin:60px auto;padding:20px}}
  .error{{background:#fef2f2;border:1px solid #fecaca;padding:12px;border-radius:6px;color:#991b1b}}
</style></head><body>
<div class="error"><h2>Unavailable</h2><p>{message}</p></div>
<p><a href="/approvals">Back to approvals</a></p>
</body></html>"""


@router.get("/approvals/by-token/{token}/approve")
def approve_by_token_page(
    token: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Confirmation page for email-based approval — GET-safe, then user POSTs."""
    svc = _service(db, enqueue_resume=None)
    try:
        approval = svc.get_approval_by_token(token)
    except AppError as e:
        return HTMLResponse(
            _ERROR_PAGE.format(message=e.message),
            status_code=400,
        )
    return HTMLResponse(_CONFIRMATION_PAGE.format(
        title="Approve Action",
        action_url=f"/api/approvals/by-token/{token}/approve"
                   f"?redirect=/incidents/{approval.incident_id}",
        token=token,
        incident_id=approval.incident_id,
        approval_id=approval.approval_id,
        action_type=approval.action_type,
        risk_level=approval.risk_level.value,
        btn_class="approve",
        btn_text="Confirm Approve",
    ))


@router.get("/approvals/by-token/{token}/reject")
def reject_by_token_page(
    token: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Confirmation page for email-based rejection — GET-safe, then user POSTs."""
    svc = _service(db, enqueue_resume=None)
    try:
        approval = svc.get_approval_by_token(token)
    except AppError as e:
        return HTMLResponse(
            _ERROR_PAGE.format(message=e.message),
            status_code=400,
        )
    return HTMLResponse(_CONFIRMATION_PAGE.format(
        title="Reject Action",
        action_url=f"/api/approvals/by-token/{token}/reject"
                   f"?redirect=/incidents/{approval.incident_id}",
        token=token,
        incident_id=approval.incident_id,
        approval_id=approval.approval_id,
        action_type=approval.action_type,
        risk_level=approval.risk_level.value,
        btn_class="reject",
        btn_text="Confirm Reject",
    ))


@router.post(
    "/approvals/by-token/{token}/approve",
    response_model=ApprovalDecisionResponse,
)
def approve_by_token(
    token: str,
    request: TokenApprovalRequest,
    redirect: str | None = None,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> ApprovalDecisionResponse | RedirectResponse:
    result = _service(db, enqueue_resume).approve_by_token(token, request)
    if redirect:
        _validate_redirect(redirect)
        return RedirectResponse(url=redirect, status_code=302)
    return result


@router.post(
    "/approvals/by-token/{token}/reject",
    response_model=ApprovalDecisionResponse,
)
def reject_by_token(
    token: str,
    request: TokenApprovalRequest,
    redirect: str | None = None,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> ApprovalDecisionResponse | RedirectResponse:
    result = _service(db, enqueue_resume).reject_by_token(token, request)
    if redirect:
        _validate_redirect(redirect)
        return RedirectResponse(url=redirect, status_code=302)
    return result
