(function () {
  function navToggle(event) {
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    var m = document.getElementById("nav-menu");
    var b = document.getElementById("nav-toggle");
    if (!m || !b) return false;
    m.classList.toggle("open");
    b.innerHTML = m.classList.contains("open") ? "&#10005;" : "&#9776;";
    return false;
  }

  function clientNowLocal() {
    var n = new Date();
    var l = new Date(n.getTime() - n.getTimezoneOffset() * 60000);
    return l.toISOString().slice(0, 16);
  }

  function setCookie(name, value) {
    document.cookie =
      name +
      "=" +
      encodeURIComponent(value) +
      "; path=/; max-age=31536000; SameSite=Lax";
  }

  function getCookie(name) {
    var prefix = name + "=";
    var hit = document.cookie
      .split(";")
      .map(function (v) {
        return v.trim();
      })
      .find(function (v) {
        return v.startsWith(prefix);
      });
    return hit ? decodeURIComponent(hit.slice(prefix.length)) : "";
  }

  function ensureToastUi() {
    if (document.getElementById("app-toast-style")) return;
    var style = document.createElement("style");
    style.id = "app-toast-style";
    style.textContent =
      ".app-toast-wrap{position:fixed;right:14px;bottom:14px;z-index:500;display:flex;flex-direction:column;gap:8px;max-width:340px}" +
      ".app-toast{border-radius:10px;padding:10px 12px;font-size:13px;line-height:1.35;box-shadow:0 8px 22px rgba(0,0,0,.18);display:flex;align-items:center;justify-content:space-between;gap:10px}" +
      ".app-toast-info{background:#eff6ff;border:1px solid #bfdbfe;color:#1e3a8a}" +
      ".app-toast-success{background:#ecfdf5;border:1px solid #86efac;color:#166534}" +
      ".app-toast-error{background:#fef2f2;border:1px solid #fecaca;color:#991b1b}" +
      ".app-toast button{border:none;background:none;color:inherit;font-weight:700;cursor:pointer;padding:0;font-size:12px;text-decoration:underline}" +
      "@media (max-width:640px){.app-toast-wrap{left:10px;right:10px;bottom:10px;max-width:none}}";
    document.head.appendChild(style);
  }

  function showToast(message, type, options) {
    ensureToastUi();
    var opts = options || {};
    var wrap = document.getElementById("app-toast-wrap");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.id = "app-toast-wrap";
      wrap.className = "app-toast-wrap";
      wrap.setAttribute("aria-live", "polite");
      wrap.setAttribute("aria-atomic", "true");
      document.body.appendChild(wrap);
    }
    var t = document.createElement("div");
    var kind = type || "info";
    t.className = "app-toast app-toast-" + kind;
    var text = document.createElement("div");
    var baseMessage = message || "";
    text.textContent = baseMessage;
    t.appendChild(text);
    var countdownTimer = null;
    if (typeof opts.countdownSeconds === "number" && opts.countdownSeconds > 0) {
      var remaining = Math.ceil(opts.countdownSeconds);
      var renderCountdown = function () {
        if (baseMessage.indexOf("{s}") >= 0) {
          text.textContent = baseMessage.replace("{s}", String(remaining));
        } else {
          text.textContent = baseMessage + " (" + remaining + "s)";
        }
      };
      renderCountdown();
      countdownTimer = window.setInterval(function () {
        remaining -= 1;
        if (remaining <= 0) {
          window.clearInterval(countdownTimer);
          countdownTimer = null;
          return;
        }
        renderCountdown();
      }, 1000);
    }
    if (opts.actionText && typeof opts.onAction === "function") {
      var action = document.createElement("button");
      action.type = "button";
      action.textContent = opts.actionText;
      action.addEventListener("click", function () {
        if (countdownTimer) {
          window.clearInterval(countdownTimer);
          countdownTimer = null;
        }
        opts.onAction();
        t.remove();
      });
      t.appendChild(action);
    }
    wrap.appendChild(t);
    var ms = typeof opts.durationMs === "number" ? opts.durationMs : 3200;
    window.setTimeout(function () {
      if (countdownTimer) {
        window.clearInterval(countdownTimer);
        countdownTimer = null;
      }
      t.remove();
    }, ms);
  }

  function applyClientTimeDefaults(root) {
    var ctx = root || document;
    var nowStr = clientNowLocal();
    var dayStr = nowStr.slice(0, 10);
    window.__clientNowLocal = nowStr;
    window.__clientDateLocal = dayStr;

    ctx.querySelectorAll('input[type="date"]').forEach(function (el) {
      if (!el.value && !el.dataset.noClientDefault) el.value = dayStr;
      if (!el.max) el.max = dayStr;
    });

    ctx.querySelectorAll('input[type="datetime-local"]').forEach(function (el) {
      if (!el.value && !el.dataset.noClientDefault) el.value = nowStr;
      if (!el.max) el.max = nowStr;
    });
  }

  window._navToggle = navToggle;
  window._clientNowLocal = clientNowLocal;
  window._applyClientTimeDefaults = applyClientTimeDefaults;
  window._getCookie = getCookie;
  window._showToast = showToast;

  setCookie("tz_offset", String(new Date().getTimezoneOffset()));
  try {
    var tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    if (tz) setCookie("tz", tz);
  } catch (_) {}

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      applyClientTimeDefaults(document);
    });
  } else {
    applyClientTimeDefaults(document);
  }
})();
