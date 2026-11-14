"use strict";

const TOKEN_KEY = "zay_vietsub_token";

const BACKEND_CANDIDATES = [
  "https://zay-vietsub-backend-production.up.railway.app",
  "http://localhost:8088",
  "http://127.0.0.1:8088",
];

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  user: null,
  sourceType: "url",
  currentJobId: null,
  pollTimer: null,
  checkoutIntent: null,
  pass2Segments: [],
  apiBase: "",
  backendResolved: false,
  backendPromise: null,
  maintenance: {
    maintenance_mode: false,
    maintenance_message: "Hệ thống Zay_Vietsub Beta đang bảo trì nâng cấp máy chủ. Vui lòng quay lại sau!",
  },
  ownerUsers: [],
  ownerTransactions: [],
  ownerTxFilterUserId: "",
};

// ============================================================
// Modern Top-Layer <dialog> Exit & Closedby Fallbacks
// ============================================================
let supportsDisplayTransition;
function canTransitionDisplay() {
  if (supportsDisplayTransition !== undefined) return supportsDisplayTransition;
  if (!window.CSS?.supports?.("transition-behavior", "allow-discrete") || !document.body) {
    return false;
  }
  const probe = document.createElement("div");
  probe.style.cssText = "transition: display 1s allow-discrete !important; display: block;";
  document.body.appendChild(probe);
  getComputedStyle(probe).display;
  probe.style.display = "none";
  supportsDisplayTransition = getComputedStyle(probe).display === "block";
  probe.remove();
  return supportsDisplayTransition;
}

function supportsTopLayerExit() {
  return Boolean(window.CSS?.supports?.("overlay", "auto") && canTransitionDisplay());
}

async function closeTopLayer(dialog) {
  if (!dialog || !dialog.open) return;
  if (!supportsTopLayerExit()) {
    dialog.setAttribute("data-closing", "");
    const animations = dialog.getAnimations({ subtree: true });
    if (animations.length > 0) {
      await Promise.race([
        Promise.allSettled(animations.map((a) => a.finished)),
        new Promise((r) => setTimeout(r, 1200)),
      ]);
    }
    if (!dialog.hasAttribute("data-closing")) return;
    dialog.removeAttribute("data-closing");
  }
  dialog.close();
}

function setupDialogs() {
  const dialogs = document.querySelectorAll("dialog.modal-dialog");
  for (const dialog of dialogs) {
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeTopLayer(dialog);
    });
    dialog.addEventListener("close", () => {
      dialog.returnValue = "";
    });
  }

  if (!("closedBy" in HTMLDialogElement.prototype)) {
    const isOutside = (e, dialog) => {
      const r = dialog.getBoundingClientRect();
      return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    };
    for (const dialog of document.querySelectorAll('dialog[closedby="any"]')) {
      let pressedOutside = false;
      dialog.addEventListener("pointerdown", (e) => {
        pressedOutside = isOutside(e, dialog);
      });
      dialog.addEventListener("pointerup", (e) => {
        if (pressedOutside && isOutside(e, dialog)) closeTopLayer(dialog);
      });
    }
  }

  document.querySelectorAll("[data-close-dialog]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetId = btn.getAttribute("data-close-dialog");
      const dlg = document.getElementById(targetId);
      if (dlg) closeTopLayer(dlg);
    });
  });
}

// ============================================================
// Helpers & API Client
// ============================================================
function showToast(message) {
  const region = document.getElementById("toast-region");
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  region.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function fmtVnd(amount) {
  return Number(amount || 0).toLocaleString("vi-VN") + "đ";
}

function fmtTime(sec) {
  const s = Math.max(0, Number(sec || 0));
  const mm = Math.floor(s / 60);
  const ss = (s % 60).toFixed(1).padStart(4, "0");
  return `${String(mm).padStart(2, "0")}:${ss}`;
}

// ============================================================
// Real Backend Resolution & Maintenance Status
// ============================================================
function resolveMediaUrl(url) {
  if (!url) return "";
  if ((url.startsWith("/outputs/") || url.startsWith("/api/")) && state.apiBase) {
    return `${state.apiBase}${url}`;
  }
  return url;
}

function updateMaintenanceUI(maint) {
  if (maint) {
    state.maintenance = {
      maintenance_mode: Boolean(maint.maintenance_mode),
      maintenance_message:
        maint.maintenance_message ||
        "Hệ thống Zay_Vietsub Beta đang bảo trì nâng cấp máy chủ. Vui lòng quay lại sau!",
    };
  }
  const banner = document.getElementById("maintenance-banner");
  const bannerTxt = document.getElementById("maintenance-banner-text");
  if (banner && bannerTxt) {
    bannerTxt.textContent = state.maintenance.maintenance_message;
    banner.classList.toggle("hidden", !state.maintenance.maintenance_mode);
  }

  const statServer = document.getElementById("owner-stat-server");
  const toggleBtn = document.getElementById("maintenance-toggle-btn");
  const msgInput = document.getElementById("maintenance-message-input");
  if (statServer) {
    statServer.textContent = state.maintenance.maintenance_mode ? "Đang Bảo Trì" : "Đang Hoạt Động";
    statServer.classList.toggle("is-maint", state.maintenance.maintenance_mode);
    statServer.classList.toggle("is-ok", !state.maintenance.maintenance_mode);
  }
  if (toggleBtn) {
    toggleBtn.textContent = state.maintenance.maintenance_mode
      ? "Mở Lại Server (Hoạt Động)"
      : "Đóng Server (Bảo Trì)";
    toggleBtn.className = state.maintenance.maintenance_mode
      ? "btn btn-success-outline btn-sm"
      : "btn btn-danger-outline btn-sm";
  }
  if (msgInput && maint?.maintenance_message && document.activeElement !== msgInput) {
    msgInput.value = maint.maintenance_message;
  }
}

async function resolveApiBase() {
  if (state.backendResolved) return state.apiBase;
  if (state.backendPromise) return state.backendPromise;

  state.backendPromise = (async () => {
    const host = window.location.hostname || "";
    if (host === "127.0.0.1" || host === "localhost") {
      state.apiBase = "";
      state.backendResolved = true;
      return state.apiBase;
    }

    for (const candidate of BACKEND_CANDIDATES) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 3200);
      try {
        const resp = await fetch(`${candidate}/api/health`, {
          method: "GET",
          signal: controller.signal,
        });
        if (resp.ok) {
          const payload = await resp.json().catch(() => null);
          if (payload && payload.status === "ok") {
            state.apiBase = candidate;
            state.backendResolved = true;
            updateMaintenanceUI({ maintenance_mode: Boolean(payload.maintenance_mode) });
            return state.apiBase;
          }
        }
      } catch {
        /* Try next backend candidate */
      } finally {
        clearTimeout(timer);
      }
    }

    state.apiBase = BACKEND_CANDIDATES[0];
    state.backendResolved = true;
    return state.apiBase;
  })();

  return state.backendPromise;
}

