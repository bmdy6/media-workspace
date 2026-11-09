"""media/web/backend/api/auth_router.py - 登录、邀请码注册与个人信息。"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Depends, Header, status

from pydantic import BaseModel, Field

from media.database import session_scope, User, InviteCode

from media.database.models import get_utc_now

from ..auth import hash_password, verify_password, create_access_token, get_current_user, user_from_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])

class LoginRequest(BaseModel):
    username: str
    password: str

class RegisterRequest(BaseModel):
    username: str
    password: str
    invite_code: str

class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict

@router.post("/login", response_model=AuthResponse)
def login(req: LoginRequest):
    """用户登录"""
    with session_scope() as db:
        user = db.query(User).filter(User.username == req.username.strip()).first()
        if not user or not verify_password(req.password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户名或密码错误",
            )

        token = create_access_token(data={"sub": user.username})
        return AuthResponse(
            access_token=token,
            user={
                "id": user.id,
                "username": user.username,
                "is_admin": user.is_admin,
            },
        )

@router.post("/register", response_model=AuthResponse)
def register(req: RegisterRequest):
    """通过邀请码注册新用户"""
    username = req.username.strip()
    invite_code_str = req.invite_code.strip()

    if len(username) < 2:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名至少2个字符")
    if len(req.password) < 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="密码至少6个字符")
    if len(req.password.encode("utf-8")) > 72:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="密码的 UTF-8 编码不能超过72字节")

    with session_scope() as db:
        # 1. 检查用户名重复
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名已存在")

        # 2. 校验邀请码
        invite = db.query(InviteCode).filter(
            InviteCode.code == invite_code_str,
            InviteCode.status == "active",
        ).first()
        if not invite:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="邀请码无效或已被使用，请向管理员索取新邀请码",
            )

        # Claim inside the same transaction as user creation. Two sessions may
        # both read an active invite; only one conditional update may consume it.
        claimed = db.query(InviteCode).filter(
            InviteCode.id == invite.id, InviteCode.status == "active",
        ).update({InviteCode.status: "used"}, synchronize_session=False)
        if claimed != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邀请码无效或已被使用，请向管理员索取新邀请码")

        # 3. 创建用户
        new_user = User(
            username=username,
            password_hash=hash_password(req.password),
            is_admin=False,
        )
        db.add(new_user)
        db.flush()

        # 4. 消费邀请码
        invite.status = "used"
        invite.used_by = new_user.id
        invite.used_at = get_utc_now()

        token = create_access_token(data={"sub": new_user.username})
        return AuthResponse(
            access_token=token,
            user={
                "id": new_user.id,
                "username": new_user.username,
                "is_admin": False,
            },
        )

@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    """获取当前已登录用户信息"""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "is_admin": current_user.is_admin,
    }
