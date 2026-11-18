(() => {
  "use strict";

  /* ============================================================
     ELEMENT-REFERENZEN & STATE
  ============================================================ */
  const $ = (id) => document.getElementById(id);

  function sanitizeOriginDefault() {
    try {
      const proto = window.location.protocol || "";
      const host = window.location.hostname || "";
      if (proto.startsWith("http") && host !== "localhost" && host !== "127.0.0.1") {
        return window.location.origin;
      }
      if (proto === "http:" && (host === "localhost" || host === "127.0.0.1") && window.location.port) {
        return window.location.origin;
      }
    } catch (_e) {}
    return "";
  }

  const SILENT_WAV_DATA_URI =
    "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";

  const S = {
    homeUrl: localStorage.getItem("jarvis_home_url") || sanitizeOriginDefault(),
    piUrl: localStorage.getItem("jarvis_pi_url") || "",
    activeBaseUrl: "",
    activeHostKind: "away",
    syncToken: localStorage.getItem("jarvis_sync_token") || "",
    pairedHostType: localStorage.getItem("jarvis_paired_host_type") || "",
    geminiKey: localStorage.getItem("jarvis_gemini_key") || "",
    groqKey: localStorage.getItem("jarvis_groq_key") || "",
    pollinationsEnabled: localStorage.getItem("jarvis_pollinations") !== "false",
    ttsEnabled: localStorage.getItem("jarvis_tts_enabled") !== "false",
    onlineVoice: localStorage.getItem("jarvis_online_voice") || "de-DE-KillianNeural",
    offlineVoice: localStorage.getItem("jarvis_offline_voice") || "auto",
    offlinePitch: parseFloat(localStorage.getItem("jarvis_offline_pitch") || "0.55"),
    offlineRate: parseFloat(localStorage.getItem("jarvis_offline_rate") || "0.88"),
    voiceClipsCount: 0,
    voicePackSyncedOnce: false,
    audioUnlocked: false,
    audioCtx: null,
    htmlAudio: null,
    currentAudioSource: null,
    speechStopFlag: false,
    isHomeLan: false,
    cameraActive: true,
    facingMode: "environment",
    torchActive: false,
    currentStream: null,
    videoTrack: null,
    attachedImageBase64: null,
    isScanning: false,
    isSpeaking: false,
    isListening: false,
    qrScanning: false,
    qrStream: null,
    qrTimerId: null,
    memories: JSON.parse(localStorage.getItem("jarvis_memory") || "[]"),
    pendingSync: JSON.parse(localStorage.getItem("jarvis_pending_sync") || "[]"),
    recognition: null,
  };

  const el = {
    radarBadge: $("radar-badge"),
    radarText: $("radar-text"),
    arContainer: $("ar-container"),
    video: $("camera-stream"),
    coreCanvas: $("core-canvas"),
    snapshotCanvas: $("snapshot-canvas"),
    laserScanline: $("laser-scanline"),
    camFacingLabel: $("cam-facing-label"),
    torchBadge: $("torch-badge"),
    torchBtnLabel: $("torch-btn-label"),
    camPowerLabel: $("cam-power-label"),
    btnCamSwitch: $("btn-cam-switch"),
    btnCamTorch: $("btn-cam-torch"),
    btnCamSnap: $("btn-cam-snap"),
    btnCamPower: $("btn-cam-power"),
    chatStream: $("chat-stream"),
    jarvisStatus: $("jarvis-status"),
    activeModelTag: $("active-model-tag"),
    memorySyncTag: $("memory-sync-tag"),
    textForm: $("text-form"),
    textInput: $("text-input"),
    btnSend: $("btn-send"),
    btnAttachPreview: $("btn-attach-preview"),
    attachedThumb: $("attached-thumb"),
    btnMic: $("btn-mic"),
    btnQuickSync: $("btn-quick-sync"),
    btnTtsToggle: $("btn-tts-toggle"),
    ttsLabel: $("tts-label"),
    settingsDrawer: $("settings-drawer"),
    btnSettingsToggle: $("btn-settings-toggle"),
    btnDrawerClose: $("btn-drawer-close"),
    cfgHomeUrl: $("cfg-home-url"),
    cfgPiUrl: $("cfg-pi-url"),
    cfgSyncToken: $("cfg-sync-token"),
    statPairingStatus: $("stat-pairing-status"),
    btnScanPairingQr: $("btn-scan-pairing-qr"),
    btnImportPairingJson: $("btn-import-pairing-json"),
    btnTestConnection: $("btn-test-connection"),
    cfgGeminiKey: $("cfg-gemini-key"),
    cfgGroqKey: $("cfg-groq-key"),
    cfgPollinations: $("cfg-pollinations-enabled"),
    statLocalMems: $("stat-local-mems"),
    statPendingMems: $("stat-pending-mems"),
    btnForceSync: $("btn-force-sync"),
    btnClearCache: $("btn-clear-cache"),
    btnSaveSettings: $("btn-save-settings"),
    cfgOnlineVoice: $("cfg-online-voice"),
    cfgOfflineVoice: $("cfg-offline-voice"),
    cfgOfflinePitch: $("cfg-offline-pitch"),
    lblOfflinePitch: $("lbl-offline-pitch"),
    cfgOfflineRate: $("cfg-offline-rate"),
    lblOfflineRate: $("lbl-offline-rate"),
    statVoiceClips: $("stat-voice-clips"),
    voicePackStatus: $("voice-pack-status"),
    btnTestVoice: $("btn-test-voice"),
    btnCloneVoicePack: $("btn-clone-voice-pack"),
    qrModal: $("qr-scanner-modal"),
    qrVideo: $("qr-video"),
    qrCanvas: $("qr-canvas"),
    qrScanStatus: $("qr-scan-status"),
    btnQrClose: $("btn-qr-close"),
  };

  /* ============================================================
     SERVICE WORKER REGISTRIERUNG (Offline-PWA)
  ============================================================ */
  if ("serviceWorker" in navigator && window.location.protocol.startsWith("http")) {
    window.addEventListener("load", () => {
      const swPath = window.location.pathname.includes("/mobile") ? "/mobile/sw.js" : "sw.js";
      navigator.serviceWorker.register(swPath).catch(() => {});
    });
  }

  /* ============================================================
     AUDIO-CONTEXT & HTML5 AUDIO UNLOCK BEIM ERSTEN TOUCH (Android WebView)
  ============================================================ */
  function ensureHtmlAudioElement() {
    if (!S.htmlAudio) {
      S.htmlAudio = new Audio();
      S.htmlAudio.preload = "auto";
      S.htmlAudio.setAttribute("playsinline", "true");
    }
    return S.htmlAudio;
  }

  function unlockAudioEngine() {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (AudioCtx) {
        if (!S.audioCtx) {
          S.audioCtx = new AudioCtx();
        }
        if (S.audioCtx.state === "suspended") {
          S.audioCtx.resume().catch(() => {});
        }
        if (!S.audioUnlocked && S.audioCtx.state === "running") {
          const buf = S.audioCtx.createBuffer(1, 1, 22050);
          const src = S.audioCtx.createBufferSource();
          src.buffer = buf;
          src.connect(S.audioCtx.destination);
          src.start(0);
        }
      }
    } catch (_e) {}

    try {
      const audioEl = ensureHtmlAudioElement();
      if (!S.audioUnlocked) {
        audioEl.muted = true;
        audioEl.src = SILENT_WAV_DATA_URI;
        const p = audioEl.play();
        if (p && typeof p.then === "function") {
          p.then(() => {
            audioEl.pause();
            audioEl.currentTime = 0;
            audioEl.muted = false;
            S.audioUnlocked = true;
          }).catch(() => {
            audioEl.muted = false;
          });
        } else {
          audioEl.muted = false;
          S.audioUnlocked = true;
        }
      }
    } catch (_e) {}

    try {
      if (window.speechSynthesis) {
        window.speechSynthesis.getVoices();
      }
    } catch (_e) {}
  }

  ["touchstart", "pointerdown", "mousedown", "click", "keydown"].forEach((evtName) => {
    document.addEventListener(evtName, unlockAudioEngine, { passive: true });
  });

  /* ============================================================
     KAMERA-STEUERUNG & AR-SCANNER
  ============================================================ */
  async function startCamera() {
    stopCamera();
    try {
      const constraints = {
        video: {
          facingMode: { ideal: S.facingMode },
          width: { ideal: 1920 },
          height: { ideal: 1080 },
        },
        audio: false,
      };

      const stream = await navigator.mediaDevices.getUserMedia(constraints);
      S.currentStream = stream;
      el.video.srcObject = stream;
      el.video.classList.remove("hidden");
      el.coreCanvas.classList.add("hidden");
      S.cameraActive = true;
      el.camPowerLabel.textContent = "🚫 CAM AUS";

      const tracks = stream.getVideoTracks();
      if (tracks.length > 0) {
        S.videoTrack = tracks[0];
        const caps = S.videoTrack.getCapabilities ? S.videoTrack.getCapabilities() : {};
        if (caps.facingMode && caps.facingMode.length > 0) {
          const current = caps.facingMode[0];
          el.camFacingLabel.textContent = current === "user" ? "FRONTKAMERA" : "RÜCKKAMERA (MAIN)";
        } else {
          el.camFacingLabel.textContent = S.facingMode === "user" ? "FRONTKAMERA" : "RÜCKKAMERA (MAIN)";
        }
      }
      resetTorch();
    } catch (err) {
      console.warn("Kamera konnte nicht gestartet werden:", err);
      el.camFacingLabel.textContent = "KAMERA NICHT VERFÜGBAR";
      switchToCoreVisualizer();
    }
  }

  function stopCamera() {
    if (S.currentStream) {
      S.currentStream.getTracks().forEach((t) => t.stop());
      S.currentStream = null;
      S.videoTrack = null;
    }
  }

  function resetTorch() {
    S.torchActive = false;
    el.torchBadge.className = "ar-badge torch-off";
    el.torchBadge.textContent = "🔦 TORCH: AUS";
    el.torchBtnLabel.textContent = "🔦 LICHT";
  }

  async function toggleTorch() {
    if (!S.videoTrack) return;
    try {
      const caps = S.videoTrack.getCapabilities ? S.videoTrack.getCapabilities() : {};
      if (!caps.torch) {
        alert("Taschenlampe/Torch wird auf diesem Kamerasensor nicht unterstützt.");
        return;
      }
      S.torchActive = !S.torchActive;
      await S.videoTrack.applyConstraints({
        advanced: [{ torch: S.torchActive }],
      });
      el.torchBadge.className = `ar-badge ${S.torchActive ? "torch-on" : "torch-off"}`;
      el.torchBadge.textContent = S.torchActive ? "🔦 TORCH: AN" : "🔦 TORCH: AUS";
      el.torchBtnLabel.textContent = S.torchActive ? "🔦 LICHT AUS" : "🔦 LICHT";
    } catch (err) {
      console.warn("Fehler beim Schalten der Taschenlampe:", err);
    }
  }

  function switchCameraFacing() {
    S.facingMode = S.facingMode === "environment" ? "user" : "environment";
    startCamera();
  }

  function toggleCameraPower() {
    if (S.cameraActive) {
      stopCamera();
      S.cameraActive = false;
      el.video.classList.add("hidden");
      el.coreCanvas.classList.remove("hidden");
      el.camPowerLabel.textContent = "📷 CAM EIN";
      el.camFacingLabel.textContent = "SENSOR DEAKTIVIERT";
      resetTorch();
    } else {
      startCamera();
    }
  }

  function snapFrame() {
    if (!S.cameraActive || !el.video.videoWidth) {
      return null;
    }
    const canvas = el.snapshotCanvas;
    canvas.width = el.video.videoWidth;
    canvas.height = el.video.videoHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(el.video, 0, 0, canvas.width, canvas.height);
    const b64 = canvas.toDataURL("image/jpeg", 0.85);

    S.attachedImageBase64 = b64;
    el.attachedThumb.src = b64;
    el.btnAttachPreview.classList.remove("hidden");

    triggerScanAnimation();
    return b64;
  }

  function triggerScanAnimation() {
    el.laserScanline.classList.remove("hidden");
    setTimeout(() => {
      if (!S.isScanning) {
        el.laserScanline.classList.add("hidden");
      }
    }, 2000);
  }

  function removeAttachedImage() {
    S.attachedImageBase64 = null;
    el.attachedThumb.src = "";
    el.btnAttachPreview.classList.add("hidden");
  }
  el.btnAttachPreview.addEventListener("click", removeAttachedImage);

  /* ============================================================
     3D HOLOGRAPHISCHER KERN (Standby-Visualizer bei Cam-Aus)
  ============================================================ */
  let coreAnimId = null;
  const SPH_POINTS = 380;
  const SPH = [];
  for (let i = 0; i < SPH_POINTS; i++) {
    const y = 1 - (i / (SPH_POINTS - 1)) * 2;
    const radius = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = 2.3999632 * i;
    SPH.push({ x: Math.cos(theta) * radius, y, z: Math.sin(theta) * radius });
  }

  function switchToCoreVisualizer() {
    S.cameraActive = false;
    el.video.classList.add("hidden");
    el.coreCanvas.classList.remove("hidden");
    el.camPowerLabel.textContent = "📷 CAM EIN";
    resizeCoreCanvas();
    if (!coreAnimId) renderCore();
  }

  function resizeCoreCanvas() {
    if (!el.arContainer) return;
    const rect = el.arContainer.getBoundingClientRect();
    el.coreCanvas.width = rect.width;
    el.coreCanvas.height = rect.height;
  }
  window.addEventListener("resize", () => {
    if (!S.cameraActive) resizeCoreCanvas();
  });

  let coreRotY = 0;
  function renderCore() {
    coreAnimId = requestAnimationFrame(renderCore);
    if (S.cameraActive) return;

    const ctx = el.coreCanvas.getContext("2d");
    const w = el.coreCanvas.width;
    const h = el.coreCanvas.height;
    if (!w || !h) return;

    ctx.clearRect(0, 0, w, h);
    const cx = w / 2;
    const cy = h / 2;
    const R = Math.min(w, h) * 0.35;

    coreRotY += 0.015;
    const cosY = Math.cos(coreRotY);
    const sinY = Math.sin(coreRotY);

    ctx.save();
    ctx.globalCompositeOperation = "lighter";

    ctx.strokeStyle = "rgba(255, 123, 41, 0.4)";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(cx, cy, R * 1.15, 0, Math.PI * 2);
    ctx.stroke();

    for (let i = 0; i < SPH_POINTS; i++) {
      const p = SPH[i];
      const x1 = p.x * cosY + p.z * sinY;
      const z1 = -p.x * sinY + p.z * cosY;
      const persp = 1 / (1 - z1 * 0.25);
      const px = cx + x1 * R * persp;
      const py = cy + p.y * R * persp;

      if (z1 < -0.4) continue;
      const rad = 1.2 + (z1 + 0.4) * 0.8;
      ctx.fillStyle = z1 > 0.3 ? "rgba(255, 200, 140, 0.95)" : "rgba(255, 123, 41, 0.6)";
      ctx.beginPath();
      ctx.arc(px, py, rad, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.fillStyle = "#ffffff";
    ctx.font = '800 12px "Orbitron", sans-serif';
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("CORE STANDBY", cx, cy);
    ctx.restore();
  }

  /* ============================================================
     1-KLICK QR-CODE-KOPPLUNG & VERBINDUNGS-RADAR (PC + PI + CLOUD)
  ============================================================ */
  function normalizeHttpUrl(rawUrl) {
    const clean = String(rawUrl || "").trim().replace(/\/+$/, "");
    if (!clean || !/^https?:\/\//i.test(clean)) return "";
    return clean;
  }

  function updatePairingStatusUi() {
    if (!el.statPairingStatus) return;
    if (S.syncToken || S.homeUrl || S.piUrl) {
      const parts = [];
      if (S.homeUrl) parts.push(`PC: ${S.homeUrl}`);
      if (S.piUrl) parts.push(`Pi: ${S.piUrl}`);
      if (S.syncToken) parts.push(`Token: ${S.syncToken}`);
      el.statPairingStatus.innerHTML = `<span style="color:#22c55e">✓ Gekoppelt (${parts.join(" | ")})</span>`;
    } else {
      el.statPairingStatus.textContent = "Kopplung: Noch nicht per QR verbunden";
    }
  }

  function getAuthHeaders(extra = {}) {
    const hdrs = { ...extra };
    if (S.syncToken) hdrs["X-Jarvis-Sync-Token"] = S.syncToken;
    return hdrs;
  }

  function applyPairingPayload(rawInput) {
    let data = rawInput;
    if (typeof rawInput === "string") {
      const txt = rawInput.trim();
      if (!txt) return false;
      try {
        data = JSON.parse(txt);
      } catch (_e) {
        if (/^https?:\/\//i.test(txt)) {
          data = { type: "JARVIS_PAIRING_V1", lan_url: txt };
        } else {
          return false;
        }
      }
    }
    if (!data || typeof data !== "object") return false;
    const isPairing =
      data.type === "JARVIS_PAIRING_V1" || Boolean(data.lan_url || data.pi_url || data.sync_token);
    if (!isPairing) return false;

    const lanUrl = normalizeHttpUrl(data.lan_url || "");
    const piUrl = normalizeHttpUrl(data.pi_url || "");
    const hostType = String(data.host_type || "").trim().toLowerCase();

    if (hostType === "pi" && piUrl) {
      S.piUrl = piUrl;
      if (lanUrl && lanUrl !== piUrl) S.homeUrl = lanUrl;
    } else {
      if (lanUrl) S.homeUrl = lanUrl;
      if (piUrl) S.piUrl = piUrl;
    }
    if (data.sync_token) S.syncToken = String(data.sync_token).trim();
    if (hostType) S.pairedHostType = hostType;
    if (data.groq_key) S.groqKey = String(data.groq_key).trim();
    if (data.gemini_key) S.geminiKey = String(data.gemini_key).trim();

    localStorage.setItem("jarvis_home_url", S.homeUrl);
    localStorage.setItem("jarvis_pi_url", S.piUrl);
    localStorage.setItem("jarvis_sync_token", S.syncToken);
    localStorage.setItem("jarvis_paired_host_type", S.pairedHostType);
    localStorage.setItem("jarvis_groq_key", S.groqKey);
    localStorage.setItem("jarvis_gemini_key", S.geminiKey);

    if (el.cfgHomeUrl) el.cfgHomeUrl.value = S.homeUrl;
    if (el.cfgPiUrl) el.cfgPiUrl.value = S.piUrl;
    if (el.cfgSyncToken) el.cfgSyncToken.value = S.syncToken;
    if (el.cfgGroqKey) el.cfgGroqKey.value = S.groqKey;
    if (el.cfgGeminiKey) el.cfgGeminiKey.value = S.geminiKey;

    updatePairingStatusUi();
    unlockAudioEngine();

    const summary =
      `✓ 1-Klick QR-Kopplung abgeschlossen! Verbunden mit ` +
      `${S.homeUrl || S.piUrl || "Cloud-Keys"}${S.piUrl ? ` (Pi: ${S.piUrl})` : ""}.`;
    addChatMessage("jarvis", summary);
    checkConnectionRadar().then(() => {
      syncWithHomeBase();
    });
    speakResponse("QR-Kopplung abgeschlossen, Chef. Alle Systeme und Keys sind synchronisiert.");
    return true;
  }

  function getCandidateHosts() {
    const list = [];
    const h = normalizeHttpUrl(S.homeUrl);
    const p = normalizeHttpUrl(S.piUrl);
    if (h) list.push({ url: h, kind: "pc" });
    if (p && p !== h) list.push({ url: p, kind: "pi" });
    return list;
  }

  async function probeHost(url) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 2000);
    try {
      const res = await fetch(`${url}/api/mobile/ping`, {
        signal: controller.signal,
        headers: getAuthHeaders({ Accept: "application/json" }),
      });
      clearTimeout(timeoutId);
      if (!res.ok) return null;
      return await res.json();
    } catch (_e) {
      clearTimeout(timeoutId);
      return null;
    }
  }

  async function checkConnectionRadar() {
    const candidates = getCandidateHosts();
    for (const cand of candidates) {
      const data = await probeHost(cand.url);
      if (data && data.ok !== false) {
        S.activeBaseUrl = cand.url;
        S.activeHostKind = cand.kind;
        setConnectionState(true, data.hostname || (cand.kind === "pi" ? "RASPBERRY PI" : "HOME PC"), cand.kind);
        if (data.model_label) {
          el.activeModelTag.textContent = data.model_label.replace("MODEL:", "PC:").trim();
        }
        if (S.pendingSync.length > 0) {
          syncWithHomeBase();
        }
        if (!S.voicePackSyncedOnce && S.voiceClipsCount < 5) {
          S.voicePackSyncedOnce = true;
          cloneVoicePackFromServer(true);
        }
        return true;
      }
    }

    S.activeBaseUrl = "";
    S.activeHostKind = "away";
    setConnectionState(false, null, "away");
    return false;
  }

  function setConnectionState(isHome, hostName, kind = "pc") {
    S.isHomeLan = isHome;
    if (isHome) {
      el.radarBadge.className = "radar-badge synced";
      const prefix = kind === "pi" ? "PI LAN SYNCED" : "HOME LAN SYNCED";
      el.radarText.textContent = `${prefix} (${hostName || "ONLINE"})`;
    } else {
      el.radarBadge.className = "radar-badge away";
      el.radarText.textContent = "AWAY // AUTARK CLOUD";
      if (S.groqKey && S.geminiKey) {
        el.activeModelTag.textContent = "PHONE: GROQ + GEMINI";
      } else if (S.groqKey) {
        el.activeModelTag.textContent = "PHONE: GROQ LLAMA 3.3";
      } else if (S.geminiKey) {
        el.activeModelTag.textContent = "PHONE: GEMINI 2.5";
      } else {
        el.activeModelTag.textContent = "PHONE: KEYLESS";
      }
    }
  }

  setInterval(checkConnectionRadar, 5000);
  checkConnectionRadar();

  /* ============================================================
     QR-SCANNER KAMERA-MODUS (BarcodeDetector + gebündeltes jsQR)
  ============================================================ */
  function stopQrScanner(restoreMainCam = true) {
    S.qrScanning = false;
    if (S.qrTimerId) {
      clearTimeout(S.qrTimerId);
      S.qrTimerId = null;
    }
    if (S.qrStream) {
      S.qrStream.getTracks().forEach((t) => t.stop());
      S.qrStream = null;
    }
    if (el.qrVideo) {
      el.qrVideo.srcObject = null;
    }
    if (el.qrModal) {
      el.qrModal.classList.add("hidden");
    }
    if (restoreMainCam && S.cameraActive) {
      startCamera();
    }
  }

  async function startQrScanner() {
    unlockAudioEngine();
    if (!el.qrModal || !el.qrVideo || !el.qrCanvas) return;

    const wasMainCam = S.cameraActive;
    stopCamera();
    el.qrModal.classList.remove("hidden");
    if (el.qrScanStatus) {
      el.qrScanStatus.textContent = "Starte QR-Scanner-Kamera…";
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false,
      });
      S.qrStream = stream;
      el.qrVideo.srcObject = stream;
      await el.qrVideo.play().catch(() => {});
      S.qrScanning = true;
      if (el.qrScanStatus) {
        el.qrScanStatus.textContent = "QR-Code aus der Windows-/Pi-App ins Zielviereck halten…";
      }

      const detector =
        "BarcodeDetector" in window
          ? new window.BarcodeDetector({ formats: ["qr_code"] })
          : null;
      const ctx = el.qrCanvas.getContext("2d", { willReadFrequently: true });

      const scanTick = async () => {
        if (!S.qrScanning) return;
        try {
          if (el.qrVideo.readyState >= 2 && el.qrVideo.videoWidth > 0) {
            let decodedText = "";

            if (detector) {
              try {
                const codes = await detector.detect(el.qrVideo);
                if (codes && codes.length > 0 && codes[0].rawValue) {
                  decodedText = codes[0].rawValue;
                }
              } catch (_e) {}
            }

            if (!decodedText && typeof window.jsQR === "function") {
              const vw = el.qrVideo.videoWidth;
              const vh = el.qrVideo.videoHeight;
              const scale = Math.min(1, 640 / Math.max(vw, vh));
              const cw = Math.max(120, Math.round(vw * scale));
              const ch = Math.max(120, Math.round(vh * scale));
              el.qrCanvas.width = cw;
              el.qrCanvas.height = ch;
              ctx.drawImage(el.qrVideo, 0, 0, cw, ch);
              const imgData = ctx.getImageData(0, 0, cw, ch);
              const qr = window.jsQR(imgData.data, cw, ch, { inversionAttempts: "attemptBoth" });
              if (qr && qr.data) {
                decodedText = qr.data;
              }
            }

            if (decodedText) {
              const ok = applyPairingPayload(decodedText);
              if (ok) {
                if (el.settingsDrawer) el.settingsDrawer.classList.add("hidden");
                stopQrScanner(wasMainCam);
                return;
              }
              if (el.qrScanStatus) {
                el.qrScanStatus.textContent = "QR erkannt, aber kein JARVIS-Kopplungspaket. Erneut versuchen…";
              }
            }
          }
        } catch (_e) {}

        if (S.qrScanning) {
          S.qrTimerId = setTimeout(scanTick, 160);
        }
      };

      S.qrTimerId = setTimeout(scanTick, 250);
    } catch (err) {
      if (el.qrScanStatus) {
        el.qrScanStatus.textContent = `Kamera-Fehler: ${err.message || err}. Bitte Kameraberechtigung erteilen.`;
      }
    }
  }

  /* ============================================================
     GEDÄCHTNIS-SYNC (memory.json <-> localStorage)
  ============================================================ */
  async function syncWithHomeBase() {
    const baseUrl = S.activeBaseUrl || normalizeHttpUrl(S.homeUrl) || normalizeHttpUrl(S.piUrl);
    if (!S.isHomeLan || !baseUrl) {
      updateSyncUi();
      return;
    }
    try {
      el.memorySyncTag.textContent = "SYNCING…";
      const payload = {
        mobile_memories: S.pendingSync,
        last_sync_timestamp: Date.now() / 1000,
      };

      const res = await fetch(`${baseUrl}/api/mobile/sync`, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        const data = await res.json();
        if (data.memories && Array.isArray(data.memories)) {
          S.memories = data.memories;
          localStorage.setItem("jarvis_memory", JSON.stringify(S.memories));
        }
        S.pendingSync = [];
        localStorage.setItem("jarvis_pending_sync", JSON.stringify([]));
        updateSyncUi();
        addChatMessage("jarvis", `✓ Gedächtnis mit Heim-Server abgeglichen (${S.memories.length} Fakten & Notizen).`);
      }
    } catch (err) {
      console.warn("Sync fehlgeschlagen:", err);
    } finally {
      updateSyncUi();
    }
  }

  function recordLocalMemory(text, category = "fact") {
    const item = {
      text,
      category,
      created_at: new Date().toLocaleString("de-DE"),
    };
    S.memories.push(item);
    S.pendingSync.push(item);
    localStorage.setItem("jarvis_memory", JSON.stringify(S.memories));
    localStorage.setItem("jarvis_pending_sync", JSON.stringify(S.pendingSync));
    updateSyncUi();
  }

  function updateSyncUi() {
    el.memorySyncTag.textContent = `SYNC: ${S.memories.length} NOTIZEN`;
    el.statLocalMems.textContent = String(S.memories.length);
    el.statPendingMems.textContent = String(S.pendingSync.length);
  }
  updateSyncUi();
  updatePairingStatusUi();

  /* ============================================================
     KI-ANFRAGEN: DUAL-ROUTING (PC / Raspberry Pi vs. Autark Unterwegs)
  ============================================================ */
  async function submitQuery(queryText, imgBase64 = null) {
    unlockAudioEngine();
    const text = (queryText || "").trim();
    const image = imgBase64 || S.attachedImageBase64;
    removeAttachedImage();

    if (!text && !image) return;

    addChatMessage("user", text || "Foto zur Analyse gesendet.", image);
    setJarvisStatus("busy", "VERARBEITE ANFRAGE…");
    if (image) triggerScanAnimation();

    let answer = "";
    let serverAudioB64 = "";
    let serverAudioMime = "audio/mp3";

    try {
      const targetBase = S.isHomeLan ? (S.activeBaseUrl || normalizeHttpUrl(S.homeUrl) || normalizeHttpUrl(S.piUrl)) : "";
      if (targetBase) {
        const payloadBody = JSON.stringify({
          text,
          image_base64: image,
          include_audio: true,
          voice: S.onlineVoice || "de-DE-KillianNeural",
          pitch: "-15Hz",
          rate: "-5%",
        });

        let res = await fetch(`${targetBase}/api/mobile_chat`, {
          method: "POST",
          headers: getAuthHeaders({ "Content-Type": "application/json" }),
          body: payloadBody,
        });
        if (res.status === 404) {
          res = await fetch(`${targetBase}/api/mobile/ask`, {
            method: "POST",
            headers: getAuthHeaders({ "Content-Type": "application/json" }),
            body: payloadBody,
          });
        }

        if (!res.ok) throw new Error(`Server-Fehler: ${res.status}`);
        const data = await res.json();
        answer = data.text || "Keine Antwort erhalten.";
        serverAudioB64 = data.audio_base64 || "";
        serverAudioMime = data.audio_mime || "audio/mp3";
      } else {
        answer = await handleAutarkMobileQuery(text, image);
      }

      addChatMessage("jarvis", answer);
      speakResponse(answer, serverAudioB64, serverAudioMime);

      if (/merk(?:e)?\s+dir|projektnotiz/i.test(text)) {
        recordLocalMemory(text.replace(/^(?:jarvis,?\s*)?(?:bitte\s*)?(?:merk(?:e)?\s+dir|notier(?:e)?)\s*/i, ""));
      }
    } catch (err) {
      console.error(err);
      const errMsg = `Chef, Verbindung unterbrochen: ${err.message || err}`;
      addChatMessage("jarvis", errMsg);
      speakResponse(errMsg);
    } finally {
      setJarvisStatus("ready", "STATUS: BEREIT");
      el.laserScanline.classList.add("hidden");
    }
  }

  /* ============================================================
     AUTARKES MOBILES SPRACHMODELL (Groq Llama 3.3 + Gemini 2.5 Flash)
  ============================================================ */
  function buildJarvisMobileSystemPrompt() {
    const context = S.memories.slice(-8).map((m) => m.text).join("; ");
    const pairedNote = S.pairedHostType
      ? `Gekoppelter Heim-Host: ${S.pairedHostType.toUpperCase()} (${S.homeUrl || S.piUrl || "Unterwegs"}). `
      : "";
    return (
      "Du bist J.A.R.V.I.S., der hochintelligente, ruhige und loyale KI-Assistent des Chefs auf seinem Smartphone. " +
      "Antworte direkt, präzise, kompetent und auf Deutsch. Vermeide lange Listen oder steife Floskeln, sodass deine Antworten sich beim Vorlesen natürlich anhören. " +
      pairedNote +
      (context ? `Bekannte Notizen & Fakten des Chefs: ${context}.` : "")
    );
  }

  async function callGeminiMobile(promptText, sysPrompt, imgBase64 = null) {
    if (!S.geminiKey) return null;
    const url = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=${encodeURIComponent(S.geminiKey)}`;
    const parts = [];
    if (imgBase64) {
      const cleanB64 = imgBase64.includes(",") ? imgBase64.split(",")[1] : imgBase64;
      parts.push({ text: `${sysPrompt}\n\nFrage des Chefs zum Live-Kamerabild: ${promptText || "Was siehst du?"}` });
      parts.push({ inline_data: { mime_type: "image/jpeg", data: cleanB64 } });
    } else {
      parts.push({ text: `${sysPrompt}\n\nChef: ${promptText}` });
    }
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contents: [{ role: "user", parts }] }),
    });
    if (!res.ok) return null;
    const json = await res.json();
    return json?.candidates?.[0]?.content?.parts?.[0]?.text || null;
  }

  async function callGroqMobile(promptText, sysPrompt) {
    if (!S.groqKey) return null;
    const url = "https://api.groq.com/openai/v1/chat/completions";
    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${S.groqKey}`,
      },
      body: JSON.stringify({
        model: "llama-3.3-70b-versatile",
        messages: [
          { role: "system", content: sysPrompt },
          { role: "user", content: promptText },
        ],
      }),
    });
    if (!res.ok) return null;
    const json = await res.json();
    return json?.choices?.[0]?.message?.content || null;
  }

  async function handleAutarkMobileQuery(text, imgBase64) {
    const sysPrompt = buildJarvisMobileSystemPrompt();

    // 1. Mit Bild & Gemini Key: Direktes Multimodal-Vision über Gemini 2.5 Flash
    if (imgBase64 && S.geminiKey) {
      const gemVis = await callGeminiMobile(text, sysPrompt, imgBase64);
      if (gemVis) return gemVis;
    }

    // 2. Nur Text & Groq Key: Ultra-schnelles LLaMA 3.3 70B
    if (!imgBase64 && S.groqKey) {
      const groqAns = await callGroqMobile(text, sysPrompt);
      if (groqAns) return groqAns;
    }

    // 3. Gemini 2.5 Flash Text-Fallback (falls nur Gemini-Key gekoppelt wurde oder Groq Limit erreicht hat)
    if (S.geminiKey) {
      const gemTxt = await callGeminiMobile(text, sysPrompt, null);
      if (gemTxt) return gemTxt;
    }

    // 4. Kostenloser Keyless Fallback über Pollinations AI
    if (S.pollinationsEnabled) {
      const fullPrompt = `${sysPrompt}\nChef: ${text || "Beschreibe kurz deine Fähigkeiten als mobiler Jarvis."}`;
      const url = `https://text.pollinations.ai/${encodeURIComponent(fullPrompt)}?model=openai`;
      const res = await fetch(url);
      if (res.ok) {
        return (await res.text()).trim();
      }
    }

    return "Kein API-Key hinterlegt und keine Verbindung zum Heim-Server. Bitte scanne den QR-Code aus der Windows-/Pi-App oder trage einen Groq-/Gemini-Key ein.";
  }

  /* ============================================================
     CHAT-LOG & UI-HELFER
  ============================================================ */
  function addChatMessage(role, text, imageThumb = null) {
    const bubble = document.createElement("div");
    bubble.className = `msg-bubble ${role}`;

    const head = document.createElement("div");
    head.className = "msg-head";
    head.textContent = role === "jarvis" ? "JARVIS" : "CHEF";
    bubble.appendChild(head);

    if (imageThumb) {
      const thumbWrap = document.createElement("div");
      thumbWrap.className = "msg-thumb-wrap";
      const img = document.createElement("img");
      img.src = imageThumb;
      thumbWrap.appendChild(img);
      bubble.appendChild(thumbWrap);
    }

    const body = document.createElement("div");
    body.className = "msg-body";
    body.textContent = text;
    bubble.appendChild(body);

    const timeEl = document.createElement("div");
    timeEl.className = "msg-time";
    timeEl.textContent = new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    bubble.appendChild(timeEl);

    el.chatStream.appendChild(bubble);
    el.chatStream.scrollTop = el.chatStream.scrollHeight;
  }

  function setJarvisStatus(state, text) {
    el.jarvisStatus.className = `status-tag ${state}`;
    el.jarvisStatus.textContent = text;
  }

  /* ============================================================
     SPRACHEINGABE (Speech Recognition)
  ============================================================ */
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SpeechRec) {
    const rec = new SpeechRec();
    rec.lang = "de-DE";
    rec.continuous = false;
    rec.interimResults = false;

    rec.onstart = () => {
      S.isListening = true;
      el.btnMic.classList.add("listening");
      setJarvisStatus("busy", "HÖRE ZU…");
    };

    rec.onresult = (evt) => {
      const transcript = evt.results[0][0].transcript;
      el.textInput.value = transcript;
      if (S.attachedImageBase64) {
        submitQuery(transcript, S.attachedImageBase64);
        el.textInput.value = "";
      }
    };

    rec.onerror = () => {
      stopListening();
    };

    rec.onend = () => {
      stopListening();
    };

    S.recognition = rec;
  }

  function startListening() {
    unlockAudioEngine();
    if (!S.recognition) {
      alert("Spracherkennung im mobilen Browser nicht verfügbar. Bitte tippe deinen Befehl ein.");
      return;
    }
    try {
      S.recognition.start();
    } catch (_e) {
      S.recognition.stop();
    }
  }

  function stopListening() {
    S.isListening = false;
    el.btnMic.classList.remove("listening");
    setJarvisStatus("ready", "STATUS: BEREIT");
  }

  el.btnMic.addEventListener("click", () => {
    unlockAudioEngine();
    if (S.isListening) {
      if (S.recognition) S.recognition.stop();
    } else {
      startListening();
    }
  });

  /* ============================================================
     3-STUFEN MOBILE VOICE ENGINE
     Stufe 1: Server-Voice-Stream (de-DE-KillianNeural MP3 / Piper) + IndexedDB Cache
     Stufe 2: Unterwegs Online-TTS (Google Translate TTS 180-Zeichen-Sätze)
     Stufe 3: Cordova / Web Speech API (de-DE + getVoices() Reload)
  ============================================================ */
  const VOICE_DB_NAME = "jarvis_voice_cache_db";
  const VOICE_STORE_NAME = "voice_clips";

  function normalizeVoiceKey(text) {
    return String(text || "")
      .replace(/[*#_`]/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase();
  }

  function openVoiceCacheDb() {
    return new Promise((resolve, reject) => {
      if (!window.indexedDB) {
        reject(new Error("IndexedDB nicht verfügbar"));
        return;
      }
      const req = window.indexedDB.open(VOICE_DB_NAME, 1);
      req.onupgradeneeded = (evt) => {
        const db = evt.target.result;
        if (!db.objectStoreNames.contains(VOICE_STORE_NAME)) {
          db.createObjectStore(VOICE_STORE_NAME, { keyPath: "key" });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function getCachedVoiceClip(text) {
    try {
      const db = await openVoiceCacheDb();
      const key = normalizeVoiceKey(text);
      return await new Promise((resolve) => {
        const tx = db.transaction(VOICE_STORE_NAME, "readonly");
        const req = tx.objectStore(VOICE_STORE_NAME).get(key);
        req.onsuccess = () => {
          if (!req.result) return resolve(null);
          resolve({
            audio_b64: req.result.audio_b64,
            mime: req.result.mime || "audio/wav",
          });
        };
        req.onerror = () => resolve(null);
      });
    } catch (_e) {
      return null;
    }
  }

  async function setCachedVoiceClip(text, audioB64, voice = "de-DE-KillianNeural", mime = "audio/mp3") {
    if (!text || !audioB64) return;
    try {
      const db = await openVoiceCacheDb();
      const key = normalizeVoiceKey(text);
      await new Promise((resolve) => {
        const tx = db.transaction(VOICE_STORE_NAME, "readwrite");
        tx.objectStore(VOICE_STORE_NAME).put({
          key,
          text: String(text).trim(),
          audio_b64: audioB64,
          mime,
          voice,
          updated_at: Date.now(),
        });
        tx.oncomplete = () => resolve(true);
        tx.onerror = () => resolve(false);
      });
      await refreshVoiceClipCount();
    } catch (_e) {}
  }

  async function refreshVoiceClipCount() {
    try {
      const db = await openVoiceCacheDb();
      const cnt = await new Promise((resolve) => {
        const tx = db.transaction(VOICE_STORE_NAME, "readonly");
        const req = tx.objectStore(VOICE_STORE_NAME).count();
        req.onsuccess = () => resolve(req.result || 0);
        req.onerror = () => resolve(0);
      });
      S.voiceClipsCount = cnt;
      if (el.statVoiceClips) el.statVoiceClips.textContent = String(cnt);
    } catch (_e) {}
  }

  async function clearVoiceCacheDb() {
    try {
      const db = await openVoiceCacheDb();
      await new Promise((resolve) => {
        const tx = db.transaction(VOICE_STORE_NAME, "readwrite");
        tx.objectStore(VOICE_STORE_NAME).clear();
        tx.oncomplete = () => resolve(true);
        tx.onerror = () => resolve(false);
      });
      await refreshVoiceClipCount();
    } catch (_e) {}
  }

  async function cloneVoicePackFromServer(silent = false) {
    const baseUrl = S.activeBaseUrl || normalizeHttpUrl(S.homeUrl) || normalizeHttpUrl(S.piUrl);
    if (!baseUrl) return false;
    if (el.voicePackStatus && !silent) {
      el.voicePackStatus.textContent = "Klone Jarvis-Stimme vom Server…";
    }
    try {
      const res = await fetch(
        `${baseUrl}/api/mobile/voice-pack?voice=${encodeURIComponent(S.onlineVoice)}&pitch=-15Hz&rate=-5%`,
        { headers: getAuthHeaders() }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const clips = data.clips || {};
      let stored = 0;
      for (const [phrase, b64] of Object.entries(clips)) {
        if (b64) {
          await setCachedVoiceClip(phrase, b64, data.voice || S.onlineVoice, "audio/wav");
          stored++;
        }
      }
      S.voicePackSyncedOnce = true;
      await refreshVoiceClipCount();
      if (el.voicePackStatus) {
        el.voicePackStatus.innerHTML = `<span style="color:#22c55e">✓ ${stored} Sätze in Original-Stimme (${data.voice || S.onlineVoice}) offline geklont</span>`;
      }
      return stored > 0;
    } catch (err) {
      if (el.voicePackStatus && !silent) {
        el.voicePackStatus.innerHTML = `<span style="color:#ffb852">⚠ Server nicht erreichbar (${err.message || err})</span>`;
      }
      return false;
    }
  }

  function stopActiveSpeech() {
    S.speechStopFlag = true;
    try {
      if (S.htmlAudio) {
        S.htmlAudio.pause();
        S.htmlAudio.removeAttribute("src");
        S.htmlAudio.load();
      }
    } catch (_e) {}
    try {
      if (S.currentAudioSource) {
        S.currentAudioSource.stop();
        S.currentAudioSource.disconnect();
        S.currentAudioSource = null;
      }
    } catch (_e) {}
    try {
      if (window.speechSynthesis) window.speechSynthesis.cancel();
    } catch (_e) {}
    S.isSpeaking = false;
  }

  async function playHtmlAudioUrl(srcUrl, timeoutMs = 15000) {
    const audio = ensureHtmlAudioElement();
    return new Promise((resolve) => {
      let settled = false;
      const done = (ok) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        audio.onended = null;
        audio.onerror = null;
        S.isSpeaking = false;
        resolve(ok);
      };
      const timer = setTimeout(() => done(false), timeoutMs);
      audio.onended = () => done(true);
      audio.onerror = () => done(false);
      audio.src = srcUrl;
      audio.muted = false;
      S.isSpeaking = true;
      const playPromise = audio.play();
      if (playPromise && typeof playPromise.then === "function") {
        playPromise.catch(() => done(false));
      }
    });
  }

  async function playAudioBase64(audioB64, mimeType = "audio/mp3") {
    if (!audioB64) return false;
    stopActiveSpeech();
    S.speechStopFlag = false;
    const mime = mimeType || "audio/mp3";

    // Primärer Android-WebView-Pfad über entsperrtes HTML5 <audio>
    try {
      const okHtml = await playHtmlAudioUrl(`data:${mime};base64,${audioB64}`, 30000);
      if (okHtml) return true;
    } catch (_e) {}

    // Sekundärer Pfad über WebAudio API (inkl. leichtem Bass-Shelf)
    try {
      const binaryStr = window.atob(audioB64);
      const len = binaryStr.length;
      const bytes = new Uint8Array(len);
      for (let i = 0; i < len; i++) {
        bytes[i] = binaryStr.charCodeAt(i);
      }

      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (AudioCtx) {
        if (!S.audioCtx) S.audioCtx = new AudioCtx();
        if (S.audioCtx.state === "suspended") {
          await S.audioCtx.resume();
        }
        const audioBuffer = await S.audioCtx.decodeAudioData(bytes.buffer.slice(0));
        const source = S.audioCtx.createBufferSource();
        source.buffer = audioBuffer;

        const bassFilter = S.audioCtx.createBiquadFilter();
        bassFilter.type = "lowshelf";
        bassFilter.frequency.value = 170;
        bassFilter.gain.value = 2.2;

        source.connect(bassFilter);
        bassFilter.connect(S.audioCtx.destination);

        S.currentAudioSource = source;
        S.isSpeaking = true;
        return await new Promise((resolve) => {
          source.onended = () => {
            S.isSpeaking = false;
            if (S.currentAudioSource === source) S.currentAudioSource = null;
            resolve(true);
          };
          source.start(0);
        });
      }
    } catch (err) {
      console.warn("AudioContext-Wiedergabe fehlgeschlagen:", err);
      S.isSpeaking = false;
    }
    return false;
  }

  async function fetchServerTtsAudio(cleanText) {
    const baseUrl = S.activeBaseUrl || normalizeHttpUrl(S.homeUrl) || normalizeHttpUrl(S.piUrl);
    if (!baseUrl) return null;
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 7000);
      const res = await fetch(`${baseUrl}/api/tts_audio`, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          text: cleanText,
          voice: S.onlineVoice || "de-DE-KillianNeural",
          pitch: "-15Hz",
          rate: "-5%",
        }),
        signal: controller.signal,
      });
      clearTimeout(timeoutId);
      if (res.ok) {
        const json = await res.json();
        if (json && json.audio_base64) {
          const mime = json.audio_mime || "audio/mp3";
          await setCachedVoiceClip(cleanText, json.audio_base64, json.voice || S.onlineVoice, mime);
          return { b64: json.audio_base64, mime };
        }
      }
    } catch (_e) {}

    // Fallback auf Legacy-/tts WAV Endpunkt
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5500);
      const res = await fetch(`${baseUrl}/tts`, {
        method: "POST",
        headers: getAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          text: cleanText,
          voice: S.onlineVoice || "de-DE-KillianNeural",
          pitch: "-15Hz",
          rate: "-5%",
        }),
        signal: controller.signal,
      });
      clearTimeout(timeoutId);
      if (!res.ok) return null;
      const buf = await res.arrayBuffer();
      if (!buf || buf.byteLength < 64) return null;
      const bytes = new Uint8Array(buf);
      let binary = "";
      const chunkSize = 0x8000;
      for (let i = 0; i < bytes.length; i += chunkSize) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
      }
      const b64 = window.btoa(binary);
      await setCachedVoiceClip(cleanText, b64, S.onlineVoice, "audio/wav");
      return { b64, mime: "audio/wav" };
    } catch (_e) {
      return null;
    }
  }

  function splitTextIntoTtsChunks(text, maxLen = 180) {
    const clean = String(text || "").replace(/\s+/g, " ").trim();
    if (!clean) return [];
    const chunks = [];
    let rest = clean;
    while (rest.length > maxLen) {
      let cut = -1;
      for (const sep of [". ", "! ", "? ", "; ", ", ", " "]) {
        const idx = rest.lastIndexOf(sep, maxLen);
        if (idx > 25) {
          cut = idx + (sep === " " ? 0 : 1);
          break;
        }
      }
      if (cut <= 0) cut = maxLen;
      chunks.push(rest.slice(0, cut).trim());
      rest = rest.slice(cut).trim();
    }
    if (rest) chunks.push(rest);
    return chunks;
  }

  async function speakViaGoogleTranslateTts(cleanText) {
    if (!navigator.onLine) return false;
    const chunks = splitTextIntoTtsChunks(cleanText, 180);
    if (!chunks.length) return false;

    S.speechStopFlag = false;
    for (const chunk of chunks) {
      if (S.speechStopFlag || !S.ttsEnabled) return true;
      const ttsUrl =
        `https://translate.google.com/translate_tts?ie=UTF-8&tl=de&client=tw-ob&q=${encodeURIComponent(chunk)}`;

      let played = false;
      try {
        const controller = new AbortController();
        const tid = setTimeout(() => controller.abort(), 4500);
        const res = await fetch(ttsUrl, { signal: controller.signal });
        clearTimeout(tid);
        if (res.ok) {
          const blob = await res.blob();
          if (blob && blob.size > 128) {
            const objUrl = URL.createObjectURL(blob);
            played = await playHtmlAudioUrl(objUrl, 15000);
            URL.revokeObjectURL(objUrl);
          }
        }
      } catch (_e) {}

      if (!played) {
        played = await playHtmlAudioUrl(ttsUrl, 12000);
      }
      if (!played) return false;
    }
    return true;
  }

  function scoreGermanMaleVoice(v) {
    const id = `${v.name || ""} ${v.voiceURI || ""} ${v.lang || ""}`.toLowerCase();
    if (!id.includes("de")) return -999;
    let score = 10;
    if (v.localService) score += 25;
    if (id.includes("de-de-x-deg")) score += 120;
    if (id.includes("de-de-x-deb")) score += 100;
    if (id.includes("de-de-x-nfh")) score += 90;
    if (id.includes("killian")) score += 150;
    if (id.includes("conrad")) score += 145;
    if (
      id.includes("stefan") ||
      id.includes("klaus") ||
      id.includes("markus") ||
      id.includes("martin") ||
      id.includes("yannick") ||
      id.includes("viktor")
    ) {
      score += 85;
    }
    if (id.includes("male") || id.includes("männlich")) score += 60;
    if (
      id.includes("female") ||
      id.includes("weiblich") ||
      id.includes("hedda") ||
      id.includes("katja") ||
      id.includes("amala") ||
      id.includes("serafina") ||
      id.includes("anna") ||
      id.includes("helena") ||
      id.includes("petra") ||
      id.includes("de-de-x-sfg") ||
      id.includes("de-de-x-sfh")
    ) {
      score -= 80;
    }
    return score;
  }

  async function loadSystemVoicesWithReload() {
    if (!window.speechSynthesis) return [];
    let voices = window.speechSynthesis.getVoices() || [];
    if (voices.length > 0) return voices;

    await new Promise((resolve) => {
      let resolved = false;
      const finish = () => {
        if (resolved) return;
        resolved = true;
        resolve();
      };
      const prevHandler = window.speechSynthesis.onvoiceschanged;
      window.speechSynthesis.onvoiceschanged = () => {
        if (typeof prevHandler === "function") prevHandler();
        finish();
      };
      setTimeout(finish, 350);
    });
    voices = window.speechSynthesis.getVoices() || [];
    return voices;
  }

  async function pickDeepGermanOfflineVoice() {
    const voices = await loadSystemVoicesWithReload();
    if (!voices.length) return null;

    if (S.offlineVoice && S.offlineVoice !== "auto") {
      const exact = voices.find((v) => v.voiceURI === S.offlineVoice || v.name === S.offlineVoice);
      if (exact) return exact;
    }

    const deVoices = voices.filter((v) => (v.lang || "").toLowerCase().startsWith("de"));
    if (!deVoices.length) return voices[0] || null;

    deVoices.sort((a, b) => scoreGermanMaleVoice(b) - scoreGermanMaleVoice(a));
    return deVoices[0];
  }

  function populateOfflineVoiceSelect() {
    if (!el.cfgOfflineVoice || !window.speechSynthesis) return;
    const voices = (window.speechSynthesis.getVoices() || []).filter((v) =>
      (v.lang || "").toLowerCase().startsWith("de")
    );
    voices.sort((a, b) => scoreGermanMaleVoice(b) - scoreGermanMaleVoice(a));

    const currentVal = S.offlineVoice || "auto";
    el.cfgOfflineVoice.innerHTML =
      '<option value="auto">Automatisch: Tiefste männliche DE-Stimme (de-de-x-deg / Killian)</option>';

    for (const v of voices) {
      const opt = document.createElement("option");
      opt.value = v.voiceURI || v.name;
      const tag = v.localService ? "Offline" : "Online";
      opt.textContent = `${v.name} (${v.lang} // ${tag})`;
      if (opt.value === currentVal) opt.selected = true;
      el.cfgOfflineVoice.appendChild(opt);
    }
  }

  if (window.speechSynthesis) {
    window.speechSynthesis.onvoiceschanged = populateOfflineVoiceSelect;
    setTimeout(populateOfflineVoiceSelect, 300);
  }

  async function speakViaOfflineSystemTts(cleanText) {
    // Cordova / Android Native TTS Plugin Pfad (falls installiert)
    if (window.TTS && typeof window.TTS.speak === "function") {
      S.isSpeaking = true;
      window.TTS.speak(
        {
          text: cleanText,
          locale: "de-DE",
          pitch: Math.max(0.5, Math.min(0.8, S.offlinePitch || 0.55)),
          rate: Math.max(0.75, Math.min(1.0, S.offlineRate || 0.88)),
        },
        () => {
          S.isSpeaking = false;
        },
        () => {
          S.isSpeaking = false;
        }
      );
      return;
    }

    // Web Speech API mit explizitem getVoices()-Reload & lang = "de-DE"
    if (!window.speechSynthesis) return;
    try {
      window.speechSynthesis.cancel();
      const bestVoice = await pickDeepGermanOfflineVoice();
      const utterance = new SpeechSynthesisUtterance(cleanText);
      utterance.lang = "de-DE";
      utterance.pitch = Math.max(0.5, Math.min(0.8, S.offlinePitch || 0.55));
      utterance.rate = Math.max(0.75, Math.min(1.0, S.offlineRate || 0.88));
      if (bestVoice) utterance.voice = bestVoice;

      S.isSpeaking = true;
      utterance.onend = () => {
        S.isSpeaking = false;
      };
      utterance.onerror = () => {
        S.isSpeaking = false;
      };
      window.speechSynthesis.speak(utterance);
    } catch (err) {
      console.warn("Offline-TTS Fehler:", err);
      S.isSpeaking = false;
    }
  }

  async function speakResponse(text, preloadedAudioB64 = "", preloadedMime = "audio/mp3") {
    if (!S.ttsEnabled) return;
    const clean = String(text || "")
      .replace(/[*#_`]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    if (!clean) return;

    // STUFE 1A: Direkt mitgeliefertes Base64-Audio vom PC/Pi-Server abspielen & offline cachen
    if (preloadedAudioB64) {
      await setCachedVoiceClip(clean, preloadedAudioB64, S.onlineVoice, preloadedMime || "audio/mp3");
      const played = await playAudioBase64(preloadedAudioB64, preloadedMime || "audio/mp3");
      if (played) return;
    }

    // STUFE 1B: Lokaler IndexedDB-Stimmen-Cache (sofort offline)
    const cached = await getCachedVoiceClip(clean);
    if (cached && cached.audio_b64) {
      const played = await playAudioBase64(cached.audio_b64, cached.mime || "audio/mp3");
      if (played) return;
    }

    // STUFE 1C: Live Server-Voice-Stream über POST /api/tts_audio (de-DE-KillianNeural / Piper)
    if (S.isHomeLan) {
      const srvAudio = await fetchServerTtsAudio(clean);
      if (srvAudio && srvAudio.b64) {
        const played = await playAudioBase64(srvAudio.b64, srvAudio.mime || "audio/mp3");
        if (played) return;
      }
    }

    // STUFE 2: Unterwegs-Stimme über Online Google Translate TTS Stream (180-Zeichen-Sätze)
    const cloudPlayed = await speakViaGoogleTranslateTts(clean);
    if (cloudPlayed) return;

    // STUFE 3: Cordova / WebView System-TTS (de-DE + getVoices() Reload)
    await speakViaOfflineSystemTts(clean);
  }

  el.btnTtsToggle.addEventListener("click", () => {
    unlockAudioEngine();
    S.ttsEnabled = !S.ttsEnabled;
    localStorage.setItem("jarvis_tts_enabled", String(S.ttsEnabled));
    el.ttsLabel.textContent = S.ttsEnabled ? "🔊 STIMME" : "🔇 STUMM";
    if (!S.ttsEnabled) stopActiveSpeech();
  });

  if (el.cfgOfflinePitch) {
    el.cfgOfflinePitch.addEventListener("input", () => {
      const v = parseFloat(el.cfgOfflinePitch.value || "0.55");
      if (el.lblOfflinePitch) el.lblOfflinePitch.textContent = v.toFixed(2);
    });
  }

  if (el.cfgOfflineRate) {
    el.cfgOfflineRate.addEventListener("input", () => {
      const v = parseFloat(el.cfgOfflineRate.value || "0.88");
      if (el.lblOfflineRate) el.lblOfflineRate.textContent = v.toFixed(2);
    });
  }

  if (el.btnTestVoice) {
    el.btnTestVoice.addEventListener("click", () => {
      unlockAudioEngine();
      speakResponse("Systemcheck abgeschlossen, Chef. Meine deutsche Stimme ist auf dem Smartphone aktiv.");
    });
  }

  if (el.btnCloneVoicePack) {
    el.btnCloneVoicePack.addEventListener("click", async () => {
      unlockAudioEngine();
      await cloneVoicePackFromServer(false);
    });
  }

  /* ============================================================
     EVENT-LISTENER
  ============================================================ */
  el.btnCamSwitch.addEventListener("click", switchCameraFacing);
  el.btnCamTorch.addEventListener("click", toggleTorch);
  el.btnCamPower.addEventListener("click", toggleCameraPower);

  el.btnCamSnap.addEventListener("click", () => {
    unlockAudioEngine();
    const snap = snapFrame();
    if (snap) {
      const txt = el.textInput.value.trim();
      if (txt) {
        submitQuery(txt, snap);
        el.textInput.value = "";
      } else {
        startListening();
      }
    }
  });

  el.textForm.addEventListener("submit", (e) => {
    e.preventDefault();
    unlockAudioEngine();
    const val = el.textInput.value.trim();
    if (val || S.attachedImageBase64) {
      submitQuery(val, S.attachedImageBase64);
      el.textInput.value = "";
    }
  });

  el.btnQuickSync.addEventListener("click", () => {
    unlockAudioEngine();
    syncWithHomeBase();
  });

  if (el.btnScanPairingQr) {
    el.btnScanPairingQr.addEventListener("click", startQrScanner);
  }
  if (el.btnQrClose) {
    el.btnQrClose.addEventListener("click", () => stopQrScanner(true));
  }
  if (el.btnImportPairingJson) {
    el.btnImportPairingJson.addEventListener("click", () => {
      const raw = prompt("Kopplungs-JSON aus der Windows-/Pi-App hier einfügen:");
      if (raw && !applyPairingPayload(raw)) {
        alert("Ungültiges JARVIS-Kopplungspaket.");
      }
    });
  }

  // Settings Drawer Toggle
  el.btnSettingsToggle.addEventListener("click", () => {
    unlockAudioEngine();
    el.cfgHomeUrl.value = S.homeUrl;
    if (el.cfgPiUrl) el.cfgPiUrl.value = S.piUrl;
    if (el.cfgSyncToken) el.cfgSyncToken.value = S.syncToken;
    el.cfgGeminiKey.value = S.geminiKey;
    el.cfgGroqKey.value = S.groqKey;
    el.cfgPollinations.checked = S.pollinationsEnabled;
    if (el.cfgOnlineVoice) el.cfgOnlineVoice.value = S.onlineVoice;
    populateOfflineVoiceSelect();
    if (el.cfgOfflineVoice) el.cfgOfflineVoice.value = S.offlineVoice;
    if (el.cfgOfflinePitch) el.cfgOfflinePitch.value = String(S.offlinePitch);
    if (el.lblOfflinePitch) el.lblOfflinePitch.textContent = Number(S.offlinePitch).toFixed(2);
    if (el.cfgOfflineRate) el.cfgOfflineRate.value = String(S.offlineRate);
    if (el.lblOfflineRate) el.lblOfflineRate.textContent = Number(S.offlineRate).toFixed(2);
    updatePairingStatusUi();
    refreshVoiceClipCount();
    updateSyncUi();
    el.settingsDrawer.classList.remove("hidden");

    const apkProbeBase = normalizeHttpUrl(S.activeBaseUrl || S.homeUrl || S.piUrl);
    if (apkProbeBase) {
      fetch(`${apkProbeBase}/api/apk/status`, { headers: getAuthHeaders() })
        .then((r) => r.json())
        .then((res) => {
          const st = $("mobile-apk-status");
          const btn = $("btn-mobile-dl-apk");
          if (btn && res.download_url) btn.href = res.download_url;
          if (st) {
            if (res.available) {
              st.innerHTML = `<span style="color:#22c55e">🟢 APK bereit (${res.size_mb} MB)</span>`;
            } else {
              st.innerHTML = `<span style="color:#ffb852">⚪ Auf PC noch nicht gebaut (via 4_Build_Android_APK.bat)</span>`;
            }
          }
        })
        .catch(() => {});
    }
  });

  el.btnDrawerClose.addEventListener("click", () => {
    el.settingsDrawer.classList.add("hidden");
  });

  el.btnTestConnection.addEventListener("click", async () => {
    S.homeUrl = normalizeHttpUrl(el.cfgHomeUrl.value) || sanitizeOriginDefault();
    if (el.cfgPiUrl) S.piUrl = normalizeHttpUrl(el.cfgPiUrl.value);
    if (el.cfgSyncToken) S.syncToken = el.cfgSyncToken.value.trim();
    localStorage.setItem("jarvis_home_url", S.homeUrl);
    localStorage.setItem("jarvis_pi_url", S.piUrl);
    localStorage.setItem("jarvis_sync_token", S.syncToken);
    updatePairingStatusUi();
    await checkConnectionRadar();
    alert(S.isHomeLan ? `✓ Verbindung hergestellt (${S.activeBaseUrl})!` : "Keine Verbindung zu Windows-PC oder Pi gefunden.");
  });

  el.btnForceSync.addEventListener("click", async () => {
    await syncWithHomeBase();
    await cloneVoicePackFromServer(false);
  });

  el.btnClearCache.addEventListener("click", async () => {
    if (confirm("Lokalen Offline-Gedächtnis- und Stimmen-Cache auf dem Handy leeren?")) {
      S.memories = [];
      S.pendingSync = [];
      localStorage.removeItem("jarvis_memory");
      localStorage.removeItem("jarvis_pending_sync");
      await clearVoiceCacheDb();
      updateSyncUi();
    }
  });

  el.btnSaveSettings.addEventListener("click", () => {
    S.homeUrl = normalizeHttpUrl(el.cfgHomeUrl.value) || sanitizeOriginDefault();
    if (el.cfgPiUrl) S.piUrl = normalizeHttpUrl(el.cfgPiUrl.value);
    if (el.cfgSyncToken) S.syncToken = el.cfgSyncToken.value.trim();
    S.geminiKey = el.cfgGeminiKey.value.trim();
    S.groqKey = el.cfgGroqKey.value.trim();
    S.pollinationsEnabled = el.cfgPollinations.checked;
    if (el.cfgOnlineVoice) S.onlineVoice = el.cfgOnlineVoice.value || "de-DE-KillianNeural";
    if (el.cfgOfflineVoice) S.offlineVoice = el.cfgOfflineVoice.value || "auto";
    if (el.cfgOfflinePitch) S.offlinePitch = parseFloat(el.cfgOfflinePitch.value || "0.55");
    if (el.cfgOfflineRate) S.offlineRate = parseFloat(el.cfgOfflineRate.value || "0.88");

    localStorage.setItem("jarvis_home_url", S.homeUrl);
    localStorage.setItem("jarvis_pi_url", S.piUrl);
    localStorage.setItem("jarvis_sync_token", S.syncToken);
    localStorage.setItem("jarvis_gemini_key", S.geminiKey);
    localStorage.setItem("jarvis_groq_key", S.groqKey);
    localStorage.setItem("jarvis_pollinations", String(S.pollinationsEnabled));
    localStorage.setItem("jarvis_online_voice", S.onlineVoice);
    localStorage.setItem("jarvis_offline_voice", S.offlineVoice);
    localStorage.setItem("jarvis_offline_pitch", String(S.offlinePitch));
    localStorage.setItem("jarvis_offline_rate", String(S.offlineRate));

    updatePairingStatusUi();
    checkConnectionRadar();
    el.settingsDrawer.classList.add("hidden");
  });

  /* ============================================================
     INITIALER START
  ============================================================ */
  refreshVoiceClipCount();
  startCamera();
})();
