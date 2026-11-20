/* ClipPilot AI dashboard — vanilla JS, no framework, no CDN. */
const $ = (id) => document.getElementById(id);
const token = () => localStorage.getItem("clippilot_token");

function showMsg(el, text, ok) {
  el.textContent = text;
  el.className = "msg " + (ok ? "ok" : "error");
}

async function api(path, opts = {}) {
  const headers = opts.headers || {};
  const t = token();
  if (t) headers["Authorization"] = "Bearer " + t;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) { logout(); throw new Error("Session expired — please log in again."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || ("Request failed: " + res.status));
  return data;
}

function logout() {
  localStorage.removeItem("clippilot_token");
  localStorage.removeItem("clippilot_user");
  $("app").style.display = "none";
  $("loginSection").style.display = "block";
  $("authBox").innerHTML = "";
}

async function login(e) {
  e.preventDefault();
  try {
    const data = await api("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("loginEmail").value, password: $("loginPassword").value }),
    });
    localStorage.setItem("clippilot_token", data.access_token);
    localStorage.setItem("clippilot_user", data.name + " (" + data.role + ")");
    afterLogin();
  } catch (err) { showMsg($("loginMsg"), err.message); }
}

function afterLogin() {
  $("loginSection").style.display = "none";
  $("app").style.display = "block";
  $("authBox").innerHTML = "Logged in as <b>" + localStorage.getItem("clippilot_user") + "</b> "
    + '<button class="small" onclick="logout()">Log out</button>';
  refreshAll();
}

function refreshAll() { loadVideos(); loadClips(); loadAccounts(); loadBilling(); loadApiKeys(); loadBroll(); }

document.querySelectorAll(".tabs button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "editor") loadEditorOptions();
  });
});

function pollTask(taskId, label) {
  const box = $("taskBox");
  box.innerHTML = "<div class='row'><b>" + label + "</b><div class='progress'><div style='width:0%'></div></div><div class='muted'>starting…</div></div>";
  const bar = box.querySelector(".progress > div");
  const txt = box.querySelector(".muted");
  const timer = setInterval(async () => {
    try {
      const t = await api("/api/videos/tasks/" + taskId);
      bar.style.width = t.progress + "%";
      txt.textContent = t.stage + " (" + t.progress + "%)";
      if (t.status === "done") {
        clearInterval(timer);
        txt.textContent = "Done ✓ " + JSON.stringify(t.result);
        refreshAll();
      } else if (t.status === "failed") {
        clearInterval(timer);
        txt.textContent = "Failed: " + t.error;
      }
    } catch (err) { clearInterval(timer); txt.textContent = "Polling failed: " + err.message; }
  }, 1500);
}

async function loadVideos() {
  const list = $("videoList");
  try {
    const videos = await api("/api/videos");
    if (!videos.length) { list.innerHTML = "<p class='muted'>No videos yet — upload one above.</p>"; return; }
    list.innerHTML = videos.map((v) => `
      <div class="row">
        <span class="title">${esc(v.title)}</span>
        ${v.source_type === "sample" ? "<span class='badge sample'>SAMPLE</span>" : ""}
        <span class="badge">${esc(v.status)}</span>
        <span class="badge">${esc(v.transcript_status)}</span>
        <div class="muted">${Math.round(v.duration_sec)}s · ${v.width}x${v.height} · ${(v.size_bytes / 1048576).toFixed(1)} MB · ${esc(v.source_type)}</div>
        <div style="margin-top:8px">
          <button class="small" onclick="analyzeVideo(${v.id})">Analyze</button>
          <button class="small" onclick="transcribeVideo(${v.id})">Transcribe (local Whisper)</button>
          <button class="small" onclick="uploadTranscript(${v.id})">Upload transcript (SRT/TXT)</button>
          ${v.stored_path !== undefined && v.source_type !== "sample" ? `<button class="small" onclick="previewVideoFile(${v.id})">Preview</button>` : ""}
          <button class="small danger" onclick="deleteVideo(${v.id})">Delete</button>
          <input type="file" id="tr-${v.id}" accept=".srt,.txt" style="display:none">
        </div>
      </div>`).join("");
  } catch (err) { list.innerHTML = "<p class='msg error'>" + esc(err.message) + "</p>"; }
}

function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

