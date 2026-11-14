"""FastAPI server for Zay_Vietsub.vn — Auto Chinese/English -> Vietnamese Video Studio."""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.auth import (
    OWNER_EMAIL,
    PLANS,
    SINGLE_CREDIT_PRICE_VND,
    admin_delete_user,
    admin_list_transactions,
    admin_list_users,
    admin_update_user,
    consume_translation_credit,
    list_user_transactions,
    login_user,
    logout_token,
    purchase_single_credits,
    refund_translation_credit,
    register_user,
    require_current_user,
    require_owner_user,
    upgrade_membership_plan,
)
from backend.database import (
    BASE_DIR,
    OUTPUTS_DIR,
    TEMP_DIR,
    UPLOADS_DIR,
    create_job_record,
    get_job_record,
    get_system_settings,
    init_db,
    list_user_jobs,
    serialize_public_job,
    set_system_maintenance,
    update_job_record,
)
from backend.video_pipeline import (
    generate_line_preview_mp3,
    generate_voice_preview_mp3,
    probe_media_info,
    render_final_video,
    run_translation_pipeline,
    sanitize_error_message,
    validate_safe_video_url,
)

STATIC_DIR = BASE_DIR / "static"
ALLOWED_VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".m4v"})
ALLOWED_AUDIO_EXTS = frozenset({".mp3", ".wav", ".m4a", ".ogg", ".aac"})
MAX_UPLOAD_VIDEO_BYTES = 150 * 1024 * 1024
MAX_UPLOAD_AUDIO_BYTES = 25 * 1024 * 1024
MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("MAX_CONCURRENT_JOBS", "1")))

_ACTIVE_JOB_LOCKS: set[str] = set()
_ACTIVE_JOBS_MUTEX = threading.Lock()
_RATE_BUCKETS: dict[str, list[float]] = defaultdict(list)
_RATE_LOCK = threading.Lock()

init_db()

app = FastAPI(
    title="Zay_Vietsub.vn",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_client_ip(request: Request) -> str:
    cf_ip = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf_ip:
        return cf_ip
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(request: Request, bucket: str, max_reqs: int, window_sec: int = 60) -> None:
    key = f"{bucket}:{_get_client_ip(request)}"
    now = time.monotonic()
    cutoff = now - window_sec
    with _RATE_LOCK:
        recent = [ts for ts in _RATE_BUCKETS[key] if ts > cutoff]
        if len(recent) >= max_reqs:
            _RATE_BUCKETS[key] = recent
            raise HTTPException(
                status_code=429,
                detail="Bạn thao tác quá nhanh, vui lòng thử lại sau 1 phút.",
            )
        recent.append(now)
        _RATE_BUCKETS[key] = recent


def _save_upload_limited(upload: UploadFile, dest: Path, max_bytes: int, label: str) -> None:
    total = 0
    try:
        with dest.open("wb") as out_f:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"{label} vượt quá dung lượng tối đa {max_bytes // (1024 * 1024)}MB.",
                    )
                out_f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=sanitize_error_message(exc)) from exc


@app.middleware("http")
async def add_security_headers(request: Request, call_next: Any) -> Any:
    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    if request.url.path in {"/", "/index.html", "/app.js", "/styles.css"}:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")


class RegisterPayload(BaseModel):
    email: str
    full_name: str
    password: str


class LoginPayload(BaseModel):
    email: str
    password: str


class UpgradePayload(BaseModel):
    plan: str


class BuyCreditsPayload(BaseModel):
    quantity: int


class RerenderPayload(BaseModel):
    pass2_subtitles: list[dict[str, Any]]
    settings: dict[str, Any]


class LinePreviewPayload(BaseModel):
    text_vi: str
    voice_id: str = "vi-female-review"
    speed: float = 1.20


class MaintenancePayload(BaseModel):
    enabled: bool
    message: str | None = None


class AdminUserUpdatePayload(BaseModel):
    plan: str | None = None
    extra_credits: int | None = None
    monthly_quota: int | None = None
    reset_used: bool = False
    is_banned: bool | None = None
    credit_delta: int | None = None
    note: str | None = None
    role: str | None = None


def _ensure_not_in_maintenance(user: dict[str, Any] | None = None) -> None:
    sys_state = get_system_settings()
    if sys_state["maintenance_mode"] and not (user and user.get("is_owner")):
        raise HTTPException(
            status_code=503,
            detail=sys_state["maintenance_message"],
        )


@app.get("/")
def serve_home() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/styles.css")
def serve_styles() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "styles.css"), media_type="text/css")


