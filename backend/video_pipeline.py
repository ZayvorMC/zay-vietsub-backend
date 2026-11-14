"""Video download, duration check (<10m), speech/sub extraction, constant blur bar,
ASS subtitle burning, synchronized Edge-TTS dubbing, and <30% BGM mixing."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import cv2
import edge_tts
import numpy as np
import speech_recognition as sr_lib
import yt_dlp

from backend.database import BASE_DIR, OUTPUTS_DIR, TEMP_DIR, update_job_record
from backend.translator import translate_two_pass

SAMPLE_RATE = 44100
MAX_DURATION_SECONDS = 600.0  # Strict < 10 minutes limit

_WHISPER_LOCK = threading.Lock()
_WHISPER_MODEL: Any = None


def _safe_unlink(path: Path) -> None:
    """Best-effort file removal that ignores transient Windows file locks."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def sanitize_error_message(raw_msg: Any) -> str:
    """Scrub host file paths and internal tracebacks before exposing errors to clients."""
    msg = str(raw_msg or "Lỗi xử lý video.").strip()
    if "WinError 32" in msg or "being used by another process" in msg:
        return "Tệp âm thanh tạm đang bận xử lý, vui lòng thử lại sau vài giây."

    msg = re.sub(r"[A-Za-z]:\\[^\s'\"<>|]+", "[server-file]", msg)
    msg = re.sub(r"[A-Za-z]:/[^\s'\"<>|]+", "[server-file]", msg)
    msg = re.sub(r"(?:/Users|/home|/var|/tmp|/etc)/[^\s'\"<>|]+", "[server-file]", msg)
    first_line = next((line.strip() for line in msg.splitlines() if line.strip()), "Lỗi xử lý video.")
    return first_line[:240]


def validate_safe_video_url(url: str) -> str:
    """Block SSRF, private-network targets, CLI flag injection, and local file schemes."""
    clean = (url or "").strip()
    if not clean or clean.startswith("-") or len(clean) > 512:
        raise ValueError("Đường dẫn video không hợp lệ hoặc quá dài.")

    parsed = urlparse(clean)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Chỉ hỗ trợ liên kết video http:// hoặc https:// công khai.")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("Liên kết video thiếu tên miền hợp lệ.")

    blocked_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    if host in blocked_hosts or host.endswith((".local", ".internal", ".localhost", ".lan", ".home")):
        raise ValueError("Liên kết nội bộ hoặc địa chỉ máy chủ cục bộ không được phép.")

    try:
        ip_obj = ipaddress.ip_address(host.strip("[]"))
        if (
            ip_obj.is_loopback
            or ip_obj.is_private
            or ip_obj.is_link_local
            or ip_obj.is_reserved
            or ip_obj.is_multicast
            or ip_obj.is_unspecified
        ):
            raise ValueError("Liên kết nội bộ hoặc địa chỉ mạng riêng không được phép.")
    except ValueError as exc:
        if "không được phép" in str(exc):
            raise

    return clean


def get_ffmpeg_bin() -> str:
    """Locate ffmpeg executable on the system."""
    candidates = [
        os.environ.get("FFMPEG_PATH", ""),
        str(BASE_DIR / "ffmpeg.exe"),
        str(BASE_DIR.parent / "ffmpeg.exe"),
        shutil.which("ffmpeg") or "",
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return str(Path(cand).resolve())
    raise FileNotFoundError(
        "Không tìm thấy ffmpeg.exe. Vui lòng đặt ffmpeg.exe cạnh tool hoặc vào PATH."
    )


def _round_even(val: float, minimum: int = 2) -> int:
    rounded = int(round(val / 2.0) * 2)
    return max(minimum, rounded)


def probe_media_info(media_path: Path) -> dict[str, Any]:
    """Probe video resolution, fps, duration, and audio presence without ffprobe."""
    ffmpeg_bin = get_ffmpeg_bin()
    width, height, fps, cv_duration = 1280, 720, 25.0, 0.0

    cap = cv2.VideoCapture(str(media_path))
    if cap.isOpened():
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        f = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        if w > 0 and h > 0:
            width, height = w, h
        if 1.0 <= f <= 120.0:
            fps = f
        if frames > 0 and fps > 0:
            cv_duration = frames / fps
        cap.release()

    proc = subprocess.run(
        [ffmpeg_bin, "-i", str(media_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    stderr = proc.stderr or ""
    ff_duration = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if m:
        hh, mm, ss = int(m.group(1)), int(m.group(2)), float(m.group(3))
        ff_duration = hh * 3600 + mm * 60 + ss

    has_audio = "Audio:" in stderr
    duration = ff_duration if ff_duration > 0 else cv_duration
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "duration": round(duration, 3),
        "has_audio": has_audio,
    }


def _trim_video_if_needed(video_path: Path, job_dir: Path, max_sec: float = 598.0) -> Path:
    """Trim video to under 10 minutes if requested."""
    ffmpeg_bin = get_ffmpeg_bin()
    trimmed = job_dir / f"trimmed_{video_path.stem}.mp4"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            str(video_path),
            "-t",
            f"{max_sec:.2f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(trimmed),
        ],
        check=True,
        capture_output=True,
    )
    return trimmed


def download_video_from_url(
    url: str, job_dir: Path, auto_trim_10m: bool = True
) -> dict[str, Any]:
    """Download video & subtitles from Bilibili, YouTube, TikTok, etc. (<10 minutes)."""
    url = validate_safe_video_url(url)
    ffmpeg_bin = get_ffmpeg_bin()
    ffmpeg_dir = str(Path(ffmpeg_bin).parent)

    common_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    }
    if "bilibili.com" in url or "b23.tv" in url:
        common_headers["Referer"] = "https://www.bilibili.com/"
    elif "bilibili.tv" in url:
        common_headers["Referer"] = "https://www.bilibili.tv/"

    probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": ffmpeg_dir,
        "http_headers": common_headers,
    }

    with yt_dlp.YoutubeDL(probe_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = str(info.get("title") or "Video Zay_Vietsub").strip()
    duration = float(info.get("duration") or 0.0)
    if duration > MAX_DURATION_SECONDS and not auto_trim_10m:
        mins = int(duration // 60)
        secs = int(duration % 60)
        raise ValueError(
            f"Video dài {mins}p{secs:02d}s, vượt giới hạn < 10 phút. "
            "Hãy bật tùy chọn 'Tự cắt 10 phút đầu' hoặc chọn video ngắn hơn."
        )

    out_tmpl = str(job_dir / "source_video.%(ext)s")
    dl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": ffmpeg_dir,
        "http_headers": common_headers,
        "format": "bestvideo[height<=1080][vcodec^=avc1]+bestaudio/bestvideo[height<=1080]+bestaudio/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": out_tmpl,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["zh-Hans", "zh-CN", "zh-Hant", "zh", "en", "en-US", "en-GB"],
        "subtitlesformat": "vtt/srt/best",
    }

    try:
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            ydl.download([url])
    except Exception:
        # Fallback if subtitle download or strict subtitle extractor fails
        fallback_opts = {
            **dl_opts,
            "writesubtitles": False,
            "writeautomaticsub": False,
        }
        with yt_dlp.YoutubeDL(fallback_opts) as ydl:
            ydl.download([url])

    video_candidates = [
        p
        for p in job_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
    ]
    if not video_candidates:
        raise RuntimeError("Không thể tải tệp video từ đường dẫn này.")

    video_path = max(video_candidates, key=lambda p: p.stat().st_size)
    media_info = probe_media_info(video_path)
    if media_info["duration"] > MAX_DURATION_SECONDS:
        if auto_trim_10m:
            video_path = _trim_video_if_needed(video_path, job_dir, 598.0)
            media_info = probe_media_info(video_path)
        else:
            raise ValueError("Video vượt quá giới hạn 10 phút.")

    subtitle_files = sorted(
        [
            p
            for p in job_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".vtt", ".srt"}
        ],
        key=lambda p: ("zh" not in p.name.lower(), p.name),
    )

    return {
        "video_path": video_path,
        "title": title,
        "media_info": media_info,
        "subtitle_files": subtitle_files,
    }