async function apiFetch(url, options = {}) {
  const base = await resolveApiBase();
  const targetUrl = url.startsWith("http") ? url : `${base}${url}`;
  const headers = new Headers(options.headers || {});
  if (state.token) {
    headers.set("Authorization", `Bearer ${state.token}`);
  }
  let resp;
  try {
    resp = await fetch(targetUrl, { ...options, headers });
  } catch {
    throw new Error("Không kết nối được tới máy chủ xử lý. Vui lòng kiểm tra server đang bật và thử lại.");
  }
  const contentType = resp.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await resp.json() : null;
  if (!resp.ok) {
    const errMsg = data?.detail || `Lỗi máy chủ (${resp.status})`;
    throw new Error(errMsg);
  }
  return data;
}

async function loadSystemStatus() {
  try {
    const maint = await apiFetch("/api/system/status");
    updateMaintenanceUI(maint);
  } catch {
    /* ignore transient check */
  }
}

// ============================================================
// Auth & User State
// ============================================================
function renderUserUI() {
  const userPill = document.getElementById("user-pill");
  const openAuthBtn = document.getElementById("open-auth-btn");
  const planBadge = document.getElementById("header-plan-badge");
  const creditsEl = document.getElementById("header-credits-count");
  const userBtn = document.getElementById("header-user-btn");
  const statusNote = document.getElementById("studio-status-note");
  const ownerNav = document.getElementById("owner-nav-link");
  const ownerSection = document.getElementById("owner-section");

  if (!state.user) {
    userPill.classList.add("hidden");
    openAuthBtn.classList.remove("hidden");
    planBadge.classList.remove("is-owner");
    if (ownerNav) ownerNav.classList.add("hidden");
    if (ownerSection) ownerSection.classList.add("hidden");
    statusNote.textContent = "Đăng nhập tài khoản Free để nhận ngay 10 lượt dịch miễn phí mỗi tháng.";
    return;
  }

  userPill.classList.remove("hidden");
  openAuthBtn.classList.add("hidden");

  const isOwner = Boolean(state.user.is_owner);
  const isUnlimited = Boolean(state.user.is_unlimited);

  planBadge.textContent = isOwner ? "OWNER" : String(state.user.plan || "free").toUpperCase();
  planBadge.classList.toggle("is-owner", isOwner);
  creditsEl.textContent = isUnlimited ? "∞" : String(state.user.total_remaining ?? 0);
  userBtn.textContent = state.user.full_name;

  if (isOwner) {
    statusNote.textContent = "Tài khoản OWNER — Quyền tối cao • Vô hạn lượt dịch video & quản trị toàn hệ thống.";
  } else if (isUnlimited) {
    statusNote.textContent = `Tài khoản ${state.user.plan_name}: Vô hạn lượt dịch.`;
  } else {
    statusNote.textContent = `Tài khoản ${state.user.plan_name}: còn ${state.user.monthly_remaining} lượt tháng + ${state.user.extra_credits} lượt mua lẻ.`;
  }

  if (ownerNav) ownerNav.classList.toggle("hidden", !isOwner);
  if (ownerSection) ownerSection.classList.toggle("hidden", !isOwner);
  if (isOwner) {
    loadOwnerOverview();
  }
}

async function restoreSession() {
  await loadSystemStatus();
  if (!state.token) {
    renderUserUI();
    return;
  }
  try {
    const res = await apiFetch("/api/auth/me");
    state.user = res.user;
    if (res.maintenance) updateMaintenanceUI(res.maintenance);
    renderUserUI();
    loadJobHistory();
  } catch {
    state.token = "";
    state.user = null;
    localStorage.removeItem(TOKEN_KEY);
    renderUserUI();
  }
}

function openAuthDialog(tab = "signin") {
  switchAuthTab(tab);
  document.getElementById("signin-error").classList.add("hidden");
  document.getElementById("signup-error").classList.add("hidden");
  document.getElementById("auth-dialog").showModal();
}

function switchAuthTab(tab) {
  document.querySelectorAll("[data-auth-tab]").forEach((btn) => {
    btn.classList.toggle("is-active", btn.getAttribute("data-auth-tab") === tab);
  });
  document.getElementById("signin-form").classList.toggle("hidden", tab !== "signin");
  document.getElementById("signup-form").classList.toggle("hidden", tab !== "signup");
}

function setupAuth() {
  document.getElementById("open-auth-btn").addEventListener("click", () => openAuthDialog("signin"));
  document.querySelectorAll("[data-auth-tab]").forEach((btn) => {
    btn.addEventListener("click", () => switchAuthTab(btn.getAttribute("data-auth-tab")));
  });

  document.querySelectorAll(".toggle-pw-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const input = document.getElementById(btn.getAttribute("data-target"));
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      btn.textContent = showing ? "Hiện" : "Ẩn";
    });
  });

  document.getElementById("signin-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errEl = document.getElementById("signin-error");
    errEl.classList.add("hidden");
    try {
      const data = await apiFetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: document.getElementById("signin-email").value,
          password: document.getElementById("signin-password").value,
        }),
      });
      state.token = data.token;
      state.user = data.user;
      localStorage.setItem(TOKEN_KEY, data.token);
      renderUserUI();
      loadJobHistory();
      closeTopLayer(document.getElementById("auth-dialog"));
      showToast(`Chào mừng ${data.user.full_name} quay lại!`);
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove("hidden");
    }
  });

  document.getElementById("signup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errEl = document.getElementById("signup-error");
    errEl.classList.add("hidden");
    try {
      const data = await apiFetch("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          full_name: document.getElementById("signup-name").value,
          email: document.getElementById("signup-email").value,
          password: document.getElementById("signup-password").value,
        }),
      });
      state.token = data.token;
      state.user = data.user;
      localStorage.setItem(TOKEN_KEY, data.token);
      renderUserUI();
      loadJobHistory();
      closeTopLayer(document.getElementById("auth-dialog"));
      showToast("Đăng ký thành công! Bạn được tặng 10 lượt dịch Free/tháng.");
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove("hidden");
    }
  });

  document.getElementById("logout-btn").addEventListener("click", async () => {
    try {
      await apiFetch("/api/auth/logout", { method: "POST" });
    } catch {
      /* ignore */
    }
    state.token = "";
    state.user = null;
    localStorage.removeItem(TOKEN_KEY);
    renderUserUI();
    renderHistoryList([]);
    showToast("Đã đăng xuất tài khoản.");
  });
}

