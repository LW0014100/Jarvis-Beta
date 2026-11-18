(() => {
  "use strict";

  /* ============================================================
     GLOBALE FEHLER-ABSICHERUNG (verhindert WebView2-Blackscreen)
  ============================================================ */
  window.onerror = function (message, source, lineno, colno, error) {
    try {
      console.error("[JARVIS HUD ERROR]", message, source, lineno, colno, error);
    } catch (_e) { }
    return true;
  };
  window.onunhandledrejection = function (event) {
    try {
      console.error("[JARVIS HUD PROMISE ERROR]", event && event.reason);
      if (event && typeof event.preventDefault === "function") event.preventDefault();
    } catch (_e) { }
  };

  /* ============================================================
     KONSTANTEN & HELFER
  ============================================================ */
  const $ = (id) => document.getElementById(id);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const lerp = (a, b, t) => a + (b - a) * t;
  const TAU = Math.PI * 2;
  const GOLDEN_ANGLE = 2.399963229728653;
  const FD = '"Orbitron","Rajdhani","Segoe UI",sans-serif';
  const FM = '"JetBrains Mono","Consolas",monospace';
  const WS_RECONNECT_MS = 1500;
  const HIST_LEN = 80;
  const RING_SLOTS = 300;
  const BUFFER_SECONDS = 30;

  const hexToRgb = (h) => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
  const C_OR = hexToRgb("#ff7b29");
  const C_GO = hexToRgb("#ffb852");
  const mix = (t, a = C_OR, b = C_GO, alpha = 1) => {
    const k = clamp(t, 0, 1);
    return `rgba(${Math.round(lerp(a[0], b[0], k))},${Math.round(lerp(a[1], b[1], k))},${Math.round(lerp(a[2], b[2], k))},${alpha})`;
  };

  const SETTING_TABS = {
    ai: [
      { key: "GEMINI_API_KEY", label: "Google Gemini API-Key", secret: true },
      { key: "GROQ_API_KEY", label: "Groq API-Key", secret: true },
      { key: "OPENROUTER_API_KEY", label: "OpenRouter API-Key", secret: true },
      { key: "DEEPSEEK_API_KEY", label: "DeepSeek API-Key", secret: true },
      { key: "OLLAMA_TEXT_MODEL", label: "Ollama Textmodell (Offline)", secret: false },
      { key: "OLLAMA_VISION_MODEL", label: "Ollama Visionmodell (Offline)", secret: false },
    ],
    sys: [
      { key: "AUTOSTART", label: "Autostart bei System-Start", secret: false, isToggle: true, desc: "Startet das Jarvis Command Center automatisch nach dem Desktop-Login (Raspberry Pi & Windows)." },
      { key: "MAX_AGENT_STEPS", label: "Maximale Agenten-Schritte (50 – 500, Standard: 200)", secret: false },
      { key: "WAKE_WORD", label: "Wachwort", secret: false },
      { key: "CITY", label: "Stadt (Live-Wetter)", secret: false },
      { key: "WATCHLIST", label: "Aktien-Watchlist (Komma-getrennt)", secret: false },
      { key: "WHISPER_MODEL", label: "Whisper Modellgröße (tiny/base/small)", secret: false },
      { key: "PIPER_MODEL_PATH", label: "Piper Stimmmodell (.onnx Pfad)", secret: false },
      { key: "CAMERA_INDEX", label: "Webcam Geräte-Index", secret: false },
      { key: "NAS_MEMORY_PATH", label: "NAS / Massenspeicher-Pfad (z. B. \\\\NAS-IP\\JarvisBrain oder Z:\\JarvisBrain – Fallback: %LOCALAPPDATA%\\JarvisBrain)", secret: false },
      { key: "ARCHIVE_PATH", label: "NAS- / Archiv-Pfad", secret: false },
    ],
    comm: [
      { key: "TELEGRAM_BOT_TOKEN", label: "Telegram Bot-Token", secret: true },
      { key: "TELEGRAM_ALLOWED_CHAT_ID", label: "Telegram Chat-ID", secret: false },
      { key: "EMAIL_USER", label: "E-Mail-Adresse (IMAP/SMTP)", secret: false },
      { key: "EMAIL_PASS", label: "E-Mail App-Passwort", secret: true },
      { key: "IMAP_SERVER", label: "IMAP-Server", secret: false },
      { key: "SMTP_SERVER", label: "SMTP-Server", secret: false },
      { key: "N8N_BASE_URL", label: "n8n Base-URL (Standard: http://localhost:5678)", secret: false },
      { key: "N8N_API_KEY", label: "n8n REST-API-Key (optional – sonst Webhook-Fallback)", secret: true },
      { key: "N8N_WEBHOOK_URL", label: "n8n Standard-Webhook-URL (optional)", secret: false },
    ],
  };

  /* ============================================================
     ZUSTAND
  ============================================================ */
  const S = {
    status: "Bereit",
    statusEng: "READY",
    statusSub: "STANDBY",
    isOnline: true,
    micMuted: false,
    camPaused: false,
    level: 0,
    levelS: 0,
    vad: false,
    speaking: false,
    wsConnected: false,
    latency: 0,
    modelName: "",
    tel: {
      cpu_pct: 0, ram_used_gb: 0, ram_total_gb: 0, ram_pct: 0, temp_c: null,
      disk_free_gb: 0, disk_total_gb: 0, net_up_kbps: 0, net_down_kbps: 0,
      proc_count: 0, ollama_status: "", network: { hostname: "--", ip: "--", online: false },
    },
    telAt: 0,
    fps: 60,
    frames: 0,
    fpsT: performance.now(),
    start: performance.now(),
    hist: {
      ram: new Array(HIST_LEN).fill(0),
    },
    disp: { cpu: 0, ram: 0, temp: null, down: 0, up: 0 },
    gauge: { cpu: 0, ram: 0 },
    ring: new Float32Array(RING_SLOTS).fill(-1),
    ringIdx: 0,
    lastOllama: "",
    pulses: [],
    cameraActive: false,
    desktopVisionMode: true,
    agentRunning: false,
    visionStatus: "DESKTOP-VISION: 30s PULS // WEBCAM: STANDBY (ON-DEMAND)",
    autostartEnabled: false,
    activeProvider: "auto",
    modelPriority: ["gemini", "groq", "openrouter", "deepseek", "pollinations"],
    providerVerifications: {},
    settingsTab: "ai",
    settingsDraft: {},
  };

  /* ============================================================
     ZUSTANDSABHÄNGIGES FARBSYSTEM
     Bereit = Eisblau/Cyan | Hört zu = Blau | Denkt/API = Gold/Orange
     Spricht = Smaragdgrün/Hellblau | Fehler/Offline = Rot
     Die Farben werden weich überblendet und live als CSS-Variablen
     (--acc-rgb / --acc2-rgb) an das gesamte HUD weitergegeben.
  ============================================================ */
  const PALETTES = {
    READY:      { a: [56, 215, 255],  b: [176, 238, 255] },
    LISTENING:  { a: [70, 168, 255],  b: [158, 214, 255] },
    PROCESSING: { a: [255, 176, 46],  b: [255, 216, 132] },
    THINKING:   { a: [255, 166, 36],  b: [255, 210, 118] },
    OPTICS:     { a: [255, 138, 58],  b: [255, 200, 140] },
    ACTING:     { a: [255, 152, 48],  b: [255, 206, 128] },
    SPEAKING:   { a: [38, 238, 158],  b: [128, 224, 255] },
    ALERT:      { a: [255, 62, 74],   b: [255, 152, 156] },
    OFFLINE:    { a: [255, 72, 78],   b: [255, 160, 150] },
  };
  const STATE_SPEED = {
    READY: 0.8, LISTENING: 1.2, PROCESSING: 2.0, THINKING: 2.8,
    OPTICS: 1.8, ACTING: 2.2, SPEAKING: 1.5, ALERT: 3.2, OFFLINE: 0.5,
  };
  const PAL = {
    a: PALETTES.READY.a.slice(),
    b: PALETTES.READY.b.slice(),
    rgb: "56,215,255",
    rgb2: "176,238,255",
    key: "READY",
  };
  const rootStyle = document.documentElement.style;
  const ca = (alpha) => `rgba(${PAL.rgb},${alpha})`;
  const cb = (alpha) => `rgba(${PAL.rgb2},${alpha})`;

  function resolvePaletteKey() {
    if (!S.wsConnected && performance.now() - S.start > 5000) return "OFFLINE";
    const e = S.statusEng;
    if (e === "ALERT") return "ALERT";
    if (e === "SPEAKING" || (e === "READY" && S.speaking)) return "SPEAKING";
    if (e === "READY" && !S.isOnline) return "OFFLINE";
    return PALETTES[e] ? e : "READY";
  }

  function updatePalette(dt) {
    const key = resolvePaletteKey();
    const tgt = PALETTES[key];
    const k = 1 - Math.exp(-dt * 4.5);
    for (let i = 0; i < 3; i++) {
      PAL.a[i] += (tgt.a[i] - PAL.a[i]) * k;
      PAL.b[i] += (tgt.b[i] - PAL.b[i]) * k;
    }
    const ra = `${Math.round(PAL.a[0])},${Math.round(PAL.a[1])},${Math.round(PAL.a[2])}`;
    const rb = `${Math.round(PAL.b[0])},${Math.round(PAL.b[1])},${Math.round(PAL.b[2])}`;
    if (ra !== PAL.rgb || rb !== PAL.rgb2) {
      PAL.rgb = ra;
      PAL.rgb2 = rb;
      rootStyle.setProperty("--acc-rgb", ra);
      rootStyle.setProperty("--acc2-rgb", rb);
    }
    if (key !== PAL.key) {
      PAL.key = key;
      document.body.dataset.state = key.toLowerCase();
    }
  }

  /* ============================================================
     PERFORMANCE-GOVERNOR (hält das UI auch auf schwacher Hardware flüssig)
  ============================================================ */
  const PERF = { q: 1, cost: 6, slow: 0, fast: 0 };

  /* ============================================================
     AUDIO-MODELL (Mikrofon-Pegel + TTS-Sprachpegel -> Frequenzbänder)
     Läuft komplett im Render-Takt und wird nur von S.level gespeist;
     blockierende Backend-/API-Threads können die Animation nie anhalten.
  ============================================================ */
  const NBAND = 48;
  const AUD = {
    raw: 0,
    B: new Float32Array(NBAND),
    P: new Float32Array(NBAND),
  };

  function updateAudio(dt, t) {
    const speaking = S.speaking || S.statusEng === "SPEAKING";
    let target = S.micMuted && !speaking ? 0 : S.level;
    if (speaking && target < 0.07) {
      // Kein Pegel vom TTS gemeldet: natürliche synthetische Sprachhüllkurve
      target = 0.26 + 0.34 * Math.abs(Math.sin(t * 6.9)) * (0.55 + 0.45 * Math.abs(Math.sin(t * 2.3 + 1.1)));
    }
    AUD.raw += (target - AUD.raw) * (1 - Math.exp(-dt * 16));

    const up = 1 - Math.exp(-dt * 30);
    const down = 1 - Math.exp(-dt * 7.5);
    for (let i = 0; i < NBAND; i++) {
      const f = i / NBAND;
      const form = Math.abs(0.55 + 0.45 * Math.sin(f * 9.0 + t * 3.1) * Math.cos(f * 5.0 - t * 1.7));
      const tilt = 1 - f * 0.5;
      const jitter = 0.72 + 0.28 * Math.sin(t * (5.0 + i * 0.37) + i * 1.3);
      const idle = 0.04 + 0.022 * Math.sin(t * 1.3 + i * 0.55);
      const tgt = clamp(AUD.raw * 1.35 * form * tilt * jitter + idle, 0, 1);
      AUD.B[i] += (tgt - AUD.B[i]) * (tgt > AUD.B[i] ? up : down);
      AUD.P[i] = Math.max(AUD.B[i], AUD.P[i] - dt * 0.42);
    }
  }

  /* ============================================================
     CHAT-LOG (Glas-Panel im Core-Stage) & SYSTEM-LOG GARBAGE COLLECTION
  ============================================================ */
  const MAX_CHAT_DOM_NODES = 30;
  const MAX_LOG_DOM_LINES = 40;
  const chat = { list: [], byId: new Map(), max: MAX_CHAT_DOM_NODES, dirty: false, seq: 0 };
  const stamp = () => {
    const d = new Date();
    return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
  };

  function pruneDomChildren(container, maxCount) {
    if (!container) return;
    while (container.children.length > maxCount) {
      container.removeChild(container.firstChild);
    }
  }

  function pushSystemLogLine(line) {
    const clean = String(line || "").trim();
    if (!clean) return;
    const box = $("system-log") || $("log-box");
    if (!box) return;
    const row = document.createElement("div");
    row.className = "sys-log-line";
    row.textContent = clean.length > 260 ? clean.slice(0, 257) + "..." : clean;
    box.appendChild(row);
    pruneDomChildren(box, MAX_LOG_DOM_LINES);
    const logCnt = $("log-count");
    if (logCnt) logCnt.textContent = String(box.children.length);
  }

  function chatPush(role, text, id, time) {
    const clean = String(text || "").replace(/^"|"$/g, "").trim();
    if (!clean) return;
    const bounded = clean.length > 1500 ? clean.slice(0, 1497) + "..." : clean;
    const key = `${role}:${id === undefined || id === null ? `n${++chat.seq}` : id}`;
    let e = chat.byId.get(key);
    if (!e) {
      e = { key, role, text: bounded, time: time || stamp(), node: null, body: null };
      chat.byId.set(key, e);
      chat.list.push(e);
      while (chat.list.length > chat.max) {
        const old = chat.list.shift();
        chat.byId.delete(old.key);
        if (old.node && old.node.parentNode) old.node.parentNode.removeChild(old.node);
        old.node = null;
        old.body = null;
      }
    } else {
      e.text = bounded;
      if (time) e.time = time;
    }
    chat.dirty = true;
  }

  function flushChat() {
    if (!chat.dirty) return;
    chat.dirty = false;
    const host = $("chat-log");
    if (!host) return;
    const stick = host.scrollHeight - host.scrollTop - host.clientHeight < 30;
    for (const e of chat.list) {
      if (!e.node) {
        const row = document.createElement("div");
        row.className = `cm cm-${e.role}`;
        const tm = document.createElement("span");
        tm.className = "cm-t";
        tm.textContent = e.time;
        const rl = document.createElement("span");
        rl.className = "cm-r";
        rl.textContent = e.role === "user" ? "DU ›" : "JARVIS ›";
        const body = document.createElement("span");
        body.className = "cm-x";
        row.appendChild(tm);
        row.appendChild(rl);
        row.appendChild(body);
        host.appendChild(row);
        e.node = row;
        e.body = body;
      }
      if (e.body.textContent !== e.text) e.body.textContent = e.text;
    }
    pruneDomChildren(host, MAX_CHAT_DOM_NODES);
    const cnt = $("chat-count");
    if (cnt) cnt.textContent = String(host.children.length).padStart(2, "0");
    if (stick) host.scrollTop = host.scrollHeight;
  }
  setInterval(flushChat, 120);

  const core = { speed: 0.8, compass: 0, segA: 0, segB: 0, arc: 0, inner: 0, scale: 0, ay: 0, ax: 0.3, sweep: 0, pulse: 0 };

  /* ============================================================
     CANVAS-MANAGER (High-DPI, keine Verzerrung)
     - Die Bitmap-Auflösung wird an der TATSÄCHLICH dargestellten
       Canvas-Box gemessen (nicht am Eltern-Element) -> kein Stretching.
     - Eine einzige uniforme Skalierung (dpr) für beide Achsen ->
       Kreise bleiben bei jedem Seitenverhältnis 100 % rund.
  ============================================================ */
  const cvs = {};
  const readDpr = () => Math.min(Math.max(window.devicePixelRatio || 1, 1), 3);

  function resizeCv(cv) {
    if (!cv || !cv.el) return;
    const r = cv.el.getBoundingClientRect();
    const w = Math.max(4, r.width);
    const h = Math.max(4, r.height);
    const dpr = readDpr();
    const bw = Math.max(1, Math.round(w * dpr));
    const bh = Math.max(1, Math.round(h * dpr));
    cv.w = w;
    cv.h = h;
    cv.dpr = dpr;
    if (cv.el.width !== bw) cv.el.width = bw;
    if (cv.el.height !== bh) cv.el.height = bh;
  }

  function makeCv(id) {
    const elc = $(id);
    if (!elc) return null;
    const cv = { el: elc, ctx: elc.getContext("2d", { alpha: true }), w: 10, h: 10, dpr: 1 };
    resizeCv(cv);
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() => resizeCv(cv)).observe(elc);
    }
    cvs[id] = cv;
    return cv;
  }

  function begin(cv) {
    if (!cv) return null;
    const ctx = cv.ctx;
    ctx.setTransform(cv.dpr, 0, 0, cv.dpr, 0, 0);
    ctx.clearRect(0, 0, cv.w, cv.h);
    // Anti-Aliasing für Linien, Kurven und Schrift
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.imageSmoothingEnabled = true;
    if ("imageSmoothingQuality" in ctx) ctx.imageSmoothingQuality = "high";
    if ("textRendering" in ctx) ctx.textRendering = "geometricPrecision";
    if ("fontKerning" in ctx) ctx.fontKerning = "normal";
    return ctx;
  }

  function resizeAll() {
    Object.values(cvs).forEach((c) => c && resizeCv(c));
  }
  window.addEventListener("resize", resizeAll);
  (function watchDpr() {
    if (!window.matchMedia) return;
    const mq = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
    const onChange = () => { resizeAll(); watchDpr(); };
    if (mq.addEventListener) mq.addEventListener("change", onChange, { once: true });
  })();

  const cvWave = makeCv("cv-wave");
  const cvSpectrum = makeCv("cv-spectrum");
  const cvCore = makeCv("cv-core");
  const cvRuler = makeCv("cv-ruler");
  const cvGCpu = makeCv("cv-gauge-cpu");
  const cvGRam = makeCv("cv-gauge-ram");
  const cvChRam = makeCv("cv-ch-ram");
  const cvNet = makeCv("cv-net");

  /* ============================================================
     DOM-REFERENZEN
  ============================================================ */
  const el = {
    modelName: $("model-name"),
    modelDropdown: $("model-dropdown"),
    modeLabel: $("mode-label"),
    modeSwitch: $("mode-switch"),
    btnNet: $("btn-net"),
    btnSettings: $("btn-settings"),
    btnPower: $("btn-power"),
    powerMenu: $("power-menu"),
    valLevel: $("val-level"),
    valVad: $("val-vad"),
    sttText: $("stt-transcript-text"),
    jarvisText: $("jarvis-reply-text"),
    coreStage: $("core-stage"),
    btnMic: $("btn-mic"),
    micLabel: $("mic-label"),
    btnCam: $("btn-cam"),
    btnCamToggle: $("btn-cam-toggle"),
    btnDesktopVisionToggle: $("btn-desktop-vision-toggle"),
    desktopVisionFeed: $("desktop-vision-feed"),
    desktopVisionBadge: $("desktop-vision-badge"),
    camPanelTitle: $("cam-panel-title"),
    camToggleLabel: $("cam-toggle-label"),
    camOfflineOverlay: $("cam-offline-overlay"),
    camFeed: $("cam-feed"),
    camLiveText: $("cam-live-text"),
    camPaused: $("cam-paused"),
    amBar1: $("am-bar-1"),
    amBar2: $("am-bar-2"),
    amBar3: $("am-bar-3"),
    amSeg1: $("am-seg-1"),
    amSeg2: $("am-seg-2"),
    sgTemp: $("sg-temp"),
    sgM1: $("sg-m1"),
    sgM2: $("sg-m2"),
    sgM3: $("sg-m3"),
    sgM4: $("sg-m4"),
    ndDown: $("nd-down"),
    ndUp: $("nd-up"),
    ndGpr: $("nd-gpr"),
    ndRain: $("nd-rain"),
    brmActBar: $("brm-act-bar"),
    brmSpeed: $("brm-speed"),
    brmSpdBar: $("brm-spd-bar"),
    hudDiagLogs: $("hud-diag-logs"),
    netOverlay: $("net-overlay"),
    niHost: $("ni-host"),
    niIp: $("ni-ip"),
    niNet: $("ni-net"),
    niIo: $("ni-io"),
    niOllama: $("ni-ollama"),
    niProc: $("ni-proc"),
    niDisk: $("ni-disk"),
    settingsModal: $("settings-modal"),
    settingsFields: $("settings-fields"),
    settingsForm: $("settings-form"),
    settingsFeedback: $("settings-feedback"),
  };

  // Erzeuge 16 Segmente für die beiden LED-Balken
  if (el.amSeg1) {
    el.amSeg1.innerHTML = "";
    for (let i = 0; i < 16; i++) el.amSeg1.appendChild(document.createElement("i"));
  }
  if (el.amSeg2) {
    el.amSeg2.innerHTML = "";
    for (let i = 0; i < 16; i++) el.amSeg2.appendChild(document.createElement("i"));
  }

  /* ============================================================
     TELEMETRIE-WERTE & DIAGNOSTIK
  ============================================================ */
  function isDemo() {
    const ref = Math.max(S.telAt, S.start);
    return performance.now() - ref > 6000;
  }

  function telValues() {
    if (isDemo()) {
      const t = performance.now() / 1000;
      return {
        cpu: clamp(22 + 10 * Math.sin(t * 0.6) + 5 * Math.sin(t * 1.7), 2, 98),
        ram: clamp(44 + 4 * Math.sin(t * 0.21), 5, 95),
        temp: 49 + 3 * Math.sin(t * 0.3),
        down: Math.max(0, 12 + 6 * Math.sin(t * 0.9) + 4 * Math.sin(t * 2.3)),
        up: Math.max(0, 0.4 + 0.3 * Math.sin(t * 1.4)),
        diskFree: S.tel.disk_free_gb || 1399,
        diskTotal: S.tel.disk_total_gb || 2048,
      };
    }
    const t = S.tel;
    return {
      cpu: Number(t.cpu_pct || 0),
      ram: Number(t.ram_pct || 0),
      temp: t.temp_c === null || t.temp_c === undefined ? 53 : Number(t.temp_c),
      down: Number(t.net_down_kbps || 0) / 1024,
      up: Number(t.net_up_kbps || 0) / 1024,
      diskFree: S.tel.disk_free_gb || 1399,
      diskTotal: S.tel.disk_total_gb || 2048,
    };
  }

  function updateReadouts() {
    const tv = telValues();
    S.disp.cpu += (tv.cpu - S.disp.cpu) * 0.2;
    S.disp.ram += (tv.ram - S.disp.ram) * 0.2;
    S.disp.temp = tv.temp;
    S.disp.down = tv.down;
    S.disp.up = tv.up;

    S.hist.ram.push(S.disp.ram);
    if (S.hist.ram.length > HIST_LEN) S.hist.ram.shift();

    // Split-Gauge Werte (RAM & Temp)
    if (el.sgTemp) el.sgTemp.textContent = `TEMP: ${Math.round(S.disp.temp)}°C`;
    if (el.sgM1) el.sgM1.textContent = `${(S.disp.cpu * 0.43).toFixed(1)}%`;
    if (el.sgM2) el.sgM2.textContent = `${Math.round(S.disp.ram)}%`;
    if (el.sgM3) el.sgM3.textContent = (S.disp.down * 0.05).toFixed(1);
    if (el.sgM4) el.sgM4.textContent = `TEMP: ${Math.round(S.disp.temp)}°C`;

    // Netzwerk-Panel
    if (el.ndDown) el.ndDown.textContent = `${S.disp.down.toFixed(1)} MB/s`;
    if (el.ndUp) el.ndUp.textContent = `${S.disp.up.toFixed(1)} IB/s`;
    if (el.ndGpr) el.ndGpr.textContent = `${tv.diskFree.toFixed(1)} GPR`;
    if (el.ndRain) el.ndRain.textContent = `${S.disp.ram.toFixed(1)}% RAain`;

    // Untere Status-Balken
    if (el.brmActBar) el.brmActBar.style.width = `${clamp(35 + S.disp.cpu * 0.55, 10, 100)}%`;
    if (el.brmSpdBar) el.brmSpdBar.style.width = `${clamp(20 + S.disp.down * 4, 10, 100)}%`;
    if (el.brmSpeed) el.brmSpeed.textContent = `${(S.disp.down * 0.78 + 1.2).toFixed(2)} M/s`;

    // Audio-Meters animieren
    const lvl = S.micMuted ? 0 : S.levelS;
    if (el.amBar1) el.amBar1.style.width = `${clamp(45 + lvl * 50, 15, 100)}%`;
    if (el.amBar2) el.amBar2.style.width = `${clamp(60 + lvl * 40, 20, 100)}%`;
    if (el.amBar3) el.amBar3.style.width = `${clamp(30 + lvl * 65, 10, 100)}%`;

    if (el.amSeg1) {
      const lit = Math.round(clamp(0.2 + lvl * 0.8, 0, 1) * 16);
      const kids = el.amSeg1.children;
      for (let i = 0; i < kids.length; i++) kids[i].classList.toggle("lit", i < lit);
    }
    if (el.amSeg2) {
      const lit = Math.round(clamp(0.35 + lvl * 0.65, 0, 1) * 16);
      const kids = el.amSeg2.children;
      for (let i = 0; i < kids.length; i++) kids[i].classList.toggle("lit", i < lit);
    }
  }
  setInterval(updateReadouts, 250);

  /* ============================================================
     LIVE-TELEMETRIE-PANEL & STATUS-CHIP (Glas-Overlays im Core-Stage)
  ============================================================ */
  const hud = {
    cpuV: $("tm-cpu-v"), cpuB: $("tm-cpu-b"),
    ramV: $("tm-ram-v"), ramB: $("tm-ram-b"),
    tempV: $("tm-temp-v"), provV: $("tm-prov-v"),
    linkV: $("tm-link-v"), modeV: $("tm-mode-v"),
    latV: $("tm-lat-v"), fpsV: $("tm-fps-v"),
    chipText: $("hud-state-text"), chipSub: $("hud-state-sub"),
    chat: $("hud-chat"),
  };
  if (hud.chat) hud.chat.addEventListener("click", (ev) => ev.stopPropagation());

  function providerLabel() {
    const m = (S.modelName || "").toUpperCase();
    if (m.includes("GEMINI")) return "GEMINI";
    if (m.includes("GROQ")) return "GROQ";
    if (m.includes("OLLAMA")) return "OLLAMA";
    if (m.includes("DEEPSEEK")) return "DEEPSEEK";
    if (m.includes("OPENROUTER")) return "OPENROUTER";
    if (m.includes("POLLINATIONS")) return "POLLINATIONS";
    const p = (S.activeProvider || "auto").toUpperCase();
    return p === "AUTO" ? "AUTO-KASKADE" : p;
  }

  function updateHudPanels() {
    if (document.hidden || S.windowMinimized) return;
    const cpu = clamp(S.disp.cpu, 0, 100);
    const ram = clamp(S.disp.ram, 0, 100);
    if (hud.cpuV) hud.cpuV.textContent = `${Math.round(cpu)}%`;
    if (hud.cpuB) hud.cpuB.style.width = `${cpu}%`;
    if (hud.ramV) hud.ramV.textContent = `${Math.round(ram)}%`;
    if (hud.ramB) hud.ramB.style.width = `${ram}%`;
    if (hud.tempV) hud.tempV.textContent = S.disp.temp === null || S.disp.temp === undefined ? "--" : `${Math.round(S.disp.temp)}°C`;
    if (hud.provV) hud.provV.textContent = providerLabel();
    if (hud.modeV) hud.modeV.textContent = S.isOnline ? "ONLINE" : "OFFLINE";
    if (hud.linkV) {
      const linked = S.wsConnected;
      hud.linkV.textContent = linked ? "VERBUNDEN" : "GETRENNT";
      hud.linkV.classList.toggle("on", linked);
      hud.linkV.classList.toggle("off", !linked);
    }
    if (hud.latV) hud.latV.textContent = S.latency > 0 ? `${S.latency.toFixed(2)}s` : "--";
    if (hud.fpsV) hud.fpsV.textContent = `${S.fps}`;
    if (hud.chipText) hud.chipText.textContent = PAL.key === "OFFLINE" ? "OFFLINE" : S.statusEng;
    if (hud.chipSub) hud.chipSub.textContent = S.statusSub;
  }
  setInterval(updateHudPanels, 250);

  // Schnelle hexadezimale Log-Animation im HUD
  const DIAG_HEX = [
    "09YPR03KD07", "SOBWO1G9T", "09OW09SFUNE", "SOCBD5B7", "SODBROV",
    "ENSPO4RAAI6", "X89F2A90C", "A7B012F4E", "B6940C002", "FF8A23B9",
  ];
  let diagStep = 0;
  setInterval(() => {
    if (document.hidden || S.windowMinimized || !el.hudDiagLogs) return;
    diagStep = (diagStep + 1) % DIAG_HEX.length;
    const lines = [];
    for (let i = 0; i < 6; i++) {
      const idx = (diagStep + i) % DIAG_HEX.length;
      lines.push(`<div class="dl">LOGS : ${DIAG_HEX[idx]}...</div>`);
    }
    el.hudDiagLogs.innerHTML = lines.join("");
  }, 900);

  /* ============================================================
     ZUSTAND-SYNCHRONISATION
  ============================================================ */
  function renderMode(online) {
    S.isOnline = Boolean(online);
    if (el.modeLabel) {
      el.modeLabel.textContent = S.isOnline ? "ONLINE MODE" : "OFFLINE MODE";
      el.modeLabel.classList.toggle("on", S.isOnline);
    }
    if (el.modeSwitch) {
      el.modeSwitch.classList.toggle("on", S.isOnline);
      el.modeSwitch.setAttribute("aria-checked", String(S.isOnline));
    }
  }

  function setModel(name) {
    if (!name) return;
    S.modelName = name.replace(/^MODEL:\s*/i, "").trim();
    if (el.modelName) el.modelName.textContent = S.modelName;
  }

  function setStatus(status, eng, sub) {
    S.status = status || "Bereit";
    S.statusEng = (eng || "READY").toUpperCase();
    S.statusSub = (sub || "STANDBY").toUpperCase();
    updateHudPanels();
  }

  function updateVadUi() {
    const active = S.vad && !S.micMuted;
    if (el.valVad) {
      el.valVad.textContent = active ? "VAD ACTIVE" : (S.micMuted ? "MIC MUTED" : "STANDBY");
      el.valVad.classList.toggle("active", active);
    }
    if (el.valLevel) {
      el.valLevel.textContent = `${Math.round(clamp(S.level, 0, 1) * 100)}%`;
    }
  }

  function renderMic(muted) {
    S.micMuted = Boolean(muted);
    if (el.micLabel) el.micLabel.textContent = S.micMuted ? "Mikrofon stumm" : "Mikrofon aktiv";
    if (el.btnMic) el.btnMic.classList.toggle("active", S.micMuted);
    updateVadUi();
  }

  function renderVisionStatus(text) {
    const clean = String(text || "DESKTOP-VISION: 30s PULS // WEBCAM: STANDBY (ON-DEMAND)").trim();
    S.visionStatus = clean;
    if (el.camOfflineOverlay) {
      const subEl = el.camOfflineOverlay.querySelector(".cam-offline-sub");
      if (subEl) subEl.textContent = clean;
    }
    const stepEl = $("agent-monitor-step");
    if (stepEl && (!S.agentMonitor || !S.agentMonitor.active)) {
      stepEl.textContent = clean;
    }
    if (S.statusEng === "READY") {
      S.statusSub = clean.includes("1.2s")
        ? "1.2s LIVE-TRACKING"
        : (clean.includes("SNAPSHOT") ? "WEBCAM SNAPSHOT" : "30s SCREEN PULS");
    }
  }

  function renderCamera(paused) {
    S.camPaused = Boolean(paused);
    if (el.camPaused) el.camPaused.classList.toggle("hidden", !S.camPaused);
    if (el.camLiveText) {
      el.camLiveText.textContent = _shouldShowDesktopVision()
        ? (S.agentRunning ? "2 FPS" : "1 FPS")
        : (S.cameraActive ? (S.camPaused ? "PAUSE" : "LIVE") : "STANDBY");
    }
    if (el.btnCam) el.btnCam.classList.toggle("active", S.camPaused);
    if (el.camFeed && (!S.cameraActive || S.camPaused)) {
      el.camFeed.removeAttribute("src");
    }
    syncViewportSource();
  }

  let _desktopPreviewTimer = null;
  let _desktopPreviewInFlight = false;

  function _shouldShowDesktopVision() {
    return Boolean(S.desktopVisionMode && (!S.cameraActive || S.camPaused || S.desktopVisionForced));
  }

  function _clearDesktopPreviewTimer() {
    if (_desktopPreviewTimer) {
      clearTimeout(_desktopPreviewTimer);
      _desktopPreviewTimer = null;
    }
  }

  function _loadNextDesktopFrame() {
    _clearDesktopPreviewTimer();
    if (!el.desktopVisionFeed || document.hidden || S.windowMinimized || !_shouldShowDesktopVision()) {
      _desktopPreviewInFlight = false;
      return;
    }
    if (_desktopPreviewInFlight) return;
    _desktopPreviewInFlight = true;
    el.desktopVisionFeed.src = `/api/live_desktop_preview.jpg?t=${Date.now()}`;
  }

  function _scheduleDesktopPreview(delayMs) {
    _clearDesktopPreviewTimer();
    if (!el.desktopVisionFeed || document.hidden || S.windowMinimized || !_shouldShowDesktopVision()) {
      return;
    }
    const interval = typeof delayMs === "number" ? delayMs : (S.agentRunning ? 500 : 1000);
    _desktopPreviewTimer = setTimeout(_loadNextDesktopFrame, interval);
  }

  if (el.desktopVisionFeed) {
    el.desktopVisionFeed.addEventListener("load", () => {
      _desktopPreviewInFlight = false;
      _scheduleDesktopPreview();
    });
    el.desktopVisionFeed.addEventListener("error", () => {
      _desktopPreviewInFlight = false;
      _scheduleDesktopPreview(2000);
    });
  }

  function syncViewportSource() {
    const showDesktop = _shouldShowDesktopVision();
    const cam3Grid = $("cam-3mon-grid");
    if (el.btnDesktopVisionToggle) {
      el.btnDesktopVisionToggle.classList.toggle("desktop-on", Boolean(S.desktopVisionMode));
    }
    if (el.camPanelTitle) {
      el.camPanelTitle.textContent = showDesktop ? "3-MONITOR LIVE VISION" : "DESK CAM ACTIVE";
    }
    if (el.desktopVisionBadge) {
      el.desktopVisionBadge.classList.toggle("hidden", !showDesktop);
    }
    if (cam3Grid) {
      cam3Grid.classList.toggle("hidden", !showDesktop);
    }
    if (el.desktopVisionFeed) {
      // Im 3-Monitor-Live-Modus übernimmt #cam-3mon-grid via Binär-WS (kein unnötiges Einzel-Polling)
      el.desktopVisionFeed.classList.add("hidden");
      _clearDesktopPreviewTimer();
      _desktopPreviewInFlight = false;
      el.desktopVisionFeed.removeAttribute("src");
    }
    if (el.camOfflineOverlay) {
      el.camOfflineOverlay.classList.toggle("hidden", S.cameraActive || showDesktop);
    }
    if (el.camFeed) {
      el.camFeed.classList.toggle("hidden", showDesktop);
      if (!showDesktop && S.cameraActive && !S.camPaused) {
        reloadCamera();
      } else if (showDesktop || !S.cameraActive || S.camPaused) {
        el.camFeed.removeAttribute("src");
      }
    }
  }

  function renderCamSensor(active) {
    S.cameraActive = Boolean(active);
    if (el.btnCamToggle) {
      el.btnCamToggle.classList.toggle("cam-on", S.cameraActive);
      el.btnCamToggle.classList.toggle("cam-off", !S.cameraActive);
    }
    if (el.camToggleLabel) {
      el.camToggleLabel.textContent = S.cameraActive ? "CAM AUS" : "CAM EIN";
    }
    if (el.camLiveText) {
      el.camLiveText.textContent = _shouldShowDesktopVision()
        ? (S.agentRunning ? "2 FPS" : "1 FPS")
        : (S.cameraActive ? (S.camPaused ? "PAUSE" : "LIVE") : "STANDBY");
    }
    syncViewportSource();
  }

  function reloadCamera() {
    if (el.camFeed && S.cameraActive && !S.camPaused && !_shouldShowDesktopVision()) {
      el.camFeed.src = `/api/camera.mjpeg?t=${Date.now()}`;
    }
  }

  function bufferSeconds() {
    const active = S.ring.reduce((acc, v) => (v >= 0 ? acc + 1 : acc), 0);
    return (active / RING_SLOTS) * BUFFER_SECONDS;
  }

  setInterval(() => {
    S.ring[S.ringIdx] = S.micMuted ? 0 : clamp(S.level, 0, 1);
    S.ringIdx = (S.ringIdx + 1) % RING_SLOTS;
  }, 100);

  /* ============================================================
     SNAPSHOT & WEBSOCKET
  ============================================================ */
  function applySnapshot(snap) {
    if (!snap || typeof snap !== "object") return;
    if (snap.is_online !== undefined) renderMode(snap.is_online);
    if (snap.model_label) setModel(snap.model_label);
    if (snap.status) setStatus(snap.status, snap.status_eng, snap.status_sub);
    if (snap.mic_muted !== undefined) renderMic(snap.mic_muted);
    if (snap.camera_paused !== undefined) renderCamera(snap.camera_paused);
    if (snap.camera_active !== undefined) {
      renderCamSensor(snap.camera_active);
    } else if (snap.camera_paused !== undefined) {
      renderCamSensor(!snap.camera_paused);
    }
    if (snap.vision_status) renderVisionStatus(snap.vision_status);
    if (snap.autostart_enabled !== undefined) {
      S.autostartEnabled = Boolean(snap.autostart_enabled);
      S.settingsDraft.AUTOSTART = S.autostartEnabled ? "true" : "false";
    }
    if (snap.active_provider !== undefined) {
      S.activeProvider = snap.active_provider;
      if (el.modelDropdown) el.modelDropdown.value = snap.active_provider;
    }
    if (snap.model_priority && Array.isArray(snap.model_priority)) {
      S.modelPriority = snap.model_priority;
    }
    if (snap.audio_level !== undefined) S.level = Number(snap.audio_level || 0);
    if (snap.vad_active !== undefined) S.vad = Boolean(snap.vad_active);
    if (snap.speaking_active !== undefined) S.speaking = Boolean(snap.speaking_active);
    updateVadUi();

    if (snap.user_text && el.sttText) {
      el.sttText.textContent = String(snap.user_text).replace(/^"|"$/g, "");
    }
    if (snap.jarvis_text && el.jarvisText) {
      el.jarvisText.textContent = snap.jarvis_text;
    }
    if (snap.response_time_s !== undefined) S.latency = Number(snap.response_time_s || 0);
    if (Array.isArray(snap.dialog)) {
      snap.dialog.slice(-MAX_CHAT_DOM_NODES).forEach((d) => chatPush(d.role === "user" ? "user" : "jarvis", d.text, d.id, d.time));
    }
    if (Array.isArray(snap.logs)) {
      const logBox = $("system-log") || $("log-box");
      if (logBox) {
        logBox.innerHTML = "";
        snap.logs.slice(-MAX_LOG_DOM_LINES).forEach((line) => pushSystemLogLine(line));
      }
    }
    if (snap.telemetry) applyTelemetry(snap.telemetry);
    if (snap.agent_monitor) renderAgentMonitor(snap.agent_monitor);
    renderSmartSuggestion(snap.smart_suggestion || null);
    if (snap.license) renderLicenseState(snap.license);
    if (snap.beta_guard && typeof window.__applyBetaGuardState === "function") window.__applyBetaGuardState(snap.beta_guard);
    if (snap.n8n && typeof window.__renderN8nState === "function") window.__renderN8nState(snap.n8n);
    if (Array.isArray(snap.monitors) && typeof window.__applyMonitorTopology === "function") {
      window.__applyMonitorTopology({ monitors: snap.monitors });
    }
    if (snap.brain_activity && typeof window.__applyBrainActivity === "function") {
      window.__applyBrainActivity(snap.brain_activity, false);
    }
  }

  function renderAgentMonitor(mon) {
    if (!mon || typeof mon !== "object") return;
    S.agentMonitor = mon;
    const stepEl = $("agent-monitor-step");
    const badgeEl = $("agent-autonomy-badge");
    const nasBadgeEl = $("nas-brain-badge");
    const panelEl = $("agent-monitor-panel");
    if (stepEl && (mon.step_label || S.visionStatus)) {
      const activeRun = Boolean(mon.active);
      const lbl = String(mon.step_label || "");
      const keepExplicit = activeRun || lbl.includes("NOT-STOPP") || lbl.includes("FEHLER");
      stepEl.textContent = keepExplicit ? lbl : (S.visionStatus || lbl);
      stepEl.classList.toggle("active-run", activeRun);
    }
    if (badgeEl) {
      const isMode2 = mon.autonomy_mode === "mode_2";
      badgeEl.textContent = isMode2 ? "MODUS 2: 100% AUTONOM" : "MODUS 1: SAFE-CHECK";
      badgeEl.className = `agent-mode-badge ${isMode2 ? "mode-2" : "mode-1"}`;
    }
    if (nasBadgeEl && mon.nas_brain) {
      const nb = mon.nas_brain;
      const connected = Boolean(nb.connected);
      const hybridTxt = nb.status_label || (connected ? "NVMe HOT-CACHE: OK // NAS HDD: SYNCED" : "NAS BRAIN: OFFLINE");
      nasBadgeEl.textContent = hybridTxt;
      nasBadgeEl.classList.toggle("connected", connected);
      if (nb.active_path) {
        nasBadgeEl.title = `HotCache (NVMe): ${nb.hot_cache_path || ""} | Cold-NAS: ${nb.active_path} | Episoden: ${nb.episodes_count || 0} | UI-Skills: ${nb.ui_skills_count || 0} | Sync: ${nb.last_cold_sync || ""}`;
      }
    }
    if (mon.agents && typeof mon.agents === "object") {
      ["overmind", "desktop_video", "web_ghost", "finance_quant"].forEach((id) => {
        const ag = mon.agents[id];
        if (!ag) return;
        const cardEl = $(`swarm-card-${id}`);
        const stateEl = $(`swarm-state-${id}`);
        const agStepEl = $(`swarm-step-${id}`);
        if (cardEl) {
          cardEl.classList.toggle("active", Boolean(ag.active));
          cardEl.classList.toggle("stopped", ag.status === "STOPPED");
        }
        if (stateEl && ag.status) {
          stateEl.textContent = ag.status;
        }
        if (agStepEl && ag.step) {
          agStepEl.textContent = ag.step;
        }
      });
    }
    if (panelEl) {
      panelEl.classList.toggle("agent-running", Boolean(mon.active));
    }
    S.agentRunning = Boolean(mon.active);
    if (el.camLiveText && _shouldShowDesktopVision()) {
      el.camLiveText.textContent = S.agentRunning ? "2 FPS" : "1 FPS";
    }
    if (typeof mon.multi_cursor_enabled === "boolean") {
      renderSwarmCursorToggle(mon.multi_cursor_enabled);
    }
  }

  function renderSwarmCursorToggle(enabled) {
    const btn = $("btn-swarm-cursor-toggle");
    if (!btn) return;
    const isOn = Boolean(enabled);
    btn.classList.toggle("is-on", isOn);
    btn.classList.toggle("is-off", !isOn);
    btn.setAttribute("aria-pressed", isOn ? "true" : "false");
    btn.textContent = isOn ? "[ ⚡ 4-CURSOR SWARM: ON ]" : "[ ⚡ 4-CURSOR SWARM: OFF ]";
  }

  function renderSmartSuggestion(sug) {
    const banner = $("smart-suggestion-banner");
    const txtEl = $("smart-suggestion-text");
    if (!banner || !txtEl) return;
    if (sug && typeof sug === "object" && sug.text) {
      txtEl.textContent = sug.pill_text || `💡 VORSCHLAG: ${sug.text}`;
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }
  }

  function renderLicenseState(lic) {
    if (!lic || typeof lic !== "object") return;
    S.license = lic;
    const badge = $("beta-version-badge");
    const banner = $("license-status-banner");
    const licClose = $("license-close");
    if (badge) {
      badge.textContent = "PUBLIC BETA v0.1";
    }
    if (banner) {
      banner.textContent = "Public Beta v0.1 (Open Access) – keine Lizenzsperre aktiv. Alle Funktionen stehen direkt zur Verfügung.";
    }
    if (licClose) {
      licClose.style.display = "inline-block";
    }
  }

  function applyTelemetry(data) {
    S.tel = { ...S.tel, ...data };
    S.telAt = performance.now();
    if (typeof window.__updateLive3ScreenMeta === "function") {
      window.__updateLive3ScreenMeta(data);
    }
  }

  function handleWs(msg) {
    if (!msg) return;
    if ((msg.expired === true || msg.type === "beta_guard" || msg.beta_guard) && typeof window.__applyBetaGuardState === "function") {
      if (window.__applyBetaGuardState(msg)) return;
    }
    if (!msg.type) return;
    switch (msg.type) {
      case "snapshot": applySnapshot(msg.data); break;
      case "agent_monitor": renderAgentMonitor(msg.agent_monitor || msg.data); break;
      case "swarm_cursor_mode": renderSwarmCursorToggle(msg.enabled); break;
      case "smart_suggestion": renderSmartSuggestion(msg.suggestion || null); break;
      case "license_status": if (msg.license) renderLicenseState(msg.license); break;
      case "n8n_status":
        if (msg.n8n && typeof window.__renderN8nState === "function") window.__renderN8nState(msg.n8n);
        break;
      case "telemetry_event":
        if (msg.event && typeof window.__onTelemetryEvent === "function") window.__onTelemetryEvent(msg.event);
        if (typeof window.__relayHudBroadcast === "function") window.__relayHudBroadcast(msg);
        break;
      case "brain_activity":
        if (typeof window.__applyBrainActivity === "function") window.__applyBrainActivity(msg, true);
        break;
      case "monitor_topology":
        if (typeof window.__applyMonitorTopology === "function") window.__applyMonitorTopology(msg);
        break;
      case "neural_thought":
        if (typeof window.__onNeuralThought === "function") window.__onNeuralThought(msg);
        if (typeof window.__relayHudBroadcast === "function") window.__relayHudBroadcast(msg);
        break;
      case "hud_window_command":
        if (typeof window.__relayHudBroadcast === "function") window.__relayHudBroadcast(msg);
        break;
      case "audio":
        S.level = Number(msg.level || 0);
        S.vad = Boolean(msg.vad_active);
        S.speaking = Boolean(msg.speaking_active);
        updateVadUi();
        break;
      case "status": setStatus(msg.status, msg.status_eng, msg.status_sub); break;
      case "mode": renderMode(msg.is_online); break;
      case "mic_muted": renderMic(msg.muted); break;
      case "model": setModel(msg.model_label); break;
      case "active_provider":
        S.activeProvider = msg.provider;
        if (el.modelDropdown) el.modelDropdown.value = msg.provider;
        break;
      case "autostart":
        S.autostartEnabled = Boolean(msg.enabled);
        S.settingsDraft.AUTOSTART = S.autostartEnabled ? "true" : "false";
        if (S.settingsTab === "sys") renderSettingsFields();
        break;
      case "user_text": {
        const uRaw = msg.raw || String(msg.text || "").replace(/^"|"$/g, "");
        if (el.sttText) el.sttText.textContent = uRaw;
        if (typeof window.__updateLiveSubtitle === "function") window.__updateLiveSubtitle("user", uRaw);
        chatPush("user", msg.raw || msg.text, msg.turn_id, msg.time);
        break;
      }
      case "jarvis_text":
        if (el.jarvisText) el.jarvisText.textContent = msg.text;
        if (typeof window.__updateLiveSubtitle === "function") window.__updateLiveSubtitle("action", msg.text);
        chatPush("jarvis", msg.text, msg.turn_id, msg.time);
        break;
      case "log":
        pushSystemLogLine(msg.line || "");
        break;
      case "response_time": S.latency = Number(msg.seconds || 0); break;
      case "camera_paused":
        renderCamera(msg.paused);
        if (msg.active !== undefined) renderCamSensor(msg.active);
        else renderCamSensor(!msg.paused);
        if (msg.vision_status) renderVisionStatus(msg.vision_status);
        break;
      case "vision_status":
        renderVisionStatus(msg.vision_status);
        break;
      case "telemetry": applyTelemetry(msg.data || {}); break;
      case "window_state":
        S.windowMinimized = Boolean(msg.minimized);
        if (!S.windowMinimized) {
          lastT = performance.now();
          syncSnapshot();
        }
        break;
      default: break;
    }
  }

  let ws = null;
  let wsTimer = null;
  function connectWs() {
    if (wsTimer) { clearTimeout(wsTimer); wsTimer = null; }
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${window.location.host}/ws`);
    ws.onopen = () => {
      S.wsConnected = true;
      if (typeof window.__setWebDemoMode === "function") window.__setWebDemoMode(false);
    };
    ws.onmessage = (evt) => {
      try { handleWs(JSON.parse(evt.data)); } catch (_e) { }
    };
    ws.onclose = () => {
      S.wsConnected = false;
      ws = null;
      wsTimer = setTimeout(connectWs, WS_RECONNECT_MS);
    };
    ws.onerror = () => { try { ws && ws.close(); } catch (_e) { } };
  }

  function syncSnapshot() {
    fetch("/api/state").then((r) => r.json()).then(applySnapshot).catch(() => {});
  }

  /* ============================================================
     INTERAKTION & API ACTIONS
  ============================================================ */
  async function postJson(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.json();
  }
  const action = (name) => postJson("/api/action", { action: name }).catch(() => ({}));

  async function toggleMode() {
    const next = !S.isOnline;
    renderMode(next);
    try { await postJson("/api/mode", { enabled: next }); } catch (_e) { renderMode(!next); }
  }
  if (el.modeSwitch) {
    el.modeSwitch.addEventListener("click", toggleMode);
    el.modeSwitch.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleMode(); }
    });
  }

  if (el.btnNet) {
    el.btnNet.addEventListener("click", () => {
      const hidden = el.netOverlay.classList.toggle("hidden");
      el.btnNet.classList.toggle("active", !hidden);
    });
  }
  const netClose = $("net-close");
  if (netClose) {
    netClose.addEventListener("click", () => {
      el.netOverlay.classList.add("hidden");
      el.btnNet.classList.remove("active");
    });
  }

  if (el.btnPower) {
    el.btnPower.addEventListener("click", (e) => { e.stopPropagation(); el.powerMenu.classList.toggle("hidden"); });
  }
  document.addEventListener("click", (e) => {
    if (el.powerMenu && !el.powerMenu.classList.contains("hidden") && !el.powerMenu.contains(e.target)) {
      el.powerMenu.classList.add("hidden");
    }
  });
  const menuMin = $("menu-min");
  if (menuMin) menuMin.addEventListener("click", () => { el.powerMenu.classList.add("hidden"); action("minimize"); });
  const menuExit = $("menu-exit");
  if (menuExit) {
    menuExit.addEventListener("click", () => {
      el.powerMenu.classList.add("hidden");
      if (confirm("Jarvis Command Center wirklich beenden? Alle Hintergrundprozesse werden gestoppt.")) {
        postJson("/api/shutdown", {}).catch(() => {});
        setStatus("Beendet", "OFFLINE", "SHUTDOWN");
        if (el.jarvisText) el.jarvisText.textContent = "Jarvis fährt herunter. Auf Wiedersehen, Chef.";
        setTimeout(() => {
          try { window.close(); } catch (_e) {}
        }, 800);
      }
    });
  }

  if (el.modelDropdown) {
    el.modelDropdown.addEventListener("change", async (e) => {
      const prov = e.target.value;
      S.activeProvider = prov;
      try {
        await postJson("/api/set_active_provider", { provider: prov });
      } catch (_e) {}
    });
  }

  if (el.coreStage) el.coreStage.addEventListener("click", () => action("speak"));
  const btnCamInfo = $("btn-cam-info");
  if (btnCamInfo) btnCamInfo.addEventListener("click", () => action("check_camera"));
  const actStatus = $("act-status");
  if (actStatus) actStatus.addEventListener("click", () => action("system_status"));
  const actAlarm = $("act-alarm");
  if (actAlarm) actAlarm.addEventListener("click", () => action("alarm_status"));
  const actCam = $("act-cam");
  if (actCam) actCam.addEventListener("click", () => action("check_camera"));

  if (el.btnMic) {
    el.btnMic.addEventListener("click", async () => {
      const res = await action("toggle_mic");
      if (res && res.mic_muted !== undefined) renderMic(res.mic_muted);
    });
  }
  if (el.btnCam) {
    el.btnCam.addEventListener("click", async () => {
      const res = await action("toggle_cam");
      if (res && res.camera_paused !== undefined) {
        renderCamera(res.camera_paused);
        renderCamSensor(res.active !== undefined ? res.active : !res.camera_paused);
        if (res.vision_status) renderVisionStatus(res.vision_status);
      }
    });
  }
  if (el.btnDesktopVisionToggle) {
    el.btnDesktopVisionToggle.addEventListener("click", () => {
      if (_shouldShowDesktopVision()) {
        S.desktopVisionMode = false;
        S.desktopVisionForced = false;
      } else {
        S.desktopVisionMode = true;
        S.desktopVisionForced = true;
      }
      renderCamSensor(S.cameraActive);
    });
  }
  if (el.btnCamToggle) {
    el.btnCamToggle.addEventListener("click", async () => {
      try {
        const res = await postJson("/api/camera/toggle", {});
        if (res && res.active !== undefined) {
          if (res.active) {
            S.desktopVisionForced = false;
          }
          if (res.camera_paused !== undefined) renderCamera(res.camera_paused);
          renderCamSensor(res.active);
          if (res.vision_status) renderVisionStatus(res.vision_status);
        }
      } catch (_e) {}
    });
  }

  const btnSwarmCursorToggle = $("btn-swarm-cursor-toggle");
  if (btnSwarmCursorToggle) {
    btnSwarmCursorToggle.addEventListener("click", async () => {
      const nextState = !btnSwarmCursorToggle.classList.contains("is-on");
      renderSwarmCursorToggle(nextState);
      try {
        const res = await postJson("/api/swarm/cursor_mode", { enabled: nextState });
        if (res && typeof res.enabled === "boolean") renderSwarmCursorToggle(res.enabled);
        if (res && res.agent_monitor) renderAgentMonitor(res.agent_monitor);
      } catch (_e) {}
    });
  }

  const btnExecutiveBriefing = $("btn-executive-briefing");
  if (btnExecutiveBriefing) {
    btnExecutiveBriefing.addEventListener("click", async () => {
      btnExecutiveBriefing.disabled = true;
      btnExecutiveBriefing.textContent = "[ ⚡ BRIEFING LÄUFT... ]";
      try {
        await postJson("/api/executive/briefing", { open_browser_cockpit: true });
      } catch (_e) {
      } finally {
        btnExecutiveBriefing.disabled = false;
        btnExecutiveBriefing.textContent = "[ ⚡ EXECUTIVE BRIEFING ]";
      }
    });
  }

  const btnSuggestionConfirm = $("btn-suggestion-confirm");
  if (btnSuggestionConfirm) {
    btnSuggestionConfirm.addEventListener("click", async () => {
      try {
        await postJson("/api/executive/suggestion/confirm", {});
        renderSmartSuggestion(null);
      } catch (_e) {}
    });
  }

  const btnSuggestionDismiss = $("btn-suggestion-dismiss");
  if (btnSuggestionDismiss) {
    btnSuggestionDismiss.addEventListener("click", async () => {
      try {
        await postJson("/api/executive/suggestion/dismiss", {});
        renderSmartSuggestion(null);
      } catch (_e) {}
    });
  }

  const btnKillAgent = $("btn-kill-agent");
  if (btnKillAgent) {
    btnKillAgent.addEventListener("click", async () => {
      btnKillAgent.classList.add("triggered");
      const stepEl = $("agent-monitor-step");
      if (stepEl) stepEl.textContent = "🛑 [NOT-STOPP] Agent sofort angehalten!";
      try {
        const res = await postJson("/api/agent/kill", {});
        if (res && res.agent_monitor) renderAgentMonitor(res.agent_monitor);
      } catch (_e) {}
      setTimeout(() => btnKillAgent.classList.remove("triggered"), 900);
    });
  }

  /* ============================================================
     SETTINGS-MODAL & PRIORITY CASCADE (#1 bis #5)
  ============================================================ */
  const PROVIDER_METAS = {
    gemini: { name: "Google Gemini", model: "Gemini 2.5 Flash", envKey: "GEMINI_API_KEY", secret: true, keyless: false },
    groq: { name: "Groq LLaMA", model: "LLaMA 3.3 70B Versatile", envKey: "GROQ_API_KEY", secret: true, keyless: false },
    openrouter: { name: "OpenRouter", model: "OpenRouter Cascade / Free", envKey: "OPENROUTER_API_KEY", secret: true, keyless: false },
    deepseek: { name: "DeepSeek", model: "DeepSeek V3 Chat", envKey: "DEEPSEEK_API_KEY", secret: true, keyless: false },
    pollinations: { name: "Pollinations AI", model: "Keyless Free Online AI", envKey: "POLLINATIONS_API_KEY", secret: false, keyless: true },
  };

  function movePriority(fromIdx, toIdx) {
    if (toIdx < 0 || toIdx >= S.modelPriority.length) return;
    const item = S.modelPriority.splice(fromIdx, 1)[0];
    S.modelPriority.splice(toIdx, 0, item);
    postJson("/api/model_priority", { priority: S.modelPriority }).catch(() => {});
    renderSettingsFields();
  }

  async function verifyKey(provId, keyVal, statusEl, btnEl) {
    statusEl.className = "prio-status-badge checking";
    statusEl.textContent = "🟡 PRÜFE...";
    btnEl.disabled = true;
    try {
      const res = await postJson("/api/verify_key", { provider: provId, api_key: keyVal });
      if (res && res.valid) {
        statusEl.className = "prio-status-badge valid";
        statusEl.textContent = `🟢 GÜLTIG (${res.latency_ms}ms)`;
        S.providerVerifications[provId] = { valid: true, latency: res.latency_ms };
      } else {
        statusEl.className = "prio-status-badge invalid";
        const detail = (res && res.detail) ? res.detail.slice(0, 24) : "FEHLER";
        statusEl.textContent = `🔴 ${detail}`;
        S.providerVerifications[provId] = { valid: false, detail };
      }
    } catch (_e) {
      statusEl.className = "prio-status-badge invalid";
      statusEl.textContent = "🔴 TIMEOUT / FEHLER";
    } finally {
      btnEl.disabled = false;
    }
  }

  S.vaultDraft = {};
  S.vaultAccounts = {};

  function openSettings() {
    el.settingsModal.classList.remove("hidden");
    Promise.all([
      fetch("/api/settings").then((r) => r.json()).catch(() => ({})),
      fetch("/api/model_priority").then((r) => r.json()).catch(() => ({ priority: S.modelPriority })),
      fetch("/api/vault").then((r) => r.json()).catch(() => ({})),
    ]).then(([settingsData, prioData, vaultData]) => {
      S.settingsDraft = { ...settingsData };
      if (prioData && Array.isArray(prioData.priority) && prioData.priority.length) {
        S.modelPriority = prioData.priority;
      }
      if (settingsData && settingsData.AUTOSTART !== undefined) {
        S.autostartEnabled = settingsData.AUTOSTART === "true";
      }
      if (vaultData && typeof vaultData === "object") {
        S.vaultDraft = { ...(vaultData.fields || vaultData), AUTONOMY_MODE: vaultData.AUTONOMY_MODE || vaultData.autonomy_mode || "mode_1" };
        S.vaultAccounts = vaultData.SAVED_ACCOUNTS_LIST || vaultData.saved_accounts || [];
      }
      renderSettingsFields();
    });
  }
  function closeSettings() {
    el.settingsModal.classList.add("hidden");
  }
  if (el.btnSettings) el.btnSettings.addEventListener("click", openSettings);
  const settingsClose = $("settings-close");
  if (settingsClose) settingsClose.addEventListener("click", closeSettings);

  document.querySelectorAll(".s-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".s-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      S.settingsTab = tab.getAttribute("data-tab");
      renderSettingsFields();
    });
  });

  function formatExecutionModeBadgeText(execMode, autonomyMode) {
    const isFull = (execMode === 'FULL_AUTO' || autonomyMode === 'mode_2');
    return isFull
        ? '⚡ MODUS B: 100% VOLL-ZUGRIFF'
        : '🛡️ MODUS A: HALB-AUTONOM';
}

function syncHudExecutionModeBadge(execMode, autonomyMode) {
    const badge = document.getElementById('agent-autonomy-badge');
    if (!badge) return;
    const isFull = (execMode === 'FULL_AUTO' || autonomyMode === 'mode_2');
    const desiredText = formatExecutionModeBadgeText(execMode, autonomyMode);
    if (badge.textContent !== desiredText) {
        badge.textContent = desiredText;
    }
    badge.dataset.executionMode = isFull ? 'FULL_AUTO' : 'SEMI_AUTO';
    badge.classList.toggle('mode-2', isFull);
    badge.style.cursor = 'pointer';
    badge.title = 'Klicken zum Umschalten zwischen 🛡️ MODUS A (Halb-Autonom) und ⚡ MODUS B (100% Voll-Zugriff)';
    if (!badge.dataset.quickToggleBound) {
        badge.dataset.quickToggleBound = '1';
        badge.addEventListener('click', async () => {
            const current = badge.dataset.executionMode || 'SEMI_AUTO';
            const nextMode = current === 'FULL_AUTO' ? 'SEMI_AUTO' : 'FULL_AUTO';
            try {
                const res = await fetch('/api/execution_mode', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ execution_mode: nextMode })
                });
                const data = await res.json();
                if (data && data.ok) {
                    syncHudExecutionModeBadge(data.execution_mode, data.autonomy_mode);
                }
            } catch (err) {
                console.warn('Execution mode toggle failed:', err);
            }
        });
        const obs = new MutationObserver(() => {
            const txt = badge.textContent || '';
            if (txt.includes('MODUS 1') || txt.includes('MODUS 2')) {
                const full = txt.includes('MODUS 2') || badge.classList.contains('mode-2');
                badge.textContent = formatExecutionModeBadgeText(full ? 'FULL_AUTO' : 'SEMI_AUTO', full ? 'mode_2' : 'mode_1');
            }
        });
        obs.observe(badge, { childList: true, characterData: true, subtree: true });
    }
}

setTimeout(async () => {
    try {
        const res = await fetch('/api/execution_mode');
        const data = await res.json();
        if (data && data.ok) {
            syncHudExecutionModeBadge(data.execution_mode, data.autonomy_mode);
        }
    } catch (_) {}
}, 900);

async function renderVaultSettingsTabV2(container) {
    container.innerHTML = '<div style="padding:18px;color:#94a3b8;">Lade AES-Fernet Tresor, 2-Stufen-Kaufberechtigung & Trading-Scanner...</div>';
    let vaultData = {};
    let portfolioData = {};
    try {
        const [vRes, pRes] = await Promise.all([
            fetch('/api/vault'),
            fetch('/api/trading/portfolio')
        ]);
        const vJson = await vRes.json();
        const pJson = await pRes.json();
        vaultData = (vJson && vJson.vault) ? vJson.vault : {};
        portfolioData = (pJson && pJson.ok) ? pJson : {};
    } catch (e) {
        console.warn('Vault load error:', e);
    }

    const execMode = vaultData.EXECUTION_MODE || (vaultData.AUTONOMY_MODE === 'mode_2' ? 'FULL_AUTO' : 'SEMI_AUTO');
    const isFullAuto = execMode === 'FULL_AUTO';
    syncHudExecutionModeBadge(execMode, vaultData.AUTONOMY_MODE);

    const realizedTotal = Number(portfolioData.realized_profit_total_eur || 0).toFixed(2);
    const cashEur = Number(portfolioData.cash_eur || 10000).toFixed(2);
    const journal = Array.isArray(portfolioData.trade_journal) ? portfolioData.trade_journal : [];

    container.innerHTML = `
        <div class="vault-settings-wrap" style="display:flex;flex-direction:column;gap:16px;padding:6px 4px;">
            <!-- AUFGABE 1: 2-Stufen-Kaufberechtigung (EXECUTION_MODE) -->
            <div style="background:rgba(15,23,42,0.85);border:1px solid rgba(56,189,248,0.35);border-radius:10px;padding:14px;">
                <div style="font-weight:700;color:#38bdf8;font-size:13px;letter-spacing:0.06em;margin-bottom:8px;">
                    ⚖️ 1. 2-STUFEN-KAUFBERECHTIGUNG (EXECUTION_MODE)
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
                    <label id="exec-mode-card-a" style="display:flex;flex-direction:column;gap:6px;padding:12px;border-radius:8px;cursor:pointer;border:2px solid ${!isFullAuto ? '#22c55e' : 'rgba(148,163,184,0.25)'};background:${!isFullAuto ? 'rgba(34,197,94,0.12)' : 'rgba(15,23,42,0.6)'};">
                        <div style="display:flex;align-items:center;gap:8px;font-weight:700;color:#f8fafc;font-size:12.5px;">
                            <input type="radio" name="jarvis_exec_mode" value="SEMI_AUTO" ${!isFullAuto ? 'checked' : ''} />
                            🛡️ MODUS A: HALB-AUTONOM (Standard)
                        </div>
                        <div style="font-size:11.5px;color:#cbd5e1;line-height:1.4;">
                            <strong>Vorbereiten -> Ich drücke selbst auf Kaufen:</strong> Öffnet den Browser, loggt sich ein, trägt Aktie/Produkt, Stückzahl & Debitkarte komplett ein, parkt die Maus direkt neben dem Kauf-Button und ruft dich per Sprache.
                        </div>
                    </label>
                    <label id="exec-mode-card-b" style="display:flex;flex-direction:column;gap:6px;padding:12px;border-radius:8px;cursor:pointer;border:2px solid ${isFullAuto ? '#f59e0b' : 'rgba(148,163,184,0.25)'};background:${isFullAuto ? 'rgba(245,158,11,0.14)' : 'rgba(15,23,42,0.6)'};">
                        <div style="display:flex;align-items:center;gap:8px;font-weight:700;color:#f8fafc;font-size:12.5px;">
                            <input type="radio" name="jarvis_exec_mode" value="FULL_AUTO" ${isFullAuto ? 'checked' : ''} />
                            ⚡ MODUS B: VOLL-ZUGRIFF (100% Autonom)
                        </div>
                        <div style="font-size:11.5px;color:#cbd5e1;line-height:1.4;">
                            <strong>100% Autonomes Kaufen & Verkaufen:</strong> Volle Handels- und Kaufvollmacht – füllt alles aus UND klickt auch den finalen Kauf- oder Verkaufs-Button komplett selbstständig ohne Rückfrage!
                        </div>
                    </label>
                </div>
            </div>

            <!-- AUFGABE 2: Autonomer E-Mail- & Account-Schöpfer (mail_factory.py) -->
            <div style="background:rgba(15,23,42,0.85);border:1px solid rgba(56,189,248,0.3);border-radius:10px;padding:14px;">
                <div style="font-weight:700;color:#38bdf8;font-size:13px;letter-spacing:0.06em;margin-bottom:6px;">
                    📧 2. AUTONOMER E-MAIL- & ACCOUNT-SCHÖPFER (DUAL-SYSTEM)
                </div>
                <div style="font-size:11.5px;color:#94a3b8;margin-bottom:10px;">
                    Erstellt auf Wunsch vollautomatisch echte E-Mail-Postfächer + sichere 20-Zeichen-Passwörter, speichert sie AES-verschlüsselt im Tresor und liest 6-stellige Verifizierungscodes im Hintergrund aus.
                </div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;">
                    <button type="button" id="btn-mail-gen-id" class="hud-btn" style="padding:7px 11px;font-size:11.5px;">🔑 20-Zeichen Passwort & Identität generieren</button>
                    <button type="button" id="btn-mail-instant" class="hud-btn" style="padding:7px 11px;font-size:11.5px;border-color:#22c55e;color:#4ade80;">⚡ Methode 1: 1-Sekunden E-Mail erstellen (api.mail.tm)</button>
                    <button type="button" id="btn-mail-browser" class="hud-btn" style="padding:7px 11px;font-size:11.5px;">🌐 Methode 2: Browser E-Mail anlegen (Proton.me 1-FPS)</button>
                    <button type="button" id="btn-mail-poll" class="hud-btn" style="padding:7px 11px;font-size:11.5px;">📩 6-stelligen Bestätigungscode abrufen</button>
                </div>
                <div id="mail-factory-status" style="font-size:11.5px;color:#e2e8f0;background:rgba(2,6,23,0.7);padding:8px 10px;border-radius:6px;border:1px solid rgba(148,163,184,0.2);">
                    Aktives Agenten-Postfach: <strong>${vaultData.VAULT_MAILTM_ADDRESS || vaultData.VAULT_EMAIL || 'Noch keines generiert'}</strong>
                </div>
            </div>

            <!-- AUFGABE 3: Tresor für Debit-Kreditkarte & 3D-Secure / PushTAN -->
            <div style="background:rgba(15,23,42,0.85);border:1px solid rgba(56,189,248,0.3);border-radius:10px;padding:14px;">
                <div style="font-weight:700;color:#38bdf8;font-size:13px;letter-spacing:0.06em;margin-bottom:6px;">
                    💳 3. DEBIT-KREDITKARTE, IBAN & 3D-SECURE / S-PUSHTAN (AES-FERNET VERSCHLÜSSELT)
                </div>
                <div style="font-size:11.5px;color:#94a3b8;margin-bottom:10px;">
                    Zero-Knowledge Schutz: Deine Kartendaten werden lokal per <code>{{VAULT_CARD_NUMBER}}</code>, <code>{{VAULT_CARD_EXPIRY}}</code> & <code>{{VAULT_CARD_CVC}}</code> eingetippt und niemals im Klartext an Cloud-KIs gesendet. Bei 3D-Secure/PushTAN wartet Jarvis bis zu 90s geduldig auf deine Handy-Freigabe.
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Karteninhaber (VAULT_CARD_HOLDER)</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_CARD_HOLDER" placeholder="${vaultData.VAULT_CARD_HOLDER || 'Max Mustermann'}" style="width:100%;padding:7px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Kartennummer 16-stellig (VAULT_CARD_NUMBER)</label>
                        <input type="password" class="vault-input" data-vault-key="VAULT_CARD_NUMBER" placeholder="${vaultData.VAULT_CARD_NUMBER || '•••• •••• •••• ••••'}" style="width:100%;padding:7px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Ablaufdatum MM/JJ (VAULT_CARD_EXPIRY)</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_CARD_EXPIRY" placeholder="${vaultData.VAULT_CARD_EXPIRY || '08/29'}" style="width:100%;padding:7px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">CVC / Sicherheitscode 3-stellig (VAULT_CARD_CVC)</label>
                        <input type="password" class="vault-input" data-vault-key="VAULT_CARD_CVC" placeholder="${vaultData.VAULT_CARD_CVC || '•••'}" style="width:100%;padding:7px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">IBAN Referenzkonto (VAULT_IBAN)</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_IBAN" placeholder="${vaultData.VAULT_IBAN || 'DE00 •••• ••••'}" style="width:100%;padding:7px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Rechnungsadresse (VAULT_BILLING_ADDRESS)</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_BILLING_ADDRESS" placeholder="${vaultData.VAULT_BILLING_ADDRESS || 'Musterstraße 1, 10115 Berlin'}" style="width:100%;padding:7px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                </div>
            </div>

            <!-- Persönliche Identitäts- & E-Mail-Felder -->
            <div style="background:rgba(15,23,42,0.85);border:1px solid rgba(56,189,248,0.25);border-radius:10px;padding:14px;">
                <div style="font-weight:700;color:#38bdf8;font-size:13px;letter-spacing:0.06em;margin-bottom:8px;">
                    👤 PERSÖNLICHE IDENTITÄT & LOGINS (ZERO-KNOWLEDGE PLATZHALTER)
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Vollständiger Name (VAULT_FULL_NAME)</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_FULL_NAME" placeholder="${vaultData.VAULT_FULL_NAME || 'Name'}" style="width:100%;padding:7px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Benutzername (VAULT_USERNAME)</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_USERNAME" placeholder="${vaultData.VAULT_USERNAME || 'Username'}" style="width:100%;padding:7px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">E-Mail-Adresse (VAULT_EMAIL)</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_EMAIL" placeholder="${vaultData.VAULT_EMAIL || 'mail@example.com'}" style="width:100%;padding:7px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Passwort 20-stellig (VAULT_PASSWORD)</label>
                        <input type="password" class="vault-input" data-vault-key="VAULT_PASSWORD" placeholder="${vaultData.VAULT_PASSWORD || '••••••••'}" style="width:100%;padding:7px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Geburtsdatum (VAULT_BIRTHDATE)</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_BIRTHDATE" placeholder="${vaultData.VAULT_BIRTHDATE || '01.01.1995'}" style="width:100%;padding:7px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Telefonnummer (VAULT_PHONE)</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_PHONE" placeholder="${vaultData.VAULT_PHONE || '+49 ...'}" style="width:100%;padding:7px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                </div>
            </div>

            <!-- AUFGABE 4: Autonomer "Buy Low, Sell High" Aktien-Analyst & Trader -->
            <div style="background:rgba(15,23,42,0.85);border:1px solid rgba(56,189,248,0.3);border-radius:10px;padding:14px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                    <div style="font-weight:700;color:#38bdf8;font-size:13px;letter-spacing:0.06em;">
                        📈 4. AUTONOMER "BUY LOW, SELL HIGH" AKTIEN-ANALYST & TRADER
                    </div>
                    <div style="font-size:12px;color:#4ade80;font-weight:700;">
                        Cash: ${cashEur} € | Realisierter Gewinn: ${realizedTotal} €
                    </div>
                </div>
                <div style="font-size:11.5px;color:#94a3b8;margin-bottom:10px;">
                    KAUF-SIGNAL (<code>BUY_LOW</code>): Kurs am unteren Bollinger-Band, <code>RSI &lt; 32</code> &amp; Kerze dreht nach oben.<br/>
                    VERKAUF-SIGNAL (<code>SELL_HIGH</code>): Position im Plus + Take-Profit erreicht ODER <code>RSI &gt; 72</code> &amp; Kurs knickt ab.
                </div>
                <div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:8px;margin-bottom:10px;">
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Take-Profit Ziel (%)</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_TAKE_PROFIT_PCT" value="${vaultData.VAULT_TAKE_PROFIT_PCT || '3.5'}" style="width:100%;padding:6px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Stop-Loss Schutz (%)</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_STOP_LOSS_PCT" value="${vaultData.VAULT_STOP_LOSS_PCT || '2.0'}" style="width:100%;padding:6px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Tageslimit (EUR)</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_MAX_DAILY_LIMIT_EUR" value="${vaultData.VAULT_MAX_DAILY_LIMIT_EUR || '250.00'}" style="width:100%;padding:6px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                    <div>
                        <label style="font-size:11px;color:#cbd5e1;">Web-Broker URL</label>
                        <input type="text" class="vault-input" data-vault-key="VAULT_BROKER_URL" placeholder="${vaultData.VAULT_BROKER_URL || 'https://app.traderepublic.com'}" style="width:100%;padding:6px;border-radius:6px;background:#0f172a;border:1px solid #334155;color:#f8fafc;" />
                    </div>
                </div>
                <div style="display:flex;gap:8px;margin-bottom:8px;">
                    <button type="button" id="btn-scan-watchlist" class="hud-btn" style="padding:7px 12px;font-size:11.5px;border-color:#38bdf8;color:#38bdf8;">🔍 Watchlist jetzt scannen (RSI-14 & Bollinger-Bänder)</button>
                </div>
                <div id="trading-scanner-output" style="font-size:11.5px;color:#cbd5e1;background:rgba(2,6,23,0.7);padding:8px;border-radius:6px;max-height:150px;overflow-y:auto;">
                    ${journal.length > 0 ? journal.slice(0, 5).map(j => `<div>• [${j.timestamp}] <strong>${j.side} ${j.qty}x ${j.symbol}</strong> | Kauf: ${j.buy_price} € ${j.sell_price ? `| Verkauf: ${j.sell_price} € | Gewinn: <span style="color:${j.realized_profit_eur >= 0 ? '#4ade80' : '#f87171'};">${j.realized_profit_eur >= 0 ? '+' : ''}${j.realized_profit_eur} €</span>` : ''} (${j.signal})</div>`).join('') : 'Noch keine abgeschlossenen Trades im Journal. Klicke auf "Watchlist jetzt scannen", um Live-Signale zu berechnen.'}
                </div>
            </div>

            <div style="display:flex;justify-content:flex-end;gap:10px;">
                <button type="button" id="btn-save-vault-v2" class="hud-btn" style="padding:9px 18px;font-weight:700;border-color:#22c55e;color:#4ade80;">
                    💾 TRESOR & KAUF-MODUS VERSCHLÜSSELT SPEICHERN
                </button>
            </div>
        </div>
    `;

    const radios = container.querySelectorAll('input[name="jarvis_exec_mode"]');
    radios.forEach(r => {
        r.addEventListener('change', async () => {
            const selected = r.value;
            try {
                const res = await fetch('/api/execution_mode', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ execution_mode: selected })
                });
                const data = await res.json();
                if (data && data.ok) {
                    syncHudExecutionModeBadge(data.execution_mode, data.autonomy_mode);
                    renderVaultSettingsTabV2(container);
                }
            } catch (err) {
                console.warn('Failed to switch execution mode:', err);
            }
        });
    });

    const statusBox = container.querySelector('#mail-factory-status');
    const btnGenId = container.querySelector('#btn-mail-gen-id');
    if (btnGenId) {
        btnGenId.addEventListener('click', async () => {
            statusBox.textContent = 'Generiere sichere 20-Zeichen-Identität...';
            const res = await fetch('/api/mail_factory/generate_identity', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
            const d = await res.json();
            statusBox.innerHTML = `✅ Identität im Tresor gespeichert: <strong>${d.full_name}</strong> (User: <code>${d.username}</code> | Passwort: ${d.password_length} Zeichen verschlüsselt)`;
        });
    }
    const btnInstant = container.querySelector('#btn-mail-instant');
    if (btnInstant) {
        btnInstant.addEventListener('click', async () => {
            statusBox.textContent = 'Erstelle 1-Sekunden E-Mail-Postfach über api.mail.tm...';
            const res = await fetch('/api/mail_factory/create_instant', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
            const d = await res.json();
            statusBox.innerHTML = `✅ ${d.message || d.email} (Passwort: ${d.password_length || 20} Zeichen im Tresor gespeichert)`;
        });
    }
    const btnBrowserMail = container.querySelector('#btn-mail-browser');
    if (btnBrowserMail) {
        btnBrowserMail.addEventListener('click', async () => {
            statusBox.textContent = 'Starte 1-FPS Browser-Erstellung (Proton.me)...';
            const res = await fetch('/api/mail_factory/create_browser', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: 'proton' }) });
            const d = await res.json();
            statusBox.innerHTML = `🌐 ${d.message || d.email}`;
        });
    }
    const btnPollMail = container.querySelector('#btn-mail-poll');
    if (btnPollMail) {
        btnPollMail.addEventListener('click', async () => {
            statusBox.textContent = 'Prüfe Postfach auf 6-stelligen Verifizierungscode...';
            const res = await fetch('/api/mail_factory/poll');
            const d = await res.json();
            if (d && d.ok) {
                statusBox.innerHTML = `📩 Code empfangen: <strong style="color:#4ade80;font-size:13px;">${d.code || 'Kein Zahlencode'}</strong> | Link: ${d.link ? `<a href="${d.link}" target="_blank" style="color:#38bdf8;">Bestätigungslink</a>` : 'Kein Link'}`;
            } else {
                statusBox.innerHTML = `ℹ️ ${d.error || 'Noch keine neue Verifizierungs-Mail eingegangen.'}`;
            }
        });
    }

    const btnScan = container.querySelector('#btn-scan-watchlist');
    const scanOut = container.querySelector('#trading-scanner-output');
    if (btnScan && scanOut) {
        btnScan.addEventListener('click', async () => {
            scanOut.textContent = 'Berechne RSI(14), Bollinger-Bänder (20, 2.0) & Kerzendrehung für Watchlist...';
            const res = await fetch('/api/trading/scan_now', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ interval: '15m', auto_execute: false })
            });
            const d = await res.json();
            if (d && Array.isArray(d.signals)) {
                scanOut.innerHTML = d.signals.map(s => {
                    const color = s.signal === 'BUY_LOW' ? '#4ade80' : (s.signal === 'SELL_HIGH' ? '#fbbf24' : '#94a3b8');
                    return `<div style="margin-bottom:4px;">• <strong>${s.symbol}</strong>: ${s.price} ${s.currency} | RSI: ${s.rsi_14} | BB-Unten: ${s.bb_lower} | <span style="color:${color};font-weight:700;">[${s.signal}]</span> ${s.recommendation}</div>`;
                }).join('');
            }
        });
    }

    const btnSave = container.querySelector('#btn-save-vault-v2');
    if (btnSave) {
        btnSave.addEventListener('click', async () => {
            const payload = {};
            const selRadio = container.querySelector('input[name="jarvis_exec_mode"]:checked');
            if (selRadio) {
                payload.EXECUTION_MODE = selRadio.value;
            }
            container.querySelectorAll('.vault-input').forEach(inp => {
                const key = inp.getAttribute('data-vault-key');
                const val = (inp.value || '').trim();
                if (key && val) {
                    payload[key] = val;
                }
            });
            const res = await fetch('/api/vault', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const d = await res.json();
            if (d && d.ok) {
                btnSave.textContent = '✅ IM AES-TRESOR GESPEICHERT!';
                setTimeout(() => renderVaultSettingsTabV2(container), 800);
            }
        });
    }
}

function renderVaultSettingsTab(container) {
    renderVaultSettingsTabV2(container);
    return;
}

function _legacyRenderVaultSettingsTab(container) {
    const wrap = document.createElement("div");
    wrap.className = "vault-settings-panel";

    // Header banner: AES-256 Fernet & Zero-Knowledge Info
    const banner = document.createElement("div");
    banner.className = "vault-banner";
    banner.innerHTML = `
      <div class="vb-title">🔐 LOKALER AES-FERNET TRESOR &amp; ZERO-KNOWLEDGE SCHUTZ</div>
      <div class="vb-desc">
        Alle Daten werden lokal verschlüsselt in <code>%LOCALAPPDATA%\\JarvisCommandCenter\\secure_vault.enc</code> gespeichert.
        Cloud-KIs sehen nur Platzhalter wie <code>{{VAULT_PASSWORD}}</code> oder <code>{{VAULT_IBAN}}</code> – deine echten Daten verlassen niemals deinen PC!
      </div>
    `;
    wrap.appendChild(banner);

    // 1. AUTONOMIE-LEVEL-SCHALTER
    const modeSection = document.createElement("div");
    modeSection.className = "vault-section";
    const modeTitle = document.createElement("div");
    modeTitle.className = "vault-sec-title";
    modeTitle.textContent = "⚡ AUTONOMIE-LEVEL-SCHALTER (SICHERHEITS-MODUS)";
    modeSection.appendChild(modeTitle);

    const modeGrid = document.createElement("div");
    modeGrid.className = "vault-mode-grid";

    const modes = [
      {
        id: "mode_1",
        title: "MODUS 1: BESTÄTIGUNG VOR GELD-TRANSAKTION",
        desc: "Sicherheits-Check vor dem finalen Kauf-Klick. Jarvis bereitet Orders und Formulare komplett vor und fragt dich kurz vor dem Absenden um Freigabe.",
      },
      {
        id: "mode_2",
        title: "MODUS 2: 100% VOLL-AUTONOM",
        desc: "Führt Käufe, Aktien-/Krypto-Trades (bis zum Tageslimit) und komplette Account-Erstellungen inkl. 2FA-E-Mail-Verifizierung selbstständig aus.",
      },
    ];

    modes.forEach((m) => {
      const btn = document.createElement("div");
      const active = (S.vaultDraft.AUTONOMY_MODE || "mode_1") === m.id;
      btn.className = `vault-mode-card ${active ? "active" : ""} ${m.id === "mode_2" ? "mode-danger" : "mode-safe"}`;
      btn.innerHTML = `<div class="vm-head">${active ? "🟢 AKTIV // " : ""}${m.title}</div><div class="vm-desc">${m.desc}</div>`;
      btn.addEventListener("click", () => {
        S.vaultDraft.AUTONOMY_MODE = m.id;
        renderSettingsFields();
      });
      modeGrid.appendChild(btn);
    });
    modeSection.appendChild(modeGrid);
    wrap.appendChild(modeSection);

    // Helper for adding field sections
    function addFieldSection(secTitle, fields) {
      const sec = document.createElement("div");
      sec.className = "vault-section";
      const st = document.createElement("div");
      st.className = "vault-sec-title";
      st.textContent = secTitle;
      sec.appendChild(st);

      const grid = document.createElement("div");
      grid.className = "vault-fields-grid";

      fields.forEach(({ key, label, secret, placeholder, options }) => {
        const row = document.createElement("div");
        row.className = "s-row";
        const lbl = document.createElement("label");
        lbl.innerHTML = `${label} <code class="vk-tag">{{${key}}}</code>`;
        row.appendChild(lbl);

        if (options) {
          const sel = document.createElement("select");
          sel.className = "vault-select";
          options.forEach((opt) => {
            const o = document.createElement("option");
            o.value = opt.value;
            o.textContent = opt.label;
            if ((S.vaultDraft[key] || options[0].value) === opt.value) o.selected = true;
            sel.appendChild(o);
          });
          sel.addEventListener("change", (e) => {
            S.vaultDraft[key] = e.target.value;
          });
          row.appendChild(sel);
        } else {
          const inp = document.createElement("input");
          inp.type = secret ? "password" : "text";
          inp.name = key;
          inp.placeholder = placeholder || "";
          inp.value = S.vaultDraft[key] || "";
          inp.autocomplete = "off";
          inp.addEventListener("input", (e) => {
            S.vaultDraft[key] = e.target.value;
          });
          row.appendChild(inp);
        }
        grid.appendChild(row);
      });
      sec.appendChild(grid);
      wrap.appendChild(sec);
    }

    // 2. STANDARD-IDENTITÄT FÜR ACCOUNT-ERSTELLUNGEN
    addFieldSection("👤 STANDARD-IDENTITÄT FÜR AUTONOME ACCOUNT-ERSTELLUNGEN", [
      { key: "VAULT_FULLNAME", label: "Vollständiger Name", secret: false, placeholder: "Max Mustermann" },
      { key: "VAULT_EMAIL", label: "Standard E-Mail-Adresse", secret: false, placeholder: "name@beispiel.de" },
      { key: "VAULT_PASSWORD", label: "Standard-Passwort (Verschlüsselt)", secret: true, placeholder: "Sicheres Standard-Passwort" },
      { key: "VAULT_USERNAME", label: "Standard-Benutzername", secret: false, placeholder: "max_cyber99" },
      { key: "VAULT_PHONE", label: "Telefonnummer", secret: true, placeholder: "+49 170 ..." },
      { key: "VAULT_BIRTHDATE", label: "Geburtsdatum (TT.MM.JJJJ)", secret: false, placeholder: "01.01.1995" },
      { key: "VAULT_STREET", label: "Straße & Hausnummer", secret: false, placeholder: "Musterstraße 12" },
      { key: "VAULT_POSTAL_CODE", label: "Postleitzahl", secret: false, placeholder: "10115" },
      { key: "VAULT_CITY", label: "Stadt / Ort", secret: false, placeholder: "Berlin" },
    ]);

    // 3. IMAP-ZUGANG FÜR AUTOMATISCHE 2FA-CODES & BESTÄTIGUNGSMAILS
    addFieldSection("📧 IMAP-ZUGANG (AUTOMATISCHES AUSLESEN VON 2FA-CODES & AKTIVIERUNGSLINKS)", [
      { key: "VAULT_IMAP_HOST", label: "IMAP-Server", secret: false, placeholder: "imap.gmail.com" },
      { key: "VAULT_IMAP_EMAIL", label: "IMAP E-Mail-Konto", secret: false, placeholder: "name@gmail.com" },
      { key: "VAULT_IMAP_PASSWORD", label: "IMAP App-Passwort (Verschlüsselt)", secret: true, placeholder: "16-stelliges App-Passwort" },
    ]);

    // 4. ZAHLUNGS- & TRADING-DATEN (ALPACA & BROWSER-BROKER)
    addFieldSection("📈 ZAHLUNGS- & TRADING-DATEN (AKTIEN, KRYPTO & WEB-BROKER)", [
      {
        key: "VAULT_TRADING_MODE",
        label: "Börsen-Handelsmodus",
        secret: false,
        options: [
          { value: "paper", label: "🧪 PAPER-TRADING (Echtzeit-Kurse mit 10.000 € Spielgeld – Gefahrlos testen)" },
          { value: "live", label: "💸 LIVE-TRADING (Echtgeld-Handel über Alpaca / Web-Broker)" },
        ],
      },
      { key: "VAULT_MAX_DAILY_LIMIT_EUR", label: "Max. Tageslimit in €", secret: false, placeholder: "250.00" },
      { key: "VAULT_IBAN", label: "IBAN / Zahlungsprofil (Verschlüsselt)", secret: true, placeholder: "DE00 0000 ..." },
      { key: "VAULT_ALPACA_KEY_ID", label: "Alpaca / Broker API-Key ID", secret: true, placeholder: "PK..." },
      { key: "VAULT_ALPACA_SECRET_KEY", label: "Alpaca / Broker API-Secret", secret: true, placeholder: "Geheimer Broker-Key" },
      { key: "VAULT_BROKER_URL", label: "Web-Broker URL (Weg B: Sichtbarer Browser-Handel)", secret: false, placeholder: "https://app.traderepublic.com" },
    ]);

    // 5. GESPEICHERTE ACCOUNTS AUS AUTONOMEN REGISTRIERUNGEN
    const accArr = Array.isArray(S.vaultAccounts) ? S.vaultAccounts : [];
    if (accArr.length > 0) {
      const accSec = document.createElement("div");
      accSec.className = "vault-section";
      accSec.innerHTML = `<div class="vault-sec-title">🗂 VOM AGENTEN AUTOMATISCH ERSTELLTE &amp; GESPEICHERTE ACCOUNTS (${accArr.length})</div>`;
      const accList = document.createElement("div");
      accList.className = "vault-acc-list";
      accArr.forEach((info) => {
        const item = document.createElement("div");
        item.className = "vault-acc-item";
        item.innerHTML = `<b>${info.domain || "-"}</b> — Login: <span>${info.email || info.username || "-"}</span> — Passwort: <code>••••••••</code>`;
        accList.appendChild(item);
      });
      accSec.appendChild(accList);
      wrap.appendChild(accSec);
    }

    container.appendChild(wrap);
  }

  function renderApkSettingsTab(container) {
    const wrap = document.createElement("div");
    wrap.className = "apk-settings-panel";
    wrap.style.cssText = "display:flex;flex-direction:column;gap:12px;padding:8px;font-family:var(--fm);";

    const title = document.createElement("div");
    title.style.cssText = "font-family:var(--fd);font-size:12px;color:var(--c-copper);letter-spacing:1px;font-weight:700;";
    title.textContent = "📱 JARVIS ANDROID MOBILE & CAMERA EDITION (.APK)";
    wrap.appendChild(title);

    const desc = document.createElement("div");
    desc.style.cssText = "font-size:11px;color:var(--c-muted);line-height:1.5;";
    desc.textContent = "Verbindet dein Android-Smartphone direkt mit dem JARVIS-Core im Heim-WLAN. Bietet AR-Kamera, Crosshair-Tracking, Live-Sprachchat, Taschenlampen-Steuerung und bidirektionalen Gedächtnisabgleich.";
    wrap.appendChild(desc);

    const card = document.createElement("div");
    card.className = "apk-card";

    const statusRow = document.createElement("div");
    statusRow.style.cssText = "display:flex;justify-content:space-between;align-items:center;font-size:11px;";
    statusRow.innerHTML = `<span>STATUS:</span><b id="ui-apk-status" style="color:var(--c-gold)">PRÜFE...</b>`;
    card.appendChild(statusRow);

    const btnRow = document.createElement("div");
    btnRow.style.cssText = "display:flex;gap:10px;flex-wrap:wrap;";

    const dlBtn = document.createElement("a");
    dlBtn.id = "ui-apk-dl-btn";
    dlBtn.href = "/download/jarvis.apk";
    dlBtn.download = "JARVIS_Mobile.apk";
    dlBtn.className = "apk-download-btn";
    dlBtn.style.cssText = "flex:1;min-width:200px;text-align:center;text-decoration:none;";
    dlBtn.textContent = "📲 ANDROID APK HERUNTERLADEN";
    btnRow.appendChild(dlBtn);

    const buildBtn = document.createElement("button");
    buildBtn.type = "button";
    buildBtn.className = "mini-btn";
    buildBtn.style.cssText = "padding:10px 14px;background:rgba(255,184,82,0.15);border:1px solid #ffb852;color:#ffb852;cursor:pointer;";
    buildBtn.textContent = "⚙ APK JETZT BAUEN";
    buildBtn.addEventListener("click", async () => {
      buildBtn.disabled = true;
      buildBtn.textContent = "⏳ BAU WIRD GESTARTET...";
      try {
        await postJson("/api/apk/build", {});
        setTimeout(checkStatus, 3000);
      } catch (_e) {}
    });
    btnRow.appendChild(buildBtn);
    card.appendChild(btnRow);

    const urlBox = document.createElement("div");
    urlBox.style.cssText = "background:rgba(8,10,14,0.9);border:1px dashed rgba(255,123,41,0.3);padding:8px 10px;font-size:10px;color:var(--c-copper);word-break:break-all;";
    urlBox.innerHTML = `LOKALER DOWNLOAD-LINK FÜRS SMARTPHONE:<br><b id="ui-apk-url" style="color:#fff">http://.../download/jarvis.apk</b>`;
    card.appendChild(urlBox);

    const hint = document.createElement("div");
    hint.style.cssText = "font-size:10px;color:var(--c-muted);line-height:1.4;";
    hint.innerHTML = "💡 <b>USB-Direktinstallation:</b> Wenn du dein Android-Smartphone per USB anschließt (mit aktiviertem USB-Debugging), installiert <code>4_Build_Android_APK.bat</code> die App vollautomatisch direkt auf dein Handy.";
    card.appendChild(hint);

    wrap.appendChild(card);

    // ------------------------------------------------------------
    // 1-KLICK QR-CODE-KOPPLUNG (Windows <-> Raspberry Pi <-> Handy)
    // ------------------------------------------------------------
    const qrCard = document.createElement("div");
    qrCard.className = "apk-card";
    qrCard.style.cssText = "display:flex;flex-direction:column;gap:10px;";

    const qrHead = document.createElement("div");
    qrHead.style.cssText = "display:flex;justify-content:space-between;align-items:center;font-size:11px;font-weight:700;color:var(--c-copper);letter-spacing:0.8px;";
    qrHead.innerHTML = `<span>📷 1-KLICK QR-CODE-KOPPLUNG (PC / PI / HANDY)</span><span id="ui-qr-host-badge" style="color:var(--c-gold)">LADE...</span>`;
    qrCard.appendChild(qrHead);

    const qrBody = document.createElement("div");
    qrBody.style.cssText = "display:flex;gap:14px;align-items:center;flex-wrap:wrap;";

    const qrImgWrap = document.createElement("div");
    qrImgWrap.style.cssText = "background:#ffffff;padding:8px;border-radius:6px;border:2px solid var(--c-copper);box-shadow:0 0 18px rgba(255,123,41,0.28);flex-shrink:0;";
    const qrImg = document.createElement("img");
    qrImg.id = "ui-pairing-qr-img";
    qrImg.alt = "JARVIS QR Pairing";
    qrImg.style.cssText = "display:block;width:168px;height:168px;image-rendering:pixelated;background:#fff;";
    qrImgWrap.appendChild(qrImg);
    qrBody.appendChild(qrImgWrap);

    const qrInfo = document.createElement("div");
    qrInfo.style.cssText = "flex:1;min-width:210px;display:flex;flex-direction:column;gap:6px;font-size:10.5px;color:var(--c-muted);";

    const qrExplain = document.createElement("div");
    qrExplain.style.cssText = "line-height:1.4;color:#e5e7eb;";
    qrExplain.textContent = "Öffne in der JARVIS Handy-App (⚙ Einstellungen → QR-CODE SCANNEN) die Kamera und halte sie auf diesen QR-Code. LAN-IP, Pi-URL, Sync-Token und deine KI-Keys werden sofort übertragen.";
    qrInfo.appendChild(qrExplain);

    const qrMeta = document.createElement("div");
    qrMeta.id = "ui-pairing-qr-meta";
    qrMeta.style.cssText = "background:rgba(8,10,14,0.9);border:1px solid rgba(255,123,41,0.25);padding:6px 8px;font-size:10px;line-height:1.5;word-break:break-all;";
    qrMeta.textContent = "Ermittle Netzwerkdaten & Keys...";
    qrInfo.appendChild(qrMeta);

    const piInputRow = document.createElement("div");
    piInputRow.style.cssText = "display:flex;gap:6px;align-items:center;flex-wrap:wrap;";
    const piInp = document.createElement("input");
    piInp.id = "ui-pairing-pi-url";
    piInp.type = "url";
    piInp.placeholder = "Optional: Pi-URL z. B. http://192.168.1.100:8000";
    piInp.style.cssText = "flex:1;min-width:160px;background:rgba(6,8,12,0.95);border:1px solid rgba(255,123,41,0.35);color:#fff;padding:5px 8px;font-family:var(--fm);font-size:10px;";
    piInputRow.appendChild(piInp);

    const refreshQrBtn = document.createElement("button");
    refreshQrBtn.type = "button";
    refreshQrBtn.className = "mini-btn";
    refreshQrBtn.textContent = "🔄 QR AKTUALISIEREN";
    refreshQrBtn.style.cssText = "padding:6px 10px;cursor:pointer;";
    piInputRow.appendChild(refreshQrBtn);

    const copyQrBtn = document.createElement("button");
    copyQrBtn.type = "button";
    copyQrBtn.className = "mini-btn";
    copyQrBtn.textContent = "📋 JSON KOPIEREN";
    copyQrBtn.style.cssText = "padding:6px 10px;cursor:pointer;";
    piInputRow.appendChild(copyQrBtn);

    qrInfo.appendChild(piInputRow);
    qrBody.appendChild(qrInfo);
    qrCard.appendChild(qrBody);
    wrap.appendChild(qrCard);
    container.appendChild(wrap);

    let lastQrJson = "";
    async function loadPairingQr(savePi = false) {
      try {
        const piVal = (piInp.value || "").trim();
        const res = savePi
          ? await postJson("/api/pairing_qr", { pi_url: piVal })
          : await fetch("/api/pairing_qr").then((r) => r.json());
        if (!res || !res.ok) return;
        lastQrJson = res.qr_json || JSON.stringify(res.qr_payload || {});
        if (res.qr_image_base64) {
          qrImg.src = res.qr_image_base64;
        }
        if (res.pi_url && !piInp.value) {
          piInp.value = res.pi_url;
        }
        const hostBadge = document.getElementById("ui-qr-host-badge");
        if (hostBadge) {
          hostBadge.textContent = String(res.host_type || "windows").toUpperCase();
        }
        const gOk = res.groq_key ? "✓" : "–";
        const gemOk = res.gemini_key ? "✓" : "–";
        qrMeta.innerHTML =
          `LAN: <b style="color:#fff">${res.lan_url || "-"}</b><br>` +
          `PI: <b style="color:#fff">${res.pi_url || "(nicht gesetzt)"}</b><br>` +
          `SYNC: <b style="color:var(--c-gold)">${res.sync_token || "-"}</b> | ` +
          `GROQ: <b>${gOk}</b> | GEMINI: <b>${gemOk}</b>`;
      } catch (_e) {
        qrMeta.textContent = "QR-Daten konnten nicht geladen werden.";
      }
    }
    refreshQrBtn.addEventListener("click", () => loadPairingQr(true));
    copyQrBtn.addEventListener("click", async () => {
      if (!lastQrJson) return;
      try {
        await navigator.clipboard.writeText(lastQrJson);
        copyQrBtn.textContent = "✓ KOPIERT";
        setTimeout(() => {
          copyQrBtn.textContent = "📋 JSON KOPIEREN";
        }, 1500);
      } catch (_e) {}
    });
    loadPairingQr(false);

    async function checkStatus() {
      try {
        const res = await fetch("/api/apk/status").then((r) => r.json());
        const stEl = document.getElementById("ui-apk-status");
        const urlEl = document.getElementById("ui-apk-url");
        if (urlEl && res.download_url) urlEl.textContent = res.download_url;
        if (stEl) {
          if (res.available) {
            stEl.style.color = "#22c55e";
            stEl.textContent = `🟢 BEREIT (${res.size_mb} MB)`;
          } else {
            stEl.style.color = "#ffb852";
            stEl.textContent = "⚪ NOCH NICHT GEBAUT (KLICK AUF 'APK JETZT BAUEN')";
          }
        }
      } catch (_e) {}
    }
    checkStatus();
  }

  function renderSettingsFields() {
    if (!el.settingsFields) return;
    el.settingsFields.innerHTML = "";

    if (S.settingsTab === "vault") {
      renderVaultSettingsTab(el.settingsFields);
      return;
    }

    if (S.settingsTab === "mobile") {
      renderApkSettingsTab(el.settingsFields);
      return;
    }

    if (S.settingsTab === "ai") {
      const cascadeContainer = document.createElement("div");
      cascadeContainer.className = "prio-cascade-wrap";

      const introLbl = document.createElement("div");
      introLbl.style.cssText = "font-family:var(--fm);font-size:10px;color:var(--c-copper);margin-bottom:4px;letter-spacing:1px;";
      introLbl.textContent = "⚡ PRIORITÄTS-KASKADE (AUTOMATISCHER FALLBACK #1 BIS #5):";
      cascadeContainer.appendChild(introLbl);

      S.modelPriority.forEach((provId, idx) => {
        const meta = PROVIDER_METAS[provId] || { name: provId, model: "", envKey: `${provId.toUpperCase()}_API_KEY`, secret: true, keyless: false };
        const card = document.createElement("div");
        card.className = `prio-card ${idx === 0 ? "active-primary" : ""}`;

        // Header: Rank + Nav buttons + Name + Model
        const head = document.createElement("div");
        head.className = "prio-card-head";

        const leftWrap = document.createElement("div");
        leftWrap.className = "prio-rank-wrap";

        const badge = document.createElement("span");
        badge.className = "prio-rank-badge";
        badge.textContent = `#${idx + 1}`;

        const navWrap = document.createElement("div");
        navWrap.className = "prio-btn-nav";

        const btnUp = document.createElement("button");
        btnUp.type = "button";
        btnUp.className = "prio-nav-btn";
        btnUp.textContent = "▲";
        btnUp.disabled = idx === 0;
        btnUp.title = "Priorität nach oben verschieben";
        btnUp.addEventListener("click", () => movePriority(idx, idx - 1));

        const btnDown = document.createElement("button");
        btnDown.type = "button";
        btnDown.className = "prio-nav-btn";
        btnDown.textContent = "▼";
        btnDown.disabled = idx === S.modelPriority.length - 1;
        btnDown.title = "Priorität nach unten verschieben";
        btnDown.addEventListener("click", () => movePriority(idx, idx + 1));

        navWrap.appendChild(btnUp);
        navWrap.appendChild(btnDown);

        const nameSpan = document.createElement("span");
        nameSpan.className = "prio-prov-name";
        nameSpan.textContent = meta.name;

        leftWrap.appendChild(badge);
        leftWrap.appendChild(navWrap);
        leftWrap.appendChild(nameSpan);

        const modelSpan = document.createElement("span");
        modelSpan.className = "prio-model-info";
        modelSpan.textContent = meta.model;

        head.appendChild(leftWrap);
        head.appendChild(modelSpan);
        card.appendChild(head);

        // Input row: Key Field + Verify Button + Status LED
        const inputRow = document.createElement("div");
        inputRow.className = "prio-input-row";

        const inputWrap = document.createElement("div");
        inputWrap.className = "prio-input-wrap";

        const keyInp = document.createElement("input");
        keyInp.className = "prio-key-input";
        keyInp.type = meta.secret ? "password" : "text";
        keyInp.placeholder = meta.keyless ? "Kostenlos ohne Key nutzbar (optional eigener Key)" : "API-Key einfügen...";
        keyInp.value = S.settingsDraft[meta.envKey] || "";
        keyInp.autocomplete = "off";
        keyInp.addEventListener("input", (e) => {
          S.settingsDraft[meta.envKey] = e.target.value;
        });
        inputWrap.appendChild(keyInp);

        const verifyBtn = document.createElement("button");
        verifyBtn.type = "button";
        verifyBtn.className = "prio-verify-btn";
        verifyBtn.textContent = "PRÜFEN";

        const statusBadge = document.createElement("span");
        const cached = S.providerVerifications[provId];
        if (cached) {
          statusBadge.className = `prio-status-badge ${cached.valid ? "valid" : "invalid"}`;
          statusBadge.textContent = cached.valid ? `🟢 GÜLTIG (${cached.latency}ms)` : `🔴 ${cached.detail || "FEHLER"}`;
        } else if (!keyInp.value && !meta.keyless) {
          statusBadge.className = "prio-status-badge";
          statusBadge.textContent = "⚪ LEER";
        } else if (meta.keyless) {
          statusBadge.className = "prio-status-badge valid";
          statusBadge.textContent = "🟢 BEREIT (KEYLESS)";
        } else {
          statusBadge.className = "prio-status-badge";
          statusBadge.textContent = "⚪ NICHT GEPRÜFT";
        }

        verifyBtn.addEventListener("click", () => {
          verifyKey(provId, S.settingsDraft[meta.envKey] || "", statusBadge, verifyBtn);
        });

        inputRow.appendChild(inputWrap);
        inputRow.appendChild(verifyBtn);
        inputRow.appendChild(statusBadge);

        card.appendChild(inputRow);
        cascadeContainer.appendChild(card);
      });

      el.settingsFields.appendChild(cascadeContainer);

      // Local Ollama Settings below the cascade
      const ollamaList = [
        { key: "OLLAMA_HOST", label: "Ollama Host URL (Standard: http://127.0.0.1:11434)", secret: false },
        { key: "OLLAMA_TEXT_MODEL", label: "Ollama Textmodell (Standard: qwen2.5:7b oder gemma3:4b)", secret: false },
        { key: "OLLAMA_VISION_MODEL", label: "Ollama Visionmodell (Standard: moondream)", secret: false },
      ];
      ollamaList.forEach(({ key, label, secret }) => {
        const row = document.createElement("div");
        row.className = "s-row";
        const lbl = document.createElement("label");
        lbl.textContent = label;
        const inp = document.createElement("input");
        inp.type = secret ? "password" : "text";
        inp.name = key;
        inp.value = S.settingsDraft[key] || "";
        inp.autocomplete = "off";
        inp.addEventListener("input", (e) => {
          S.settingsDraft[key] = e.target.value;
        });
        row.appendChild(lbl);
        row.appendChild(inp);
        el.settingsFields.appendChild(row);
      });
      return;
    }

    // Standard list for sys and comm tabs
    const list = SETTING_TABS[S.settingsTab] || [];
    list.forEach((item) => {
      const { key, label, secret, isToggle, desc } = item;
      if (isToggle) {
        const toggleRow = document.createElement("div");
        toggleRow.className = "s-toggle-row";

        const infoDiv = document.createElement("div");
        infoDiv.className = "s-toggle-info";

        const lbl = document.createElement("label");
        lbl.textContent = label;

        const sub = document.createElement("span");
        sub.className = "s-toggle-sub";
        sub.textContent = desc || "";

        infoDiv.appendChild(lbl);
        infoDiv.appendChild(sub);

        const sw = document.createElement("div");
        const isCurrentOn = S.settingsDraft[key] === "true" || (key === "AUTOSTART" && S.autostartEnabled);
        sw.className = `pill-switch ${isCurrentOn ? "on" : ""}`;
        sw.setAttribute("role", "switch");
        sw.setAttribute("aria-checked", String(isCurrentOn));
        sw.title = `${label} umschalten`;

        const knob = document.createElement("span");
        knob.className = "knob";
        sw.appendChild(knob);

        async function toggleAutostart() {
          const next = !sw.classList.contains("on");
          sw.classList.toggle("on", next);
          sw.setAttribute("aria-checked", String(next));
          S.settingsDraft[key] = next ? "true" : "false";
          if (key === "AUTOSTART") {
            S.autostartEnabled = next;
            try {
              const res = await postJson("/api/autostart", { enabled: next });
              if (res && res.enabled !== undefined) {
                sw.classList.toggle("on", res.enabled);
                sw.setAttribute("aria-checked", String(res.enabled));
                S.autostartEnabled = res.enabled;
                S.settingsDraft.AUTOSTART = res.enabled ? "true" : "false";
              }
            } catch (_e) {}
          }
        }

        sw.addEventListener("click", toggleAutostart);
        toggleRow.appendChild(infoDiv);
        toggleRow.appendChild(sw);
        el.settingsFields.appendChild(toggleRow);
        return;
      }

      const row = document.createElement("div");
      row.className = "s-row";
      const lbl = document.createElement("label");
      lbl.textContent = label;
      const inp = document.createElement("input");
      inp.type = secret ? "password" : "text";
      inp.name = key;
      inp.value = S.settingsDraft[key] || "";
      inp.autocomplete = "off";
      inp.addEventListener("input", (e) => {
        S.settingsDraft[key] = e.target.value;
      });
      row.appendChild(lbl);
      row.appendChild(inp);
      el.settingsFields.appendChild(row);
    });
  }

  if (el.settingsForm) {
    el.settingsForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const [resSettings, resVault] = await Promise.all([
          postJson("/api/settings", S.settingsDraft),
          postJson("/api/vault", S.vaultDraft || {}),
        ]);
        if (resVault && resVault.vault) {
          S.vaultDraft = { ...(resVault.vault.fields || resVault.vault), AUTONOMY_MODE: resVault.vault.AUTONOMY_MODE || resVault.vault.autonomy_mode || "mode_1" };
          S.vaultAccounts = resVault.vault.SAVED_ACCOUNTS_LIST || resVault.vault.saved_accounts || [];
        }
        if ((resSettings && resSettings.ok) || (resVault && resVault.ok)) {
          el.settingsFeedback.textContent = "✓ Einstellungen & AES-Tresor verschlüsselt gespeichert";
          setTimeout(closeSettings, 800);
        }
      } catch (_e) {
        el.settingsFeedback.textContent = "Fehler beim Speichern";
      }
    });
  }

  /* ============================================================
     HILFSFUNKTIONEN FÜR DAS RENDERING
  ============================================================ */
  // Lastfarbe: Akzent -> Gold (hoch) -> Rot (kritisch)
  function loadColor(v, alpha) {
    const f = clamp(v / 100, 0, 1);
    let r;
    let g;
    let b;
    if (f < 0.65) {
      r = PAL.a[0]; g = PAL.a[1]; b = PAL.a[2];
    } else if (f < 0.85) {
      const k = (f - 0.65) / 0.2;
      r = lerp(PAL.a[0], 255, k); g = lerp(PAL.a[1], 176, k); b = lerp(PAL.a[2], 46, k);
    } else {
      const k = (f - 0.85) / 0.15;
      r = lerp(255, 255, k); g = lerp(176, 64, k); b = lerp(46, 72, k);
    }
    return `rgba(${Math.round(r)},${Math.round(g)},${Math.round(b)},${alpha})`;
  }

  // Ring-Stroke mit Glow (zwei Durchgänge, deutlich günstiger als shadowBlur)
  function strokeGlow(ctx, path, color, lw, glowColor, glowMul) {
    if (PERF.q >= 1 && glowColor) {
      ctx.strokeStyle = glowColor;
      ctx.lineWidth = lw * (glowMul || 3);
      ctx.beginPath();
      path();
      ctx.stroke();
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = lw;
    ctx.beginPath();
    path();
    ctx.stroke();
  }

  function dashedRing(ctx, cx, cy, r, lw, pattern, angle, color) {
    const circ = TAU * r;
    // Muster so skalieren, dass es exakt ganzzahlig um den Kreis passt (kein "Naht"-Sprung)
    const sum = pattern.reduce((a, b) => a + b, 0);
    const reps = Math.max(1, Math.round(circ / sum));
    const k = circ / (reps * sum);
    ctx.save();
    ctx.setLineDash(pattern.map((p) => p * k));
    ctx.lineDashOffset = -angle * r;
    ctx.strokeStyle = color;
    ctx.lineWidth = lw;
    ctx.lineCap = "butt";
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, TAU);
    ctx.stroke();
    ctx.restore();
  }

  function tickRing(ctx, cx, cy, r, count, rot, lenMinor, lenMajor, majorEvery, colorMinor, colorMajor, lwMinor, lwMajor) {
    ctx.beginPath();
    for (let i = 0; i < count; i++) {
      if (i % majorEvery === 0) continue;
      const a = (i / count) * TAU + rot;
      const c = Math.cos(a);
      const s = Math.sin(a);
      ctx.moveTo(cx + c * (r - lenMinor), cy + s * (r - lenMinor));
      ctx.lineTo(cx + c * r, cy + s * r);
    }
    ctx.strokeStyle = colorMinor;
    ctx.lineWidth = lwMinor;
    ctx.stroke();
    ctx.beginPath();
    for (let i = 0; i < count; i += majorEvery) {
      const a = (i / count) * TAU + rot;
      const c = Math.cos(a);
      const s = Math.sin(a);
      ctx.moveTo(cx + c * (r - lenMajor), cy + s * (r - lenMajor));
      ctx.lineTo(cx + c * r, cy + s * r);
    }
    ctx.strokeStyle = colorMajor;
    ctx.lineWidth = lwMajor;
    ctx.stroke();
  }

  function hexGrid(ctx, w, h, t) {
    if (PERF.q < 1) return;
    const s = clamp(Math.min(w, h) * 0.06, 16, 34);
    const dx = s * 1.5;
    const dy = Math.sqrt(3) * s;
    ctx.beginPath();
    for (let col = -1; col * dx < w + s; col++) {
      const x = col * dx;
      const off = (col & 1) ? dy / 2 : 0;
      for (let y = -dy + off; y < h + dy; y += dy) {
        for (let k = 0; k < 4; k++) {
          const a = (Math.PI / 3) * k;
          const px = x + Math.cos(a) * s;
          const py = y + Math.sin(a) * s;
          if (k === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
        }
      }
    }
    ctx.strokeStyle = ca(0.05 + 0.012 * Math.sin(t * 0.8));
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  /* ============================================================
     ZEICHNEN: OSZILLOSKOP-AUDIO-WELLE (cv-wave)
  ============================================================ */
  function drawWaveform(t) {
    if (!cvWave) return;
    const ctx = begin(cvWave);
    if (!ctx) return;
    const w = cvWave.w;
    const h = cvWave.h;
    const mid = h / 2;

    // Oszilloskop-Gitter
    ctx.lineWidth = 1;
    ctx.strokeStyle = ca(0.07);
    ctx.beginPath();
    for (let x = 0.5; x < w; x += 14) { ctx.moveTo(x, 0); ctx.lineTo(x, h); }
    for (let y = 0.5; y < h; y += 11) { ctx.moveTo(0, y); ctx.lineTo(w, y); }
    ctx.stroke();

    // Mittellinie mit Fade-Out an den Rändern
    const lg = ctx.createLinearGradient(0, 0, w, 0);
    lg.addColorStop(0, ca(0));
    lg.addColorStop(0.5, ca(0.45));
    lg.addColorStop(1, ca(0));
    ctx.strokeStyle = lg;
    ctx.beginPath();
    ctx.moveTo(0, mid);
    ctx.lineTo(w, mid);
    ctx.stroke();

    const amp = 1.5 + AUD.raw * h * 0.46;
    const n = Math.max(24, Math.floor(w / 2));
    const grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, cb(0));
    grad.addColorStop(0.18, cb(0.95));
    grad.addColorStop(0.82, cb(0.95));
    grad.addColorStop(1, cb(0));

    const trace = (phase, scale) => {
      ctx.beginPath();
      for (let i = 0; i <= n; i++) {
        const u = i / n;
        const env = Math.pow(Math.sin(Math.PI * u), 0.85);
        const y = mid + amp * scale * env * (
          Math.sin(u * 15 + t * 9 + phase) * 0.55 +
          Math.sin(u * 33 - t * 14 + phase * 1.7) * 0.3 +
          Math.sin(u * 71 + t * 23 - phase) * 0.15 * clamp(AUD.raw * 2, 0.2, 1)
        );
        const x = u * w;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    };

    if (PERF.q >= 1) {
      ctx.strokeStyle = ca(0.14);
      ctx.lineWidth = 5;
      trace(0, 1);
    }
    ctx.strokeStyle = ca(0.3);
    ctx.lineWidth = 1;
    trace(1.4, 0.72);
    ctx.strokeStyle = grad;
    ctx.lineWidth = 1.7;
    trace(0, 1);
  }

  /* ============================================================
     ZEICHNEN: FREQUENZ-SPEKTRUM (cv-spectrum) – LED-Segmente
  ============================================================ */
  function drawSpectrum() {
    if (!cvSpectrum) return;
    const ctx = begin(cvSpectrum);
    if (!ctx) return;
    const w = cvSpectrum.w;
    const h = cvSpectrum.h;
    const n = Math.max(12, Math.min(40, Math.floor(w / 9)));
    const gap = 2.5;
    const bw = (w - gap * (n - 1)) / n;
    const segH = 3;
    const segGap = 1.6;
    const maxSeg = Math.max(3, Math.floor(h / (segH + segGap)));

    for (let i = 0; i < n; i++) {
      const f = (i / (n - 1)) * (NBAND - 1);
      const i0 = Math.floor(f);
      const i1 = Math.min(NBAND - 1, i0 + 1);
      const v = lerp(AUD.B[i0], AUD.B[i1], f - i0);
      const pk = lerp(AUD.P[i0], AUD.P[i1], f - i0);
      const lit = Math.round(clamp(v, 0, 1) * maxSeg);
      const x = i * (bw + gap);
      for (let s = 0; s < maxSeg; s++) {
        const y = h - (s + 1) * (segH + segGap) + segGap;
        if (s < lit) {
          const k = s / maxSeg;
          ctx.fillStyle = k > 0.8 ? cb(1) : mixRgba(PAL.a, PAL.b, k, 0.55 + 0.45 * k);
        } else {
          ctx.fillStyle = ca(0.07);
        }
        ctx.fillRect(x, y, bw, segH);
      }
      const ps = Math.min(maxSeg - 1, Math.round(clamp(pk, 0, 1) * maxSeg));
      if (ps > lit) {
        ctx.fillStyle = cb(0.95);
        ctx.fillRect(x, h - (ps + 1) * (segH + segGap) + segGap, bw, segH);
      }
    }
  }

  function mixRgba(a, b, k, alpha) {
    return `rgba(${Math.round(lerp(a[0], b[0], k))},${Math.round(lerp(a[1], b[1], k))},${Math.round(lerp(a[2], b[2], k))},${alpha})`;
  }

  /* ============================================================
     ZEICHNEN: GROSSES CPU-GAUGE (cv-gauge-cpu) – 270°-Instrument
  ============================================================ */
  const GA0 = Math.PI * 0.75;
  const GSWEEP = Math.PI * 1.5;

  function drawArcGauge(cv, value, label, t, big) {
    if (!cv) return;
    const ctx = begin(cv);
    if (!ctx) return;
    const w = cv.w;
    const h = cv.h;
    const cx = w / 2;
    const cy = h / 2;
    const R = Math.min(w, h) * (big ? 0.46 : 0.46);
    if (R < 16) return;
    const frac = clamp(value / 100, 0, 1);
    const lw = Math.max(3, R * (big ? 0.11 : 0.15));
    const rArc = R * 0.80;

    // Skalenring mit Ticks
    const ticks = big ? 54 : 36;
    ctx.beginPath();
    for (let i = 0; i <= ticks; i++) {
      const a = GA0 + (i / ticks) * GSWEEP;
      const major = i % (big ? 6 : 6) === 0;
      const r0 = R - (major ? R * 0.1 : R * 0.055);
      ctx.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
      ctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
    }
    ctx.strokeStyle = ca(0.38);
    ctx.lineWidth = 1;
    ctx.stroke();

    // Spur
    ctx.strokeStyle = ca(0.13);
    ctx.lineWidth = lw;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.arc(cx, cy, rArc, GA0, GA0 + GSWEEP);
    ctx.stroke();

    // Wert-Bogen
    const aEnd = GA0 + GSWEEP * Math.max(0.004, frac);
    strokeGlow(ctx, () => ctx.arc(cx, cy, rArc, GA0, aEnd), loadColor(value, 0.98), lw, loadColor(value, 0.16), 1.9);

    // Leuchtpunkt an der Spitze
    const px = cx + Math.cos(aEnd) * rArc;
    const py = cy + Math.sin(aEnd) * rArc;
    ctx.fillStyle = "rgba(255,255,255,0.95)";
    ctx.beginPath();
    ctx.arc(px, py, Math.max(1.6, lw * 0.28), 0, TAU);
    ctx.fill();

    // Innerer rotierender Tech-Ring
    dashedRing(ctx, cx, cy, R * 0.60, 1, [R * 0.05, R * 0.045], t * 0.5, ca(0.35));
    ctx.strokeStyle = ca(0.2);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(cx, cy, R * 0.52, 0, TAU);
    ctx.stroke();

    // Text
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#ffffff";
    ctx.font = `800 ${Math.round(R * (big ? 0.40 : 0.40))}px ${FD}`;
    ctx.fillText(`${Math.round(value)}`, cx - R * 0.04, cy - R * 0.03);
    ctx.fillStyle = cb(0.95);
    ctx.font = `700 ${Math.round(R * 0.16)}px ${FM}`;
    ctx.textAlign = "left";
    const tw = ctx.measureText("%").width;
    ctx.fillText("%", cx + R * 0.30, cy - R * 0.10);
    ctx.textAlign = "center";
    ctx.fillStyle = ca(0.9);
    ctx.font = `700 ${Math.round(R * (big ? 0.15 : 0.17))}px ${FD}`;
    ctx.fillText(label, cx, cy + R * 0.42);
    void tw;
  }

  function drawBigCpuGauge(value, t) { drawArcGauge(cvGCpu, value, "CPU LOAD", t, true); }
  function drawSubGauge(value, t) { drawArcGauge(cvGRam, value, "RAM", t, false); }

  /* ============================================================
     ZEICHNEN: SPARKLINE (RAM-Verlauf) – geglättete Fläche
  ============================================================ */
  function drawRamSparkline(t) {
    if (!cvChRam) return;
    const ctx = begin(cvChRam);
    if (!ctx) return;
    const w = cvChRam.w;
    const h = cvChRam.h;
    const data = S.hist.ram;
    if (data.length < 2) return;

    ctx.strokeStyle = ca(0.1);
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 1; i < 4; i++) {
      const y = Math.round((h / 4) * i) + 0.5;
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
    }
    ctx.stroke();

    const pts = data.map((v, i) => [(i / (data.length - 1)) * w, h - 2 - (clamp(v, 0, 100) / 100) * (h - 5)]);
    const curve = () => {
      ctx.moveTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length - 1; i++) {
        const mx = (pts[i][0] + pts[i + 1][0]) / 2;
        const my = (pts[i][1] + pts[i + 1][1]) / 2;
        ctx.quadraticCurveTo(pts[i][0], pts[i][1], mx, my);
      }
      const last = pts[pts.length - 1];
      ctx.lineTo(last[0], last[1]);
    };

    const fill = ctx.createLinearGradient(0, 0, 0, h);
    fill.addColorStop(0, ca(0.38));
    fill.addColorStop(1, ca(0));
    ctx.beginPath();
    curve();
    ctx.lineTo(w, h);
    ctx.lineTo(0, h);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();

    strokeGlow(ctx, curve, cb(0.98), 1.5, ca(0.18), 3.2);

    const last = pts[pts.length - 1];
    const pulse = 0.5 + 0.5 * Math.sin(t * 4);
    ctx.fillStyle = ca(0.25 + 0.25 * pulse);
    ctx.beginPath();
    ctx.arc(last[0] - 2, last[1], 3 + pulse * 2, 0, TAU);
    ctx.fill();
    ctx.fillStyle = "#fff";
    ctx.beginPath();
    ctx.arc(last[0] - 2, last[1], 1.6, 0, TAU);
    ctx.fill();
  }

  /* ============================================================
     ZEICHNEN: 30-s-LINEAL (cv-ruler) mit Puffer-Füllstand
  ============================================================ */
  function drawRuler() {
    if (!cvRuler) return;
    const ctx = begin(cvRuler);
    if (!ctx) return;
    const x0 = 8;
    const x1 = cvRuler.w - 8;
    const baseY = cvRuler.h - 10;
    const span = x1 - x0;

    const fillFrac = clamp(bufferSeconds() / BUFFER_SECONDS, 0, 1);
    const g = ctx.createLinearGradient(x0, 0, x1, 0);
    g.addColorStop(0, ca(0.05));
    g.addColorStop(1, ca(0.5));
    ctx.fillStyle = g;
    ctx.fillRect(x0, baseY - 3, span * fillFrac, 2);

    ctx.strokeStyle = ca(0.45);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x0, baseY + 0.5);
    ctx.lineTo(x1, baseY + 0.5);
    for (let s = 0; s <= BUFFER_SECONDS; s++) {
      const x = Math.round(x0 + (s / BUFFER_SECONDS) * span) + 0.5;
      const len = s % 10 === 0 ? 6 : (s % 5 === 0 ? 4 : 2);
      ctx.moveTo(x, baseY);
      ctx.lineTo(x, baseY + len);
    }
    ctx.stroke();

    ctx.fillStyle = "#8fa6b8";
    ctx.font = `8px ${FM}`;
    ctx.textBaseline = "alphabetic";
    [0, 10, 20, 30].forEach((s) => {
      ctx.textAlign = s === 0 ? "left" : (s === 30 ? "right" : "center");
      ctx.fillText(`${s}s`, x0 + (s / BUFFER_SECONDS) * span, cvRuler.h - 1);
    });
  }

  /* ============================================================
     ZEICHNEN: JARVIS-CORE (cv-core) – 7 unabhängige Layer
     Alle Radien: R = min(width, height) * Faktor, Mittelpunkt exakt
     (width / 2, height / 2) -> immer perfekt rund, nie oval.
  ============================================================ */
  const SPH_N = 460;
  const SPH = [];
  for (let i = 0; i < SPH_N; i++) {
    const y = 1 - ((i + 0.5) * 2) / SPH_N;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const a = i * GOLDEN_ANGLE;
    SPH.push({ x: Math.cos(a) * r, y, z: Math.sin(a) * r });
  }
  const PX = new Float32Array(SPH_N);
  const PY = new Float32Array(SPH_N);
  const PZ = new Float32Array(SPH_N);
  const HAS_CONIC = typeof CanvasRenderingContext2D !== "undefined" && "createConicGradient" in CanvasRenderingContext2D.prototype;
  const CARD = ["N", "E", "S", "W"];

  function drawCore(dt, t) {
    if (!cvCore) return;
    const ctx = begin(cvCore);
    if (!ctx) return;
    const w = cvCore.w;
    const h = cvCore.h;
    const cx = w / 2;
    const cy = h / 2;
    const R = Math.min(w, h) * 0.455;
    if (R < 60) return;

    const eng = PAL.key === "OFFLINE" ? "OFFLINE" : S.statusEng;
    const aud = AUD.raw;

    // Rotationsgeschwindigkeit mit weichem Ease-In/Ease-Out je Zustand
    const spTarget = STATE_SPEED[PAL.key] !== undefined ? STATE_SPEED[PAL.key] : (STATE_SPEED[eng] || 0.9);
    core.speed += (spTarget - core.speed) * (1 - Math.exp(-dt * 2.2));
    const sp = core.speed;
    const breathe = (ph) => 0.35 + 0.65 * (0.5 - 0.5 * Math.cos(t * 0.7 + ph)); // 35 %..100 % Ease-In/Out
    core.compass += dt * 0.09 * sp;
    core.segA -= dt * 0.62 * sp * breathe(0);
    core.segB += dt * 0.95 * sp * breathe(2.1);
    core.arc += dt * 0.40 * sp * breathe(4.2);
    core.inner -= dt * 1.15 * sp * breathe(1.0);
    core.scale += dt * 0.30 * sp;
    core.sweep += dt * 0.9 * sp;
    core.ay += dt * 0.42 * sp;
    core.ax = 0.28 + Math.sin(t * 0.3) * 0.25;
    core.pulse = 0.5 + 0.5 * Math.sin(t * (1.6 + sp * 0.6));

    // --- Layer 0: Hex-Grid + Ambient-Glow ---
    hexGrid(ctx, w, h, t);
    const amb = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 1.12);
    amb.addColorStop(0, ca(0.20 + 0.08 * core.pulse + aud * 0.12));
    amb.addColorStop(0.55, ca(0.06));
    amb.addColorStop(1, ca(0));
    ctx.fillStyle = amb;
    ctx.beginPath();
    ctx.arc(cx, cy, R * 1.12, 0, TAU);
    ctx.fill();

    // Radar-Sweep
    if (HAS_CONIC && PERF.q >= 1) {
      const sg = ctx.createConicGradient(core.sweep, cx, cy);
      sg.addColorStop(0, ca(0));
      sg.addColorStop(0.80, ca(0));
      sg.addColorStop(1, ca(0.20));
      ctx.fillStyle = sg;
      ctx.beginPath();
      ctx.arc(cx, cy, R * 0.80, 0, TAU);
      ctx.fill();
      ctx.strokeStyle = ca(0.5);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(core.sweep + TAU) * R * 0.80, cy + Math.sin(core.sweep + TAU) * R * 0.80);
      ctx.stroke();
    }

    // --- Layer 1: Äußerer Kompass-/Telemetrie-Ring ---
    ctx.strokeStyle = ca(0.62);
    ctx.lineWidth = 1.3;
    ctx.beginPath();
    ctx.arc(cx, cy, R, 0, TAU);
    ctx.stroke();
    ctx.strokeStyle = ca(0.22);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(cx, cy, R * 0.945, 0, TAU);
    ctx.stroke();
    tickRing(ctx, cx, cy, R, 180, core.compass, R * 0.013, R * 0.04, 15, ca(0.4), cb(0.95), 1, 1.6);

    // Grad-Beschriftung (rotiert mit dem Ring)
    const fs = Math.max(8, Math.round(R * 0.042));
    ctx.font = `600 ${fs}px ${FM}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (let i = 0; i < 12; i++) {
      const a = (i / 12) * TAU + core.compass;
      const rr = R * 0.895;
      const txt = i % 3 === 0 ? CARD[i / 3] : String(i * 30);
      ctx.save();
      ctx.translate(cx + Math.cos(a - Math.PI / 2) * rr, cy + Math.sin(a - Math.PI / 2) * rr);
      ctx.rotate(a);
      ctx.fillStyle = i % 3 === 0 ? cb(1) : ca(0.62);
      ctx.fillText(txt, 0, 0);
      ctx.restore();
    }

    // Feste Azimut-Markierung oben + Kurs-Anzeige
    ctx.fillStyle = cb(1);
    ctx.beginPath();
    ctx.moveTo(cx, cy - R * 1.0 + 1);
    ctx.lineTo(cx - R * 0.025, cy - R * 1.06);
    ctx.lineTo(cx + R * 0.025, cy - R * 1.06);
    ctx.closePath();
    ctx.fill();
    const hdg = (((-core.compass * 180) / Math.PI) % 360 + 360) % 360;
    ctx.font = `700 ${Math.max(8, Math.round(R * 0.04))}px ${FM}`;
    ctx.fillStyle = cb(0.9);
    ctx.fillText(`HDG ${hdg.toFixed(1).padStart(5, "0")}°`, cx, cy - R * 1.095);

    // Dynamische Winkel-Indikatoren (laufen unabhängig auf dem Ring)
    const markers = [
      { a: t * 0.55, c: cb(1), lab: true },
      { a: -t * 0.83 + 2.1, c: ca(0.95), lab: true },
      { a: t * 0.31 + 4.2, c: cb(0.75), lab: false },
    ];
    for (const m of markers) {
      const aa = m.a - Math.PI / 2;
      const w0 = 0.05;
      ctx.strokeStyle = m.c;
      ctx.lineWidth = Math.max(2, R * 0.016);
      ctx.beginPath();
      ctx.arc(cx, cy, R, aa - w0, aa + w0);
      ctx.stroke();
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(aa) * R * 0.93, cy + Math.sin(aa) * R * 0.93);
      ctx.lineTo(cx + Math.cos(aa) * R * 1.0, cy + Math.sin(aa) * R * 1.0);
      ctx.stroke();
      if (m.lab) {
        const deg = ((m.a * 180) / Math.PI % 360 + 360) % 360;
        ctx.font = `600 ${Math.max(7, Math.round(R * 0.036))}px ${FM}`;
        ctx.fillStyle = m.c;
        ctx.fillText(`${Math.round(deg)}°`, cx + Math.cos(aa) * R * 1.085, cy + Math.sin(aa) * R * 1.085);
      }
    }

    // --- Layer 2: Gegenläufige gestrichelte Segment-Ringe ---
    dashedRing(ctx, cx, cy, R * 0.835, Math.max(3, R * 0.026), [R * 0.30, R * 0.06, R * 0.10, R * 0.06], core.segA, ca(0.88));
    dashedRing(ctx, cx, cy, R * 0.775, Math.max(1, R * 0.008), [R * 0.05, R * 0.032], core.segB, cb(0.6));

    // --- Layer 3: Arc-Reaktor-Bögen (mit Glow) ---
    const rArc = R * 0.725;
    ctx.strokeStyle = ca(0.2);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(cx, cy, rArc, 0, TAU);
    ctx.stroke();
    for (let k = 0; k < 3; k++) {
      const a0 = core.arc + (k * TAU) / 3;
      const len = 0.85 + 0.25 * Math.sin(t * 0.9 + k * 2);
      strokeGlow(ctx, () => ctx.arc(cx, cy, rArc, a0, a0 + len), cb(0.98), Math.max(3, R * 0.03), ca(0.16), 2.6);
    }
    // Chevron-Marker (rotieren mit dem Arc-Layer)
    ctx.fillStyle = ca(0.9);
    for (let k = 0; k < 6; k++) {
      const a = -core.arc * 0.7 + (k * TAU) / 6;
      const r1 = rArc - R * 0.045;
      const r2 = rArc - R * 0.085;
      const aw = 0.035;
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
      ctx.lineTo(cx + Math.cos(a - aw) * r2, cy + Math.sin(a - aw) * r2);
      ctx.lineTo(cx + Math.cos(a + aw) * r2, cy + Math.sin(a + aw) * r2);
      ctx.closePath();
      ctx.fill();
    }

    // --- Layer 4: Reaktiver Audio-Visualizer-Ring ---
    const baseR = R * 0.545;
    const maxLen = R * 0.125;
    const bars = PERF.q >= 1 ? 96 : 64;
    const half = bars / 2;
    ctx.lineCap = "round";
    const barW = Math.max(1.4, (TAU * baseR / bars) * 0.5);
    ctx.lineWidth = barW;
    for (let pass = 0; pass < 2; pass++) {
      ctx.beginPath();
      for (let i = 0; i < bars; i++) {
        const j = i <= half ? i : bars - i;
        const bi = Math.min(NBAND - 1, Math.round((j / half) * (NBAND - 1)));
        const v = AUD.B[bi];
        const a = (i / bars) * TAU - Math.PI / 2 + core.scale * 0.15;
        const len = R * 0.012 + v * maxLen;
        const r0 = baseR;
        const r1 = baseR + len;
        const c = Math.cos(a);
        const s = Math.sin(a);
        if (pass === 0 ? v < 0.45 : v >= 0.45) {
          ctx.moveTo(cx + c * r0, cy + s * r0);
          ctx.lineTo(cx + c * r1, cy + s * r1);
        }
      }
      ctx.strokeStyle = pass === 0 ? ca(0.82) : cb(1);
      ctx.stroke();
    }
    // Peak-Punkte
    ctx.fillStyle = cb(0.9);
    ctx.beginPath();
    for (let i = 0; i < bars; i += 2) {
      const j = i <= half ? i : bars - i;
      const bi = Math.min(NBAND - 1, Math.round((j / half) * (NBAND - 1)));
      const a = (i / bars) * TAU - Math.PI / 2 + core.scale * 0.15;
      const rp = baseR + R * 0.012 + AUD.P[bi] * maxLen + R * 0.022;
      const px = cx + Math.cos(a) * rp;
      const py = cy + Math.sin(a) * rp;
      ctx.moveTo(px + 1.1, py);
      ctx.arc(px, py, 1.1, 0, TAU);
    }
    ctx.fill();

    // Wellenform-Ring (innerhalb der Balken)
    const rw0 = baseR * 0.935;
    const wAmp = R * 0.006 + aud * R * 0.05;
    const wavePath = () => {
      const steps = PERF.q >= 1 ? 200 : 120;
      for (let i = 0; i <= steps; i++) {
        const th = (i / steps) * TAU;
        const rr = rw0 + wAmp * (
          Math.sin(th * 3 + t * 3.1) + 0.6 * Math.sin(th * 7 - t * 4.3) + 0.35 * Math.sin(th * 13 + t * 6.1)
        );
        const x = cx + Math.cos(th - Math.PI / 2) * rr;
        const y = cy + Math.sin(th - Math.PI / 2) * rr;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
    };
    strokeGlow(ctx, wavePath, cb(0.95), 1.5, ca(0.14), 4);

    // --- Layer 5: Innerer Tech-Ring + Datenknoten ---
    dashedRing(ctx, cx, cy, R * 0.455, 1.2, [R * 0.02, R * 0.045], core.inner, ca(0.6));
    tickRing(ctx, cx, cy, R * 0.415, 72, core.scale, R * 0.016, R * 0.03, 6, ca(0.28), ca(0.75), 1, 1.3);
    ctx.fillStyle = cb(0.95);
    for (let k = 0; k < 5; k++) {
      const a = core.inner * 0.6 + (k * TAU) / 5;
      const px = cx + Math.cos(a) * R * 0.455;
      const py = cy + Math.sin(a) * R * 0.455;
      const sq = Math.max(2, R * 0.014);
      ctx.save();
      ctx.translate(px, py);
      ctx.rotate(a + Math.PI / 4);
      ctx.fillRect(-sq, -sq, sq * 2, sq * 2);
      ctx.restore();
    }

    // --- Layer 6: Energie-Kern ---
    const rc = R * 0.30 * (1 + 0.05 * core.pulse + aud * 0.2);
    const orb = ctx.createRadialGradient(cx, cy, 0, cx, cy, rc * 1.35);
    orb.addColorStop(0, "rgba(255,255,255,0.88)");
    orb.addColorStop(0.18, cb(0.85));
    orb.addColorStop(0.5, ca(0.42));
    orb.addColorStop(0.82, ca(0.12));
    orb.addColorStop(1, ca(0));
    ctx.fillStyle = orb;
    ctx.beginPath();
    ctx.arc(cx, cy, rc * 1.35, 0, TAU);
    ctx.fill();

    // Partikel-Kugel (Fibonacci-Sphäre, 3D)
    const Rs = R * 0.255 * (1 + aud * 0.22);
    const cosY = Math.cos(core.ay);
    const sinY = Math.sin(core.ay);
    const cosX = Math.cos(core.ax);
    const sinX = Math.sin(core.ax);
    const step = PERF.q >= 1 ? 1 : 2;
    for (let i = 0; i < SPH_N; i += step) {
      const p = SPH[i];
      const x1 = p.x * cosY + p.z * sinY;
      const z1 = -p.x * sinY + p.z * cosY;
      const y2 = p.y * cosX - z1 * sinX;
      const z2 = p.y * sinX + z1 * cosX;
      const persp = 1 / (1 - z2 * 0.2);
      PX[i] = cx + x1 * Rs * persp;
      PY[i] = cy + y2 * Rs * persp;
      PZ[i] = z2;
    }
    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    for (let i = 0; i < SPH_N; i += step) {
      const z = PZ[i];
      if (z < -0.25) continue;
      const rad = 0.8 + (z + 0.25) * 0.85;
      ctx.fillStyle = z > 0.45 ? cb(0.95) : ca(0.62);
      ctx.beginPath();
      ctx.arc(PX[i], PY[i], rad, 0, TAU);
      ctx.fill();
    }
    ctx.restore();

    // Rotierendes Hexagon um den Kern
    ctx.strokeStyle = cb(0.5);
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    for (let k = 0; k <= 6; k++) {
      const a = core.scale * 1.6 + (k * TAU) / 6;
      const px = cx + Math.cos(a) * R * 0.345;
      const py = cy + Math.sin(a) * R * 0.345;
      if (k === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();

    // Zustandstext im Kern
    let label = "STANDBY";
    if (eng === "LISTENING") label = "LISTENING";
    else if (eng === "SPEAKING") label = "SPEAKING";
    else if (eng === "THINKING") label = "THINKING";
    else if (eng === "PROCESSING") label = "PROCESSING";
    else if (eng === "OPTICS") label = "OPTICS";
    else if (eng === "ACTING") label = "ACTING";
    else if (eng === "ALERT") label = "ALERT";
    else if (eng === "OFFLINE") label = "OFFLINE";
    else if (eng === "READY") label = "READY";

    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.save();
    if (PERF.q >= 1) {
      ctx.shadowColor = `rgb(${PAL.rgb})`;
      ctx.shadowBlur = 14;
    }
    ctx.fillStyle = "#ffffff";
    ctx.font = `800 ${Math.max(10, Math.round(R * 0.092))}px ${FD}`;
    ctx.fillText(label, cx, cy - R * 0.012);
    ctx.restore();
    ctx.fillStyle = cb(0.85);
    ctx.font = `600 ${Math.max(7, Math.round(R * 0.04))}px ${FM}`;
    ctx.fillText(S.statusSub || "STANDBY", cx, cy + R * 0.075);
  }

  /* ============================================================
     SYSTEM-NETZWERK-OVERLAY
  ============================================================ */
  function drawNet(t) {
    if (!cvNet) return;
    const ctx = begin(cvNet);
    if (!ctx) return;
    const w = cvNet.w;
    const h = cvNet.h;
    const u = Math.min(w, h);
    const hr = clamp(u * 0.085, 22, 40);

    const hubs = [
      { label: "GEHIRN", sub: S.isOnline ? "CLOUD + OLLAMA" : "OLLAMA LOKAL", x: w * 0.5, y: h * 0.3, col: PAL.a },
      { label: "HAND", sub: "MAUS · APPS · WEB", x: w * 0.75, y: h * 0.7, col: PAL.b },
      { label: "STIMME", sub: "30s PUFFER · STT · TTS", x: w * 0.25, y: h * 0.7, col: [120, 200, 255] },
    ];

    const links = [[0, 1], [1, 2], [2, 0]];
    links.forEach(([i, j], li) => {
      const A = hubs[i];
      const B = hubs[j];
      ctx.save();
      ctx.setLineDash([6, 6]);
      ctx.lineDashOffset = -t * 30;
      ctx.strokeStyle = ca(0.4);
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.moveTo(A.x, A.y);
      ctx.lineTo(B.x, B.y);
      ctx.stroke();
      ctx.restore();
      // Datenpaket entlang der Verbindung
      const k = (t * 0.35 + li * 0.33) % 1;
      const px = lerp(A.x, B.x, k);
      const py = lerp(A.y, B.y, k);
      ctx.fillStyle = cb(0.95);
      ctx.beginPath();
      ctx.arc(px, py, 3, 0, TAU);
      ctx.fill();
    });

    hubs.forEach((hub, idx) => {
      const c = `${Math.round(hub.col[0])},${Math.round(hub.col[1])},${Math.round(hub.col[2])}`;
      const g = ctx.createRadialGradient(hub.x, hub.y, 0, hub.x, hub.y, hr * 1.9);
      g.addColorStop(0, `rgba(${c},0.28)`);
      g.addColorStop(1, `rgba(${c},0)`);
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(hub.x, hub.y, hr * 1.9, 0, TAU);
      ctx.fill();

      ctx.fillStyle = "#070b11";
      ctx.strokeStyle = `rgba(${c},0.95)`;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(hub.x, hub.y, hr, 0, TAU);
      ctx.fill();
      ctx.stroke();

      ctx.strokeStyle = `rgba(${c},0.8)`;
      ctx.lineWidth = 2.4;
      const a0 = t * (idx % 2 ? -1.2 : 1.2) + idx;
      ctx.beginPath();
      ctx.arc(hub.x, hub.y, hr * 1.22, a0, a0 + 1.4);
      ctx.stroke();

      ctx.fillStyle = "#fff";
      ctx.font = `700 ${Math.round(hr * 0.38)}px ${FD}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(hub.label, hub.x, hub.y);
      ctx.fillStyle = `rgba(${c},1)`;
      ctx.font = `${Math.round(hr * 0.3)}px ${FM}`;
      ctx.fillText(hub.sub, hub.x, hub.y + hr * 1.55);
    });
  }

  /* ============================================================
     HAUPTSCHLEIFE (60 FPS) – vollständig vom Backend entkoppelt
     WebSocket-Nachrichten schreiben nur in den Zustand (S); gezeichnet
     wird ausschließlich hier im requestAnimationFrame-Takt.
  ============================================================ */
  let lastT = performance.now();
  let lastDraw = 0;
  let fpsFrames = 0;
  let fpsStamp = lastT;
  let rafStarted = false;
  function startRenderLoop() {
    if (rafStarted) return;
    rafStarted = true;
    requestAnimationFrame(tick);
  }
  function tick(now) {
    requestAnimationFrame(tick);
    if (document.hidden || S.windowMinimized) { lastT = now; S.fps = 0; return; }
    if (now - lastDraw < 12.5) return; // ~60-72 FPS Obergrenze auch auf 120/144-Hz-Displays
    lastDraw = now;
    try {
      const dt = Math.min(0.05, Math.max(0.001, (now - lastT) / 1000));
      lastT = now;
      const t = now / 1000;
      const t0 = performance.now();

      S.levelS += ((S.micMuted ? 0 : S.level) - S.levelS) * (1 - Math.exp(-dt * 16));

      updatePalette(dt);
      updateAudio(dt, t);

      drawCore(dt, t);
      if (S.coreMode === "neural" && window.__jarvisBrain3D && !window.__jarvisBrain3D._paused) {
        window.__jarvisBrain3D.setAudio(AUD.raw, S.vad, S.speaking);
        window.__jarvisBrain3D.render(dt);
      }
      drawWaveform(t);
      drawSpectrum();
      drawRuler();

      S.gauge.cpu += (S.disp.cpu - S.gauge.cpu) * (1 - Math.exp(-dt * 6));
      S.gauge.ram += (S.disp.ram - S.gauge.ram) * (1 - Math.exp(-dt * 6));
      drawBigCpuGauge(S.gauge.cpu, t);
      drawSubGauge(S.gauge.ram, t);
      drawRamSparkline(t);

      if (el.netOverlay && !el.netOverlay.classList.contains("hidden")) drawNet(t);

      // FPS-Zähler + adaptive Qualität
      fpsFrames++;
      if (now - fpsStamp >= 1000) {
        S.fps = Math.round((fpsFrames * 1000) / (now - fpsStamp));
        fpsFrames = 0;
        fpsStamp = now;
      }
      const cost = performance.now() - t0;
      PERF.cost += (cost - PERF.cost) * 0.05;
      if (PERF.cost > 11) { PERF.slow++; PERF.fast = 0; } else if (PERF.cost < 5) { PERF.fast++; PERF.slow = 0; } else { PERF.slow = 0; PERF.fast = 0; }
      if (PERF.q >= 1 && PERF.slow > 120) { PERF.q = 0.6; PERF.slow = 0; }
      else if (PERF.q < 1 && PERF.fast > 600) { PERF.q = 1; PERF.fast = 0; }
    } catch (err) {
      console.error("[HUD RENDER ERROR]", err);
    }
  }

  /* ============================================================
     START
  ============================================================ */
  renderMode(true);
  renderMic(false);
  renderCamera(true);
  renderCamSensor(false);
  renderVisionStatus(S.visionStatus);
  updateVadUi();
  updateReadouts();
  syncSnapshot();
  connectWs();
  startRenderLoop();

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      S.windowMinimized = false;
      lastT = performance.now();
      syncSnapshot();
      connectWs();
      if (S.cameraActive && !S.camPaused) reloadCamera();
      if (_shouldShowDesktopVision()) _loadNextDesktopFrame();
    } else {
      _clearDesktopPreviewTimer();
    }
  });
  window.addEventListener("focus", () => {
    if (S.windowMinimized) {
      S.windowMinimized = false;
      lastT = performance.now();
      syncSnapshot();
    }
  });

  // --- Public Beta Status & Feedback-Modal Event-Handler (ohne Key-Sperre) ---
  const betaBadgeBtn = $("beta-version-badge");
  const licenseModal = $("license-modal");
  const licenseCloseBtn = $("license-close");
  const licFeedbackMsg = $("lic-feedback-msg");
  const btnLicAudio = $("btn-lic-audio");

  function openLicenseModal() {
    if (!licenseModal) return;
    licenseModal.classList.remove("hidden");
    licenseModal.setAttribute("aria-hidden", "false");
  }
  function closeLicenseModal() {
    if (!licenseModal) return;
    licenseModal.classList.add("hidden");
    licenseModal.setAttribute("aria-hidden", "true");
  }
  if (betaBadgeBtn) betaBadgeBtn.addEventListener("click", openLicenseModal);
  if (licenseCloseBtn) licenseCloseBtn.addEventListener("click", closeLicenseModal);

  if (btnLicAudio) {
    btnLicAudio.addEventListener("click", async () => {
      if (!licFeedbackMsg) return;
      licFeedbackMsg.classList.remove("error");
      licFeedbackMsg.textContent = "Prüfe Mikrofon & Audio-Ausgabe...";
      try {
        const res = await postJson("/api/license/audio_check", {});
        licFeedbackMsg.textContent = (res && res.message) ? res.message : "Audio-Check abgeschlossen.";
      } catch (_e) {
        licFeedbackMsg.classList.add("error");
        licFeedbackMsg.textContent = "Audio-Check fehlgeschlagen.";
      }
    });
  }

  const btnBetaFeedback = $("btn-beta-feedback");
  const feedbackModal = $("feedback-modal");
  const feedbackCloseBtn = $("feedback-close");
  const fbCategory = $("fb-category");
  const fbContact = $("fb-contact");
  const fbMessage = $("fb-message");
  const fbFeedbackMsg = $("fb-feedback-msg");
  const btnFbSubmit = $("btn-fb-submit");

  if (btnBetaFeedback && feedbackModal) {
    btnBetaFeedback.addEventListener("click", () => {
      if (fbFeedbackMsg) { fbFeedbackMsg.textContent = ""; fbFeedbackMsg.classList.remove("error"); }
      feedbackModal.classList.remove("hidden");
      feedbackModal.setAttribute("aria-hidden", "false");
    });
  }
  if (feedbackCloseBtn && feedbackModal) {
    feedbackCloseBtn.addEventListener("click", () => {
      feedbackModal.classList.add("hidden");
      feedbackModal.setAttribute("aria-hidden", "true");
    });
  }
  if (btnFbSubmit) {
    btnFbSubmit.addEventListener("click", async () => {
      if (!fbFeedbackMsg) return;
      fbFeedbackMsg.classList.remove("error");
      try {
        const res = await fetch("/api/beta/feedback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            category: fbCategory ? fbCategory.value : "feedback",
            contact: fbContact ? fbContact.value : "",
            message: fbMessage ? fbMessage.value : "",
          }),
        });
        const data = await res.json();
        if (res.ok && data.ok) {
          fbFeedbackMsg.textContent = data.message || "Report gespeichert!";
          if (fbMessage) fbMessage.value = "";
        } else {
          fbFeedbackMsg.classList.add("error");
          fbFeedbackMsg.textContent = data.error || "Bitte Text eingeben.";
        }
      } catch (_e) {
        fbFeedbackMsg.classList.add("error");
        fbFeedbackMsg.textContent = "Speichern fehlgeschlagen.";
      }
    });
  }

  /* ============================================================
     3D NEURAL BRAIN + TACTICAL OS LINKS + SHOW-MODUS + MULTI-WINDOW SYNC
     ============================================================ */
  // Automatischer Performance-Fallback auf Raspberry Pi 5 / ARM-Linux (60 FPS ohne Blur-Last)
  try {
    const plat = navigator.platform || "";
    const ua = navigator.userAgent || "";
    if (/arm|aarch64/i.test(plat) || /aarch64|armv/i.test(ua)) {
      document.body.classList.add("perf-lite");
    }
  } catch (_e) {}

  const brainCanvas = document.getElementById("cv-brain-3d");
  const coreStageEl = document.getElementById("core-stage");
  const scanBadgeEl = document.getElementById("hud-scan-badge");
  const btnBrainDemo = document.getElementById("btn-brain-demo");
  const btnPopLogs = document.getElementById("btn-pop-logs");
  const btnPopVision = document.getElementById("btn-pop-vision");
  const popMonitorSel = document.getElementById("popout-monitor-select");
  const cmdStepsEl = document.getElementById("cmd-steps");
  const hlCursorPath = document.getElementById("hl-cursor-path");
  const hlScanPath = document.getElementById("hl-scan-path");
  const hlDotBrain = document.getElementById("hl-dot-brain");
  const hlDotMon = document.getElementById("hl-dot-mon");
  const hudSyncBc = "BroadcastChannel" in window ? new BroadcastChannel("jarvis_hud_sync_v1") : null;

  let currentCursorMonitor = 1;
  let currentBrainState = "idle";
  let currentScanMonitor = null;
  let lastCmdStepSig = "";
  const scanFlashTimers = {};

  function pushCommandStep(category, text) {
    if (!cmdStepsEl) return;
    const clean = String(text || "").trim().slice(0, 44);
    if (!clean) return;
    const cat = String(category || "voice").toLowerCase().replace(/[^a-z0-9]/g, "");
    const sig = `${cat}:${clean}`;
    if (sig === lastCmdStepSig) return;
    lastCmdStepSig = sig;
    const chip = document.createElement("span");
    chip.className = `cmd-step ${cat}`;
    chip.textContent = clean.toUpperCase();
    cmdStepsEl.appendChild(chip);
    while (cmdStepsEl.children.length > 4) {
      cmdStepsEl.removeChild(cmdStepsEl.firstChild);
    }
  }

  function flashMonitorScanBar(monIdx, durationMs) {
    const idx = Number(monIdx);
    if (![1, 2, 3].includes(idx)) return;
    const tile = document.getElementById(`cam-mon-${idx}`);
    if (!tile) return;
    tile.classList.add("scanning");
    if (scanFlashTimers[idx]) clearTimeout(scanFlashTimers[idx]);
    scanFlashTimers[idx] = setTimeout(() => {
      if (currentBrainState !== "screen_scan") {
        tile.classList.remove("scanning");
      }
      delete scanFlashTimers[idx];
    }, durationMs || 1800);
  }

  function updateBrainMonitorLinks() {
    const stageRef = (brainCanvas && S.coreMode === "neural") ? brainCanvas : coreStageEl;
    if (!stageRef) return;
    const br = stageRef.getBoundingClientRect();
    const gridEl = document.getElementById("cam-3mon-grid");
    const gridVisible = Boolean(gridEl && !gridEl.classList.contains("hidden") && br.width > 40 && br.height > 40);

    const hideAll = () => {
      if (hlCursorPath) hlCursorPath.classList.remove("on");
      if (hlScanPath) hlScanPath.classList.remove("on");
      if (hlDotBrain) hlDotBrain.classList.remove("on");
      if (hlDotMon) hlDotMon.classList.remove("on");
      if (brain3d && typeof brain3d.setScanTargets === "function") brain3d.setScanTargets([]);
    };
    if (!gridVisible) {
      hideAll();
      return;
    }

    const bx = br.left + br.width * 0.5;
    const by = br.top + br.height * 0.5;

    const getTileAnchor = (idx) => {
      const t = document.getElementById(`cam-mon-${idx}`);
      if (!t) return null;
      const r = t.getBoundingClientRect();
      if (r.width < 20 || r.height < 16) return null;
      return { x: r.left + 2, y: r.top + r.height * 0.5, cx: r.left + r.width * 0.5, cy: r.top + r.height * 0.5 };
    };

    const makeCurve = (x1, y1, x2, y2) => {
      const dx = Math.max(40, Math.abs(x2 - x1) * 0.42);
      return `M ${x1.toFixed(1)} ${y1.toFixed(1)} C ${(x1 + dx).toFixed(1)} ${y1.toFixed(1)}, ${(x2 - dx).toFixed(1)} ${y2.toFixed(1)}, ${x2.toFixed(1)} ${y2.toFixed(1)}`;
    };

    const curAnchor = getTileAnchor(currentCursorMonitor) || getTileAnchor(1);
    if (curAnchor && hlCursorPath) {
      hlCursorPath.setAttribute("d", makeCurve(bx, by, curAnchor.x, curAnchor.y));
      hlCursorPath.classList.add("on");
      if (hlDotBrain) {
        hlDotBrain.setAttribute("cx", bx.toFixed(1));
        hlDotBrain.setAttribute("cy", by.toFixed(1));
        hlDotBrain.classList.add("on");
      }
      if (hlDotMon) {
        hlDotMon.setAttribute("cx", curAnchor.x.toFixed(1));
        hlDotMon.setAttribute("cy", curAnchor.y.toFixed(1));
        hlDotMon.classList.add("on");
      }
    } else if (hlCursorPath) {
      hlCursorPath.classList.remove("on");
    }

    // 3D Scan-Laser + Plasma-Orange Scan-Verbindungslinie beim Screen-Scan
    const isScan = currentBrainState === "screen_scan";
    if (isScan) {
      const targets = currentScanMonitor === "all"
        ? [1, 2, 3]
        : [Number(currentScanMonitor) || currentCursorMonitor || 1];
      const ndcList = [];
      let scanPathD = "";
      targets.forEach((mIdx) => {
        const a = getTileAnchor(mIdx);
        if (!a) return;
        scanPathD += (scanPathD ? " " : "") + makeCurve(bx, by, a.x, a.y);
        // Richtungsvektor vom Brain-Canvas-Zentrum Richtung Ziel-Monitor-Kachel (NDC, y nach oben)
        let nx = (a.cx - bx) / (br.width * 0.5);
        let ny = -(a.cy - by) / (br.height * 0.5);
        const mag = Math.max(Math.abs(nx), Math.abs(ny), 1);
        nx = (nx / mag) * 0.94;
        ny = (ny / mag) * 0.94;
        ndcList.push({ x: nx, y: ny });
      });
      if (hlScanPath && scanPathD) {
        hlScanPath.setAttribute("d", scanPathD);
        hlScanPath.classList.add("on");
      }
      if (brain3d && typeof brain3d.setScanTargets === "function") {
        brain3d.setScanTargets(ndcList.length ? ndcList : [{ x: 0.9, y: -0.4 }]);
      }
    } else {
      if (hlScanPath) hlScanPath.classList.remove("on");
      if (brain3d && typeof brain3d.setScanTargets === "function") brain3d.setScanTargets([]);
    }
  }

  let brain3d = null;
  let lastBrainActivity = { state: "idle", intensity: 0.28, active_monitor: null };

  function ensureBrain3D() {
    if (!brain3d && typeof window.JarvisNeuralBrain === "function" && brainCanvas) {
      brain3d = new window.JarvisNeuralBrain(brainCanvas);
      if (brain3d && brain3d.supported) {
        window.__jarvisBrain3D = brain3d;
        brain3d.setActivity(lastBrainActivity);
      } else {
        brain3d = null;
      }
    }
    return brain3d;
  }

  function applyCoreMode(mode, persist = true) {
    const next = mode === "neural" ? "neural" : "classic";
    S.coreMode = next;
    document.body.classList.toggle("core-neural", next === "neural");
    document.body.classList.toggle("core-classic", next === "classic");
    const btnCl = document.getElementById("btn-core-classic");
    const btnNe = document.getElementById("btn-core-neural");
    if (btnCl) btnCl.classList.toggle("active", next === "classic");
    if (btnNe) btnNe.classList.toggle("active", next === "neural");
    if (persist) {
      try { localStorage.setItem("jarvis_core_mode", next); } catch (_e) {}
    }
    if (next === "neural") {
      const b = ensureBrain3D();
      if (b) {
        b.setPaused(false);
        b.resize();
      }
    } else if (brain3d && typeof brain3d.setPaused === "function") {
      brain3d.setPaused(true);
    }
    updateBrainMonitorLinks();
  }

  let savedCoreMode = "classic";
  try { savedCoreMode = localStorage.getItem("jarvis_core_mode") || "classic"; } catch (_e) {}
  applyCoreMode(savedCoreMode, false);

  const btnCoreClassic = document.getElementById("btn-core-classic");
  const btnCoreNeural = document.getElementById("btn-core-neural");
  const btnPopNeuralCore = document.getElementById("btn-pop-neural-core");
  if (btnCoreClassic) btnCoreClassic.addEventListener("click", (ev) => { ev.stopPropagation(); applyCoreMode("classic", true); });
  if (btnCoreNeural) btnCoreNeural.addEventListener("click", (ev) => { ev.stopPropagation(); applyCoreMode("neural", true); });

  window.addEventListener("resize", () => {
    if (brain3d && S.coreMode === "neural") brain3d.resize();
    updateBrainMonitorLinks();
  });
  if (coreStageEl) {
    coreStageEl.addEventListener("mousemove", (ev) => {
      if (!brain3d || S.coreMode !== "neural") return;
      const r = coreStageEl.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        brain3d.setPointer(((ev.clientX - r.left) / r.width) * 2 - 1, ((ev.clientY - r.top) / r.height) * 2 - 1);
      }
    });
    coreStageEl.addEventListener("mouseleave", () => { if (brain3d) brain3d.setPointer(0, 0); });
  }
  setTimeout(updateBrainMonitorLinks, 280);

  window.__relayHudBroadcast = (msg) => {
    if (hudSyncBc && msg) {
      try { hudSyncBc.postMessage(msg); } catch (_e) {}
    }
  };

  window.__onNeuralThought = (msg) => {
    if (!msg) return;
    const cat = msg.category || "thought";
    const lbl = msg.tool ? `${msg.tool}: ${msg.summary || ""}` : (msg.summary || "");
    if (lbl) pushCommandStep(cat, lbl);
  };

  window.__applyMonitorTopology = (msg) => {
    if (!msg || !Array.isArray(msg.monitors) || !popMonitorSel) return;
    const prev = popMonitorSel.value;
    popMonitorSel.innerHTML = msg.monitors.map((m) =>
      `<option value="${m.index}">M${m.index} (${String(m.pos || "").toUpperCase()})</option>`
    ).join("");
    if (msg.monitors.some((m) => String(m.index) === String(prev))) {
      popMonitorSel.value = prev;
    } else if (msg.monitors.length >= 2) {
      popMonitorSel.value = String(msg.monitors[1].index);
    }
  };

  window.__applyBrainActivity = (msg, relay) => {
    if (!msg) return;
    lastBrainActivity = { ...msg };
    const st = String(msg.state || "idle").toLowerCase();
    currentBrainState = st;
    currentScanMonitor = st === "screen_scan" ? (msg.active_monitor || currentCursorMonitor || 1) : null;
    if (brain3d) brain3d.setActivity(msg);
    if (scanBadgeEl) {
      if (st === "screen_scan") {
        const monTxt = msg.active_monitor ? String(msg.active_monitor).toUpperCase() : "ALL";
        scanBadgeEl.textContent = `SCANNING MONITOR: ${monTxt}`;
        scanBadgeEl.classList.remove("hidden");
      } else {
        scanBadgeEl.classList.add("hidden");
      }
    }
    // 3-Monitor-Widgets, Kommando-Dock-Scanbar & Scan-Projektionslinien aktualisieren
    const actMon = msg.active_monitor;
    [1, 2, 3].forEach((idx) => {
      const hit = st === "screen_scan" && (actMon === "all" || Number(actMon) === idx);
      const wEl = document.getElementById(`mon-widget-${idx}`);
      const bEl = document.getElementById(`scan-beam-${idx}`);
      const cTile = document.getElementById(`cam-mon-${idx}`);
      if (wEl) wEl.classList.toggle("scanning", hit);
      if (bEl) bEl.classList.toggle("active", hit);
      if (cTile && !scanFlashTimers[idx]) cTile.classList.toggle("scanning", hit);
    });
    updateBrainMonitorLinks();
    if (msg.detail && typeof window.__updateLiveSubtitle === "function") {
      window.__updateLiveSubtitle("action", `${st.toUpperCase()} · ${msg.detail}`);
      const stepCat = st === "screen_scan" ? "vision" : st === "n8n" ? "n8n" : st === "thinking" ? "tool" : "voice";
      pushCommandStep(stepCat, msg.detail);
    }
    if (relay && hudSyncBc) {
      try { hudSyncBc.postMessage({ type: "brain_activity", ...msg }); } catch (_e) {}
    }
  };

  if (hudSyncBc) {
    hudSyncBc.onmessage = (ev) => {
      if (ev.data && ev.data.type === "brain_activity") {
        window.__applyBrainActivity(ev.data, false);
      }
    };
  }

  // Live-Untertitel-Updater (User / Action)
  const subUserTxt = document.getElementById("sub-user-txt");
  const subActionTxt = document.getElementById("sub-action-txt");
  window.__updateLiveSubtitle = (role, txt) => {
    const clean = String(txt || "").trim();
    if (!clean) return;
    if (role === "user" && subUserTxt) subUserTxt.textContent = clean;
    if (role !== "user" && subActionTxt) subActionTxt.textContent = clean;
    if (hudSyncBc) {
      try { hudSyncBc.postMessage({ type: "subtitle", role, txt: clean }); } catch (_e) {}
    }
  };

  // Showcase / Clean-Modus (F9) & versteckte Show-Sequenz (F8)
  const btnShowcaseF9 = document.getElementById("btn-showcase-f9");
  function toggleShowcaseMode(forceState) {
    const next = typeof forceState === "boolean" ? forceState : !document.body.classList.contains("showcase-active");
    document.body.classList.toggle("showcase-active", next);
    if (btnShowcaseF9) {
      btnShowcaseF9.classList.toggle("active", next);
      btnShowcaseF9.textContent = next ? "EXIT CLEAN (F9)" : "CLEAN (F9)";
    }
    setTimeout(() => { resizeAll(); if (brain3d) brain3d.resize(); updateBrainMonitorLinks(); }, 90);
  }
  if (btnShowcaseF9) {
    btnShowcaseF9.addEventListener("click", (ev) => { ev.stopPropagation(); toggleShowcaseMode(); });
  }
  window.addEventListener("keydown", (ev) => {
    if (typeof window.__isBetaExpired === "function" && window.__isBetaExpired()) return;
    if (ev.key === "F9") {
      ev.preventDefault();
      toggleShowcaseMode();
    } else if (ev.key === "F10" || ev.key === "F8") {
      ev.preventDefault();
      trigger10SecShowSequence();
    }
  });

  // Offline-Preview-Modus vs. Live-Backend Erkennung (kein Werbebanner)
  const connBadgeEl = document.getElementById("conn-mode-badge");
  window.__setWebDemoMode = (isWebDemo) => {
    const forceDemo = new URLSearchParams(window.location.search).get("webdemo") === "1";
    const active = Boolean(isWebDemo || forceDemo);
    document.body.classList.toggle("web-demo-mode", active);
    if (connBadgeEl) {
      connBadgeEl.textContent = active ? "OFFLINE PREVIEW" : "LIVE";
    }
  };
  if (new URLSearchParams(window.location.search).get("webdemo") === "1") {
    window.__setWebDemoMode(true);
  } else {
    fetch("/api/state", { cache: "no-store" })
      .then((r) => r.json().then((d) => ({ ok: r.ok, d })))
      .then(({ ok, d }) => {
        if (d && d.expired === true && typeof window.__applyBetaGuardState === "function") {
          window.__applyBetaGuardState(d);
          window.__setWebDemoMode(false);
          return;
        }
        if (!ok) throw new Error("offline");
        if (d && d.beta_guard && typeof window.__applyBetaGuardState === "function") window.__applyBetaGuardState(d.beta_guard);
        window.__setWebDemoMode(false);
      })
      .catch(() => window.__setWebDemoMode(true));
  }

  // Controls vor Push-to-Talk-Klick auf #core-stage schützen
  const brainCtrlWrap = document.getElementById("hud-brain-controls");
  if (brainCtrlWrap) {
    brainCtrlWrap.addEventListener("click", (ev) => ev.stopPropagation());
  }
  const monDockEl = document.getElementById("core-monitor-dock");
  if (monDockEl) {
    monDockEl.addEventListener("click", (ev) => ev.stopPropagation());
  }
  document.querySelectorAll("[data-mon-scan]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mId = Number(btn.dataset.monScan || 1);
      const pkt = { state: "screen_scan", intensity: 0.94, active_monitor: mId, cinematic: true, detail: `SCAN MONITOR ${mId}` };
      setStatus("Kamera aktiv", "SCREEN SCAN", `MONITOR ${mId}`);
      window.__updateLiveSubtitle("user", `Jarvis, analysiere Bildschirm ${mId}`);
      window.__applyBrainActivity(pkt, true);
      fetch("/api/brain/demo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...pkt, category: "vision", tool: "monitor_widget", thought: `Fokus auf Monitor ${mId}` }),
      }).catch(() => {});
    });
  });

  // Demo / Show-Zyklus
  let demoTimer = null;
  let demoStep = 0;
  let showTimers = [];
  const demoStages = [
    { state: "idle", intensity: 0.3, active_monitor: null, audio: 0.05, detail: "STANDBY WAVE", label: ["Bereit", "READY", "STANDBY"] },
    { state: "listening", intensity: 0.78, active_monitor: null, audio: 0.65, detail: "AUDIO KONVERGENZ", label: ["Höre zu", "LISTENING", "AUDIO REAKTIV"] },
    { state: "thinking", intensity: 0.96, active_monitor: null, audio: 0.12, detail: "HIGH-SPEED LEITERBAHNEN", label: ["Denke nach", "THINKING", "NEURAL SURGE"] },
    { state: "screen_scan", intensity: 0.92, active_monitor: 1, audio: 0.08, detail: "SCAN MONITOR 1 (LINKS)", label: ["Kamera aktiv", "SCREEN SCAN", "MONITOR 1"] },
    { state: "screen_scan", intensity: 0.92, active_monitor: 2, audio: 0.08, detail: "SCAN MONITOR 2 (MITTE)", label: ["Kamera aktiv", "SCREEN SCAN", "MONITOR 2"] },
    { state: "screen_scan", intensity: 0.95, active_monitor: "all", audio: 0.1, detail: "SCAN ALLE 3 MONITORE", label: ["Kamera aktiv", "SCREEN SCAN", "ALLE 3 BILDSCHIRME"] },
    { state: "speaking", intensity: 0.85, active_monitor: null, audio: 0.82, detail: "TTS RADIAL-WELLE", label: ["Spreche", "SPEAKING", "STIMM-IMPULS"] },
  ];

  function clearShowTimers() {
    showTimers.forEach((id) => clearTimeout(id));
    showTimers = [];
  }

  function setBrainAudioSynced(level, vadActive, speakingActive) {
    if (brain3d) brain3d.setAudio(level, vadActive, speakingActive);
    if (hudSyncBc) {
      try {
        hudSyncBc.postMessage({
          type: "audio",
          level: Number(level || 0),
          vad_active: Boolean(vadActive),
          speaking_active: Boolean(speakingActive),
        });
      } catch (_e) {}
    }
  }

  async function runDemoStep() {
    const step = demoStages[demoStep % demoStages.length];
    demoStep++;
    setStatus(step.label[0], step.label[1], step.label[2]);
    setBrainAudioSynced(step.audio, step.state === "listening", step.state === "speaking");
    window.__applyBrainActivity({ ...step, cinematic: true }, true);
    fetch("/api/brain/demo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        state: step.state,
        intensity: step.intensity,
        active_monitor: step.active_monitor,
        detail: step.detail,
        category: step.state === "screen_scan" ? "vision" : "thought",
        tool: "show_demo",
        thought: `Show-Modus Phase: ${step.detail}`,
      }),
    }).catch(() => {});
  }

  if (btnBrainDemo) {
    btnBrainDemo.addEventListener("click", (ev) => {
      ev.stopPropagation();
      clearShowTimers();
      if (demoTimer) {
        clearInterval(demoTimer);
        demoTimer = null;
        btnBrainDemo.classList.remove("active");
        btnBrainDemo.textContent = "DEMO";
        setStatus("Bereit", "READY", "STANDBY");
        window.__applyBrainActivity({ state: "idle", intensity: 0.28, active_monitor: null, cinematic: false, detail: "STANDBY" }, true);
        if (brain3d) brain3d.setAudio(0, false, false);
        return;
      }
      demoStep = 0;
      btnBrainDemo.classList.add("active");
      btnBrainDemo.textContent = "⏹ DEMO STOPP";
      runDemoStep();
      demoTimer = setInterval(runDemoStep, 2600);
    });
  }

  // 10-Sekunden Kettenreaktion (Multi-Screen-Scan -> Neural-Surge -> System-Analyse -> Sprachantwort)
  const btnShowSeq = document.getElementById("btn-show-seq");
  function trigger10SecShowSequence() {
    if (demoTimer) { clearInterval(demoTimer); demoTimer = null; }
    clearShowTimers();
    if (btnShowSeq) { btnShowSeq.classList.add("active"); }
    const timeline = [
      [0, () => {
        setStatus("Höre zu", "LISTENING", "VOICE TRIGGER");
        window.__updateLiveSubtitle("user", "„Jarvis, scanne alle 3 Monitore und starte die Automation!“");
        setBrainAudioSynced(0.72, true, false);
        window.__applyBrainActivity({ state: "listening", intensity: 0.84, active_monitor: null, cinematic: true, detail: "SPRACHEINGABE ERKANNT" }, true);
      }],
      [2100, () => {
        setStatus("Kamera aktiv", "SCREEN SCAN", "MONITOR 1 (LINKS)");
        setBrainAudioSynced(0.15, false, false);
        window.__applyBrainActivity({ state: "screen_scan", intensity: 0.92, active_monitor: 1, cinematic: true, detail: "SCAN MONITOR 1 · 1920x1080" }, true);
      }],
      [3300, () => {
        setStatus("Kamera aktiv", "SCREEN SCAN", "MONITOR 2 (4K MITTE)");
        window.__applyBrainActivity({ state: "screen_scan", intensity: 0.95, active_monitor: 2, cinematic: true, detail: "SCAN MONITOR 2 · 3840x2160" }, true);
      }],
      [4500, () => {
        setStatus("Kamera aktiv", "SCREEN SCAN", "ALLE 3 BILDSCHIRME");
        window.__applyBrainActivity({ state: "screen_scan", intensity: 0.98, active_monitor: "all", cinematic: true, detail: "PANORAMA-FUSION: M1 + M2 + M3" }, true);
      }],
      [5700, () => {
        setStatus("Denke nach", "N8N ARCHITECT", "GOLDENE LEITERBAHNEN");
        if (brain3d && typeof brain3d.triggerShock === "function") brain3d.triggerShock();
        window.__applyBrainActivity({ state: "n8n", intensity: 1.0, active_monitor: null, cinematic: true, detail: "N8N WORKFLOW SURGE · AKTIV" }, true);
        fetch("/api/brain/demo", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ state: "n8n", intensity: 1.0, detail: "N8N WORKFLOW SURGE", category: "n8n", tool: "n8n_architect", thought: "n8n-Trigger -> Webhook Verarbeitung -> Antwort" }),
        }).catch(() => {});
      }],
      [7900, () => {
        setStatus("Spreche", "SPEAKING", "ANTWORT STREAM");
        setBrainAudioSynced(0.86, false, true);
        window.__updateLiveSubtitle("action", "„Alle 3 Monitore geprüft und Workflow erfolgreich ausgelöst.“");
        window.__applyBrainActivity({ state: "speaking", intensity: 0.88, active_monitor: null, cinematic: true, detail: "TTS-ANTWORT AUSGABE" }, true);
      }],
      [10100, () => {
        setStatus("Bereit", "READY", "STANDBY");
        setBrainAudioSynced(0, false, false);
        window.__applyBrainActivity({ state: "idle", intensity: 0.28, active_monitor: null, cinematic: false, detail: "NEURAL CORE · BEREIT" }, true);
        if (btnShowSeq) { btnShowSeq.classList.remove("active"); }
      }],
    ];
    timeline.forEach(([delay, fn]) => showTimers.push(setTimeout(fn, delay)));
  }
  if (btnShowSeq) {
    btnShowSeq.addEventListener("click", (ev) => { ev.stopPropagation(); trigger10SecShowSequence(); });
  }

  // Offline-Preview Chips
  document.querySelectorAll("[data-web-demo]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const kind = btn.dataset.webDemo;
      if (kind === "show10s") return trigger10SecShowSequence();
      if (kind === "showcase") return toggleShowcaseMode();
      if (kind === "listening") {
        setStatus("Höre zu", "LISTENING", "OFFLINE PREVIEW");
        if (brain3d) brain3d.setAudio(0.68, true, false);
        window.__updateLiveSubtitle("user", "„Hey Jarvis, zeige System-Status und 3 Monitore.“");
        window.__applyBrainActivity({ state: "listening", intensity: 0.82, active_monitor: null, cinematic: true, detail: "AUDIO-FREQUENZ-WELLEN" }, true);
      } else if (kind === "thinking") {
        setStatus("Denke nach", "THINKING", "OFFLINE PREVIEW");
        window.__applyBrainActivity({ state: "thinking", intensity: 0.98, active_monitor: null, cinematic: true, detail: "LEITERBAHN-IMPULSE AKTIV" }, true);
      } else if (kind === "n8n") {
        setStatus("Denke nach", "N8N ARCHITECT", "OFFLINE PREVIEW");
        window.__applyBrainActivity({ state: "n8n", intensity: 1.0, active_monitor: null, cinematic: true, detail: "N8N GOLDENE LEITERBAHNEN" }, true);
      } else if (kind === "screen_scan") {
        setStatus("Kamera aktiv", "SCREEN SCAN", "3 MONITORE");
        window.__applyBrainActivity({ state: "screen_scan", intensity: 0.95, active_monitor: "all", cinematic: true, detail: "3-MONITOR PROJEKTION" }, true);
      }
    });
  });

  // N8N Bento Kachel Bindings
  const n8nBadge = document.getElementById("n8n-status-badge");
  const n8nModeLbl = document.getElementById("n8n-mode-lbl");
  const n8nWfCount = document.getElementById("n8n-wf-count");
  const n8nLastEvent = document.getElementById("n8n-last-event");
  const n8nGoalInput = document.getElementById("n8n-goal-input");
  const btnN8nCreate = document.getElementById("btn-n8n-create");
  const btnN8nRun = document.getElementById("btn-n8n-run");
  const btnN8nOpen = document.getElementById("btn-n8n-open");
  let n8nBaseUrl = "http://localhost:5678";

  window.__renderN8nState = (n8n) => {
    if (!n8n || typeof n8n !== "object") return;
    if (n8n.base_url) n8nBaseUrl = n8n.base_url;
    if (n8nBadge) {
      n8nBadge.textContent = n8n.online ? "ONLINE" : "FALLBACK";
      n8nBadge.classList.toggle("online", Boolean(n8n.online));
    }
    if (n8nModeLbl) n8nModeLbl.textContent = `${n8n.mode || "LOCAL"} · 0 EUR`;
    if (n8nWfCount) n8nWfCount.textContent = `${Number(n8n.workflow_count || 0)} FLOWS`;
    if (n8nLastEvent && n8n.last_event) n8nLastEvent.textContent = n8n.last_event;
  };

  if (btnN8nCreate) {
    btnN8nCreate.addEventListener("click", async () => {
      const goal = n8nGoalInput ? n8nGoalInput.value.trim() : "";
      if (!goal) return;
      if (n8nBadge) { n8nBadge.textContent = "BAUT…"; n8nBadge.classList.add("busy"); }
      window.__applyBrainActivity({ state: "n8n", intensity: 0.96, cinematic: true, detail: `BAUE N8N: ${goal.slice(0, 32)}` }, true);
      const res = await postJson("/api/n8n/create", { goal, activate: true }).catch(() => null);
      if (n8nBadge) n8nBadge.classList.remove("busy");
      if (res && n8nLastEvent) n8nLastEvent.textContent = res.message || "Workflow erstellt.";
      if (n8nGoalInput) n8nGoalInput.value = "";
      fetch("/api/n8n/status").then((r) => r.json()).then(window.__renderN8nState).catch(() => {});
    });
  }
  if (btnN8nRun) {
    btnN8nRun.addEventListener("click", async () => {
      const tgt = n8nGoalInput ? n8nGoalInput.value.trim() : "";
      if (n8nBadge) { n8nBadge.textContent = "LÄUFT…"; n8nBadge.classList.add("busy"); }
      const res = await postJson("/api/n8n/run", { target: tgt }).catch(() => null);
      if (n8nBadge) n8nBadge.classList.remove("busy");
      if (res && n8nLastEvent) n8nLastEvent.textContent = res.message || "Ausgeführt.";
      fetch("/api/n8n/status").then((r) => r.json()).then(window.__renderN8nState).catch(() => {});
    });
  }
  if (btnN8nOpen) {
    btnN8nOpen.addEventListener("click", () => window.open(n8nBaseUrl, "_blank"));
  }

  async function openPopoutPanel(panelKind) {
    const mon = popMonitorSel ? Number(popMonitorSel.value || 1) : 1;
    const winKey = panelKind === "neural-core"
      ? "neural-core"
      : (panelKind === "vision" || panelKind === "screen-matrix" ? "screen-matrix" : "system-telemetry");
    const routeUrl = winKey === "neural-core" ? "/neural-core" : `/window/${winKey}`;
    if (document.body.classList.contains("web-demo-mode")) {
      window.open(routeUrl, `jarvis_${winKey}`, "width=1140,height=740");
      return;
    }
    await postJson("/api/windows/open", { window: winKey, monitor: mon }).catch(() => {});
  }
  if (btnPopLogs) btnPopLogs.addEventListener("click", (ev) => { ev.stopPropagation(); openPopoutPanel("system-telemetry"); });
  if (btnPopVision) btnPopVision.addEventListener("click", (ev) => { ev.stopPropagation(); openPopoutPanel("screen-matrix"); });
  if (btnPopNeuralCore) btnPopNeuralCore.addEventListener("click", (ev) => { ev.stopPropagation(); openPopoutPanel("neural-core"); });

  // ============================================================
  // BINÄRER 10-15 FPS MULTI-MONITOR WEBSOCKET + TICKER + MAKROS
  // ============================================================
  const liveTickerTxt = document.getElementById("live-ticker-txt");
  const liveMouseEl = document.getElementById("live-mouse-xy");
  const liveFpsEl = document.getElementById("live-mon-fps");
  const btnWorkMode = document.getElementById("btn-work-mode");
  const monBlobUrls = { 1: null, 2: null, 3: null };
  const monFrameLastTs = { 1: 0, 2: 0, 3: 0 };
  const monFpsEma = { 1: 0, 2: 0, 3: 0 };

  window.__onTelemetryEvent = (ev) => {
    if (!ev) return;
    const monTag = ev.monitor ? ` [M${String(ev.monitor).toUpperCase()}]` : "";
    if (liveTickerTxt) {
      liveTickerTxt.textContent = `[${ev.ts || "--"}] ${String(ev.category || "SYS").toUpperCase()}${monTag}: ${ev.summary || ""}`;
    }
    pushCommandStep(ev.category || "voice", `${String(ev.category || "SYS").toUpperCase()}${monTag} · ${ev.summary || ""}`);
    if (ev.monitor && (ev.category === "vision" || ev.category === "window" || ev.source === "app_verify" || ev.source === "highlight")) {
      flashMonitorScanBar(ev.monitor, 1800);
    }
  };

  window.__updateLive3ScreenMeta = (data) => {
    if (!data) return;
    if (liveMouseEl && data.mouse) {
      liveMouseEl.textContent = `MAUS: ${data.mouse.x}, ${data.mouse.y} · M${data.mouse.monitor || 1}`;
      const cMon = Number(data.mouse.monitor || 1);
      const changed = cMon !== currentCursorMonitor;
      currentCursorMonitor = cMon;
      [1, 2, 3].forEach((idx) => {
        const isC = idx === cMon;
        const cTile = document.getElementById(`cam-mon-${idx}`);
        const cBadge = document.getElementById(`cam-mon-cursor-${idx}`);
        const lCard = document.getElementById(`l3s-card-${idx}`);
        if (cTile) cTile.classList.toggle("cursor-active", isC);
        if (cBadge) cBadge.classList.toggle("hidden", !isC);
        if (lCard) lCard.classList.toggle("cursor-active", isC);
      });
      if (changed) updateBrainMonitorLinks();
    }
    if (data.active_by_monitor) {
      [1, 2, 3].forEach((idx) => {
        const title = data.active_by_monitor[String(idx)];
        const wEl = document.getElementById(`l3s-win-${idx}`);
        const camWin = document.getElementById(`cam-mon-win-${idx}`);
        if (title) {
          if (wEl) wEl.textContent = title;
          if (camWin) camWin.textContent = title;
        }
      });
    }
  };

  // Monitor-Auflösungen im Live-Deck + Kommando-Dock unten rechts aktualisieren
  const prevApplyTopo = window.__applyMonitorTopology;
  window.__applyMonitorTopology = (msg) => {
    if (typeof prevApplyTopo === "function") prevApplyTopo(msg);
    if (msg && Array.isArray(msg.monitors)) {
      [1, 2, 3].forEach((idx) => {
        const card = document.getElementById(`l3s-card-${idx}`);
        const tile = document.getElementById(`cam-mon-${idx}`);
        const resEl = document.getElementById(`l3s-res-${idx}`);
        const camRes = document.getElementById(`cam-mon-res-${idx}`);
        const m = msg.monitors.find((x) => Number(x.index) === idx);
        if (card) card.style.display = m ? "flex" : "none";
        if (tile) tile.style.display = m ? "" : "none";
        if (resEl && m) resEl.textContent = `${m.w}x${m.h}`;
        if (camRes && m) camRes.textContent = `${m.w}×${m.h}`;
      });
      setTimeout(updateBrainMonitorLinks, 60);
    }
  };

  if (btnWorkMode) {
    btnWorkMode.addEventListener("click", (ev) => {
      ev.stopPropagation();
      postJson("/api/macros/run", { macro: "work_mode" }).catch(() => {});
    });
  }
  document.querySelectorAll("[data-mon-focus]").forEach((card) => {
    card.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const monId = Number(card.dataset.monFocus || 1);
      flashMonitorScanBar(monId, 1800);
      postJson("/api/monitors/highlight", { monitor: monId, label: `FOCUS M${monId}`, duration: 2.0 }).catch(() => {});
    });
  });

  function connectBinaryMonitorStream() {
    if (document.body.classList.contains("web-demo-mode")) return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const mws = new WebSocket(`${proto}//${window.location.host}/ws/monitors`);
    mws.binaryType = "arraybuffer";
    mws.onmessage = (evt) => {
      if (!(evt.data instanceof ArrayBuffer) || evt.data.byteLength < 8) return;
      const view = new DataView(evt.data);
      if (view.getUint8(0) !== 74 || view.getUint8(1) !== 77) return; // 'JM'
      const monIdx = view.getUint8(2);
      const flags = view.getUint8(3);
      const scanning = (flags & 1) !== 0;
      const isCursor = (flags & 2) !== 0;
      const fps = (view.getUint16(4, true) / 10.0).toFixed(1);
      // Echtzeit-FPS pro Monitor (Cursor-Monitor vs. Hintergrund-Monitore)
      const nowMs = performance.now();
      if (monFrameLastTs[monIdx] > 0) {
        const dtMs = Math.max(12, nowMs - monFrameLastTs[monIdx]);
        const inst = Math.min(60, 1000 / dtMs);
        monFpsEma[monIdx] = monFpsEma[monIdx] > 0 ? monFpsEma[monIdx] * 0.72 + inst * 0.28 : inst;
      }
      monFrameLastTs[monIdx] = nowMs;
      const perMonFps = monFpsEma[monIdx] > 0 ? monFpsEma[monIdx].toFixed(1) : fps;
      const camFpsEl = document.getElementById(`cam-mon-fps-${monIdx}`);
      if (camFpsEl) camFpsEl.textContent = `${perMonFps} FPS`;

      if (liveFpsEl) liveFpsEl.textContent = `${fps} FPS · WS`;
      if (el.camLiveText && _shouldShowDesktopVision()) {
        el.camLiveText.textContent = `${Math.round(Number(fps))} FPS`;
      }
      const jpgBlob = new Blob([evt.data.slice(6)], { type: "image/jpeg" });
      const nextUrl = URL.createObjectURL(jpgBlob);
      const img = document.getElementById(`l3s-img-${monIdx}`);
      const camImg = document.getElementById(`cam-mon-img-${monIdx}`);
      const card = document.getElementById(`l3s-card-${monIdx}`);
      const camTile = document.getElementById(`cam-mon-${monIdx}`);
      if (img) img.src = nextUrl;
      if (camImg) camImg.src = nextUrl;
      if (monBlobUrls[monIdx]) URL.revokeObjectURL(monBlobUrls[monIdx]);
      monBlobUrls[monIdx] = nextUrl;

      if (card) card.classList.toggle("scanning", scanning);
      if (camTile && !scanFlashTimers[monIdx]) camTile.classList.toggle("scanning", scanning);
      if (isCursor) {
        const cursorChanged = currentCursorMonitor !== monIdx;
        currentCursorMonitor = monIdx;
        [1, 2, 3].forEach((idx) => {
          const hit = idx === monIdx;
          const t = document.getElementById(`cam-mon-${idx}`);
          const p = document.getElementById(`cam-mon-cursor-${idx}`);
          const c = document.getElementById(`l3s-card-${idx}`);
          if (t) t.classList.toggle("cursor-active", hit);
          if (p) p.classList.toggle("hidden", !hit);
          if (c) c.classList.toggle("cursor-active", hit);
        });
        if (cursorChanged) updateBrainMonitorLinks();
      }
    };
    mws.onclose = () => setTimeout(connectBinaryMonitorStream, 2200);
    mws.onerror = () => { try { mws.close(); } catch (_e) {} };
  }
  connectBinaryMonitorStream();
})();