def _parse_timestamp_sec(ts: str) -> float:
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def _clean_caption_text(raw: str) -> str:
    # Remove WebVTT inline timing tags <00:00:01.000> and HTML tags
    txt = re.sub(r"<\d{2}:\d{2}:\d{2}(?:\.\d+)?>", "", raw)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _parse_subtitle_file(sub_path: Path) -> list[dict[str, Any]]:
    """Parse VTT or SRT file into clean, deduplicated dialogue segments."""
    text = sub_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    time_re = re.compile(
        r"((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{2,3})\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{2,3})"
    )

    raw_items: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        m = time_re.search(lines[i])
        if not m:
            i += 1
            continue
        start = _parse_timestamp_sec(m.group(1))
        end = _parse_timestamp_sec(m.group(2))
        i += 1
        buf: list[str] = []
        while i < len(lines) and lines[i].strip():
            buf.append(lines[i].strip())
            i += 1
        cleaned = _clean_caption_text(" ".join(buf))
        if cleaned and end > start + 0.08:
            raw_items.append({"start": start, "end": end, "text": cleaned})
        i += 1

    # Deduplicate rolling YouTube auto-captions
    merged: list[dict[str, Any]] = []
    for item in raw_items:
        if item["end"] - item["start"] < 0.12:
            continue
        if merged:
            prev = merged[-1]
            if item["text"] == prev["text"] and item["start"] <= prev["end"] + 0.5:
                prev["end"] = max(prev["end"], item["end"])
                continue
            if item["text"].startswith(prev["text"]) and (item["start"] - prev["start"]) < 2.5:
                prev["text"] = item["text"]
                prev["end"] = max(prev["end"], item["end"])
                continue
        merged.append(dict(item))

    # Re-index and ensure non-overlapping clean boundaries
    segments: list[dict[str, Any]] = []
    for idx, item in enumerate(merged):
        st = round(float(item["start"]), 3)
        ed = round(max(st + 0.5, float(item["end"])), 3)
        if segments and st < segments[-1]["end"]:
            segments[-1]["end"] = round(max(segments[-1]["start"] + 0.4, st - 0.03), 3)
        segments.append({"index": idx, "start": st, "end": ed, "text": item["text"]})
    return segments


def _detect_vad_windows(wav_path: Path) -> list[tuple[float, float]]:
    """Detect speech time windows from a 16kHz mono WAV using adaptive RMS energy."""
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if len(samples) < sr * 0.3:
        return []

    frame_len = int(sr * 0.05)  # 50ms frame
    num_frames = len(samples) // frame_len
    if num_frames == 0:
        return []

    frames = samples[: num_frames * frame_len].reshape(num_frames, frame_len)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    peak = float(np.max(rms))
    if peak < 0.012:
        return []

    noise_floor = float(np.percentile(rms, 8))
    thresh = max(0.010, min(noise_floor * 1.45, float(np.percentile(rms, 30))))
    active = rms >= thresh

    # Merge short pauses <= 8 frames (0.40s)
    gap_limit = 8
    last_true = -1
    for i, val in enumerate(active):
        if val:
            if last_true != -1 and 0 < (i - last_true - 1) <= gap_limit:
                active[last_true + 1 : i] = True
            last_true = i

    windows: list[tuple[float, float]] = []
    start_f = None
    for i, val in enumerate(active):
        if val and start_f is None:
            start_f = i
        elif not val and start_f is not None:
            if (i - start_f) >= 8:  # >= 0.40s
                windows.append((max(0.0, start_f * 0.05 - 0.12), min(num_frames * 0.05, i * 0.05 + 0.28)))
            start_f = None
    if start_f is not None and (num_frames - start_f) >= 8:
        windows.append((max(0.0, start_f * 0.05 - 0.12), num_frames * 0.05))

    # Split overly long windows (> 7.5s) at minimum energy valley
    final_windows: list[tuple[float, float]] = []
    for st, ed in windows:
        if ed - st <= 7.5:
            final_windows.append((round(st, 2), round(ed, 2)))
            continue
        cur = st
        while ed - cur > 7.5:
            search_lo = int((cur + 2.8) / 0.05)
            search_hi = min(num_frames - 1, int((cur + 7.2) / 0.05))
            if search_hi <= search_lo:
                break
            valley_f = search_lo + int(np.argmin(rms[search_lo:search_hi]))
            split_t = valley_f * 0.05
            final_windows.append((round(cur, 2), round(split_t, 2)))
            cur = split_t
        if ed - cur >= 0.45:
            final_windows.append((round(cur, 2), round(ed, 2)))
    return final_windows


def _get_whisper_model() -> Any:
    """Lazily load cached Faster-Whisper base int8 CPU model once per process."""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    with _WHISPER_LOCK:
        if _WHISPER_MODEL is None:
            from faster_whisper import WhisperModel

            _WHISPER_MODEL = WhisperModel(
                "base",
                device="cpu",
                compute_type="int8",
                cpu_threads=max(1, min(int(os.environ.get("WHISPER_THREADS", "2")), os.cpu_count() or 2)),
            )
    return _WHISPER_MODEL


def _join_whisper_words(words: list[Any]) -> str:
    txt = "".join(str(getattr(w, "word", "")) for w in words).strip()
    return re.sub(r"\s+", " ", txt).strip()