// ============================================================
// Live Preview & Studio Controls
// ============================================================
function updateLiveBlurPreview() {
  const enabled = document.getElementById("blur-enabled-checkbox").checked;
  const y = Number(document.getElementById("blur-y-range").value);
  const h = Number(document.getElementById("blur-h-range").value);
  const w = Number(document.getElementById("blur-w-range").value);
  const r = Number(document.getElementById("blur-r-range").value);

  document.getElementById("blur-y-val").textContent = `${y}%`;
  document.getElementById("blur-h-val").textContent = `${h}%`;
  document.getElementById("blur-w-val").textContent = `${w}%`;
  document.getElementById("blur-r-val").textContent = `${r}px`;

  const bar = document.getElementById("live-blur-bar");
  const left = (100 - w) / 2;
  bar.style.top = `${y}%`;
  bar.style.height = `${h}%`;
  bar.style.left = `${left}%`;
  bar.style.width = `${w}%`;
  bar.style.backdropFilter = enabled ? `blur(${Math.round(r * 0.8)}px)` : "none";
  bar.style.background = enabled ? "rgba(6, 10, 20, 0.88)" : "transparent";
  bar.style.borderStyle = enabled ? "dashed" : "dotted";
}

function setupStudioControls() {
  document.querySelectorAll("[data-source-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.getAttribute("data-source-tab");
      state.sourceType = tab;
      document.querySelectorAll("[data-source-tab]").forEach((b) => {
        const active = b === btn;
        b.classList.toggle("is-active", active);
        b.setAttribute("aria-selected", String(active));
      });
      document.getElementById("tab-panel-url").classList.toggle("hidden", tab !== "url");
      document.getElementById("tab-panel-upload").classList.toggle("hidden", tab !== "upload");
    });
  });

  const voicePreviewBtn = document.getElementById("voice-preview-btn");
  const voicePreviewAudio = document.getElementById("voice-preview-audio");
  const voiceSelect = document.getElementById("voice-select");
  const ttsSpeedRange = document.getElementById("tts-speed-range");
  const ttsSpeedVal = document.getElementById("tts-speed-val");

  if (ttsSpeedRange && ttsSpeedVal) {
    ttsSpeedRange.addEventListener("input", () => {
      ttsSpeedVal.textContent = `${Number(ttsSpeedRange.value).toFixed(2)}x`;
    });
  }

  if (voicePreviewBtn && voicePreviewAudio && voiceSelect) {
    voicePreviewBtn.addEventListener("click", async () => {
      const voiceId = voiceSelect.value;
      const spd = ttsSpeedRange ? Number(ttsSpeedRange.value || 1.2) : 1.2;
      const origLabel = "Nghe thử";
      voicePreviewBtn.disabled = true;
      voicePreviewBtn.textContent = "Đang tải...";
      try {
        await resolveApiBase();
        const srcUrl = resolveMediaUrl(
          `/api/voices/preview?voice_id=${encodeURIComponent(voiceId)}&speed=${encodeURIComponent(spd)}&t=${Date.now()}`
        );
        voicePreviewAudio.pause();
        voicePreviewAudio.src = srcUrl;
        await voicePreviewAudio.play();
        voicePreviewBtn.textContent = "Đang phát...";
      } catch (err) {
        voicePreviewBtn.textContent = origLabel;
        showToast("Không phát được giọng mẫu. Kiểm tra kết nối máy chủ backend.");
      } finally {
        voicePreviewBtn.disabled = false;
      }
    });
    voicePreviewAudio.addEventListener("ended", () => {
      voicePreviewBtn.textContent = "Nghe thử";
    });
    voicePreviewAudio.addEventListener("error", () => {
      voicePreviewBtn.textContent = "Nghe thử";
      voicePreviewBtn.disabled = false;
    });
  }

  const origRange = document.getElementById("orig-vol-range");
  const ttsRange = document.getElementById("tts-vol-range");
  const bgmRange = document.getElementById("bgm-vol-range");

  origRange.addEventListener("input", () => {
    document.getElementById("orig-vol-val").textContent = `${origRange.value}%`;
  });
  ttsRange.addEventListener("input", () => {
    document.getElementById("tts-vol-val").textContent = `${ttsRange.value}%`;
  });
  bgmRange.addEventListener("input", () => {
    if (Number(bgmRange.value) > 30) bgmRange.value = "30";
    document.getElementById("bgm-vol-val").textContent = `${bgmRange.value}%`;
  });

  document.getElementById("mute-orig-btn").addEventListener("click", () => {
    origRange.value = "0";
    document.getElementById("orig-vol-val").textContent = "0%";
  });

  const applyAllSpeedBtn = document.getElementById("apply-global-speed-all-btn");
  if (applyAllSpeedBtn) {
    applyAllSpeedBtn.addEventListener("click", () => {
      const targetSpd = Number(document.getElementById("tts-speed-range")?.value || 1.2);
      state.pass2Segments.forEach((seg) => {
        seg.speed = Number(targetSpd.toFixed(2));
      });
      renderSubtitlesEditor(state.pass2Segments);
      showToast(`Đã áp dụng tốc độ ${targetSpd.toFixed(2)}x cho toàn bộ ${state.pass2Segments.length} câu!`);
    });
  }

  const autoFitAllBtn = document.getElementById("auto-fit-speed-all-btn");
  if (autoFitAllBtn) {
    autoFitAllBtn.addEventListener("click", () => {
      state.pass2Segments.forEach((seg) => {
        const dur = Math.max(0.75, Number(seg.end || 0) - Number(seg.start || 0));
        seg.speed = computeRecommendedLineSpeed(seg.text_vi || "", dur);
      });
      renderSubtitlesEditor(state.pass2Segments);
      showToast("Đã tự động tính tốc độ vừa khít khung thoại cho từng câu!");
    });
  }

  ["blur-enabled-checkbox", "blur-y-range", "blur-h-range", "blur-w-range", "blur-r-range"].forEach((id) => {
    document.getElementById(id).addEventListener("input", updateLiveBlurPreview);
    document.getElementById(id).addEventListener("change", updateLiveBlurPreview);
  });
  updateLiveBlurPreview();

  document.getElementById("back-preview-btn").addEventListener("click", () => {
    document.getElementById("result-video-player").classList.add("hidden");
    document.getElementById("blur-preview-canvas").classList.remove("hidden");
  });
}

// ============================================================
// Job Submission, Polling & 2-Pass Subtitle Editor
// ============================================================
function setStageMode(mode) {
  const preview = document.getElementById("blur-preview-canvas");
  const player = document.getElementById("result-video-player");
  if (mode === "video") {
    preview.classList.add("hidden");
    player.classList.remove("hidden");
  } else {
    player.classList.add("hidden");
    preview.classList.remove("hidden");
  }
}