@app.get("/app.js")
def serve_app_js() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "app.js"), media_type="application/javascript")


@app.get("/Zay_Vietsub_Portable.zip")
def serve_portable_zip() -> StreamingResponse:
    return download_portable_tool()


@app.get("/api/health")
def health_check() -> dict[str, Any]:
    sys_state = get_system_settings()
    return {
        "status": "ok",
        "brand": "Zay_Vietsub.vn",
        "maintenance_mode": sys_state["maintenance_mode"],
    }


@app.get("/api/system/status")
def system_status() -> dict[str, Any]:
    return get_system_settings()


@app.get("/api/voices/preview")
def api_voice_preview(
    voice_id: str = "vi-female-review",
    speed: float = 1.20,
) -> FileResponse:
    mp3_path = generate_voice_preview_mp3(voice_id, speed=speed)
    return FileResponse(str(mp3_path), media_type="audio/mpeg")


@app.post("/api/tts/preview-line")
def api_preview_line(
    payload: LinePreviewPayload,
    request: Request,
) -> FileResponse:
    _check_rate_limit(request, "tts_preview", 30, 60)
    try:
        mp3_path = generate_line_preview_mp3(
            text_vi=payload.text_vi,
            voice_id=payload.voice_id,
            speed=payload.speed,
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve)) from ve
    except Exception as exc:
        raise HTTPException(status_code=500, detail=sanitize_error_message(exc)) from exc
    return FileResponse(str(mp3_path), media_type="audio/mpeg")


@app.get("/api/plans")
def get_plans() -> dict[str, Any]:
    return {
        "plans": [p for p in PLANS.values() if p["id"] != "owner"],
        "single_credit_price_vnd": SINGLE_CREDIT_PRICE_VND,
    }


@app.post("/api/auth/register")
def api_register(payload: RegisterPayload, request: Request) -> dict[str, Any]:
    _check_rate_limit(request, "auth", 12, 60)
    if payload.email.strip().lower() != OWNER_EMAIL:
        _ensure_not_in_maintenance()
    return register_user(payload.email, payload.full_name, payload.password)


@app.post("/api/auth/login")
def api_login(payload: LoginPayload, request: Request) -> dict[str, Any]:
    _check_rate_limit(request, "auth", 12, 60)
    if payload.email.strip().lower() != OWNER_EMAIL:
        _ensure_not_in_maintenance()
    return login_user(payload.email, payload.password)


@app.post("/api/auth/logout")
def api_logout(authorization: str | None = Header(default=None)) -> dict[str, bool]:
    if authorization and authorization.lower().startswith("bearer "):
        logout_token(authorization.split(" ", 1)[1].strip())
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(user: dict[str, Any] = Depends(require_current_user)) -> dict[str, Any]:
    return {
        "user": user,
        "plans": [p for p in PLANS.values() if p["id"] != "owner"],
        "single_credit_price_vnd": SINGLE_CREDIT_PRICE_VND,
        "maintenance": get_system_settings(),
    }


@app.post("/api/billing/upgrade")
def api_upgrade(
    payload: UpgradePayload,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, Any]:
    _ensure_not_in_maintenance(user)
    return upgrade_membership_plan(user["id"], payload.plan)


@app.post("/api/billing/buy-credits")
def api_buy_credits(
    payload: BuyCreditsPayload,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, Any]:
    _ensure_not_in_maintenance(user)
    return purchase_single_credits(user["id"], payload.quantity)


@app.get("/api/billing/history")
def api_billing_history(
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, Any]:
    return {"transactions": list_user_transactions(user["id"])}


@app.get("/api/admin/overview")
def api_admin_overview(
    owner: dict[str, Any] = Depends(require_owner_user),
) -> dict[str, Any]:
    users = admin_list_users()
    transactions = admin_list_transactions(limit=150)
    total_revenue = sum(int(tx.get("amount_vnd") or 0) for tx in transactions)
    return {
        "owner": owner,
        "users": users,
        "transactions": transactions,
        "maintenance": get_system_settings(),
        "total_revenue_vnd": total_revenue,
    }


@app.post("/api/admin/maintenance")
def api_admin_maintenance(
    payload: MaintenancePayload,
    owner: dict[str, Any] = Depends(require_owner_user),
) -> dict[str, Any]:
    state = set_system_maintenance(payload.enabled, payload.message)
    return {"ok": True, "maintenance": state}