def _load_wav_mono16k_float32(wav_path: Path) -> np.ndarray:
    """Load 16kHz mono int16 WAV directly as float32 numpy array (bypasses PyAV)."""
    with wave.open(str(wav_path), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _extract_speech_via_faster_whisper(
    wav_16k: Path, source_lang: str, title: str = ""
) -> list[dict[str, Any]]:
    """Transcribe entire audio track with Faster-Whisper word timestamps (no window cap)."""
    model = _get_whisper_model()
    audio_arr = _load_wav_mono16k_float32(wav_16k)
    if len(audio_arr) == 0:
        return []

    has_cjk_title = bool(re.search(r"[\u4e00-\u9fff]", str(title or "")))
    if source_lang.startswith("zh") or (source_lang == "auto" and has_cjk_title):
        whisper_lang = "zh"
        init_prompt = (
            "以下是普通话简体中文搞笑校园动画旁白与对话，请准确断句并加上标点："
            "言小熙、卧龙、凤雏、学霸、学习委员、摸鱼、五杀、鲁班、段位、英语课、语文课、古文、文言文、试卷、摸鱼积分、食堂、点外卖、炸鸡盒。"
        )
    elif source_lang.startswith("en"):
        whisper_lang = "en"
        init_prompt = "Clear natural English dialogue with proper punctuation."
    else:
        whisper_lang = None
        init_prompt = "请准确识别普通话或英语对话并保留自然标点符号。"

    segments_iter, _info = model.transcribe(
        audio_arr,
        language=whisper_lang,
        beam_size=4,
        best_of=4,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 250, "speech_pad_ms": 200, "threshold": 0.35},
        word_timestamps=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.85,
        log_prob_threshold=-1.5,
        initial_prompt=init_prompt,
    )

    strong_punct = ("。", "！", "？", "；", ".", "!", "?", ";")
    soft_punct = ("，", ",", "、")
    raw_phrases: list[dict[str, Any]] = []

    for seg in segments_iter:
        seg_text = str(getattr(seg, "text", "") or "").strip()
        if not seg_text:
            continue
        words = list(getattr(seg, "words", None) or [])
        if not words:
            raw_phrases.append(
                {
                    "start": max(0.0, float(seg.start)),
                    "end": max(float(seg.start) + 0.45, float(seg.end)),
                    "text": seg_text,
                }
            )
            continue

        cur_words: list[Any] = []
        for w in words:
            w_txt = str(getattr(w, "word", ""))
            if not w_txt.strip() and not cur_words:
                continue
            cur_words.append(w)
            cur_start = float(cur_words[0].start)
            cur_end = float(cur_words[-1].end)
            dur = cur_end - cur_start
            stripped = w_txt.strip()

            hit_strong = stripped.endswith(strong_punct) and dur >= 1.2
            hit_soft = stripped.endswith(soft_punct) and dur >= 3.2
            hit_long_strong = stripped.endswith(strong_punct) and dur >= 4.8
            hit_limit = dur >= 5.6

            if hit_strong or hit_soft or hit_long_strong:
                joined = _join_whisper_words(cur_words)
                if joined:
                    raw_phrases.append(
                        {"start": cur_start, "end": max(cur_start + 0.45, cur_end), "text": joined}
                    )
                cur_words = []
            elif hit_limit and len(cur_words) >= 3:
                split_idx = -1
                for wi in range(len(cur_words) - 2, 0, -1):
                    ws = str(getattr(cur_words[wi], "word", "")).strip()
                    if (float(cur_words[wi].end) - cur_start) >= 1.8 and ws.endswith(
                        strong_punct + soft_punct
                    ):
                        split_idx = wi
                        break
                if split_idx == -1:
                    best_gap = -1.0
                    for wi in range(1, len(cur_words) - 1):
                        if (float(cur_words[wi].end) - cur_start) < 2.0:
                            continue
                        gap = float(cur_words[wi + 1].start) - float(cur_words[wi].end)
                        if gap > best_gap:
                            best_gap = gap
                            split_idx = wi
                if split_idx != -1:
                    head = cur_words[: split_idx + 1]
                    tail = cur_words[split_idx + 1 :]
                    joined = _join_whisper_words(head)
                    if joined:
                        raw_phrases.append(
                            {
                                "start": float(head[0].start),
                                "end": max(float(head[0].start) + 0.45, float(head[-1].end)),
                                "text": joined,
                            }
                        )
                    cur_words = tail
                else:
                    joined = _join_whisper_words(cur_words)
                    if joined:
                        raw_phrases.append(
                            {"start": cur_start, "end": max(cur_start + 0.45, cur_end), "text": joined}
                        )
                    cur_words = []

        if cur_words:
            joined = _join_whisper_words(cur_words)
            if joined:
                st_val = float(cur_words[0].start)
                ed_val = max(st_val + 0.45, float(cur_words[-1].end))
                raw_phrases.append({"start": st_val, "end": ed_val, "text": joined})

    # Merge tiny adjacent bursts (< 1.8s) into coherent 2.5s-4.8s clauses with commas
    merged: list[dict[str, Any]] = []
    for ph in raw_phrases:
        st = max(0.0, float(ph["start"]))
        ed = max(st + 0.4, float(ph["end"]))
        txt = ph["text"].strip()
        if not txt:
            continue
        if merged:
            prev = merged[-1]
            prev_dur = prev["end"] - prev["start"]
            cur_dur = ed - st
            gap = st - prev["end"]
            if (cur_dur < 1.8 or prev_dur < 1.8 or len(txt) <= 3) and gap < 0.38 and (ed - prev["start"] <= 4.8):
                is_cjk = bool(re.search(r"[\u4e00-\u9fff]$", prev["text"]))
                if prev["text"].endswith(("，", "。", "？", "！", ",", ".", "?", "!")):
                    sep = "" if is_cjk else " "
                else:
                    sep = "，" if is_cjk else ", "
                prev["text"] = f"{prev['text']}{sep}{txt}".strip()
                prev["end"] = ed
                continue
        merged.append({"start": st, "end": ed, "text": txt})

    # Normalize strict monotonic boundaries
    final_segments: list[dict[str, Any]] = []
    for item in merged:
        st = round(float(item["start"]), 3)
        ed = round(max(st + 0.45, float(item["end"])), 3)
        if final_segments and st < final_segments[-1]["end"]:
            final_segments[-1]["end"] = round(
                max(final_segments[-1]["start"] + 0.4, st - 0.03), 3
            )
        final_segments.append(
            {
                "index": len(final_segments),
                "start": st,
                "end": ed,
                "text": item["text"],
            }
        )
    return final_segments


def _extract_speech_via_asr_fallback(
    wav_16k: Path, source_lang: str, title: str
) -> list[dict[str, Any]]:
    """Fallback chunked SpeechRecognition extractor when Faster-Whisper is unavailable."""
    windows = _detect_vad_windows(wav_16k)
    if not windows:
        return []

    if source_lang.startswith("zh"):
        asr_langs = ["zh-CN"]
    elif source_lang.startswith("en"):
        asr_langs = ["en-US"]
    else:
        has_cjk_title = bool(re.search(r"[\u4e00-\u9fff]", title))
        asr_langs = ["zh-CN", "en-US"] if has_cjk_title else ["en-US", "zh-CN"]

    probe_rec = sr_lib.Recognizer()
    audio_chunks: list[tuple[int, float, float, Any]] = []
    for idx, (st, ed) in enumerate(windows):
        dur = max(0.3, ed - st)
        with sr_lib.AudioFile(str(wav_16k)) as chunk_src:
            audio_chunk = probe_rec.record(chunk_src, duration=dur, offset=st)
        audio_chunks.append((idx, st, ed, audio_chunk))

    chosen_lang = asr_langs[0]
    prefetched: dict[int, tuple[float, float, str]] = {}
    start_parallel_idx = 0

    if len(asr_langs) > 1 and audio_chunks:
        for idx, st, ed, audio_chunk in audio_chunks[:3]:
            start_parallel_idx = idx + 1
            recognized = ""
            for lang_code in asr_langs:
                try:
                    txt = probe_rec.recognize_google(audio_chunk, language=lang_code)
                    if txt and txt.strip():
                        recognized = txt.strip()
                        chosen_lang = lang_code
                        break
                except Exception:
                    continue
            if recognized:
                prefetched[idx] = (st, ed, recognized)
                break

    def _transcribe_chunk(item: tuple[int, float, float, Any]) -> tuple[int, float, float, str]:
        idx, st, ed, audio_chunk = item
        local_rec = sr_lib.Recognizer()
        for attempt in range(3):
            try:
                txt = local_rec.recognize_google(audio_chunk, language=chosen_lang)
                if txt and txt.strip():
                    return idx, st, ed, txt.strip()
                break
            except sr_lib.UnknownValueError:
                break
            except Exception:
                time.sleep(0.25 * (attempt + 1))
        return idx, st, ed, ""

    results_by_idx: dict[int, tuple[float, float, str]] = dict(prefetched)
    remaining_chunks = audio_chunks[start_parallel_idx:]
    if remaining_chunks:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for idx, st, ed, recognized in pool.map(_transcribe_chunk, remaining_chunks):
                if recognized:
                    results_by_idx[idx] = (st, ed, recognized)

    segments: list[dict[str, Any]] = []
    for idx in sorted(results_by_idx):
        st, ed, recognized = results_by_idx[idx]
        segments.append(
            {
                "index": len(segments),
                "start": round(st, 3),
                "end": round(ed, 3),
                "text": recognized,
            }
        )
    return segments