function updateJobUI(job) {
  const pill = document.getElementById("job-status-pill");
  const stepLabel = document.getElementById("progress-step-label");
  const pctLabel = document.getElementById("progress-pct-label");
  const fill = document.getElementById("progress-fill");
  const track = document.getElementById("progress-bar-track");
  const errBox = document.getElementById("job-error-box");
  const resultActions = document.getElementById("result-actions");

  const pct = Math.max(0, Math.min(100, Number(job.progress || 0)));
  fill.style.inlineSize = `${pct}%`;
  pctLabel.textContent = `${pct}%`;
  track.setAttribute("aria-valuenow", String(pct));
  stepLabel.textContent = job.step_message || "Đang xử lý...";

  const curStep = Number(job.current_step || 0);
  document.querySelectorAll("#pipeline-steps li").forEach((li) => {
    const s = Number(li.getAttribute("data-step"));
    li.classList.toggle("is-done", job.status === "completed" || s < curStep);
    li.classList.toggle("is-active", job.status === "running" && s === curStep);
  });

  pill.className = "status-pill";
  if (job.status === "completed") {
    pill.classList.add("is-done");
    pill.textContent = "Hoàn tất";
  } else if (job.status === "failed") {
    pill.classList.add("is-fail");
    pill.textContent = "Lỗi";
  } else {
    pill.classList.add("is-running");
    pill.textContent = "Đang xử lý";
  }

  if (job.error_message) {
    errBox.textContent = job.error_message;
    errBox.classList.remove("hidden");
  } else {
    errBox.classList.add("hidden");
  }

  if (Array.isArray(job.pass2_subtitles)) {
    renderSubtitlesEditor(job.pass2_subtitles);
  }

  if (job.status === "completed" && job.output_video_url) {
    const resolvedVideoUrl = resolveMediaUrl(job.output_video_url);
    const player = document.getElementById("result-video-player");
    if (player.getAttribute("data-src") !== resolvedVideoUrl) {
      player.setAttribute("data-src", resolvedVideoUrl);
      player.src = resolvedVideoUrl;
    }
    setStageMode("video");
    const dlLink = document.getElementById("download-output-link");
    dlLink.href = resolvedVideoUrl;
    dlLink.download = `Zay_Vietsub_${job.job_id}.mp4`;
    resultActions.classList.remove("hidden");
  }
}

function computeRecommendedLineSpeed(textVi, durSec) {
  const words = String(textVi || "")
    .trim()
    .split(/\s+/)
    .filter(Boolean).length;
  const wps = words / Math.max(0.75, Number(durSec || 1));
  if (wps >= 6.8) return 1.45;
  if (wps >= 5.8) return 1.35;
  if (wps >= 4.8) return 1.25;
  if (wps >= 3.8) return 1.2;
  return 1.15;
}

function renderSubtitlesEditor(segments) {
  const defaultGlobalSpeed = Number(document.getElementById("tts-speed-range")?.value || 1.2);
  state.pass2Segments = segments.map((s) => {
    const dur = Math.max(0.75, Number(s.end || 0) - Number(s.start || 0));
    const initSpeed =
      s.speed !== undefined && s.speed !== null && !Number.isNaN(Number(s.speed))
        ? Math.max(0.75, Math.min(2.0, Number(s.speed)))
        : computeRecommendedLineSpeed(s.text_vi || "", dur) || defaultGlobalSpeed;
    return {
      ...s,
      speed: Number(initSpeed.toFixed(2)),
    };
  });

  const wrap = document.getElementById("subtitles-editor-wrap");
  const list = document.getElementById("subtitles-list");
  const badge = document.getElementById("sub-count-badge");

  if (!segments || segments.length === 0) {
    wrap.classList.add("hidden");
    return;
  }

  wrap.classList.remove("hidden");
  badge.textContent = `${segments.length} câu`;
  list.innerHTML = "";

  state.pass2Segments.forEach((seg, idx) => {
    const item = document.createElement("div");
    item.className = "sub-item";
    const durSec = Math.max(0.5, Number(seg.end || 0) - Number(seg.start || 0));

    const meta = document.createElement("div");
    meta.className = "sub-meta";
    meta.innerHTML = `<span>#${idx + 1} • ${fmtTime(seg.start)} → ${fmtTime(seg.end)} (${durSec.toFixed(1)}s)</span><span>${seg.refined ? "Đã tối ưu ngữ cảnh (Lượt 2)" : "Chuẩn Vietsub"}</span>`;

    const orig = document.createElement("div");
    orig.className = "sub-orig";
    orig.textContent = `Gốc: ${seg.original || ""}`;

    const pass1 = document.createElement("div");
    pass1.className = "sub-pass1";
    pass1.textContent = `Lượt 1 (thô): ${seg.pass1_vi || ""}`;

    const input = document.createElement("input");
    input.type = "text";
    input.className = "sub-edit-input";
    input.value = seg.text_vi || "";
    input.setAttribute("aria-label", `Chỉnh sửa câu Vietsub số ${idx + 1}`);
    input.addEventListener("input", () => {
      state.pass2Segments[idx].text_vi = input.value;
    });

    const speedRow = document.createElement("div");
    speedRow.className = "sub-speed-row";

    const speedLabel = document.createElement("label");
    speedLabel.className = "sub-speed-label";
    speedLabel.textContent = `Tốc độ nói câu #${idx + 1}:`;

    const speedSlider = document.createElement("input");
    speedSlider.type = "range";
    speedSlider.className = "range-input sub-speed-range";
    speedSlider.min = "0.75";
    speedSlider.max = "2.00";
    speedSlider.step = "0.05";
    speedSlider.value = String(seg.speed || 1.2);
    speedSlider.setAttribute("aria-label", `Tốc độ nói câu số ${idx + 1}`);

    const speedBadge = document.createElement("output");
    speedBadge.className = "mono-val sub-speed-badge";
    speedBadge.textContent = `${Number(seg.speed || 1.2).toFixed(2)}x`;

    const setLineSpeed = (val) => {
      const clamped = Math.max(0.75, Math.min(2.0, Math.round(Number(val) * 20) / 20));
      state.pass2Segments[idx].speed = Number(clamped.toFixed(2));
      speedSlider.value = String(clamped);
      speedBadge.textContent = `${clamped.toFixed(2)}x`;
    };

    speedSlider.addEventListener("input", () => {
      setLineSpeed(speedSlider.value);
    });

    const btnSlow = document.createElement("button");
    btnSlow.type = "button";
    btnSlow.className = "btn btn-ghost btn-xs";
    btnSlow.textContent = "-0.1x";
    btnSlow.title = "Giảm tốc độ câu này 0.1x";
    btnSlow.addEventListener("click", () => {
      setLineSpeed(Number(state.pass2Segments[idx].speed || 1.2) - 0.1);
    });

    const btnFast = document.createElement("button");
    btnFast.type = "button";
    btnFast.className = "btn btn-ghost btn-xs";
    btnFast.textContent = "+0.1x";
    btnFast.title = "Tăng tốc độ câu này 0.1x";
    btnFast.addEventListener("click", () => {
      setLineSpeed(Number(state.pass2Segments[idx].speed || 1.2) + 0.1);
    });

    const btnAuto = document.createElement("button");
    btnAuto.type = "button";
    btnAuto.className = "btn btn-ghost btn-xs";
    btnAuto.textContent = "Auto khít";
    btnAuto.title = "Tự tính tốc độ vừa khít độ dài câu này";
    btnAuto.addEventListener("click", () => {
      const rec = computeRecommendedLineSpeed(state.pass2Segments[idx].text_vi || "", durSec);
      setLineSpeed(rec);
    });

    const btnPreviewLine = document.createElement("button");
    btnPreviewLine.type = "button";
    btnPreviewLine.className = "btn btn-outline btn-xs sub-preview-btn";
    btnPreviewLine.textContent = "Nghe thử câu";
    btnPreviewLine.addEventListener("click", async () => {
      const txt = String(state.pass2Segments[idx].text_vi || "").trim();
      if (!txt) {
        showToast("Câu thoại trống.");
        return;
      }
      const voiceId = document.getElementById("voice-select")?.value || "vi-female-review";
      const spd = Number(state.pass2Segments[idx].speed || 1.2);
      const audioEl = document.getElementById("voice-preview-audio");
      btnPreviewLine.disabled = true;
      btnPreviewLine.textContent = "Đang tạo...";
      try {
        await resolveApiBase();
        const resp = await fetch(resolveMediaUrl("/api/tts/preview-line"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text_vi: txt, voice_id: voiceId, speed: spd }),
        });
        if (!resp.ok) throw new Error("Không tạo được bản nghe thử.");
        const blob = await resp.blob();
        const blobUrl = URL.createObjectURL(blob);
        if (audioEl) {
          audioEl.pause();
          audioEl.src = blobUrl;
          await audioEl.play();
        }
        btnPreviewLine.textContent = "Đang đọc...";
        if (audioEl) {
          audioEl.onended = () => {
            btnPreviewLine.textContent = "Nghe thử câu";
            URL.revokeObjectURL(blobUrl);
          };
        }
      } catch (err) {
        showToast("Không phát được bản nghe thử câu này.");
        btnPreviewLine.textContent = "Nghe thử câu";
      } finally {
        btnPreviewLine.disabled = false;
      }
    });

    speedRow.append(speedLabel, speedSlider, speedBadge, btnSlow, btnFast, btnAuto, btnPreviewLine);
    item.append(meta, orig, pass1, input, speedRow);
    list.appendChild(item);
  });
}