@app.post("/api/admin/users/{target_id}/update")
def api_admin_update_user(
    target_id: int,
    payload: AdminUserUpdatePayload,
    owner: dict[str, Any] = Depends(require_owner_user),
) -> dict[str, Any]:
    updated = admin_update_user(
        target_user_id=target_id,
        plan=payload.plan,
        extra_credits=payload.extra_credits,
        monthly_quota=payload.monthly_quota,
        reset_used=payload.reset_used,
        is_banned=payload.is_banned,
        credit_delta=payload.credit_delta,
        note=payload.note,
        role=payload.role,
    )
    return {"ok": True, "user": updated}


@app.delete("/api/admin/users/{target_id}")
def api_admin_delete_user(
    target_id: int,
    owner: dict[str, Any] = Depends(require_owner_user),
) -> dict[str, Any]:
    return admin_delete_user(target_id)


@app.get("/api/admin/users/{target_id}/transactions")
def api_admin_user_transactions(
    target_id: int,
    owner: dict[str, Any] = Depends(require_owner_user),
) -> dict[str, Any]:
    return {"transactions": admin_list_transactions(user_id=target_id, limit=100)}


@app.post("/api/jobs/create")
async def api_create_job(
    request: Request,
    source_type: str = Form("url"),
    source_url: str = Form(""),
    source_lang: str = Form("auto"),
    auto_trim_10m: bool = Form(True),
    original_volume: float = Form(0.0),
    tts_volume: float = Form(100.0),
    tts_speed: float = Form(1.20),
    bgm_volume: float = Form(15.0),
    bgm_preset: str = Form("lofi"),
    voice_id: str = Form("vi-female-review"),
    blur_enabled: bool = Form(True),
    blur_x_pct: float = Form(4.0),
    blur_w_pct: float = Form(92.0),
    blur_y_pct: float = Form(77.0),
    blur_h_pct: float = Form(16.0),
    blur_radius: int = Form(22),
    video_file: UploadFile | None = File(default=None),
    bgm_file: UploadFile | None = File(default=None),
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, Any]:
    _ensure_not_in_maintenance(user)
    _check_rate_limit(request, "jobs", 8, 60)

    clean_url = source_url.strip()
    if source_type not in ("url", "upload"):
        raise HTTPException(status_code=400, detail="Chỉ hỗ trợ dán Link Video hoặc Tải File MP4.")
    if source_type == "url":
        if not clean_url:
            raise HTTPException(status_code=400, detail="Vui lòng dán link Bilibili, YouTube hoặc chọn tệp video.")
        try:
            clean_url = validate_safe_video_url(clean_url)
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve)) from ve
    if source_type == "upload" and video_file is None:
        raise HTTPException(status_code=400, detail="Vui lòng chọn tệp video để tải lên.")

    if source_type == "upload" and video_file is not None:
        ext_chk = (Path(video_file.filename or "upload.mp4").suffix or ".mp4").lower()
        if ext_chk not in ALLOWED_VIDEO_EXTS:
            raise HTTPException(
                status_code=400,
                detail="Chỉ hỗ trợ tải lên video định dạng .mp4, .mov, .mkv, .webm hoặc .m4v.",
            )

    if bgm_file is not None and bgm_file.filename:
        b_ext_chk = Path(bgm_file.filename).suffix.lower()
        if b_ext_chk not in ALLOWED_AUDIO_EXTS:
            raise HTTPException(
                status_code=400,
                detail="Nhạc nền chỉ hỗ trợ định dạng .mp3, .wav, .m4a, .ogg hoặc .aac.",
            )

    # Enforce strict < 30% BGM cap server-side
    safe_bgm_volume = max(0.0, min(30.0, float(bgm_volume)))
    safe_orig_volume = max(0.0, min(100.0, float(original_volume)))
    safe_tts_volume = max(20.0, min(150.0, float(tts_volume)))
    safe_tts_speed = max(0.75, min(2.0, float(tts_speed or 1.20)))

    job_id = secrets.token_hex(6)
    with _ACTIVE_JOBS_MUTEX:
        if len(_ACTIVE_JOB_LOCKS) >= MAX_CONCURRENT_JOBS:
            raise HTTPException(
                status_code=429,
                detail="Máy chủ đang xử lý tối đa video cùng lúc, vui lòng thử lại sau ít phút.",
            )
        _ACTIVE_JOB_LOCKS.add(job_id)

    settings = {
        "auto_trim_10m": bool(auto_trim_10m),
        "original_volume": safe_orig_volume,
        "tts_volume": safe_tts_volume,
        "tts_speed": safe_tts_speed,
        "bgm_volume": safe_bgm_volume,
        "bgm_preset": bgm_preset,
        "voice_id": voice_id,
        "blur_enabled": bool(blur_enabled),
        "blur_x_pct": blur_x_pct,
        "blur_w_pct": blur_w_pct,
        "blur_y_pct": blur_y_pct,
        "blur_h_pct": blur_h_pct,
        "blur_radius": blur_radius,
    }

    input_video_path: Path | None = None
    custom_bgm_path: Path | None = None
    thread_started = False

    try:
        title = "Đang trích xuất thông tin video..."
        if source_type == "upload" and video_file is not None:
            ext = (Path(video_file.filename or "upload.mp4").suffix or ".mp4").lower()
            dest = UPLOADS_DIR / f"{job_id}_input{ext}"
            _save_upload_limited(video_file, dest, MAX_UPLOAD_VIDEO_BYTES, "Video tải lên")
            input_video_path = dest
            title = Path(video_file.filename or "Video tải lên").stem[:120]

        if bgm_file is not None and bgm_file.filename:
            b_ext = Path(bgm_file.filename).suffix.lower()
            custom_bgm_path = UPLOADS_DIR / f"{job_id}_bgm{b_ext}"
            _save_upload_limited(bgm_file, custom_bgm_path, MAX_UPLOAD_AUDIO_BYTES, "Nhạc nền tải lên")
            settings["bgm_preset"] = "custom"

        credit_source, updated_user = consume_translation_credit(user["id"])

        create_job_record(
            job_id=job_id,
            user_id=user["id"],
            source_type=source_type,
            source_url=clean_url if source_type == "url" else None,
            source_lang=source_lang,
            title=title,
            settings=settings,
            input_video_path=str(input_video_path) if input_video_path else None,
            credit_deducted=0 if credit_source == "unlimited" else 1,
        )

        def _worker() -> None:
            try:
                run_translation_pipeline(
                    job_id=job_id,
                    source_type=source_type,
                    source_url=clean_url if source_type == "url" else None,
                    input_video_path=input_video_path,
                    source_lang=source_lang,
                    title=title,
                    settings=settings,
                    custom_bgm_path=custom_bgm_path,
                    on_failure_refund=lambda: refund_translation_credit(user["id"], credit_source),
                )
            except Exception as exc:
                refund_translation_credit(user["id"], credit_source)
                update_job_record(
                    job_id,
                    status="failed",
                    progress=100,
                    step_message="Khởi tạo tác vụ thất bại (đã hoàn lượt dịch)",
                    error_message=sanitize_error_message(exc),
                )
            finally:
                with _ACTIVE_JOBS_MUTEX:
                    _ACTIVE_JOB_LOCKS.discard(job_id)

        threading.Thread(target=_worker, daemon=True).start()
        thread_started = True
        return {"job_id": job_id, "user": updated_user}
    finally:
        if not thread_started:
            with _ACTIVE_JOBS_MUTEX:
                _ACTIVE_JOB_LOCKS.discard(job_id)