def _extract_speech_via_asr(
    video_path: Path, job_dir: Path, source_lang: str, title: str
) -> list[dict[str, Any]]:
    """Extract speech segments across the full video using Faster-Whisper (SpeechRecognition fallback)."""
    ffmpeg_bin = get_ffmpeg_bin()
    wav_16k = job_dir / f"asr_mono16k_{secrets.token_hex(3)}.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-acodec",
            "pcm_s16le",
            str(wav_16k),
        ],
        check=True,
        capture_output=True,
    )

    try:
        try:
            segs = _extract_speech_via_faster_whisper(wav_16k, source_lang, title=title)
            if segs:
                return segs
        except Exception as exc:
            print(f"WHISPER_ERR: {exc}")
        return _extract_speech_via_asr_fallback(wav_16k, source_lang, title)
    finally:
        _safe_unlink(wav_16k)


def extract_dialogue_segments(
    video_path: Path,
    job_dir: Path,
    source_lang: str,
    title: str,
    has_audio: bool,
    subtitle_files: list[Path] | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    """Extract dialogue from soft subtitles first, then ASR fallback."""
    for sub_file in subtitle_files or []:
        parsed = _parse_subtitle_file(sub_file)
        if parsed:
            return True, parsed

    if not has_audio:
        return False, []

    asr_segments = _extract_speech_via_asr(video_path, job_dir, source_lang, title)
    if asr_segments:
        return True, asr_segments
    return False, []


def _decode_audio_stereo(
    ffmpeg_bin: str, audio_path: Path, sr: int = SAMPLE_RATE, af_filter: str = ""
) -> np.ndarray:
    """Decode any audio/video stream into float32 stereo numpy array, with optional audio filter."""
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(audio_path),
        "-vn",
    ]
    if af_filter:
        cmd.extend(["-af", af_filter])
    cmd.extend(
        [
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-ac",
            "2",
            "-ar",
            str(sr),
            "pipe:1",
        ]
    )
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        if af_filter:
            return _decode_audio_stereo(ffmpeg_bin, audio_path, sr, af_filter="")
        return np.zeros((0, 2), dtype=np.float32)
    return np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 2).copy()


def _trim_silence(audio: np.ndarray, sr: int = SAMPLE_RATE, threshold: float = 0.010) -> np.ndarray:
    """Trim leading and trailing silence from TTS speech clip."""
    if len(audio) == 0:
        return audio
    amp = np.max(np.abs(audio), axis=1)
    idx = np.where(amp > threshold)[0]
    if len(idx) == 0:
        return audio
    pad = int(0.014 * sr)
    start_i = max(0, int(idx[0]) - pad)
    end_i = min(len(audio), int(idx[-1]) + pad)
    return audio[start_i:end_i]


def _compact_internal_pauses(
    audio: np.ndarray,
    sr: int = SAMPLE_RATE,
    threshold: float = 0.009,
    max_pause_sec: float = 0.14,
    keep_pause_sec: float = 0.07,
) -> np.ndarray:
    """Shorten overly long internal TTS pauses so narration flows briskly without dead air."""
    if len(audio) == 0:
        return audio
    amp = np.max(np.abs(audio), axis=1)
    is_silent = amp < threshold
    max_run = int(max_pause_sec * sr)
    keep_run = int(keep_pause_sec * sr)
    if len(audio) < max_run * 2:
        return audio

    pieces: list[np.ndarray] = []
    n = len(audio)
    i = 0
    while i < n:
        if not is_silent[i]:
            j = i + 1
            while j < n and not is_silent[j]:
                j += 1
            pieces.append(audio[i:j])
            i = j
        else:
            j = i + 1
            while j < n and is_silent[j]:
                j += 1
            run_len = j - i
            if run_len > max_run:
                pieces.append(audio[i : i + keep_run])
            else:
                pieces.append(audio[i:j])
            i = j
    return np.concatenate(pieces, axis=0) if pieces else audio


def _build_atempo_chain(speed: float) -> str:
    """Build valid FFmpeg atempo filter chain for any speed > 0.5."""
    factors: list[float] = []
    rem = speed
    while rem > 2.0:
        factors.append(2.0)
        rem /= 2.0
    while rem < 0.5:
        factors.append(0.5)
        rem /= 0.5
    factors.append(rem)
    return ",".join(f"atempo={f:.4f}" for f in factors)


def _write_wav_int16(path: Path, audio: np.ndarray, sr: int = SAMPLE_RATE) -> None:
    clipped = np.clip(audio, -0.99, 0.99)
    pcm = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


DEFAULT_VOICE_ID = "vi-female-review"

# 100% Native Vietnamese Neural Voices (vi-VN) with custom studio prosody & EQ profiles.
# Foreign en-US / fr-FR voices are automatically mapped to native Vietnamese personas
# so Vietnamese tones (sắc, huyền, hỏi, ngã, nặng) are always pronounced like a real Vietnamese speaker.
VOICE_PROFILES: dict[str, dict[str, Any]] = {
    "vi-female-review": {
        "engine_voice": "vi-VN-HoaiMyNeural",
        "gender": "female",
        "base_rate_pct": 18,
        "pitch_hz": 3,
        "eq_filter": (
            "highpass=f=75,"
            "equalizer=f=210:width_type=o:width=1.2:g=1.8,"
            "equalizer=f=3300:width_type=o:width=1.4:g=-1.8,"
            "acompressor=threshold=-16dB:ratio=2.0:attack=10:release=120"
        ),
    },
    "vi-female-natural": {
        "engine_voice": "vi-VN-HoaiMyNeural",
        "gender": "female",
        "base_rate_pct": 14,
        "pitch_hz": 1,
        "eq_filter": (
            "highpass=f=70,"
            "equalizer=f=195:width_type=o:width=1.2:g=2.2,"
            "equalizer=f=3200:width_type=o:width=1.4:g=-2.0,"
            "acompressor=threshold=-16dB:ratio=2.0:attack=12:release=130"
        ),
    },
    "vi-female-sweet": {
        "engine_voice": "vi-VN-HoaiMyNeural",
        "gender": "female",
        "base_rate_pct": 16,
        "pitch_hz": 6,
        "eq_filter": (
            "highpass=f=80,"
            "equalizer=f=240:width_type=o:width=1.2:g=1.5,"
            "equalizer=f=3400:width_type=o:width=1.4:g=-1.6,"
            "acompressor=threshold=-16dB:ratio=2.0:attack=10:release=120"
        ),
    },
    "vi-male-review": {
        "engine_voice": "vi-VN-NamMinhNeural",
        "gender": "male",
        "base_rate_pct": 18,
        "pitch_hz": 1,
        "eq_filter": (
            "highpass=f=65,"
            "equalizer=f=150:width_type=o:width=1.2:g=2.2,"
            "equalizer=f=3100:width_type=o:width=1.4:g=-1.5,"
            "acompressor=threshold=-16dB:ratio=2.1:attack=10:release=120"
        ),
    },
    "vi-VN-HoaiMyNeural": {
        "engine_voice": "vi-VN-HoaiMyNeural",
        "gender": "female",
        "base_rate_pct": 12,
        "pitch_hz": 0,
        "eq_filter": (
            "highpass=f=70,"
            "equalizer=f=200:width_type=o:width=1.2:g=1.8,"
            "equalizer=f=3200:width_type=o:width=1.4:g=-1.8,"
            "acompressor=threshold=-16dB:ratio=2.0:attack=12:release=130"
        ),
    },
    "vi-VN-NamMinhNeural": {
        "engine_voice": "vi-VN-NamMinhNeural",
        "gender": "male",
        "base_rate_pct": 12,
        "pitch_hz": -2,
        "eq_filter": (
            "highpass=f=65,"
            "equalizer=f=140:width_type=o:width=1.2:g=2.4,"
            "equalizer=f=3000:width_type=o:width=1.4:g=-1.8,"
            "acompressor=threshold=-16dB:ratio=2.0:attack=12:release=130"
        ),
    },
}

