"""Authentication, membership plans, and translation credit billing."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, Header, HTTPException, status

from backend.database import get_conn, next_month_iso, utc_now, utc_now_iso

OWNER_EMAIL = "hauhuythh@gmail.com"

PLANS: dict[str, dict[str, Any]] = {
    "free": {
        "id": "free",
        "name": "Hội viên Free",
        "price_vnd": 0,
        "monthly_quota": 10,
        "badge": "Miễn phí",
        "features": [
            "10 lần dịch video / tháng",
            "Hỗ trợ Bilibili, YouTube, TikTok (<10 phút)",
            "AI dịch thuật 2 lượt chuẩn ngữ cảnh",
            "1 thanh che mờ cố định + Lồng tiếng Việt",
        ],
    },
    "pro": {
        "id": "pro",
        "name": "Hội viên Pro",
        "price_vnd": 50000,
        "monthly_quota": 30,
        "badge": "Phổ biến",
        "features": [
            "30 lần dịch video / tháng",
            "Ưu tiên hàng đợi xử lý nhanh",
            "Tùy chỉnh giọng đọc & nhạc nền (<30%)",
            "Chỉnh sửa bản dịch Lượt 2 & Render lại miễn phí",
        ],
    },
    "ultra": {
        "id": "ultra",
        "name": "Hội viên Ultra",
        "price_vnd": 100000,
        "monthly_quota": 60,
        "badge": "Cao cấp",
        "features": [
            "60 lần dịch video / tháng",
            "Tốc độ xử lý & xuất video tối đa",
            "Lưu trữ lịch sử dự án & Tool Portable",
            "Hỗ trợ đầy đủ video Tiếng Trung & Tiếng Anh",
        ],
    },
    "owner": {
        "id": "owner",
        "name": "Chủ Sở Hữu (Owner)",
        "price_vnd": 0,
        "monthly_quota": 999999,
        "badge": "OWNER",
        "features": [
            "Vô hạn lượt dịch video vĩnh viễn",
            "Toàn quyền quản lý thành viên & lịch sử nạp",
            "Bật / Tắt chế độ Bảo trì Server",
        ],
    },
}

SINGLE_CREDIT_PRICE_VND = 3000


def _hash_password(password: str, salt: str | None = None) -> str:
    salt_hex = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_hex.encode("utf-8"),
        120_000,
    ).hex()
    return f"{salt_hex}${digest}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, expected_digest = stored_hash.split("$", 1)
    except ValueError:
        return False
    candidate = _hash_password(password, salt_hex)
    return hmac.compare_digest(candidate, stored_hash)


def _refresh_cycle_if_needed(conn: Any, user_row: Any) -> Any:
    try:
        reset_dt = datetime.fromisoformat(user_row["cycle_reset_at"])
        if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)
    except Exception:
        reset_dt = utc_now()

    now = utc_now()
    if now >= reset_dt:
        new_reset = next_month_iso(now)
        conn.execute(
            "UPDATE users SET monthly_used = 0, cycle_reset_at = ? WHERE id = ?",
            (new_reset, user_row["id"]),
        )
        user_row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_row["id"],)
        ).fetchone()
    return user_row


def serialize_user(user_row: Any) -> dict[str, Any]:
    """Format user row with computed remaining credits and role flags."""
    email_lower = str(user_row["email"]).strip().lower()
    row_keys = user_row.keys()
    raw_role = user_row["role"] if "role" in row_keys else "member"
    role = "owner" if email_lower == OWNER_EMAIL or raw_role == "owner" else str(raw_role or "member")
    is_banned = bool(user_row["is_banned"]) if "is_banned" in row_keys else False

    plan_id = "owner" if role == "owner" else str(user_row["plan"] or "free")
    plan_info = PLANS.get(plan_id, PLANS["free"])

    monthly_quota = int(user_row["monthly_quota"])
    monthly_used = int(user_row["monthly_used"])
    extra_credits = int(user_row["extra_credits"])
    is_owner = role == "owner"
    is_unlimited = is_owner or plan_id == "owner" or monthly_quota >= 999999

    if is_unlimited:
        monthly_quota = 999999
        monthly_remaining = 999999
        extra_credits = 999999
        total_remaining = 999999
    else:
        monthly_remaining = max(0, monthly_quota - monthly_used)
        total_remaining = monthly_remaining + extra_credits

    return {
        "id": user_row["id"],
        "email": user_row["email"],
        "full_name": user_row["full_name"],
        "role": role,
        "is_owner": is_owner,
        "is_unlimited": is_unlimited,
        "is_banned": is_banned,
        "plan": plan_id,
        "plan_name": plan_info["name"],
        "monthly_quota": monthly_quota,
        "monthly_used": monthly_used,
        "monthly_remaining": monthly_remaining,
        "extra_credits": extra_credits,
        "total_remaining": total_remaining,
        "cycle_reset_at": user_row["cycle_reset_at"],
        "created_at": user_row["created_at"],
    }


def register_user(email: str, full_name: str, password: str) -> dict[str, Any]:
    """Create a new Free-tier account (or Owner if OWNER_EMAIL) and issue a session token."""
    clean_email = email.strip().lower()
    clean_name = full_name.strip() or clean_email.split("@")[0]
    if len(password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mật khẩu phải có ít nhất 6 ký tự.",
        )

    is_owner = clean_email == OWNER_EMAIL
    role = "owner" if is_owner else "member"
    plan = "owner" if is_owner else "free"
    quota = 999999 if is_owner else 10
    extra = 999999 if is_owner else 0

    now = utc_now_iso()
    reset_at = next_month_iso()
    pw_hash = _hash_password(password)

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE LOWER(email) = ?", (clean_email,)
        ).fetchone()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email này đã được đăng ký. Vui lòng đăng nhập.",
            )

        cursor = conn.execute(
            """
            INSERT INTO users (
                email, full_name, password_hash, role, is_banned, plan,
                monthly_quota, monthly_used, extra_credits, cycle_reset_at, created_at
            ) VALUES (?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?)
            """,
            (clean_email, clean_name, pw_hash, role, plan, quota, extra, reset_at, now),
        )
        user_id = cursor.lastrowid
        token = _create_session_row(conn, user_id)
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    return {"token": token, "user": serialize_user(user_row)}


def login_user(email: str, password: str) -> dict[str, Any]:
    """Verify credentials and return session token + user profile."""
    clean_email = email.strip().lower()
    with get_conn() as conn:
        user_row = conn.execute(
            "SELECT * FROM users WHERE LOWER(email) = ?", (clean_email,)
        ).fetchone()
        if not user_row or not _verify_password(password, user_row["password_hash"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Email hoặc mật khẩu không chính xác.",
            )
        if clean_email == OWNER_EMAIL and user_row["role"] != "owner":
            conn.execute(
                """
                UPDATE users
                SET role = 'owner', plan = 'owner', monthly_quota = 999999,
                    extra_credits = 999999, is_banned = 0
                WHERE id = ?
                """,
                (user_row["id"],),
            )
            user_row = conn.execute("SELECT * FROM users WHERE id = ?", (user_row["id"],)).fetchone()
        elif bool(user_row["is_banned"]):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tài khoản này đã bị khóa bởi Owner.",
            )

        user_row = _refresh_cycle_if_needed(conn, user_row)
        token = _create_session_row(conn, user_row["id"])

    return {"token": token, "user": serialize_user(user_row)}


def logout_token(token: str) -> None:
    """Invalidate a session token."""
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def _create_session_row(conn: Any, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = utc_now()
    expires = (now + timedelta(days=30)).isoformat()
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (token, user_id, expires, now.isoformat()),
    )
    return token


def get_user_by_token(token: str | None) -> dict[str, Any] | None:
    """Return serialized user for a valid bearer token, or None."""
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT u.*, s.expires_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ?
            """,
            (token,),
        ).fetchone()
        if not row:
            return None

        try:
            exp = datetime.fromisoformat(row["expires_at"])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
        except Exception:
            exp = utc_now() - timedelta(seconds=1)

        if utc_now() >= exp:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None

        if bool(row["is_banned"]) and str(row["email"]).strip().lower() != OWNER_EMAIL:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None

        refreshed = _refresh_cycle_if_needed(conn, row)
        return serialize_user(refreshed)