function startPollingJob(jobId) {
  state.currentJobId = jobId;
  if (state.pollTimer) clearInterval(state.pollTimer);

  const pollOnce = async () => {
    try {
      const res = await apiFetch(`/api/jobs/${jobId}`);
      const job = res.job;
      updateJobUI(job);
      if (job.status === "completed" || job.status === "failed") {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        document.getElementById("start-translate-btn").disabled = false;
        const rerenderBtn = document.getElementById("rerender-btn");
        if (rerenderBtn) rerenderBtn.disabled = false;
        restoreSession();
      }
    } catch (err) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      document.getElementById("start-translate-btn").disabled = false;
      showToast(err.message);
    }
  };

  pollOnce();
  state.pollTimer = setInterval(pollOnce, 950);
}

function collectSettingsFromForm() {
  const blurW = Number(document.getElementById("blur-w-range").value);
  return {
    auto_trim_10m: document.getElementById("auto-trim-checkbox").checked,
    original_volume: Number(document.getElementById("orig-vol-range").value),
    tts_volume: Number(document.getElementById("tts-vol-range").value),
    tts_speed: Number(document.getElementById("tts-speed-range")?.value || 1.2),
    bgm_volume: Math.min(30, Number(document.getElementById("bgm-vol-range").value)),
    bgm_preset: document.getElementById("bgm-preset-select").value,
    voice_id: document.getElementById("voice-select").value,
    blur_enabled: document.getElementById("blur-enabled-checkbox").checked,
    blur_x_pct: Number(((100 - blurW) / 2).toFixed(2)),
    blur_w_pct: blurW,
    blur_y_pct: Number(document.getElementById("blur-y-range").value),
    blur_h_pct: Number(document.getElementById("blur-h-range").value),
    blur_radius: Number(document.getElementById("blur-r-range").value),
  };
}

function setupTranslateSubmit() {
  const form = document.getElementById("translate-form");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.user) {
      showToast("Vui lòng đăng nhập hoặc tạo tài khoản Free (10 lượt/tháng) trước.");
      openAuthDialog("signup");
      return;
    }

    const sourceUrl = document.getElementById("source-url-input").value.trim();
    const videoFile = document.getElementById("video-file-input").files[0];
    if (state.sourceType === "url" && !sourceUrl) {
      showToast("Vui lòng dán link video Bilibili, YouTube, Douyin hoặc TikTok.");
      return;
    }
    if (state.sourceType === "upload" && !videoFile) {
      showToast("Vui lòng chọn file video MP4/MOV/MKV để tải lên.");
      return;
    }

    const btn = document.getElementById("start-translate-btn");
    const rerenderBtn = document.getElementById("rerender-btn");
    btn.disabled = true;
    if (rerenderBtn) rerenderBtn.disabled = true;
    setStageMode("preview");
    document.getElementById("result-actions").classList.add("hidden");
    document.getElementById("job-error-box").classList.add("hidden");

    try {
      const fd = new FormData();
      const s = collectSettingsFromForm();
      fd.append("source_type", state.sourceType);
      fd.append("source_url", sourceUrl);
      fd.append("source_lang", document.getElementById("source-lang-select").value);
      Object.entries(s).forEach(([k, v]) => fd.append(k, String(v)));

      if (state.sourceType === "upload" && videoFile) {
        fd.append("video_file", videoFile);
      }
      const bgmFile = document.getElementById("bgm-file-input").files[0];
      if (bgmFile) {
        fd.append("bgm_file", bgmFile);
      }

      const data = await apiFetch("/api/jobs/create", { method: "POST", body: fd });
      state.user = data.user;
      renderUserUI();
      showToast("Đã khởi tạo tác vụ dịch video! Đang xử lý...");
      startPollingJob(data.job_id);
    } catch (err) {
      btn.disabled = false;
      if (rerenderBtn) rerenderBtn.disabled = false;
      showToast(err.message);
      if (err.message.includes("hết lượt")) {
        document.getElementById("pricing").scrollIntoView({ behavior: "smooth" });
      }
    }
  });

  const rerenderBtn = document.getElementById("rerender-btn");
  rerenderBtn.addEventListener("click", async () => {
    if (!state.currentJobId) return;
    const startBtn = document.getElementById("start-translate-btn");
    rerenderBtn.disabled = true;
    if (startBtn) startBtn.disabled = true;
    setStageMode("preview");
    document.getElementById("result-actions").classList.add("hidden");
    document.getElementById("job-error-box").classList.add("hidden");
    try {
      await apiFetch(`/api/jobs/${state.currentJobId}/rerender`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          pass2_subtitles: state.pass2Segments,
          settings: collectSettingsFromForm(),
        }),
      });
      showToast("Đang render lại video theo chỉnh sửa mới...");
      startPollingJob(state.currentJobId);
    } catch (err) {
      rerenderBtn.disabled = false;
      if (startBtn) startBtn.disabled = false;
      showToast(err.message);
    }
  });
}