async function analyzeVideo(id) {
  try { const r = await api("/api/videos/" + id + "/analyze", { method: "POST" }); pollTask(r.task_id, "Analyzing video " + id); }
  catch (err) { alert(err.message); }
}
async function transcribeVideo(id) {
  try { const r = await api("/api/videos/" + id + "/transcribe", { method: "POST" }); pollTask(r.task_id, "Transcribing video " + id); }
  catch (err) { alert(err.message); }
}
function uploadTranscript(id) { $("tr-" + id).click(); }
document.addEventListener("change", async (e) => {
  if (e.target.id && e.target.id.startsWith("tr-")) {
    const id = e.target.id.slice(3);
    const fd = new FormData();
    fd.append("file", e.target.files[0]);
    try {
      const r = await api("/api/videos/" + id + "/transcript", { method: "POST", body: fd });
      alert("Transcript uploaded: " + r.segments + " segments, " + r.words + " words.");
      loadVideos();
    } catch (err) { alert(err.message); }
  }
});
async function previewVideoFile(id) {
  const v = $("previewVideo");
  v.src = "/api/videos/" + id + "/file";
  $("previewCard").style.display = "block";
  $("previewMeta").textContent = "Video " + id;
  document.querySelector('[data-tab="clips"]').click();
  v.scrollIntoView();
}
async function deleteVideo(id) {
  if (!confirm("Delete video " + id + " and its clips?")) return;
  await api("/api/videos/" + id, { method: "DELETE" });
  refreshAll();
}

async function loadClips() {
  const list = $("clipList");
  try {
    const clips = await api("/api/clips");
    if (!clips.length) { list.innerHTML = "<p class='muted'>No clips yet — analyze a video first.</p>"; return; }
    list.innerHTML = clips.map((c) => {
      const b = c.breakdown || {};
      const bar = (label, val) => `<div class="bar-row"><label>${label}</label><div class="bar"><div style="width:${val || 0}%"></div></div><span>${val || 0}</span></div>`;
      return `<div class="row">
        <span class="title">${esc(c.title)}</span>
        <span class="badge">${esc(c.status)}</span>
        ${c.caption_status === "burned_in" ? "<span class='badge ready'>captions burned in</span>" : ""}
        ${c.caption_status === "sidecar_only" ? "<span class='badge'>captions: SRT sidecar</span>" : ""}
        <span class="badge">score ${c.score}</span>
        <div class="muted">${c.start_sec}s – ${c.end_sec}s (${c.duration_sec}s) · preset ${esc(c.preset)} · ${esc(c.hook_title || "")}</div>
        <div class="bars">${bar("Hook", b.hook)}${bar("Engagement", b.engagement)}${bar("Value", b.value)}${bar("Shareability", b.shareability)}</div>
        <div class="muted">${esc(c.reason || "")}</div>
        <div style="margin-top:8px">
          <button class="small" onclick="generateClip(${c.id})">Generate</button>
          <button class="small" onclick="previewClip(${c.id})">Preview</button>
          <button class="small" onclick="downloadClip(${c.id})">Download</button>
          <button class="small" onclick="logStats(${c.id})">Log stats</button>
          <button class="small danger" onclick="deleteClip(${c.id})">Delete</button>
        </div>
      </div>`;
    }).join("");
  } catch (err) { list.innerHTML = "<p class='msg error'>" + esc(err.message) + "</p>"; }
}

async function generateClip(id) {
  try {
    const r = await api("/api/clips/" + id + "/generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ caption_template: "bold-yellow", preset: "default", include_hook_title: true }),
    });
    pollTask(r.task_id, "Rendering clip " + id);
  } catch (err) { alert(err.message); }
}
async function previewClip(id) {
  const v = $("previewVideo");
  v.src = "/api/clips/" + id + "/file";
  $("previewCard").style.display = "block";
  $("previewMeta").textContent = "Clip " + id;
  v.scrollIntoView();
}
function downloadClip(id) { window.open("/api/clips/" + id + "/file", "_blank"); }
async function deleteClip(id) {
  if (!confirm("Delete clip " + id + "?")) return;
  await api("/api/clips/" + id, { method: "DELETE" });
  loadClips();
}
async function logStats(id) {
  const views = parseInt(prompt("Views (real numbers you logged):", "0") || "0", 10);
  const likes = parseInt(prompt("Likes:", "0") || "0", 10);
  try {
    await api("/api/clips/" + id + "/stats", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ views, likes, comments: 0, shares: 0, note: "manual log" }),
    });
    alert("Stats logged. Future scoring on this video gets a small boost from strong clips.");
  } catch (err) { alert(err.message); }
}

async function loadEditorOptions() {
  try {
    const clips = await api("/api/clips");
    $("editorClip").innerHTML = clips.map((c) => `<option value="${c.id}">#${c.id} ${esc(c.title)} (${c.status})</option>`).join("");
  } catch (err) { $("editorClip").innerHTML = ""; }
}