@app.get("/api/jobs")
def api_list_jobs(user: dict[str, Any] = Depends(require_current_user)) -> dict[str, Any]:
    return {"jobs": [serialize_public_job(j) for j in list_user_jobs(user["id"], limit=20)]}


@app.get("/api/jobs/{job_id}")
def api_get_job(
    job_id: str,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,32}", job_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ dịch này.")
    job = get_job_record(job_id)
    if not job or (job["user_id"] != user["id"] and not user.get("is_owner")):
        raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ dịch này.")
    return {"job": serialize_public_job(job)}


@app.post("/api/jobs/{job_id}/rerender")
def api_rerender_job(
    job_id: str,
    payload: RerenderPayload,
    request: Request,
    user: dict[str, Any] = Depends(require_current_user),
) -> dict[str, Any]:
    """Re-render output video with edited Vietnamese subtitles, voice selection & mixer sliders (free)."""
    _ensure_not_in_maintenance(user)
    _check_rate_limit(request, "jobs", 8, 60)
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,32}", job_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ.")

    job = get_job_record(job_id)
    if not job or (job["user_id"] != user["id"] and not user.get("is_owner")):
        raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ.")
    in_path_str = job.get("input_video_path")
    if not in_path_str or not Path(in_path_str).exists():
        raise HTTPException(status_code=400, detail="Video nguồn của tác vụ này không còn tồn tại.")

    with _ACTIVE_JOBS_MUTEX:
        if job_id in _ACTIVE_JOB_LOCKS:
            raise HTTPException(
                status_code=409,
                detail="Video này đang được xử lý/render, vui lòng đợi hoàn tất.",
            )
        if len(_ACTIVE_JOB_LOCKS) >= MAX_CONCURRENT_JOBS:
            raise HTTPException(
                status_code=429,
                detail="Máy chủ đang xử lý tối đa video cùng lúc, vui lòng thử lại sau ít phút.",
            )
        _ACTIVE_JOB_LOCKS.add(job_id)

    thread_started = False
    try:
        safe_settings = dict(payload.settings or {})
        safe_settings["bgm_volume"] = max(0.0, min(30.0, float(safe_settings.get("bgm_volume", 15.0))))
        safe_settings["original_volume"] = max(0.0, min(100.0, float(safe_settings.get("original_volume", 0.0))))
        safe_settings["tts_volume"] = max(20.0, min(150.0, float(safe_settings.get("tts_volume", 100.0))))
        safe_settings["tts_speed"] = max(0.75, min(2.0, float(safe_settings.get("tts_speed", 1.20) or 1.20)))

        update_job_record(
            job_id,
            status="running",
            current_step=4,
            progress=60,
            step_message="Đang áp dụng chỉnh sửa & render lại video (miễn phí)...",
            pass2_subtitles_json=json.dumps(payload.pass2_subtitles, ensure_ascii=False),
            settings_json=json.dumps(safe_settings, ensure_ascii=False),
            error_message=None,
        )

        def _rerender_worker() -> None:
            try:
                input_path = Path(in_path_str)
                media_info = probe_media_info(input_path)
                job_dir = TEMP_DIR / job_id
                job_dir.mkdir(parents=True, exist_ok=True)
                out_path = render_final_video(
                    job_id=job_id,
                    input_video=input_path,
                    job_dir=job_dir,
                    media_info=media_info,
                    pass2_segments=payload.pass2_subtitles,
                    settings=safe_settings,
                    progress_cb=lambda stp, pct, msg: update_job_record(
                        job_id, status="running", current_step=stp, progress=pct, step_message=msg
                    ),
                )
                update_job_record(
                    job_id,
                    status="completed",
                    current_step=6,
                    progress=100,
                    step_message="Đã render lại video thành công!",
                    output_video_path=str(out_path),
                    output_video_url=f"/outputs/{out_path.name}?v={secrets.token_hex(3)}",
                )
            except Exception as exc:
                update_job_record(
                    job_id,
                    status="failed",
                    progress=100,
                    step_message="Render lại thất bại",
                    error_message=sanitize_error_message(exc),
                )
            finally:
                with _ACTIVE_JOBS_MUTEX:
                    _ACTIVE_JOB_LOCKS.discard(job_id)

        threading.Thread(target=_rerender_worker, daemon=True).start()
        thread_started = True
        return {"ok": True, "job_id": job_id}
    finally:
        if not thread_started:
            with _ACTIVE_JOBS_MUTEX:
                _ACTIVE_JOB_LOCKS.discard(job_id)


