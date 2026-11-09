"""media/web/backend/api/admin_router.py - 管理员邀请码生成与列表管理。"""
from __future__ import annotations

import secrets
import string
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from media.database import session_scope, InviteCode, User
from ..auth import get_current_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _generate_code(length: int = 12) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class CreateInviteRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=20)


@router.post("/invites")
def create_invites(req: CreateInviteRequest, admin: User = Depends(get_current_admin)):
    """管理员生成新的邀请码"""
    created_codes = []
    with session_scope() as db:
        for _ in range(req.count):
            code_str = _generate_code()
            invite = InviteCode(
                code=code_str,
                created_by=admin.id,
                status="active",
            )
            db.add(invite)
            created_codes.append(code_str)

    return {"codes": created_codes, "message": f"成功生成 {len(created_codes)} 个邀请码"}


@router.get("/invites")
def list_invites(admin: User = Depends(get_current_admin)):
    """管理员查看所有邀请码状态"""
    with session_scope() as db:
        invites = db.query(InviteCode).order_by(InviteCode.created_at.desc()).limit(100).all()
        results = []
        for inv in invites:
            consumer_name = inv.consumer.username if inv.consumer else None
            results.append({
                "id": inv.id,
                "code": inv.code,
                "status": inv.status,
                "created_at": inv.created_at.isoformat() if inv.created_at else None,
                "used_at": inv.used_at.isoformat() if inv.used_at else None,
                "used_by_username": consumer_name,
            })
        return {"invites": results}


@router.post("/invites/{code}/revoke")
def revoke_invite(code: str, admin: User = Depends(get_current_admin)):
    """撤销未使用的邀请码"""
    with session_scope() as db:
        invite = db.query(InviteCode).filter(InviteCode.code == code).first()
        if not invite:
            raise HTTPException(status_code=404, detail="邀请码不存在")
        if invite.status == "used":
            raise HTTPException(status_code=400, detail="该邀请码已被使用，无法撤销")
        invite.status = "revoked"
        return {"message": "邀请码已撤销"}