// ============================================================
// Membership & Single Credits Checkout
// ============================================================
function setupPricing() {
  const qtyInput = document.getElementById("single-qty-input");
  const buyBtn = document.getElementById("buy-single-btn");

  const updateBuyBtnText = () => {
    const qty = Math.max(1, Math.min(500, Number(qtyInput.value || 1)));
    buyBtn.textContent = `Mua ${qty} Lượt — ${fmtVnd(qty * 3000)}`;
  };
  qtyInput.addEventListener("input", updateBuyBtnText);
  updateBuyBtnText();

  document.querySelectorAll(".choose-plan-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!state.user) {
        showToast("Đăng nhập trước khi chọn gói hội viên.");
        openAuthDialog("signup");
        return;
      }
      const plan = btn.getAttribute("data-plan");
      const meta = {
        free: { label: "Hội viên Free", quota: "10 lần dịch / tháng", price: 0 },
        pro: { label: "Hội viên Pro", quota: "30 lần dịch / tháng", price: 50000 },
        ultra: { label: "Hội viên Ultra", quota: "60 lần dịch / tháng", price: 100000 },
      }[plan];
      state.checkoutIntent = { type: "plan", plan };
      document.getElementById("checkout-summary").innerHTML = `
        <strong>${meta.label}</strong>
        <span>Quyền lợi: ${meta.quota}</span>
        <span>Thanh toán: <strong>${fmtVnd(meta.price)}</strong></span>
      `;
      document.getElementById("checkout-ref").textContent = `ZAY-${plan.toUpperCase()}-${state.user.id}`;
      document.getElementById("checkout-error").classList.add("hidden");
      document.getElementById("pricing-dialog").showModal();
    });
  });

  buyBtn.addEventListener("click", () => {
    if (!state.user) {
      showToast("Đăng nhập trước khi mua thêm lượt dịch lẻ.");
      openAuthDialog("signup");
      return;
    }
    const qty = Math.max(1, Math.min(500, Number(qtyInput.value || 1)));
    const total = qty * 3000;
    state.checkoutIntent = { type: "credits", quantity: qty };
    document.getElementById("checkout-summary").innerHTML = `
      <strong>Mua Lẻ ${qty} Lượt Dịch</strong>
      <span>Đơn giá: 3.000đ / 1 lần dịch (Cộng dồn không hết hạn)</span>
      <span>Tổng thanh toán: <strong>${fmtVnd(total)}</strong></span>
    `;
    document.getElementById("checkout-ref").textContent = `ZAY-CREDITS-${qty}-${state.user.id}`;
    document.getElementById("checkout-error").classList.add("hidden");
    document.getElementById("pricing-dialog").showModal();
  });

  document.getElementById("confirm-checkout-btn").addEventListener("click", async () => {
    const errEl = document.getElementById("checkout-error");
    errEl.classList.add("hidden");
    if (!state.checkoutIntent) return;
    try {
      let res;
      if (state.checkoutIntent.type === "plan") {
        res = await apiFetch("/api/billing/upgrade", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ plan: state.checkoutIntent.plan }),
        });
      } else {
        res = await apiFetch("/api/billing/buy-credits", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ quantity: state.checkoutIntent.quantity }),
        });
      }
      state.user = res.user;
      renderUserUI();
      closeTopLayer(document.getElementById("pricing-dialog"));
      showToast(res.message);
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove("hidden");
    }
  });
}

// ============================================================
// History & Header Controls
// ============================================================
function renderHistoryList(jobs) {
  const container = document.getElementById("history-list");
  if (!state.user) {
    container.innerHTML = `<div class="empty-state glass-card">Đăng nhập để xem lại các video bạn đã dịch và tải về bất kỳ lúc nào.</div>`;
    return;
  }
  if (!jobs || jobs.length === 0) {
    container.innerHTML = `<div class="empty-state glass-card">Bạn chưa có video nào trong lịch sử. Hãy thử dịch một video ngay ở Studio phía trên!</div>`;
    return;
  }

  container.innerHTML = "";
  jobs.forEach((job) => {
    const card = document.createElement("article");
    card.className = "history-card glass-card";
    const statusText =
      job.status === "completed" ? "Hoàn tất" : job.status === "failed" ? "Lỗi" : "Đang chạy";
    card.innerHTML = `
      <div class="history-card-top">
        <span class="badge-soft">${statusText}</span>
        <span class="history-meta">${fmtTime(job.duration || 0)}</span>
      </div>
      <h3 class="history-title"></h3>
      <div class="history-meta">${job.step_message || ""}</div>
      <button type="button" class="btn btn-outline btn-sm">Mở trong Studio</button>
    `;
    card.querySelector(".history-title").textContent = job.title || "Video không tên";
    card.querySelector("button").addEventListener("click", () => {
      state.currentJobId = job.job_id;
      updateJobUI(job);
      document.getElementById("studio").scrollIntoView({ behavior: "smooth" });
    });
    container.appendChild(card);
  });
}

async function loadJobHistory() {
  if (!state.token) return;
  try {
    const res = await apiFetch("/api/jobs");
    renderHistoryList(res.jobs || []);
  } catch {
    /* ignore */
  }
}

// ============================================================
// Owner Admin Panel (Members, Billing History & Maintenance)
// ============================================================
function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fmtDateTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("vi-VN");
  } catch {
    return String(iso);
  }
}

function txTypeLabel(txType) {
  const map = {
    signup_bonus: "Tạo tài khoản Free",
    plan_upgrade: "Mua / Nâng cấp gói",
    single_credits: "Mua lượt dịch lẻ",
    admin_adjust: "Owner điều chỉnh",
  };
  return map[txType] || String(txType || "Khác");
}

function renderOwnerTxFilterOptions() {
  const select = document.getElementById("owner-tx-filter-select");
  if (!select) return;
  const current = String(state.ownerTxFilterUserId || "");
  select.innerHTML = `<option value="">Tất cả thành viên</option>`;
  state.ownerUsers.forEach((u) => {
    const opt = document.createElement("option");
    opt.value = String(u.id);
    opt.textContent = `${u.full_name} (${u.email}) — ${u.tx_count || 0} GD`;
    select.appendChild(opt);
  });
  select.value = current;
}