LEGACY_VOICE_MAP: dict[str, str] = {
    "en-US-AvaMultilingualNeural": "vi-female-review",
    "en-US-EmmaMultilingualNeural": "vi-female-natural",
    "fr-FR-VivienneMultilingualNeural": "vi-female-sweet",
    "en-US-AndrewMultilingualNeural": "vi-male-review",
}

FEMALE_VOICES = ["vi-female-review", "vi-female-natural", "vi-female-sweet", "vi-VN-HoaiMyNeural"]
MALE_VOICES = ["vi-male-review", "vi-VN-NamMinhNeural"]
SUPPORTED_VOICES = set(FEMALE_VOICES + MALE_VOICES + list(LEGACY_VOICE_MAP.keys()))


def resolve_voice_profile(voice_id: str | None) -> tuple[str, dict[str, Any]]:
    """Resolve any requested or legacy voice_id to a native Vietnamese voice profile."""
    raw = (voice_id or "").strip()
    mapped = LEGACY_VOICE_MAP.get(raw, raw)
    if mapped not in VOICE_PROFILES:
        mapped = DEFAULT_VOICE_ID
    return mapped, VOICE_PROFILES[mapped]


TTS_PHONETIC_FIXES: list[tuple[str, str]] = [
    (r"\bpentakill\b|\bpenta\s+kill\b", "năm mạng thông"),
    (r"\bhow are you\??\s*i'm fine,?\s*thank you!?", "Hao a diu? Am phai, thanh kiu!"),
    (r"\bahoge\b", "chỏm tóc ngố"),
    (r"\b2D\b", "hai đê"),
    (r"\b3D\b", "ba đê"),
    (r"\bskin\b", "trang phục"),
    (r"\brank\b", "mức hạng"),
    (r"\bnick\b", "tài khoản"),
    (r"\bgame\b", "gêm"),
    (r"\bchat\b", "chát"),
    (r"\btest\b", "kiểm thử"),
    (r"\bShidang\b", "nhà ăn"),
]


def _prepare_vi_text_for_tts(raw_txt: str) -> str:
    """Normalize Vietnamese text for fluent native pronunciation without spelling English tokens."""
    clean = re.sub(r"<[^>]+>|\[[^\]]*\]", "", str(raw_txt or "")).strip()
    clean = re.sub(r"[\"“”'‘’«»]", "", clean)
    for pat, repl in TTS_PHONETIC_FIXES:
        clean = re.sub(pat, repl, clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s+([,!.?…;:])", r"\1", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if clean and clean[-1] not in (".", "?", "!", "…", ";", ":"):
        clean += "."
    return clean


def generate_voice_preview_mp3(voice_id: str, speed: float = 1.20) -> Path:
    """Synthesize (and cache) a native Vietnamese voice preview MP3 for the requested voice_id."""
    profile_key, profile = resolve_voice_profile(voice_id)
    clamped_speed = max(0.75, min(2.0, float(speed or 1.20)))
    preview_dir = TEMP_DIR / "_voice_previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", f"{profile_key}_{int(round(clamped_speed * 100))}")
    out_mp3 = preview_dir / f"preview_v2_{safe_name}.mp3"

    if out_mp3.exists() and out_mp3.stat().st_size > 1024:
        return out_mp3

    sample_text = (
        "Chào các bạn! Đây là giọng đọc Tiếng Việt chuẩn tự nhiên của Zay Vietsub, "
        "ngắt nghỉ đúng dấu câu, đọc nhanh gọn và khớp khít từng khung hình."
    )
    rate_pct = max(-25, min(75, int(round((clamped_speed - 1.0) * 100)) + int(profile["base_rate_pct"] * 0.4)))
    rate_str = f"{rate_pct:+d}%"
    pitch_str = f"{int(profile['pitch_hz']):+d}Hz"
    engine_voice = profile["engine_voice"]

    async def _synth() -> None:
        for attempt in range(3):
            try:
                _safe_unlink(out_mp3)
                comm = edge_tts.Communicate(
                    text=sample_text, voice=engine_voice, rate=rate_str, pitch=pitch_str
                )
                await comm.save(str(out_mp3))
                if out_mp3.exists() and out_mp3.stat().st_size > 1024:
                    return
            except Exception:
                _safe_unlink(out_mp3)
            await asyncio.sleep(0.3 + attempt * 0.3)
        raise RuntimeError(f"Không thể tạo âm thanh nghe thử cho giọng {profile_key}.")

    asyncio.run(_synth())
    return out_mp3


def generate_line_preview_mp3(
    text_vi: str, voice_id: str = DEFAULT_VOICE_ID, speed: float = 1.20
) -> Path:
    """Synthesize a custom single subtitle line with its per-sentence speed for instant preview."""
    profile_key, profile = resolve_voice_profile(voice_id)
    clamped_speed = max(0.75, min(2.0, float(speed or 1.20)))
    tts_txt = _prepare_vi_text_for_tts(text_vi)[:360]
    if not tts_txt or not re.search(r"[0-9A-Za-zÀ-ỹ]", tts_txt):
        raise ValueError("Nội dung câu thoại trống.")

    preview_dir = TEMP_DIR / "_line_previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    out_mp3 = preview_dir / f"line_{secrets.token_hex(5)}.mp3"

    rate_pct = max(-25, min(75, int(round((clamped_speed - 1.0) * 100)) + int(profile["base_rate_pct"] * 0.35)))
    rate_str = f"{rate_pct:+d}%"
    pitch_str = f"{int(profile['pitch_hz']):+d}Hz"
    engine_voice = profile["engine_voice"]

    async def _synth() -> None:
        for attempt in range(3):
            try:
                _safe_unlink(out_mp3)
                comm = edge_tts.Communicate(
                    text=tts_txt, voice=engine_voice, rate=rate_str, pitch=pitch_str
                )
                await comm.save(str(out_mp3))
                if out_mp3.exists() and out_mp3.stat().st_size > 256:
                    return
            except Exception:
                _safe_unlink(out_mp3)
            await asyncio.sleep(0.25 + attempt * 0.25)
        raise RuntimeError("Không thể tạo bản nghe thử cho câu này.")

    asyncio.run(_synth())
    return out_mp3


