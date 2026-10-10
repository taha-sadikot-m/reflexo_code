(function () {
  "use strict";

  var MAX_INPUT = 4000;
  var MAX_HISTORY = 40;
  var STORAGE_KEY = "reflexo_widget_visitor";

  var thisScript = document.currentScript;

  function scriptEl() {
    return thisScript || document.querySelector("script[data-business-id]");
  }

  function sanitize(text) {
    var d = document.createElement("div");
    d.textContent = String(text || "");
    return d.innerHTML;
  }

  function uuid() {
    if (crypto && crypto.randomUUID) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  function visitorId() {
    try {
      var existing = localStorage.getItem(STORAGE_KEY);
      if (existing && existing.length >= 8) return existing;
      var id = uuid();
      localStorage.setItem(STORAGE_KEY, id);
      return id;
    } catch (e) {
      return uuid();
    }
  }

  function cssHref(src) {
    try {
      return new URL("widget.css", src).href;
    } catch (e) {
      return "/widget/widget.css";
    }
  }

  function ensureCss(href) {
    if (document.querySelector('link[data-reflexo-widget-css]')) return;
    var link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    link.setAttribute("data-reflexo-widget-css", "1");
    document.head.appendChild(link);
  }

  function bubble(role, html) {
    var row = document.createElement("div");
    row.className = "bubble-row" + (role === "customer" || role === "user" ? " out" : "");
    row.innerHTML = '<div class="bubble">' + html + "</div>";
    return row;
  }

  function init() {
    var el = scriptEl();
    if (!el) return;
    var businessId = el.getAttribute("data-business-id");
    var apiBase = (el.getAttribute("data-api-base") || "").replace(/\/$/, "");
    var dark = el.getAttribute("data-theme") === "dark";
    var title = el.getAttribute("data-title") === null ? "REFLEXO" : el.getAttribute("data-title");
    var position = el.getAttribute("data-position") || "right";
    if (!businessId || !apiBase) {
      console.warn("Reflexo widget: data-business-id and data-api-base are required");
      return;
    }
    ensureCss(cssHref(el.src));

    var vid = visitorId();
    var sessionId = "wg-" + uuid().replace(/-/g, "").slice(0, 12);
    var es = null;
    var typingEl = null;
    var history = [];

    var root = document.createElement("div");
    root.className = "reflexo-widget wg-live";
    root.innerHTML =
      '<button type="button" class="wg-launcher" aria-label="Open chat">' +
      '<svg class="ic" viewBox="0 0 24 24"><path d="M20.5 12.4c0 4.1-3.8 7.4-8.5 7.4-1 0-2-.15-2.9-.42L4 21.5l1.35-3.9C4.2 16.3 3.5 14.4 3.5 12.4 3.5 8.3 7.3 5 12 5s8.5 3.3 8.5 7.4Z"/></svg>' +
      "</button>" +
      '<div class="wg-panel' + (dark ? " wg-dark" : "") + '" hidden>' +
      '<div class="wg-head"><div><b>' +
      sanitize(title) +
      '</b><span>Typically replies in seconds</span></div>' +
      '<div class="wg-head-btns"><button type="button" data-close aria-label="Close">×</button></div></div>' +
      '<div class="wg-body" data-body></div>' +
      '<div class="wg-foot"><form class="wg-composer" data-form>' +
      '<input class="input" name="text" maxlength="' +
      MAX_INPUT +
      '" autocomplete="off" placeholder="Type a message…"/>' +
      '<button type="submit" class="wg-send" aria-label="Send">↑</button>' +
      "</form><div class="wg-brandline">Powered by <b>REFLEXO</b></div></div></div>";
    if (position === "left") {
      root.style.left = "20px";
      root.style.right = "auto";
    }
    document.body.appendChild(root);

    var panel = root.querySelector(".wg-panel");
    if (position === "left") {
      panel.style.left = "20px";
      panel.style.right = "auto";
    }
    var body = root.querySelector("[data-body]");
    var form = root.querySelector("[data-form]");
    var launcher = root.querySelector(".wg-launcher");

    function showTyping(on) {
      if (on && !typingEl) {
        typingEl = document.createElement("div");
        typingEl.className = "bubble-row";
        typingEl.innerHTML = '<div class="bubble typing-ind" data-typing><i></i><i></i><i></i></div>';
        body.appendChild(typingEl);
        body.scrollTop = body.scrollHeight;
      } else if (!on && typingEl) {
        typingEl.remove();
        typingEl = null;
      }
    }

    function push(role, text) {
      history.push({ role: role, content: text });
      if (history.length > MAX_HISTORY) history = history.slice(-MAX_HISTORY);
      body.appendChild(bubble(role, sanitize(text)));
      body.scrollTop = body.scrollHeight;
    }

    function connect() {
      if (es) {
        try { es.close(); } catch (e) {}
      }
      var url =
        apiBase +
        "/api/widget/" +
        encodeURIComponent(businessId) +
        "/events?session_id=" +
        encodeURIComponent(sessionId);
      es = new EventSource(url);
      es.addEventListener("progress", function (ev) {
        var data = {};
        try { data = JSON.parse(ev.data || "{}"); } catch (e) { return; }
        var t = data.type || data.event;
        if (t === "typing" || t === "broker_status") {
          showTyping(true);
        }
        if (t === "confirmation") {
          showTyping(false);
          var box = document.createElement("div");
          box.className = "wg-confirm";
          box.innerHTML =
            '<div class="wc-title">Confirm this change?</div>' +
            '<p>' + sanitize(data.preview || data.text || "Please confirm") + "</p>" +
            '<div class="quick-replies" style="margin-top:8px">' +
            '<button type="button" class="quick-reply" data-hitl="yes">Confirm</button>' +
            '<button type="button" class="quick-reply" data-hitl="cancel">Cancel</button></div>';
          body.appendChild(box);
          body.scrollTop = body.scrollHeight;
        }
        if (t === "done") {
          showTyping(false);
          var reply = data.response_text;
          if (reply) push("ai", reply);
        }
      });
    }

    async function send(text) {
      var trimmed = String(text || "").trim().slice(0, MAX_INPUT);
      if (!trimmed) return;
      push("customer", trimmed);
      showTyping(true);
      var res = await fetch(apiBase + "/api/widget/" + encodeURIComponent(businessId) + "/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: trimmed,
          visitor_id: vid,
          session_id: sessionId,
        }),
      });
      if (!res.ok) {
        showTyping(false);
        push("ai", "Could not send that just now. Please try again.");
        return;
      }
      var payload = await res.json();
      if (payload.session_id) sessionId = payload.session_id;
      connect();
    }

    async function loadHistory() {
      try {
        var res = await fetch(
          apiBase +
            "/api/widget/" +
            encodeURIComponent(businessId) +
            "/history?visitor_id=" +
            encodeURIComponent(vid) +
            "&limit=" +
            MAX_HISTORY
        );
        if (!res.ok) return;
        var data = await res.json();
        (data.items || []).forEach(function (item) {
          var role = item.role === "ai" || item.role === "assistant" ? "ai" : "customer";
          push(role, item.content || "");
        });
      } catch (e) {}
    }

    launcher.addEventListener("click", function () {
      panel.hidden = !panel.hidden;
      if (!panel.hidden) {
        connect();
        if (!history.length) loadHistory();
      }
    });
    root.querySelector("[data-close]").addEventListener("click", function () {
      panel.hidden = true;
    });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var input = form.querySelector("input");
      send(input.value);
      input.value = "";
    });
    body.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-hitl]");
      if (!btn) return;
      send(btn.getAttribute("data-hitl"));
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