function renderOwnerTransactions(txList = state.ownerTransactions, filterUserId = state.ownerTxFilterUserId) {
  const tbody = document.getElementById("owner-tx-tbody");
  const subtitle = document.getElementById("owner-tx-subtitle");
  if (!tbody) return;

  const uidStr = String(filterUserId || "");
  if (subtitle) {
    if (!uidStr) {
      subtitle.textContent = `Đang hiển thị ${txList.length} giao dịch gần nhất trên toàn hệ thống.`;
    } else {
      const targetUser = state.ownerUsers.find((u) => String(u.id) === uidStr);
      const userLabel = targetUser ? `${targetUser.full_name} (${targetUser.email})` : `ID #${uidStr}`;
      subtitle.textContent = `Lịch sử mua gói & nạp lượt của ${userLabel} — ${txList.length} giao dịch.`;
    }
  }

  if (!txList || txList.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="hint-text">Chưa có giao dịch mua gói / nạp lượt nào.</td></tr>`;
    return;
  }

  tbody.innerHTML = txList
    .map((tx) => {
      const credits = Number(tx.credits_added || 0);
      const creditsText = credits >= 0 ? `+${credits} lượt` : `${credits} lượt`;
      const memberName = tx.user_name || `User #${tx.user_id}`;
      const memberEmail = tx.user_email || "";
      return `
        <tr>
          <td>${escapeHtml(fmtDateTime(tx.created_at))}</td>
          <td>
            <div class="admin-user-cell">
              <strong>${escapeHtml(memberName)}</strong>
              <span class="admin-user-email">${escapeHtml(memberEmail)}</span>
            </div>
          </td>
          <td><span class="badge-soft">${escapeHtml(txTypeLabel(tx.tx_type))}</span></td>
          <td><strong>${escapeHtml(creditsText)}</strong></td>
          <td><strong>${escapeHtml(fmtVnd(tx.amount_vnd || 0))}</strong></td>
          <td>${escapeHtml(tx.description || "")}</td>
        </tr>
      `;
    })
    .join("");
}

async function inspectMemberTransactions(userId) {
  const uidStr = String(userId || "");
  state.ownerTxFilterUserId = uidStr;
  const select = document.getElementById("owner-tx-filter-select");
  if (select) select.value = uidStr;

  if (!uidStr) {
    renderOwnerTransactions(state.ownerTransactions, "");
    return;
  }
  try {
    const res = await apiFetch(`/api/admin/users/${uidStr}/transactions`);
    renderOwnerTransactions(res.transactions || [], uidStr);
    document.getElementById("owner-tx-panel")?.scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    showToast(err.message);
  }
}