$("trimForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("editorClip").value;
  try {
    const c = await api("/api/clips/" + id + "/trim", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start_sec: parseFloat($("trimStart").value), end_sec: parseFloat($("trimEnd").value) }),
    });
    showMsg($("editorMsg"), "Trimmed clip created: #" + c.id + " — rendering started.", true);
    loadClips();
  } catch (err) { showMsg($("editorMsg"), err.message); }
});
$("splitForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("editorClip").value;
  try {
    const r = await api("/api/clips/" + id + "/split", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ at_sec: parseFloat($("splitAt").value) }),
    });
    showMsg($("editorMsg"), "Split into clips: " + r.clip_ids.join(", ") + " — rendering started.", true);
    loadClips();
  } catch (err) { showMsg($("editorMsg"), err.message); }
});
$("exportForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("editorClip").value;
  try {
    const r = await api("/api/clips/" + id + "/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset: $("exportPreset").value }),
    });
    showMsg($("editorMsg"), "Export started (" + r.preset + ") — new clip #" + r.clip_id + ".", true);
    loadClips();
  } catch (err) { showMsg($("editorMsg"), err.message); }
});
$("mergeForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ids = $("mergeIds").value.split(",").map((x) => parseInt(x.trim(), 10)).filter((x) => !isNaN(x));
  try {
    const c = await api("/api/clips/merge", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clip_ids: ids, title: $("mergeTitle").value || "Merged clip" }),
    });
    showMsg($("editorMsg"), "Merged clip created: #" + c.id + ".", true);
    loadClips();
  } catch (err) { showMsg($("editorMsg"), err.message); }
});
$("brollForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData();
  fd.append("file", $("brollFile").files[0]);
  try {
    const r = await api("/api/clips/broll", { method: "POST", body: fd });
    alert("B-roll uploaded: " + r.filename);
    loadBroll();
  } catch (err) { alert(err.message); }
});
async function loadBroll() {
  try {
    const r = await api("/api/clips/broll");
    $("brollList").innerHTML = r.files.length
      ? "<p class='muted'>B-roll files: " + r.files.map(esc).join(", ") + "</p>"
      : "<p class='muted'>No B-roll files yet.</p>";
  } catch (err) { $("brollList").innerHTML = ""; }
}

$("uploadForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData();
  fd.append("title", $("uploadTitle").value);
  fd.append("file", $("uploadFile").files[0]);
  try {
    const v = await api("/api/videos/upload", { method: "POST", body: fd });
    alert("Uploaded: " + v.title + " (" + Math.round(v.duration_sec) + "s). Now click Analyze.");
    loadVideos();
  } catch (err) { alert(err.message); }
});
$("urlForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("/api/videos/from-url", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: $("urlInput").value }),
    });
    pollTask(r.task_id, "Fetching URL");
  } catch (err) { alert(err.message); }
});

async function loadAccounts() {
  try {
    const data = await api("/api/accounts");
    $("accountList").innerHTML = data.accounts.map((a) => `
      <div class="row"><b>${esc(a.label)}</b> — <span class="badge">${esc(a.status)}</span>
      <div class="muted">To connect: ${a.connect_steps.map(esc).join(" ")}</div></div>`).join("");
  } catch (err) { $("accountList").innerHTML = "<p class='msg error'>" + esc(err.message) + "</p>"; }
}
async function loadBilling() {
  try {
    const b = await api("/api/billing");
    $("billingBox").innerHTML = `
      <p>Plan: <b>${esc(b.plan)}</b> (MOCK — no payment processed)</p>
      <p class="muted">Usage: ${b.usage.videos}/${b.limits.videos} videos · ${b.usage.clips}/${b.limits.clips} clips</p>
      <p class="muted">Free $0 · Pro $19/mo (mock): 100 videos, 500 clips</p>
      ${b.plan === "free"
        ? '<button onclick="mockUpgrade()">Mock upgrade to Pro</button>'
        : '<button onclick="mockDowngrade()">Mock downgrade to Free</button>'}`;
  } catch (err) { $("billingBox").innerHTML = "<p class='msg error'>" + esc(err.message) + "</p>"; }
}
async function mockUpgrade() { await api("/api/billing/upgrade", { method: "POST" }); loadBilling(); }
async function mockDowngrade() { await api("/api/billing/downgrade", { method: "POST" }); loadBilling(); }
async function loadApiKeys() {
  try {
    const keys = await api("/api/api-keys");
    $("apiKeyList").innerHTML = keys.length
      ? keys.map((k) => `<div class="row"><b>${esc(k.name)}</b> <code>${esc(k.key_prefix)}…</code> — ${k.is_active ? "active" : "revoked"} <button class="small danger" onclick="revokeKey(${k.id})">Revoke</button></div>`).join("")
      : "<p class='muted'>No API keys yet.</p>";
    const prov = await api("/api/api-keys/providers");
    $("providerList").innerHTML = prov.providers.map((p) => `
      <div class="row"><b>${esc(p.name)}</b> — <span class="badge">${esc(p.status)}</span>
      <div class="muted">${esc(p.note)} (env: <code>${esc(p.env)}</code>)</div></div>`).join("");
  } catch (err) { $("apiKeyList").innerHTML = "<p class='msg error'>" + esc(err.message) + "</p>"; }
}
async function revokeKey(id) {
  await api("/api/api-keys/" + id, { method: "DELETE" });
  loadApiKeys();
}
$("apiKeyForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("/api/api-keys", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("apiKeyName").value }),
    });
    alert("API key created (shown once):\n" + r.api_key + "\n\nSend it as the X-API-Key header.");
    loadApiKeys();
  } catch (err) { alert(err.message); }
});

$("loginForm").addEventListener("submit", login);
if (token()) afterLogin();
