/*
 * Hotel chat widget. Add to any page, before </body>:
 *
 *   <script src="https://YOUR-SERVER/widget/chat.js"
 *           data-hotel="Casa Alon Hotel"
 *           data-phone="+63 917 000 0000"
 *           defer></script>
 *
 * Optional attributes:
 *   data-color        main brand color (default #123C44)
 *   data-accent       accent color (default #F2A541)
 *   data-greeting     first message shown to guests
 *   data-suggestions  quick questions separated by |
 */
(() => {
  if (window.__hotelChatLoaded) return;
  window.__hotelChatLoaded = true;

  const script = document.currentScript;
  const API = new URL(script.src).origin;
  const d = script.dataset;
  const cfg = {
    hotel: d.hotel || "our hotel",
    phone: d.phone || "",
    color: d.color || "#123C44",
    accent: d.accent || "#F2A541",
    greeting:
      d.greeting ||
      "Hi! Ask me about rooms, rates, promos, and hotel policies. I'll answer from our latest hotel information.",
    suggestions: (d.suggestions || "Current promos|Are pets allowed?|Check-in and check-out time|Airport transfer")
      .split("|").map((s) => s.trim()).filter(Boolean),
  };

  // ---------- storage (safe if the browser blocks it) ----------
  const KEY = "hotel-chat-v1";
  const HISTORY_TTL = 24 * 60 * 60 * 1000;
  const load = () => {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY));
      if (saved && Date.now() - saved.updated < HISTORY_TTL) return saved;
    } catch (_) {}
    return null;
  };
  const save = () => {
    try {
      localStorage.setItem(KEY, JSON.stringify({ session, messages: messages.slice(-40), updated: Date.now() }));
    } catch (_) {}
  };
  const newId = () =>
    (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : "s-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);

  const saved = load();
  let session = saved ? saved.session : newId();
  let messages = saved ? saved.messages : [];
  let busy = false;

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- DOM (Shadow DOM keeps the host site's CSS out) ----------
  const host = document.createElement("div");
  host.id = "hotel-chat-widget";
  document.body.appendChild(host);
  const root = host.attachShadow({ mode: "open" });

  root.innerHTML = `
  <style>
    :host { all: initial; }
    * { box-sizing: border-box; }
    .wrap {
      --brand: ${esc(cfg.color)};
      --accent: ${esc(cfg.accent)};
      --ink: #1B2A2E;
      --muted: #5B6B6F;
      --mist: #EAF2F1;
      --line: #D5E2E0;
      --paper: #FFFFFF;
      font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      color: var(--ink);
    }
    button, textarea { font: inherit; color: inherit; }
    :focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }

    .launcher {
      position: fixed; right: 20px; bottom: 20px; z-index: 2147483000;
      width: 60px; height: 60px; border-radius: 50%; border: 0; cursor: pointer;
      background: var(--brand); color: #fff;
      display: grid; place-items: center;
      box-shadow: 0 8px 24px rgba(18, 60, 68, .28);
      transition: transform .15s ease;
    }
    .launcher:hover { transform: scale(1.05); }
    .launcher svg { width: 28px; height: 28px; }
    .launcher .dot {
      position: absolute; top: 6px; right: 6px; width: 12px; height: 12px;
      border-radius: 50%; background: var(--accent); border: 2px solid var(--brand);
    }
    .launcher[aria-expanded="true"] .dot { display: none; }

    .panel {
      position: fixed; right: 20px; bottom: 92px; z-index: 2147483000;
      width: 372px; height: min(580px, calc(100vh - 120px));
      background: var(--paper); border-radius: 18px; overflow: hidden;
      display: flex; flex-direction: column;
      box-shadow: 0 18px 50px rgba(18, 60, 68, .25);
      transform-origin: bottom right;
      animation: open .18s ease-out;
    }
    .panel[hidden] { display: none; }
    @keyframes open { from { opacity: 0; transform: translateY(8px) scale(.98); } to { opacity: 1; transform: none; } }

    header {
      position: relative; background: var(--brand); color: #fff;
      padding: 16px 16px 30px; flex: none;
    }
    header h2 { margin: 0; font-size: 17px; font-weight: 650; letter-spacing: .01em; }
    header p { margin: 2px 0 0; font-size: 13px; opacity: .82; }
    header .wave { position: absolute; left: 0; right: 0; bottom: -1px; width: 100%; height: 18px; display: block; }
    .head-actions { position: absolute; top: 10px; right: 8px; display: flex; gap: 2px; }
    .icon-btn {
      border: 0; background: transparent; color: #fff; cursor: pointer;
      height: 34px; min-width: 34px; padding: 0 8px; border-radius: 8px; font-size: 13px;
      display: grid; place-items: center; opacity: .9;
    }
    .icon-btn:hover { background: rgba(255,255,255,.12); opacity: 1; }
    .icon-btn svg { width: 18px; height: 18px; }

    .log { flex: 1; overflow-y: auto; padding: 8px 14px 12px; display: flex; flex-direction: column; gap: 8px; }
    .msg { max-width: 86%; padding: 9px 12px; border-radius: 14px; white-space: pre-wrap; overflow-wrap: anywhere; }
    .bot { align-self: flex-start; background: var(--mist); border-bottom-left-radius: 4px; }
    .guest { align-self: flex-end; background: var(--brand); color: #fff; border-bottom-right-radius: 4px; }
    .note {
      align-self: flex-start; max-width: 86%; margin-top: -2px;
      font-size: 13px; color: var(--muted); padding: 0 4px;
    }
    .note a { color: var(--brand); font-weight: 600; }
    .error { align-self: stretch; font-size: 13px; color: #8A2E1F; background: #FBEDE9; padding: 8px 12px; border-radius: 10px; }

    .typing { display: inline-flex; gap: 4px; align-items: center; }
    .typing span { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); opacity: .5; animation: blink 1.2s infinite; }
    .typing span:nth-child(2) { animation-delay: .2s; }
    .typing span:nth-child(3) { animation-delay: .4s; }
    @keyframes blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: .9; } }

    .chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 14px 10px; }
    .chips[hidden] { display: none; }
    .chip {
      border: 1px solid var(--line); background: var(--paper); color: var(--brand);
      border-radius: 999px; padding: 6px 12px; font-size: 13px; cursor: pointer;
    }
    .chip:hover { border-color: var(--brand); }

    form { display: flex; gap: 8px; align-items: flex-end; padding: 10px 12px; border-top: 1px solid var(--line); flex: none; }
    textarea {
      flex: 1; resize: none; border: 1px solid var(--line); border-radius: 12px;
      padding: 9px 12px; max-height: 110px; background: var(--paper); line-height: 1.4;
    }
    textarea:focus { outline: none; border-color: var(--brand); }
    .send {
      flex: none; width: 40px; height: 40px; border: 0; border-radius: 50%; cursor: pointer;
      background: var(--brand); color: #fff; display: grid; place-items: center;
    }
    .send:disabled { opacity: .45; cursor: default; }
    .send svg { width: 18px; height: 18px; }
    .foot { font-size: 11.5px; color: var(--muted); text-align: center; padding: 0 12px 10px; flex: none; }

    @media (max-width: 480px) {
      .panel { right: 0; bottom: 0; width: 100vw; height: 100dvh; border-radius: 0; }
      .launcher[aria-expanded="true"] { display: none; }
      textarea { font-size: 16px; } /* stops iPhones from zooming in on focus */
    }
    @media (prefers-reduced-motion: reduce) {
      .panel, .typing span { animation: none; }
      .launcher { transition: none; }
    }
  </style>

  <div class="wrap">
    <button class="launcher" type="button" aria-expanded="false" aria-controls="panel" aria-label="Chat with ${esc(cfg.hotel)}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4H6.5A2.5 2.5 0 0 1 4 13.5z"/>
        <path d="M7.5 10.5c1.2-1.2 2.3-1.2 3.5 0s2.3 1.2 3.5 0 2.3-1.2 3 -.6"/>
      </svg>
      <span class="dot" aria-hidden="true"></span>
    </button>

    <section class="panel" id="panel" role="dialog" aria-label="${esc(cfg.hotel)} chat" hidden>
      <header>
        <h2>${esc(cfg.hotel)}</h2>
        <p>Answers about rooms, rates, and policies</p>
        <div class="head-actions">
          <button class="icon-btn restart" type="button" aria-label="Start a new chat">New chat</button>
          <button class="icon-btn close" type="button" aria-label="Close chat">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
          </button>
        </div>
        <svg class="wave" viewBox="0 0 400 18" preserveAspectRatio="none" aria-hidden="true">
          <path d="M0 10 C 50 0, 100 0, 150 9 S 250 18, 300 9 S 370 2, 400 8 L400 18 L0 18 Z" fill="#FFFFFF"/>
          <path d="M0 10 C 50 0, 100 0, 150 9 S 250 18, 300 9 S 370 2, 400 8" fill="none" stroke="${esc(cfg.accent)}" stroke-width="2"/>
        </svg>
      </header>

      <div class="log" role="log" aria-live="polite"></div>
      <div class="chips"></div>

      <form>
        <textarea rows="1" maxlength="1000" placeholder="Type your question" aria-label="Your question"></textarea>
        <button class="send" type="submit" aria-label="Send">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>
        </button>
      </form>
      <div class="foot">Automated replies. Our front desk confirms all bookings.</div>
    </section>
  </div>`;

  const $ = (sel) => root.querySelector(sel);
  const launcher = $(".launcher");
  const panel = $(".panel");
  const log = $(".log");
  const chips = $(".chips");
  const form = $("form");
  const input = $("textarea");
  const sendBtn = $(".send");

  // ---------- rendering ----------
  const scrollDown = () => { log.scrollTop = log.scrollHeight; };

  function bubble(role, text) {
    const el = document.createElement("div");
    el.className = "msg " + role;
    el.textContent = text; // textContent: never render bot or guest text as HTML
    log.appendChild(el);
    return el;
  }

  function handoffNote() {
    const el = document.createElement("div");
    el.className = "note";
    if (cfg.phone) {
      el.append("Need a person? Call or Viber our front desk at ");
      const a = document.createElement("a");
      a.href = "tel:" + cfg.phone.replace(/[^\d+]/g, "");
      a.textContent = cfg.phone;
      el.append(a, ".");
    } else {
      el.textContent = "Need a person? Please contact our front desk.";
    }
    log.appendChild(el);
  }

  function render(m) {
    bubble(m.role, m.text);
    if (m.handoff) handoffNote();
  }

  function renderAll() {
    log.innerHTML = "";
    bubble("bot", cfg.greeting);
    messages.forEach(render);
    chips.hidden = messages.length > 0;
    scrollDown();
  }

  function renderChips() {
    chips.innerHTML = "";
    cfg.suggestions.forEach((q) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.textContent = q;
      b.addEventListener("click", () => send(q));
      chips.appendChild(b);
    });
  }

  function showError(text) {
    const el = document.createElement("div");
    el.className = "error";
    el.setAttribute("role", "alert");
    el.textContent = text;
    log.appendChild(el);
    scrollDown();
  }

  function showTyping() {
    const el = document.createElement("div");
    el.className = "msg bot";
    el.setAttribute("aria-label", "Assistant is typing");
    el.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
    log.appendChild(el);
    scrollDown();
    return el;
  }

  // ---------- sending ----------
  async function send(raw) {
    const text = (raw || "").trim();
    if (!text || busy) return;
    busy = true;
    sendBtn.disabled = true;

    const guestMsg = { role: "guest", text };
    messages.push(guestMsg);
    render(guestMsg);
    chips.hidden = true;
    input.value = "";
    autosize();
    scrollDown();
    save();

    const typing = showTyping();
    try {
      const res = await fetch(API + "/web/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: session, message: text }),
      });
      typing.remove();
      if (res.status === 429) {
        showError("You're sending messages quickly. Wait a minute, then try again.");
      } else if (!res.ok) {
        showError(
          "The assistant couldn't answer just now. Try again in a moment" +
            (cfg.phone ? ", or contact our front desk at " + cfg.phone + "." : ".")
        );
      } else {
        const data = await res.json();
        const botMsg = { role: "bot", text: data.answer, handoff: !!data.handoff };
        messages.push(botMsg);
        render(botMsg);
        save();
      }
    } catch (_) {
      typing.remove();
      showError("Couldn't reach the assistant. Check your internet connection and try again.");
    }
    scrollDown();
    busy = false;
    sendBtn.disabled = false;
    input.focus();
  }

  // ---------- open / close ----------
  function open() {
    panel.hidden = false;
    launcher.setAttribute("aria-expanded", "true");
    scrollDown();
    input.focus();
  }
  function close() {
    panel.hidden = true;
    launcher.setAttribute("aria-expanded", "false");
    launcher.focus();
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 110) + "px";
  }

  launcher.addEventListener("click", () => (panel.hidden ? open() : close()));
  $(".close").addEventListener("click", close);
  $(".restart").addEventListener("click", () => {
    session = newId();
    messages = [];
    save();
    renderAll();
    input.focus();
  });
  panel.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  form.addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); }
  });
  input.addEventListener("input", autosize);

  renderChips();
  renderAll();
})();