function renderOwnerUsers() {
  const tbody = document.getElementById("owner-users-tbody");
  if (!tbody) return;
  const q = (document.getElementById("owner-user-search")?.value || "").trim().toLowerCase();
  const list = state.ownerUsers.filter((u) => {
    if (!q) return true;
    return (
      String(u.email || "").toLowerCase().includes(q) ||
      String(u.full_name || "").toLowerCase().includes(q)
    );
  });

  if (list.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="hint-text">Không tìm thấy thành viên nào.</td></tr>`;
    return;
  }

  tbody.innerHTML = list
    .map((u) => {
      const uid = Number(u.id);
      const isPrimaryOwner = Boolean(u.is_owner);
      const isBanned = Boolean(u.is_banned);
      const planOptions = ["free", "pro", "ultra", "owner"]
        .map(
          (p) =>
            `<option value="${p}" ${u.plan === p ? "selected" : ""}>${p.toUpperCase()}</option>`
        )
        .join("");

      const creditsHtml = u.is_unlimited
        ? `<strong>∞ Vô hạn</strong><br><span class="admin-user-email">Đặc quyền Owner</span>`
        : `<strong>${Number(u.total_remaining || 0)} lượt</strong><br><span class="admin-user-email">Tháng: ${Number(u.monthly_remaining || 0)}/${Number(u.monthly_quota || 0)} • Lẻ: ${Number(u.extra_credits || 0)}</span>`;

      const statusTag = isPrimaryOwner
        ? `<span class="plan-tag is-owner">OWNER</span>`
        : isBanned
        ? `<span class="badge-soft">ĐÃ KHÓA</span>`
        : "";

      const nonOwnerButtons = isPrimaryOwner
        ? ""
        : `
          <button type="button" class="btn ${isBanned ? "btn-success-outline" : "btn-danger-outline"} btn-sm js-owner-toggle-ban" data-uid="${uid}">
            ${isBanned ? "Mở khóa" : "Khóa"}
          </button>
          <button type="button" class="btn btn-danger-outline btn-sm js-owner-delete" data-uid="${uid}">
            Xóa
          </button>
        `;

      const resetMonthBtn = u.is_unlimited
        ? ""
        : `<button type="button" class="btn btn-ghost btn-sm js-owner-reset-used" data-uid="${uid}">Reset tháng</button>`;

      return `
        <tr data-owner-uid="${uid}">
          <td>
            <div class="admin-user-cell">
              <strong>${escapeHtml(u.full_name)} ${statusTag}</strong>
              <span class="admin-user-email">${escapeHtml(u.email)} • ID #${uid} • ${Number(u.jobs_count || 0)} video</span>
            </div>
          </td>
          <td>
            <div class="admin-inline-group">
              <select class="select-input js-owner-plan-select" data-uid="${uid}" ${isPrimaryOwner ? "disabled" : ""}>
                ${planOptions}
              </select>
              ${
                isPrimaryOwner
                  ? ""
                  : `<button type="button" class="btn btn-outline btn-sm js-owner-save-plan" data-uid="${uid}">Đổi gói</button>`
              }
            </div>
          </td>
          <td>
            <div>${creditsHtml}</div>
            <div class="admin-inline-group" style="margin-top:0.35rem;">
              <input type="number" class="admin-credit-input js-owner-credit-input" value="10" min="-999" max="9999" aria-label="Số lượt cộng/trừ">
              <button type="button" class="btn btn-outline btn-sm js-owner-add-credit" data-uid="${uid}">Cộng lượt</button>
              ${resetMonthBtn}
            </div>
          </td>
          <td>
            <strong>${escapeHtml(fmtVnd(u.total_spent_vnd || 0))}</strong>
            <br>
            <span class="admin-user-email">${Number(u.tx_count || 0)} giao dịch</span>
          </td>
          <td>
            <div class="admin-inline-group">
              <button type="button" class="btn btn-outline btn-sm js-owner-view-tx" data-uid="${uid}">
                Xem nạp (${Number(u.tx_count || 0)})
              </button>
              ${nonOwnerButtons}
            </div>
          </td>
        </tr>
      `;
    })
    .join("");
}

async function loadOwnerOverview() {
  if (!state.token || !state.user?.is_owner) return;
  try {
    const data = await apiFetch("/api/admin/overview");
    state.ownerUsers = data.users || [];
    state.ownerTransactions = data.transactions || [];
    const statUsers = document.getElementById("owner-stat-users");
    const statRev = document.getElementById("owner-stat-revenue");
    if (statUsers) statUsers.textContent = String(state.ownerUsers.length);
    if (statRev) statRev.textContent = fmtVnd(data.total_revenue_vnd || 0);
    if (data.maintenance) updateMaintenanceUI(data.maintenance);

    renderOwnerTxFilterOptions();
    renderOwnerUsers();
    if (state.ownerTxFilterUserId) {
      const res = await apiFetch(`/api/admin/users/${state.ownerTxFilterUserId}/transactions`);
      renderOwnerTransactions(res.transactions || [], state.ownerTxFilterUserId);
    } else {
      renderOwnerTransactions(state.ownerTransactions, "");
    }
  } catch (err) {
    showToast(err.message || "Không tải được bảng điều khiển Owner.");
  }
}

function setupOwnerControls() {
  const refreshBtn = document.getElementById("owner-refresh-btn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", async () => {
      await loadOwnerOverview();
      showToast("Đã cập nhật dữ liệu thành viên & doanh thu.");
    });
  }

  const maintToggleBtn = document.getElementById("maintenance-toggle-btn");
  if (maintToggleBtn) {
    maintToggleBtn.addEventListener("click", async () => {
      const msg = document.getElementById("maintenance-message-input")?.value.trim() || "";
      const nextEnabled = !state.maintenance.maintenance_mode;
      maintToggleBtn.disabled = true;
      try {
        const res = await apiFetch("/api/admin/maintenance", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: nextEnabled, message: msg }),
        });
        updateMaintenanceUI(res.maintenance);
        showToast(nextEnabled ? "Đã ĐÓNG Server (chế độ Bảo Trì)." : "Đã MỞ lại Server hoạt động bình thường!");
      } catch (err) {
        showToast(err.message);
      } finally {
        maintToggleBtn.disabled = false;
      }
    });
  }

  const saveMaintBtn = document.getElementById("save-maintenance-btn");
  if (saveMaintBtn) {
    saveMaintBtn.addEventListener("click", async () => {
      const msg = document.getElementById("maintenance-message-input")?.value.trim() || "";
      saveMaintBtn.disabled = true;
      try {
        const res = await apiFetch("/api/admin/maintenance", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: Boolean(state.maintenance.maintenance_mode), message: msg }),
        });
        updateMaintenanceUI(res.maintenance);
        showToast("Đã cập nhật thông báo bảo trì.");
      } catch (err) {
        showToast(err.message);
      } finally {
        saveMaintBtn.disabled = false;
      }
    });
  }

  document.getElementById("owner-user-search")?.addEventListener("input", () => {
    renderOwnerUsers();
  });

  document.getElementById("owner-tx-filter-select")?.addEventListener("change", (e) => {
    inspectMemberTransactions(e.target.value);
  });

  document.getElementById("owner-clear-tx-filter-btn")?.addEventListener("click", () => {
    state.ownerTxFilterUserId = "";
    const sel = document.getElementById("owner-tx-filter-select");
    if (sel) sel.value = "";
    renderOwnerTransactions(state.ownerTransactions, "");
  });

  document.getElementById("owner-users-tbody")?.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-uid]");
    if (!btn) return;
    const uid = Number(btn.getAttribute("data-uid"));
    const row = btn.closest("tr");
    const targetUser = state.ownerUsers.find((u) => Number(u.id) === uid);

    try {
      if (btn.classList.contains("js-owner-view-tx")) {
        await inspectMemberTransactions(uid);
        return;
      }
      if (btn.classList.contains("js-owner-save-plan")) {
        const plan = row.querySelector(".js-owner-plan-select")?.value;
        await apiFetch(`/api/admin/users/${uid}/update`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ plan }),
        });
        showToast(`Đã đổi gói thành viên #${uid} sang ${String(plan).toUpperCase()}.`);
        await loadOwnerOverview();
        return;
      }
      if (btn.classList.contains("js-owner-add-credit")) {
        const delta = Number(row.querySelector(".js-owner-credit-input")?.value || 0);
        if (!delta) {
          showToast("Nhập số lượt khác 0 để cộng hoặc trừ.");
          return;
        }
        await apiFetch(`/api/admin/users/${uid}/update`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            credit_delta: delta,
            note: `Owner điều chỉnh ${delta > 0 ? "+" : ""}${delta} lượt`,
          }),
        });
        showToast(`Đã cập nhật ${delta > 0 ? "+" : ""}${delta} lượt cho thành viên #${uid}.`);
        await loadOwnerOverview();
        return;
      }
      if (btn.classList.contains("js-owner-reset-used")) {
        await apiFetch(`/api/admin/users/${uid}/update`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reset_used: true }),
        });
        showToast(`Đã reset lượt tháng về 0 đã dùng cho thành viên #${uid}.`);
        await loadOwnerOverview();
        return;
      }
      if (btn.classList.contains("js-owner-toggle-ban")) {
        const nextBan = !Boolean(targetUser?.is_banned);
        await apiFetch(`/api/admin/users/${uid}/update`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ is_banned: nextBan }),
        });
        showToast(nextBan ? `Đã khóa tài khoản #${uid}.` : `Đã mở khóa tài khoản #${uid}.`);
        await loadOwnerOverview();
        return;
      }
      if (btn.classList.contains("js-owner-delete")) {
        const label = targetUser ? `${targetUser.full_name} (${targetUser.email})` : `#${uid}`;
        if (!window.confirm(`Xác nhận xóa vĩnh viễn thành viên ${label}?`)) return;
        await apiFetch(`/api/admin/users/${uid}`, { method: "DELETE" });
        if (String(state.ownerTxFilterUserId) === String(uid)) {
          state.ownerTxFilterUserId = "";
        }
        showToast(`Đã xóa thành viên ${label}.`);
        await loadOwnerOverview();
      }
    } catch (err) {
      showToast(err.message);
    }
  });
}

function setupHeaderControls() {
  const motionBtn = document.getElementById("motion-toggle-btn");
  const motionLabel = document.getElementById("motion-toggle-label");
  motionBtn.addEventListener("click", () => {
    const paused = document.body.classList.toggle("motion-paused");
    motionLabel.textContent = paused ? "Bật hiệu ứng" : "Tạm dừng hiệu ứng";
  });

  const openToolModal = () => document.getElementById("download-tool-dialog").showModal();
  document.getElementById("open-tool-btn").addEventListener("click", openToolModal);
  document.getElementById("preview-tool-guide-btn").addEventListener("click", openToolModal);
  document.getElementById("refresh-history-btn").addEventListener("click", loadJobHistory);
  document.getElementById("header-user-btn").addEventListener("click", () => {
    const targetId = state.user?.is_owner ? "owner-section" : "pricing";
    document.getElementById(targetId)?.scrollIntoView({ behavior: "smooth" });
  });
}

// ============================================================
// Init
// ============================================================
document.addEventListener("DOMContentLoaded", async () => {
  setupDialogs();
  setupHeaderControls();
  setupAuth();
  setupStudioControls();
  setupTranslateSubmit();
  setupPricing();
  setupOwnerControls();
  await resolveApiBase();
  restoreSession();
});
