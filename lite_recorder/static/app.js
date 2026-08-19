(() => {
  "use strict";

  const state = { recording: false, startedAt: null, cameras: [] };

  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.from(document.querySelectorAll(sel)); }

  function initTabs() {
    $all(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        $all(".tab-btn").forEach((b) => b.classList.remove("active"));
        $all(".tab").forEach((t) => t.classList.remove("active"));
        btn.classList.add("active");
        $("#tab-" + btn.dataset.tab).classList.add("active");
        if (btn.dataset.tab === "gallery") loadGallery();
      });
    });
  }

  async function loadSystem() {
    const res = await fetch("/api/system");
    const data = await res.json();
    const banner = $("#encoder-banner");
    if (data.encoder && data.encoder.degraded) {
      banner.textContent = "Software encoder in use: " + data.encoder.reason;
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }
  }

  function stateDotClass(s) {
    if (s === "recording") return "recording";
    if (s === "preview") return "preview";
    if (s === "error") return "error";
    return "idle";
  }

  async function loadCameras() {
    const res = await fetch("/api/cameras");
    const cameras = await res.json();
    state.cameras = cameras;
    renderCameraGrid(cameras);
    renderCameraTable(cameras);
  }

  function renderCameraGrid(cameras) {
    const grid = $("#camera-grid");
    const existingIds = new Set($all(".camera-tile").map((t) => t.dataset.id));
    const newIds = new Set(cameras.map((c) => c.id));

    for (const id of existingIds) {
      if (!newIds.has(id)) {
        const el = grid.querySelector(`.camera-tile[data-id="${CSS.escape(id)}"]`);
        if (el) el.remove();
      }
    }

    for (const cam of cameras) {
      let tile = grid.querySelector(`.camera-tile[data-id="${CSS.escape(cam.id)}"]`);
      if (!tile) {
        tile = document.createElement("div");
        tile.className = "camera-tile";
        tile.dataset.id = cam.id;
        tile.innerHTML = `
          <img loading="lazy" />
          <div class="meta">
            <span><span class="dot"></span><span class="label"></span></span>
            <span class="badge"></span>
          </div>
          <div class="error-text"></div>
        `;
        grid.appendChild(tile);
        tile.querySelector("img").src = `/api/cameras/${encodeURIComponent(cam.id)}/stream`;
      }
      tile.querySelector(".label").textContent = cam.label;
      tile.querySelector(".badge").textContent = `${cam.width}x${cam.height} ${cam.fps}fps · ${cam.source}`;
      const dot = tile.querySelector(".dot");
      dot.className = "dot " + stateDotClass(cam.state);
      tile.querySelector(".error-text").textContent = cam.error || "";
    }
  }

  function renderCameraTable(cameras) {
    const tbody = $("#camera-table tbody");
    tbody.innerHTML = "";
    for (const cam of cameras) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><input type="checkbox" data-field="enabled" ${cam.state !== "error" && cam.enabled !== false ? "checked" : ""}></td>
        <td><input type="text" data-field="label" value="${escapeHtml(cam.label)}"></td>
        <td>${cam.source}</td>
        <td>
          <input type="number" data-field="width" value="${cam.width}" style="width:70px">
          x
          <input type="number" data-field="height" value="${cam.height}" style="width:70px">
        </td>
        <td><input type="number" data-field="fps" value="${cam.fps}"></td>
        <td><input type="number" data-field="bitrate_kbps" value="4000"></td>
        <td>${cam.state}${cam.error ? " — " + escapeHtml(cam.error) : ""}</td>
      `;
      tbody.appendChild(tr);
      tr.querySelectorAll("input").forEach((input) => {
        input.addEventListener("change", () => patchCamera(cam.id, tr));
      });
    }
  }

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s || "";
    return div.innerHTML;
  }

  async function patchCamera(id, row) {
    const patch = {};
    row.querySelectorAll("input").forEach((input) => {
      const field = input.dataset.field;
      if (!field) return;
      if (input.type === "checkbox") patch[field] = input.checked;
      else if (input.type === "number") patch[field] = Number(input.value);
      else patch[field] = input.value;
    });
    await fetch(`/api/cameras/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    loadCameras();
  }

  async function loadRecordingStatus() {
    const res = await fetch("/api/recording/status");
    const data = await res.json();
    state.recording = data.recording;
    const btn = $("#record-toggle");
    const status = $("#record-status");
    const timer = $("#record-timer");
    if (data.recording) {
      btn.textContent = "■ Stop Recording";
      btn.classList.add("recording");
      status.textContent = "Recording";
      timer.textContent = formatElapsed(data.elapsed);
    } else {
      btn.textContent = "● Start Recording";
      btn.classList.remove("recording");
      status.textContent = "Not recording";
      timer.textContent = "";
    }
  }

  function formatElapsed(seconds) {
    const s = Math.floor(seconds % 60).toString().padStart(2, "0");
    const m = Math.floor((seconds / 60) % 60).toString().padStart(2, "0");
    const h = Math.floor(seconds / 3600).toString().padStart(2, "0");
    return `${h}:${m}:${s}`;
  }

  async function toggleRecording() {
    const btn = $("#record-toggle");
    btn.disabled = true;
    try {
      if (state.recording) {
        await fetch("/api/recording/stop", { method: "POST" });
      } else {
        await fetch("/api/recording/start", { method: "POST" });
      }
    } finally {
      btn.disabled = false;
      loadRecordingStatus();
      loadCameras();
    }
  }

  async function rescanCameras() {
    await fetch("/api/cameras/rescan", { method: "POST" });
    loadCameras();
  }

  async function loadGallery() {
    const res = await fetch("/api/recordings");
    const sessions = await res.json();
    const container = $("#gallery-list");
    container.innerHTML = "";
    if (sessions.length === 0) {
      container.innerHTML = '<p style="color:var(--muted)">No recordings yet.</p>';
      return;
    }
    for (const session of sessions) {
      const div = document.createElement("div");
      div.className = "session";
      const filesHtml = session.files
        .map((f) => {
          if (f.status !== "complete") {
            return `<div class="file-card failed">
              <div class="file-meta"><span>${escapeHtml(f.label)}</span></div>
              <div class="error-text">${escapeHtml(f.error || "recording failed")}</div>
            </div>`;
          }
          return `<div class="file-card">
            <video controls preload="none" poster="${f.thumbnail_url}" src="${f.url}"></video>
            <div class="file-meta"><span>${escapeHtml(f.label)}</span><span>${formatBytes(f.size_bytes)}</span></div>
            <div class="file-actions">
              <a href="${f.url}" download>Download</a>
              <button data-path="${f.url.replace("/media/", "")}">Delete</button>
            </div>
          </div>`;
        })
        .join("");
      div.innerHTML = `
        <div class="session-header">
          <span>${session.date} ${session.time}</span>
          <span>${session.files.length} camera(s)</span>
        </div>
        <div class="files">${filesHtml}</div>
      `;
      container.appendChild(div);
    }
    container.querySelectorAll("button[data-path]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Delete this recording permanently?")) return;
        await fetch("/api/recordings/" + btn.dataset.path, { method: "DELETE" });
        loadGallery();
      });
    });
  }

  function formatBytes(bytes) {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    let n = bytes;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i++;
    }
    return `${n.toFixed(1)} ${units[i]}`;
  }

  function init() {
    initTabs();
    $("#record-toggle").addEventListener("click", toggleRecording);
    $("#rescan-btn").addEventListener("click", rescanCameras);
    loadSystem();
    loadCameras();
    loadRecordingStatus();
    setInterval(loadCameras, 4000);
    setInterval(loadRecordingStatus, 1000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
