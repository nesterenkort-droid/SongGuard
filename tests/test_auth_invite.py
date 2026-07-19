"""Auth + invite logic against a real (rolled-back) Postgres transaction."""

from sqlalchemy import func, select

from app.auth import service as auth_service
from app.config import settings
from app.models import AuditEvent, Invite, User


async def test_uninvited_login_rejected(db_session):
    nonce = await auth_service.create_nonce(auth_service.MODE_LOGIN)
    result = await auth_service.confirm_start(
        db_session, f"login-{nonce}", 999001, "Stranger"
    )
    assert result["ok"] is False
    user = await db_session.scalar(select(User).where(User.tg_user_id == 999001))
    assert user is None


async def test_invite_join_creates_user_and_consumes_invite(db_session):
    admin = User(tg_user_id=999500, display_name="Creator", is_admin=True)
    db_session.add(admin)
    await db_session.flush()
    invite = Invite(token="testtoken123", created_by_user_id=admin.id)
    db_session.add(invite)
    await db_session.flush()

    nonce = await auth_service.create_nonce(
        auth_service.MODE_JOIN, invite_token="testtoken123"
    )
    result = await auth_service.confirm_start(
        db_session, f"join-{nonce}", 999002, "Invitee"
    )
    assert result["ok"] is True
    assert result["registered"] is True

    user = await db_session.scalar(select(User).where(User.tg_user_id == 999002))
    assert user is not None
    assert user.is_admin is False

    used = await db_session.scalar(select(Invite).where(Invite.token == "testtoken123"))
    assert used.used_by_user_id == user.id

    count = await db_session.scalar(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.action == "user.register", AuditEvent.entity_id == user.id
        )
    )
    assert count >= 1


async def test_admin_bootstrap_without_invite(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_tg_ids", "777001")
    nonce = await auth_service.create_nonce(auth_service.MODE_LOGIN)
    result = await auth_service.confirm_start(
        db_session, f"login-{nonce}", 777001, "Boss"
    )
    assert result["ok"] is True
    user = await db_session.scalar(select(User).where(User.tg_user_id == 777001))
    assert user is not None
    assert user.is_admin is True