def require_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """FastAPI dependency that requires a valid Bearer token."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Vui lòng đăng nhập để sử dụng tính năng dịch video.",
        )
    token = authorization.split(" ", 1)[1].strip()
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Phiên đăng nhập đã hết hạn hoặc tài khoản bị khóa. Vui lòng đăng nhập lại.",
        )
    return user


def require_owner_user(user: dict[str, Any] = Depends(require_current_user)) -> dict[str, Any]:
    """FastAPI dependency restricting endpoint access strictly to the Owner account."""
    if not user.get("is_owner"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chỉ tài khoản Owner (hauhuythh@gmail.com) mới có quyền thực hiện thao tác này.",
        )
    return user


def consume_translation_credit(user_id: int) -> tuple[str, dict[str, Any]]:
    """
    Deduct 1 translation credit from the user.
    Owner or unlimited plans never get deducted.
    Returns (source_used: 'unlimited' | 'monthly' | 'extra', updated_user_dict).
    """
    with get_conn() as conn:
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user_row:
            raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")

        serialized = serialize_user(user_row)
        if serialized["is_unlimited"]:
            return "unlimited", serialized

        user_row = _refresh_cycle_if_needed(conn, user_row)
        monthly_remaining = max(0, int(user_row["monthly_quota"]) - int(user_row["monthly_used"]))
        extra_credits = int(user_row["extra_credits"])

        if monthly_remaining > 0:
            conn.execute(
                "UPDATE users SET monthly_used = monthly_used + 1 WHERE id = ?",
                (user_id,),
            )
            source = "monthly"
        elif extra_credits > 0:
            conn.execute(
                "UPDATE users SET extra_credits = extra_credits - 1 WHERE id = ?",
                (user_id,),
            )
            source = "extra"
        else:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Bạn đã hết lượt dịch! Vui lòng nâng cấp gói Pro/Ultra hoặc mua lẻ 3.000đ/lần.",
            )

        updated = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return source, serialize_user(updated)


def refund_translation_credit(user_id: int, source: str = "monthly") -> None:
    """Refund 1 translation credit when a job fails before completion."""
    if source == "unlimited":
        return
    with get_conn() as conn:
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user_row:
            return
        if source == "monthly" and int(user_row["monthly_used"]) > 0:
            conn.execute(
                "UPDATE users SET monthly_used = monthly_used - 1 WHERE id = ?",
                (user_id,),
            )
        else:
            conn.execute(
                "UPDATE users SET extra_credits = extra_credits + 1 WHERE id = ?",
                (user_id,),
            )


def upgrade_membership_plan(user_id: int, target_plan: str) -> dict[str, Any]:
    """Upgrade user's membership plan (free / pro / ultra) and record transaction."""
    if target_plan not in ("free", "pro", "ultra"):
        raise HTTPException(status_code=400, detail="Gói hội viên không hợp lệ.")

    plan = PLANS[target_plan]
    now = utc_now_iso()
    reset_at = next_month_iso()

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE users
            SET plan = ?, monthly_quota = ?, monthly_used = 0, cycle_reset_at = ?
            WHERE id = ?
            """,
            (plan["id"], plan["monthly_quota"], reset_at, user_id),
        )
        conn.execute(
            """
            INSERT INTO transactions (user_id, tx_type, amount_vnd, credits_added, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                f"upgrade_{plan['id']}",
                plan["price_vnd"],
                plan["monthly_quota"],
                f"Kích hoạt {plan['name']} ({plan['monthly_quota']} lần dịch/tháng)",
                now,
            ),
        )
        updated = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    return {
        "user": serialize_user(updated),
        "message": f"Đã kích hoạt {plan['name']} thành công ({plan['monthly_quota']} lần/tháng)!",
    }