@app.get("/api/tool/download")
def download_portable_tool() -> StreamingResponse:
    """Package Zay_Vietsub.vn source & launcher into a portable ZIP archive."""
    buf = io.BytesIO()
    include_items = [
        BASE_DIR / "app.py",
        BASE_DIR / "requirements.txt",
        BASE_DIR / "Chay_Zay_Vietsub.bat",
    ]
    include_dirs = [BASE_DIR / "backend", BASE_DIR / "static"]

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_p in include_items:
            if file_p.is_file():
                zf.write(file_p, arcname=f"Zay_Vietsub/{file_p.name}")

        for dir_p in include_dirs:
            if not dir_p.is_dir():
                continue
            for item in dir_p.rglob("*"):
                if (
                    item.is_file()
                    and "__pycache__" not in item.parts
                    and item.suffix.lower() not in {".mp4", ".zip"}
                ):
                    rel = item.relative_to(BASE_DIR)
                    zf.write(item, arcname=f"Zay_Vietsub/{rel.as_posix()}")

        readme = (
            "=== ZAY_VIETSUB.VN — TOOL AUTO DICH VIDEO AI ===\n\n"
            "1. Cai Python 3.10+ va dat ffmpeg.exe cung thu muc hoac vao PATH.\n"
            "2. Bam dup vao file Chay_Zay_Vietsub.bat de tu dong cai thu vien va mo Web Studio.\n"
            "3. Truy cap http://127.0.0.1:8088 tren dien thoai hoac laptop.\n"
        )
        zf.writestr("Zay_Vietsub/HUONG_DAN.txt", readme)

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="Zay_Vietsub_Portable.zip"'
        },
    )