def synthesize_synced_vietnamese_dub(
    segments: list[dict[str, Any]],
    total_duration: float,
    job_dir: Path,
    voice_id: str = DEFAULT_VOICE_ID,
    base_tts_speed: float = 1.20,
) -> tuple[np.ndarray, np.ndarray]:
    """Generate time-aligned Vietnamese TTS audio buffer and speech activity envelope."""
    ffmpeg_bin = get_ffmpeg_bin()
    total_samples = max(int(total_duration * SAMPLE_RATE) + SAMPLE_RATE, SAMPLE_RATE)
    tts_buffer = np.zeros((total_samples, 2), dtype=np.float32)
    speech_mask = np.zeros(total_samples, dtype=np.float32)

    run_tag = secrets.token_hex(4)

    # Best-effort cleanup of prior TTS clips without failing on transient Windows locks
    for pat in ("tts_*.mp3", "tts_*.wav"):
        for old_file in job_dir.glob(pat):
            _safe_unlink(old_file)

    valid_segs = [s for s in segments if str(s.get("text_vi", "")).strip()]
    if not valid_segs:
        return tts_buffer, speech_mask

    _, primary_profile = resolve_voice_profile(voice_id)
    primary_engine = primary_profile["engine_voice"]
    fallback_engine = (
        "vi-VN-NamMinhNeural" if primary_engine == "vi-VN-HoaiMyNeural" else "vi-VN-HoaiMyNeural"
    )
    engine_chain = [primary_engine, fallback_engine]
    pitch_str = f"{int(primary_profile['pitch_hz']):+d}Hz"
    eq_filter = str(primary_profile.get("eq_filter") or "")
    global_speed = max(0.75, min(2.0, float(base_tts_speed or 1.20)))

    # Pre-calculate slot window and required native TTS rate for each segment
    seg_plan: dict[int, dict[str, Any]] = {}
    for pos, seg in enumerate(valid_segs):
        idx = int(seg["index"])
        start_sec = float(seg["start"])
        end_sec = float(seg["end"])
        natural_slot = max(0.75, end_sec - start_sec)
        if pos + 1 < len(valid_segs):
            next_start = float(valid_segs[pos + 1]["start"])
            hard_limit_sec = max(0.55, next_start - start_sec - 0.02)
            slot_sec = max(0.65, min(natural_slot + 0.85, hard_limit_sec))
        else:
            hard_limit_sec = max(0.65, total_duration - start_sec - 0.02)
            slot_sec = hard_limit_sec

        tts_txt = _prepare_vi_text_for_tts(str(seg.get("text_vi", "")))
        word_count = len(tts_txt.split())
        user_line_speed = max(0.75, min(2.0, float(seg.get("speed") or global_speed)))
        # At +0% rate, vi-VN neural voices speak ~3.8 words/sec; auto-boost native rate if sentence is dense
        min_fit_mult = (word_count / 3.85) / max(0.55, slot_sec * 0.90)
        effective_mult = max(user_line_speed, min_fit_mult)
        rate_pct = max(
            -25,
            min(
                75,
                int(round((effective_mult - 1.0) * 100)) + int(primary_profile["base_rate_pct"] * 0.35),
            ),
        )
        seg_plan[idx] = {
            "tts_txt": tts_txt,
            "rate_str": f"{rate_pct:+d}%",
            "slot_sec": slot_sec,
            "hard_limit_sec": hard_limit_sec,
            "user_line_speed": user_line_speed,
        }

    async def synth_all() -> list[tuple[int, Path]]:
        results: list[tuple[int, Path]] = []
        sem = asyncio.Semaphore(3)

        async def _one(seg: dict[str, Any]) -> None:
            idx = int(seg["index"])
            plan = seg_plan.get(idx)
            if not plan:
                return
            clean_txt = plan["tts_txt"]
            if not clean_txt or not re.search(r"[0-9A-Za-zÀ-ỹ]", clean_txt):
                return
            out_mp3 = job_dir / f"tts_{run_tag}_{idx}.mp3"
            async with sem:
                for cand_engine in engine_chain:
                    max_attempts = 3 if cand_engine == primary_engine else 2
                    for attempt in range(max_attempts):
                        try:
                            _safe_unlink(out_mp3)
                            comm = edge_tts.Communicate(
                                text=clean_txt,
                                voice=cand_engine,
                                rate=plan["rate_str"],
                                pitch=pitch_str if cand_engine == primary_engine else "+0Hz",
                            )
                            await comm.save(str(out_mp3))
                            if out_mp3.exists() and out_mp3.stat().st_size > 256:
                                results.append((idx, out_mp3))
                                return
                        except Exception:
                            _safe_unlink(out_mp3)
                        await asyncio.sleep(0.30 + attempt * 0.30)

        await asyncio.gather(*[_one(seg) for seg in valid_segs])
        return results

    mp3_list = asyncio.run(synth_all())
    mp3_map = {idx: path for idx, path in mp3_list}

    try:
        for pos, seg in enumerate(valid_segs):
            idx = int(seg["index"])
            mp3_path = mp3_map.get(idx)
            plan = seg_plan.get(idx)
            if not mp3_path or not mp3_path.exists() or not plan:
                continue

            pcm = _decode_audio_stereo(ffmpeg_bin, mp3_path, SAMPLE_RATE, af_filter=eq_filter)
            pcm = _trim_silence(pcm, SAMPLE_RATE, threshold=0.010)
            pcm = _compact_internal_pauses(pcm, SAMPLE_RATE)
            if len(pcm) == 0:
                continue

            start_sec = float(seg["start"])
            hard_limit_sec = float(plan["hard_limit_sec"])
            speech_dur = len(pcm) / SAMPLE_RATE

            # Never slow down a finished clip; only compress with atempo if it still exceeds hard_limit_sec
            # so 100% of the sentence is spoken without ever getting chopped mid-sentence.
            if speech_dur > hard_limit_sec * 0.97:
                speed = min(2.25, speech_dur / max(0.35, hard_limit_sec * 0.95))
            else:
                speed = 1.0

            if speed > 1.02:
                trimmed_wav = job_dir / f"tts_{run_tag}_{idx}_trim.wav"
                sped_wav = job_dir / f"tts_{run_tag}_{idx}_sped.wav"
                try:
                    _write_wav_int16(trimmed_wav, pcm, SAMPLE_RATE)
                    subprocess.run(
                        [
                            ffmpeg_bin,
                            "-y",
                            "-i",
                            str(trimmed_wav),
                            "-af",
                            _build_atempo_chain(speed),
                            str(sped_wav),
                        ],
                        check=True,
                        capture_output=True,
                    )
                    pcm = _decode_audio_stereo(ffmpeg_bin, sped_wav, SAMPLE_RATE)
                    pcm = _trim_silence(pcm, SAMPLE_RATE, threshold=0.010)
                finally:
                    _safe_unlink(trimmed_wav)
                    _safe_unlink(sped_wav)

            if len(pcm) == 0:
                continue

            max_samples = int(hard_limit_sec * SAMPLE_RATE)
            if len(pcm) > max_samples > 200:
                pcm = pcm[:max_samples].copy()

            # Smooth 12ms fade-in and 24ms fade-out
            fade_in = min(int(0.012 * SAMPLE_RATE), len(pcm) // 5)
            if fade_in > 4:
                pcm[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)[:, None]
            fade_out = min(int(0.024 * SAMPLE_RATE), len(pcm) // 4)
            if fade_out > 4:
                pcm[-fade_out:] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)[:, None]

            start_idx = max(0, int(start_sec * SAMPLE_RATE))
            end_idx = min(total_samples, start_idx + len(pcm))
            if end_idx > start_idx:
                tts_buffer[start_idx:end_idx] += pcm[: end_idx - start_idx]
                speech_mask[start_idx:end_idx] = 1.0
    finally:
        for p in mp3_map.values():
            _safe_unlink(p)

    # Smooth ducking envelope (~240ms window)
    kernel = max(1, int(0.24 * SAMPLE_RATE))
    if np.any(speech_mask > 0):
        half = kernel // 2
        padded = np.pad(speech_mask, (half, kernel - half - 1), mode="edge")
        csum = np.cumsum(np.insert(padded, 0, 0.0))
        speech_mask = np.clip((csum[kernel:] - csum[:-kernel]) / kernel, 0.0, 1.0)

    return tts_buffer, speech_mask


def build_bgm_track(
    preset: str,
    total_samples: int,
    custom_bgm_path: Path | None = None,
) -> np.ndarray:
    """Create or load a stereo background music array matching total_samples."""
    if preset == "none":
        return np.zeros((total_samples, 2), dtype=np.float32)

    ffmpeg_bin = get_ffmpeg_bin()
    if preset == "custom" and custom_bgm_path and custom_bgm_path.exists():
        raw = _decode_audio_stereo(ffmpeg_bin, custom_bgm_path, SAMPLE_RATE)
        if len(raw) == 0:
            return np.zeros((total_samples, 2), dtype=np.float32)
        repeats = (total_samples // len(raw)) + 1
        tiled = np.tile(raw, (repeats, 1))[:total_samples].copy()
        return tiled

    cache_wav = TEMP_DIR / f"bgm_preset_{preset}.wav"
    if not cache_wav.exists():
        loop_sec = 16.0
        n = int(loop_sec * SAMPLE_RATE)
        t = np.linspace(0, loop_sec, n, endpoint=False, dtype=np.float32)

        if preset == "cinematic":
            chords = [(220.0, 261.63, 329.63), (174.61, 220.0, 261.63), (196.0, 246.94, 293.66), (164.81, 196.0, 246.94)]
        elif preset == "ambient":
            chords = [(261.63, 329.63, 392.0), (246.94, 293.66, 392.0), (220.0, 261.63, 329.63), (196.0, 246.94, 329.63)]
        else:  # lofi default
            chords = [(261.63, 329.63, 392.0, 493.88), (220.0, 261.63, 329.63, 392.0), (174.61, 220.0, 261.63, 329.63), (196.0, 246.94, 293.66, 349.23)]

        left = np.zeros(n, dtype=np.float32)
        right = np.zeros(n, dtype=np.float32)
        seg_n = n // len(chords)
        for ci, notes in enumerate(chords):
            s0 = ci * seg_n
            s1 = n if ci == len(chords) - 1 else (ci + 1) * seg_n
            lt = t[s0:s1] - t[s0]
            dur_c = (s1 - s0) / SAMPLE_RATE
            env = np.sin(np.pi * np.clip(lt / dur_c, 0.0, 1.0)) ** 0.45
            chord_l = np.zeros_like(lt)
            chord_r = np.zeros_like(lt)
            for ni, freq in enumerate(notes):
                detune = 1.0 + (0.0025 if ni % 2 == 0 else -0.0025)
                chord_l += 0.14 * np.sin(2 * np.pi * freq * lt) + 0.03 * np.sin(4 * np.pi * freq * lt)
                chord_r += 0.14 * np.sin(2 * np.pi * freq * detune * lt) + 0.03 * np.sin(4 * np.pi * freq * lt)
            # Gentle warm pulse
            pulse = 0.85 + 0.15 * np.sin(2 * np.pi * 1.5 * lt)
            left[s0:s1] = chord_l * env * pulse
            right[s0:s1] = chord_r * env * pulse

        stereo = np.stack([left, right], axis=-1)
        _write_wav_int16(cache_wav, stereo, SAMPLE_RATE)

    loop_audio = _decode_audio_stereo(ffmpeg_bin, cache_wav, SAMPLE_RATE)
    repeats = (total_samples // len(loop_audio)) + 1
    return np.tile(loop_audio, (repeats, 1))[:total_samples].copy()


def _wrap_vi_subtitle(text: str, max_chars: int) -> str:
    """Wrap Vietnamese subtitle into at most 2 balanced lines."""
    words = text.strip().split()
    if not words:
        return ""
    if len(text) <= max_chars or len(words) <= 3:
        return text

    # Split roughly near the midpoint for balanced 2-line display
    total_len = len(text)
    best_idx = 1
    best_diff = 9999
    cur = 0
    for idx in range(1, len(words)):
        cur += len(words[idx - 1]) + 1
        diff = abs(cur - total_len / 2)
        if diff < best_diff:
            best_diff = diff
            best_idx = idx
    line1 = " ".join(words[:best_idx])
    line2 = " ".join(words[best_idx:])
    return f"{line1}\\N{line2}"


def _fmt_ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def compute_blur_box(width: int, height: int, settings: dict[str, Any]) -> tuple[int, int, int, int]:
    """Compute even-pixel (bx, by, bw, bh) for the constant single blur bar."""
    ew = _round_even(width)
    eh = _round_even(height)
    x_pct = max(0.0, min(40.0, float(settings.get("blur_x_pct", 4.0))))
    w_pct = max(20.0, min(100.0, float(settings.get("blur_w_pct", 92.0))))
    y_pct = max(5.0, min(90.0, float(settings.get("blur_y_pct", 77.0))))
    h_pct = max(6.0, min(35.0, float(settings.get("blur_h_pct", 16.0))))

    bx = _round_even(ew * x_pct / 100.0, minimum=0)
    bw = _round_even(ew * w_pct / 100.0, minimum=32)
    if bx + bw > ew:
        bw = _round_even(ew - bx, minimum=32)

    by = _round_even(eh * y_pct / 100.0, minimum=0)
    bh = _round_even(eh * h_pct / 100.0, minimum=24)
    if by + bh > eh:
        by = max(0, _round_even(eh - bh, minimum=0))

    return bx, by, bw, bh


def generate_ass_file(
    segments: list[dict[str, Any]],
    width: int,
    height: int,
    blur_box: tuple[int, int, int, int],
    out_ass_path: Path,
) -> int:
    """Write ASS subtitle file centered cleanly inside the constant blur bar."""
    bx, by, bw, bh = blur_box
    cx = bx + bw // 2
    cy = by + bh // 2
    font_size = max(20, min(int(bh * 0.36), int(height * 0.052)))
    max_chars = max(22, int(bw / (font_size * 0.54)))

    header = (
        "\ufeff[Script Info]\n"
        "Title: Zay_Vietsub.vn Subtitles\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: ZaySub,Arial,{font_size},&H00FFFFFF,&H0000FFFF,&H000A0A14,&H80000000,"
        "-1,0,0,0,100,100,0.4,0,1,3.2,1.4,5,24,24,18,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events: list[str] = []
    for seg in segments:
        txt = str(seg.get("text_vi", "")).strip()
        if not txt:
            continue
        display_txt = txt[:-1].strip() if txt.endswith(".") and not txt.endswith("...") else txt
        if not display_txt:
            display_txt = txt
        st = _fmt_ass_time(float(seg["start"]))
        ed = _fmt_ass_time(float(seg["end"]))
        wrapped = _wrap_vi_subtitle(display_txt, max_chars)
        events.append(
            f"Dialogue: 0,{st},{ed},ZaySub,,0,0,0,,{{\\an5\\pos({cx},{cy})}}{wrapped}"
        )

    out_ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return len(events)


def render_final_video(
    job_id: str,
    input_video: Path,
    job_dir: Path,
    media_info: dict[str, Any],
    pass2_segments: list[dict[str, Any]],
    settings: dict[str, Any],
    custom_bgm_path: Path | None = None,
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> Path:
    """Synthesize dubbed audio, mix original/TTS/BGM, burn constant blur bar + ASS subs."""
    ffmpeg_bin = get_ffmpeg_bin()
    duration = max(1.0, float(media_info["duration"]))
    width = _round_even(media_info["width"])
    height = _round_even(media_info["height"])

    if progress_cb:
        progress_cb(4, 68, "Đang lồng tiếng Việt AI (Edge-TTS) khít từng câu...")

    voice_id = str(settings.get("voice_id", DEFAULT_VOICE_ID))
    base_tts_speed = max(0.75, min(2.0, float(settings.get("tts_speed", 1.20) or 1.20)))
    tts_buffer, speech_mask = synthesize_synced_vietnamese_dub(
        pass2_segments,
        duration,
        job_dir,
        voice_id=voice_id,
        base_tts_speed=base_tts_speed,
    )

    if progress_cb:
        progress_cb(5, 80, "Đang hòa trộn âm thanh gốc, giọng đọc & nhạc nền (<30%)...")

    total_samples = len(tts_buffer)
    orig_vol = max(0.0, min(100.0, float(settings.get("original_volume", 0.0)))) / 100.0
    tts_vol = max(0.0, min(150.0, float(settings.get("tts_volume", 100.0)))) / 100.0
    bgm_vol = max(0.0, min(30.0, float(settings.get("bgm_volume", 15.0)))) / 100.0
    bgm_preset = str(settings.get("bgm_preset", "lofi"))

    if orig_vol > 0.001 and media_info.get("has_audio", True):
        orig_buffer = _decode_audio_stereo(ffmpeg_bin, input_video, SAMPLE_RATE)
        if len(orig_buffer) < total_samples:
            orig_buffer = np.pad(orig_buffer, ((0, total_samples - len(orig_buffer)), (0, 0)))
        else:
            orig_buffer = orig_buffer[:total_samples]
        orig_duck = 1.0 - 0.35 * speech_mask[:, None]
        orig_mix = orig_buffer * orig_vol * orig_duck
    else:
        orig_mix = np.zeros((total_samples, 2), dtype=np.float32)

    bgm_buffer = build_bgm_track(bgm_preset, total_samples, custom_bgm_path)
    bgm_duck = 1.0 - 0.55 * speech_mask[:, None]
    bgm_mix = bgm_buffer * bgm_vol * bgm_duck

    final_audio = orig_mix + (tts_buffer * tts_vol) + bgm_mix
    peak = float(np.max(np.abs(final_audio)))
    if peak > 0.95:
        final_audio = np.tanh(final_audio * (0.92 / peak)) * 0.96

    for old_mix in job_dir.glob("mixed_audio*.wav"):
        _safe_unlink(old_mix)
    mixed_wav = job_dir / f"mixed_audio_{secrets.token_hex(3)}.wav"
    _write_wav_int16(mixed_wav, final_audio, SAMPLE_RATE)

    if progress_cb:
        progress_cb(6, 90, "Đang che mờ lời gốc cố định xuyên suốt & xuất video...")

    blur_box = compute_blur_box(width, height, settings)
    bx, by, bw, bh = blur_box
    blur_enabled = bool(settings.get("blur_enabled", True))
    req_radius = max(2, min(32, int(settings.get("blur_radius", 18))))
    # FFmpeg boxblur requires radius <= min(plane_w, plane_h) / 2; for yuv420p chroma plane is half-size
    max_safe_luma = max(2, min(bw, bh) // 2 - 2)
    luma_r = min(req_radius, max_safe_luma)
    chroma_r = max(1, min(luma_r // 2, min(bw, bh) // 4 - 1))

    ass_path = job_dir / "sub.ass"
    num_events = generate_ass_file(pass2_segments, width, height, blur_box, ass_path)

    filter_parts = [f"[0:v]scale={width}:{height}"]
    if blur_enabled:
        filter_parts = [
            f"[0:v]scale={width}:{height},split=2[main][crop_src];"
            f"[crop_src]crop={bw}:{bh}:{bx}:{by},"
            f"boxblur=luma_radius={luma_r}:luma_power=3:chroma_radius={chroma_r}:chroma_power=2,"
            f"drawbox=x=0:y=0:w={bw}:h={bh}:color=black@0.84:t=fill[blur_bar];"
            f"[main][blur_bar]overlay={bx}:{by}"
        ]

    filter_str = filter_parts[0]
    if num_events > 0:
        filter_str += ",ass=sub.ass"
    filter_str += "[vout]"

    output_video = OUTPUTS_DIR / f"zay_vietsub_{job_id}.mp4"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(input_video.resolve()),
        "-i",
        str(mixed_wav.resolve()),
        "-filter_complex",
        filter_str,
        "-map",
        "[vout]",
        "-map",
        "1:a",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "21",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-t",
        f"{duration:.3f}",
        "-movflags",
        "+faststart",
        str(output_video.resolve()),
    ]
    try:
        subprocess.run(cmd, cwd=str(job_dir), check=True, capture_output=True)
    finally:
        _safe_unlink(mixed_wav)
    return output_video


def run_translation_pipeline(
    job_id: str,
    source_type: str,
    source_url: str | None,
    input_video_path: Path | None,
    source_lang: str,
    title: str,
    settings: dict[str, Any],
    custom_bgm_path: Path | None = None,
    on_failure_refund: Callable[[], None] | None = None,
) -> None:
    """Execute full end-to-end translation & dubbing pipeline for a background job."""
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def _update(step: int, progress: int, message: str, **extra: Any) -> None:
        update_job_record(
            job_id,
            status="running",
            current_step=step,
            progress=progress,
            step_message=message,
            **extra,
        )

    try:
        auto_trim = bool(settings.get("auto_trim_10m", True))
        subtitle_files: list[Path] = []

        if source_type == "url" and source_url:
            _update(1, 10, "Bước 1/6: Đang tải video & phụ đề từ link (giới hạn < 10 phút)...", error_message=None)
            dl = download_video_from_url(source_url, job_dir, auto_trim_10m=auto_trim)
            input_video_path = dl["video_path"]
            title = dl["title"]
            media_info = dl["media_info"]
            subtitle_files = dl["subtitle_files"]
        else:
            if not input_video_path or not input_video_path.exists():
                raise FileNotFoundError("Không tìm thấy tệp video đầu vào.")
            _update(1, 12, "Bước 1/6: Đang kiểm tra độ dài video (< 10 phút)...", error_message=None)
            media_info = probe_media_info(input_video_path)
            if media_info["duration"] > MAX_DURATION_SECONDS:
                if auto_trim:
                    input_video_path = _trim_video_if_needed(input_video_path, job_dir, 598.0)
                    media_info = probe_media_info(input_video_path)
                else:
                    raise ValueError(
                        "Video dài hơn 10 phút. Vui lòng bật tự cắt 10 phút hoặc chọn video ngắn hơn."
                    )

        _update(
            2,
            28,
            "Bước 2/6: Đang tự động trích lời thoại trong video...",
            title=title,
            duration=media_info["duration"],
            input_video_path=str(input_video_path),
        )

        has_speech, raw_segments = extract_dialogue_segments(
            input_video_path,
            job_dir,
            source_lang,
            title,
            media_info.get("has_audio", True),
            subtitle_files,
        )

        if has_speech and raw_segments:
            _update(3, 45, "Bước 3/6: AI đang dịch thuật 2 lượt (chuẩn hóa từ & ngữ cảnh)...")
            trans_res = translate_two_pass(raw_segments, source_lang=source_lang)
            pass1 = trans_res["pass1"]
            pass2 = trans_res["pass2"]
            engine_note = trans_res["engine_pass2"]
        else:
            _update(3, 45, "Bước 3/6: Video không có lời thoại — bỏ qua dịch & giữ cấu hình âm thanh...")
            pass1 = []
            pass2 = []
            engine_note = "no-speech"

        update_job_record(
            job_id,
            has_speech=1 if has_speech else 0,
            pass1_subtitles_json=json.dumps(pass1, ensure_ascii=False),
            pass2_subtitles_json=json.dumps(pass2, ensure_ascii=False),
        )

        output_video = render_final_video(
            job_id=job_id,
            input_video=input_video_path,
            job_dir=job_dir,
            media_info=media_info,
            pass2_segments=pass2,
            settings=settings,
            custom_bgm_path=custom_bgm_path,
            progress_cb=lambda stp, pct, msg: _update(stp, pct, msg),
        )

        msg_done = (
            f"Hoàn tất Vietsub & Lồng tiếng ({len(pass2)} câu • {engine_note})!"
            if has_speech and pass2
            else "Hoàn tất xử lý video không lời!"
        )
        update_job_record(
            job_id,
            status="completed",
            current_step=6,
            progress=100,
            step_message=msg_done,
            output_video_path=str(output_video),
            output_video_url=f"/outputs/{output_video.name}",
            error_message=None,
        )
    except Exception as exc:
        if on_failure_refund:
            on_failure_refund()
        update_job_record(
            job_id,
            status="failed",
            progress=100,
            step_message="Xử lý thất bại (đã hoàn lại lượt dịch)",
            error_message=sanitize_error_message(exc),
        )
