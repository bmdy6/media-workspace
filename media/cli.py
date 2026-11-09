"""media/cli.py - Media 命令行管理与运维工具。

用法:
  # 1. 创建初始管理员账号
  python -m media.cli admin create admin

  # 2. 生成邀请码发给朋友
  python -m media.cli invite create
  python -m media.cli invite create --count 5

  # 3. 查看邀请码使用情况
  python -m media.cli invite list

  # 4. 终端直接测试生图
  python -m media.cli test-gen "深色科技感芯片海报" --style tech_poster --ratio 3:4
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import string
import sys
from pathlib import Path

# 确保根目录在 sys.path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from media.database import init_db, session_scope, User, InviteCode
from media.web.backend.auth import hash_password
from media.core.image_service import image_core

init_db()


def _gen_code(length: int = 12) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def handle_admin_create(args: argparse.Namespace) -> None:
    username = args.username.strip()
    password = args.password.strip()
    if len(password) < 6 or len(password.encode("utf-8")) > 72:
        raise ValueError("Password must contain at least 6 characters and at most 72 UTF-8 bytes")
    with session_scope() as db:
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            existing.password_hash = hash_password(password)
            existing.is_admin = True
            print(f"[OK] 管理员 {username} 已存在，已重置密码并确保管理员权限。")
            return

        admin = User(
            username=username,
            password_hash=hash_password(password),
            is_admin=True,
        )
        db.add(admin)
        print(f"[OK] 成功创建管理员账号: {username}")


def handle_invite_create(args: argparse.Namespace) -> None:
    count = args.count
    with session_scope() as db:
        # 获取任意管理员 ID，若无则设为 None
        admin = db.query(User).filter(User.is_admin == True).first()
        admin_id = admin.id if admin else None

        codes = []
        for _ in range(count):
            c = _gen_code()
            invite = InviteCode(code=c, created_by=admin_id, status="active")
            db.add(invite)
            codes.append(c)

    print(f"\n[OK] 成功生成 {count} 个邀请码 (发给朋友注册即用):")
    for c in codes:
        print(f"  ➜  {c}")
    print()


def handle_invite_list(args: argparse.Namespace) -> None:
    with session_scope() as db:
        invites = db.query(InviteCode).order_by(InviteCode.created_at.desc()).limit(50).all()
        if not invites:
            print("当前暂无邀请码。使用 `python -m media.cli invite create` 生成。")
            return

        print(f"\n{'ID':<5} {'邀请码':<16} {'状态':<10} {'使用者':<12} {'创建时间':<20}")
        print("-" * 65)
        for inv in invites:
            user_name = inv.consumer.username if inv.consumer else "-"
            c_time = inv.created_at.strftime("%Y-%m-%d %H:%M") if inv.created_at else "-"
            print(f"{inv.id:<5} {inv.code:<16} {inv.status:<10} {user_name:<12} {c_time:<20}")
        print()


def handle_test_gen(args: argparse.Namespace) -> None:
    print(f"[Core] 开始执行生图任务: {args.prompt} (style={args.style}, ratio={args.ratio})...")
    res = image_core.generate(
        prompt=args.prompt,
        style=args.style,
        aspect_ratio=args.ratio,
        model_id=args.model_id,
        storage_policy=args.storage_policy,
        request_id=args.request_id,
    )
    if res.status == "success":
        print(f"[OK] 生图成功 ({res.duration_ms}ms)!")
        print(f"模型: {res.model_id}")
        print(f"COS 访问地址: {res.image_url}")
    else:
        print(f"[ERR] 生图失败: {res.error_msg}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m media.cli", description="Media 基础设施命令行运维工具")
    subparsers = parser.add_subparsers(dest="cmd")

    # admin 子命令
    admin_parser = subparsers.add_parser("admin", help="管理员用户管理")
    admin_sub = admin_parser.add_subparsers(dest="admin_cmd")
    admin_create = admin_sub.add_parser("create", help="创建或重置管理员账号")
    admin_create.add_argument("username", help="管理员用户名")
    admin_create.add_argument("password", nargs="?", help="管理员密码（省略时安全输入）")

    # invite 子命令
    invite_parser = subparsers.add_parser("invite", help="邀请码管理")
    invite_sub = invite_parser.add_subparsers(dest="invite_cmd")
    invite_create = invite_sub.add_parser("create", help="生成新邀请码")
    invite_create.add_argument("--count", type=int, default=1, help="生成的邀请码数量")
    invite_sub.add_parser("list", help="查看邀请码列表")

    # test-gen 子命令
    gen_parser = subparsers.add_parser("test-gen", help="测试底座生图能力")
    gen_parser.add_argument("prompt", help="生图提示词")
    gen_parser.add_argument("--style", default="default", help="风格预设")
    gen_parser.add_argument("--ratio", help="画幅比例（默认读取运行配置）")
    gen_parser.add_argument("--model-id", help="能力目录中的生图模型 ID")
    gen_parser.add_argument("--storage-policy", help="能力目录中的转存策略 ID")
    gen_parser.add_argument("--request-id", help="调用方请求标识（最多 128 字符）")

    args = parser.parse_args()
    if args.cmd == "admin" and getattr(args, "admin_cmd", None) == "create":
        args.password = args.password or getpass.getpass("管理员密码: ")
        handle_admin_create(args)
    elif args.cmd == "invite" and getattr(args, "invite_cmd", None) == "create":
        handle_invite_create(args)
    elif args.cmd == "invite" and getattr(args, "invite_cmd", None) == "list":
        handle_invite_list(args)
    elif args.cmd == "test-gen":
        handle_test_gen(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
