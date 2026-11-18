/* Simpler Beta-Datums-Check + Vollbild-Overlay ("Beta-Phase beendet") */
(function (global) {
  "use strict";

  const EXPIRY_DATE = new Date("2027-02-01");
  let locked = false;

  function showLockOverlay() {
    if (locked || !document.body) return;
    locked = true;
    document.body.classList.add("beta-expired");

    if (global.__jarvisBrain3D && typeof global.__jarvisBrain3D.destroy === "function") {
      try { global.__jarvisBrain3D.destroy(); } catch (_e) {}
    }

    const overlay = document.createElement("div");
    overlay.id = "beta-lock-overlay";
    overlay.className = "beta-lock-overlay";
    overlay.innerHTML = [
      '<div class="beta-lock-card">',
      '  <div class="beta-lock-tag">J.A.R.V.I.S. // PUBLIC BETA v0.1</div>',
      '  <h1 class="beta-lock-title">Beta-Phase beendet</h1>',
      '  <p class="beta-lock-desc">Der Testzeitraum dieser Public Beta (bis 01.02.2027) ist abgelaufen.</p>',
      "</div>",
    ].join("");
    document.body.appendChild(overlay);
  }

  function applyBetaGuardState(payload) {
    const st = payload && payload.beta_guard ? payload.beta_guard : payload;
    if (st && st.expired === true) {
      showLockOverlay();
      return true;
    }
    return false;
  }

  global.__applyBetaGuardState = applyBetaGuardState;
  global.__isBetaExpired = () => Boolean(locked || new Date() >= EXPIRY_DATE);

  function checkBeta() {
    if (new Date() >= EXPIRY_DATE) {
      showLockOverlay();
      return;
    }
    fetch("/api/beta_status", { cache: "no-store" })
      .then((r) => r.json())
      .then(applyBetaGuardState)
      .catch(() => {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", checkBeta);
  } else {
    checkBeta();
  }
})(window);