def purchase_single_credits(user_id: int, quantity: int) -> dict[str, Any]:
    """Purchase extra pay-per-use translation credits at 3,000 VND each."""
    if quantity < 1 or quantity > 500:
        raise HTTPException(
            status_code=400,
            detail="Số lượng lượt mua lẻ phải từ 1 đến 500.",
        )

    total_vnd = quantity * SINGLE_CREDIT_PRICE_VND
    now = utc_now_iso()

    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET extra_credits = extra_credits + ? WHERE id = ?",
            (quantity, user_id),
        )
        conn.execute(
            """
            INSERT INTO transactions (user_id, tx_type, amount_vnd, credits_added, description, created_at)
            VALUES (?, 'buy_credits', ?, ?, ?, ?)
            """,
            (
                user_id,
                total_vnd,
                quantity,
                f"Mua lẻ {quantity} lượt dịch ({total_vnd:,}đ)",
                now,
            ),
        )
        updated = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    return {
        "user": serialize_user(updated),
        "amount_vnd": total_vnd,
        "message": f"Đã nạp thêm {quantity} lượt dịch lẻ ({total_vnd:,}đ) vào tài khoản!",
    }


def list_user_transactions(user_id: int, limit: int = 15) -> list[dict[str, Any]]:
    """Fetch recent billing transactions for a user."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, tx_type, amount_vnd, credits_added, description, created_at
            FROM transactions
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def admin_list_users() -> list[dict[str, Any]]:
    """Return all registered users with spending & job statistics for Owner dashboard."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                u.*,
                COALESCE(tx.total_spent, 0) AS total_spent_vnd,
                COALESCE(tx.tx_count, 0) AS tx_count,
                COALESCE(jb.jobs_count, 0) AS jobs_count
            FROM users u
            LEFT JOIN (
                SELECT user_id, SUM(amount_vnd) AS total_spent, COUNT(*) AS tx_count
                FROM transactions
                GROUP BY user_id
            ) tx ON tx.user_id = u.id
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS jobs_count
                FROM translation_jobs
                GROUP BY user_id
            ) jb ON jb.user_id = u.id
            ORDER BY (LOWER(u.email) = ?) DESC, u.id DESC
            """,
            (OWNER_EMAIL,),
        ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        item = serialize_user(row)
        item["total_spent_vnd"] = int(row["total_spent_vnd"] or 0)
        item["tx_count"] = int(row["tx_count"] or 0)
        item["jobs_count"] = int(row["jobs_count"] or 0)
        result.append(item)
    return result


def admin_update_user(
    target_user_id: int,
    plan: str | None = None,
    extra_credits: int | None = None,
    monthly_quota: int | None = None,
    reset_used: bool = False,
    is_banned: bool | None = None,
    credit_delta: int | None = None,
    note: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Update a user's plan, credits, quota, role, or ban state (Owner only)."""
    now = utc_now_iso()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy thành viên này.")

        target_email = str(row["email"]).strip().lower()
        is_primary_owner = target_email == OWNER_EMAIL

        updates: dict[str, Any] = {}
        log_parts: list[str] = []
        delta_logged = 0

        if plan is not None:
            if plan not in PLANS:
                raise HTTPException(status_code=400, detail="Gói hội viên không hợp lệ.")
            if is_primary_owner and plan != "owner":
                raise HTTPException(status_code=400, detail="Không thể hạ cấp tài khoản Owner gốc.")
            updates["plan"] = plan
            if monthly_quota is None:
                updates["monthly_quota"] = PLANS[plan]["monthly_quota"]
            if plan == "owner":
                updates["role"] = "owner"
                updates["monthly_quota"] = 999999
                updates["extra_credits"] = 999999
            log_parts.append(f"Đổi gói -> {PLANS[plan]['name']}")

        if role is not None:
            if role not in ("member", "admin", "owner"):
                raise HTTPException(status_code=400, detail="Vai trò không hợp lệ.")
            if is_primary_owner and role != "owner":
                raise HTTPException(status_code=400, detail="Không thể thay đổi quyền của Owner gốc.")
            updates["role"] = role
            if role == "owner":
                updates["plan"] = "owner"
                updates["monthly_quota"] = 999999
                updates["extra_credits"] = 999999
            log_parts.append(f"Quyền -> {role}")

        if monthly_quota is not None and not (is_primary_owner or updates.get("role") == "owner"):
            updates["monthly_quota"] = max(0, min(999999, int(monthly_quota)))

        if reset_used:
            updates["monthly_used"] = 0
            log_parts.append("Reset lượt tháng đã dùng")

        if extra_credits is not None and not (is_primary_owner or updates.get("role") == "owner"):
            updates["extra_credits"] = max(0, min(999999, int(extra_credits)))
            log_parts.append(f"Đặt lượt lẻ = {updates['extra_credits']}")

        if credit_delta is not None and int(credit_delta) != 0 and not is_primary_owner:
            cur_extra = int(updates.get("extra_credits", row["extra_credits"]))
            updates["extra_credits"] = max(0, min(999999, cur_extra + int(credit_delta)))
            delta_logged = int(credit_delta)
            sign = "+" if delta_logged > 0 else ""
            log_parts.append(f"Owner cấp {sign}{delta_logged} lượt")

        if is_banned is not None:
            if is_primary_owner and is_banned:
                raise HTTPException(status_code=400, detail="Không thể khóa tài khoản Owner gốc.")
            updates["is_banned"] = 1 if is_banned else 0
            if is_banned:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (target_user_id,))
            log_parts.append("Khóa tài khoản" if is_banned else "Mở khóa tài khoản")

        if updates:
            assigns = ", ".join(f"{col} = ?" for col in updates)
            vals = list(updates.values()) + [target_user_id]
            conn.execute(f"UPDATE users SET {assigns} WHERE id = ?", vals)

        if log_parts or note:
            desc = (note.strip() if note and note.strip() else " • ".join(log_parts)) or "Owner cập nhật tài khoản"
            conn.execute(
                """
                INSERT INTO transactions (user_id, tx_type, amount_vnd, credits_added, description, created_at)
                VALUES (?, 'owner_adjust', 0, ?, ?, ?)
                """,
                (target_user_id, delta_logged, f"[OWNER] {desc}", now),
            )

        updated = conn.execute("SELECT * FROM users WHERE id = ?", (target_user_id,)).fetchone()
    return serialize_user(updated)


def admin_delete_user(target_user_id: int) -> dict[str, Any]:
    """Delete a non-Owner user account and all related records."""
    with get_conn() as conn:
        row = conn.execute("SELECT id, email FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy thành viên.")
        if str(row["email"]).strip().lower() == OWNER_EMAIL:
            raise HTTPException(status_code=400, detail="Không thể xóa tài khoản Owner gốc!")
        conn.execute("DELETE FROM users WHERE id = ?", (target_user_id,))
    return {"ok": True, "deleted_user_id": target_user_id, "deleted_email": row["email"]}


def admin_list_transactions(user_id: int | None = None, limit: int = 120) -> list[dict[str, Any]]:
    """Return purchase/deposit transactions across all users or for a specific member."""
    with get_conn() as conn:
        if user_id is not None:
            rows = conn.execute(
                """
                SELECT t.*, u.email AS user_email, u.full_name AS user_name, u.plan AS user_plan
                FROM transactions t
                JOIN users u ON u.id = t.user_id
                WHERE t.user_id = ?
                ORDER BY t.created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT t.*, u.email AS user_email, u.full_name AS user_name, u.plan AS user_plan
                FROM transactions t
                JOIN users u ON u.id = t.user_id
                ORDER BY t.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]
