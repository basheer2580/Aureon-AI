(() => {
  "use strict";

  const TOKEN_KEY = "aureon_access_token";
  const REFRESH_KEY = "aureon_refresh_token";
  const USER_KEY = "aureon_user_id";
  const PREMIUM_KEY = "aureon_premium";
  const ADMIN_KEY = "aureon_admin_token";
  const OAUTH_CONSENT_KEY = "aureon_oauth_privacy_consent_pending";
  const ADMIN_IDENTIFIER = "admin@aureon.ai";
  const MAX_FILES = 15;
  const API_BASE_URL = String(window.AUREON_CONFIG?.apiBaseUrl || "").replace(/\/+$/, "");
  const IS_NATIVE_APP = Boolean(window.Capacitor?.isNativePlatform?.());
  const $ = (selector, parent = document) => parent.querySelector(selector);
  const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
  const apiUrl = (path) => API_BASE_URL ? `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}` : path;

  const state = {
    token: localStorage.getItem(TOKEN_KEY) || "",
    userId: localStorage.getItem(USER_KEY) || "",
    premium: localStorage.getItem(PREMIUM_KEY) === "true",
    adminToken: sessionStorage.getItem(ADMIN_KEY) || "",
    authMode: "login",
    authStep: "credentials",
    pendingIdentifier: "",
    sessionId: crypto.randomUUID(),
    socket: null,
    sending: false,
    attachments: [],
    plan: null,
    landingText: {},
    downloads: [],
    domains: [],
    activeTab: "preview",
  };

  const onAppRoute = IS_NATIVE_APP || location.pathname === "/app" || location.pathname.startsWith("/app/");
  const landing = $("#landing-page");
  const appPage = $("#app-page");
  const footer = $("#site-footer");
  if (onAppRoute) {
    landing.classList.add("hidden");
    footer.classList.add("hidden");
    $(".topbar").classList.add("hidden");
    appPage.classList.remove("hidden");
    $$(".topnav a").forEach((link) => link.classList.remove("active"));
  } else {
    appPage.classList.add("hidden");
    $("#nav-launch").setAttribute("href", "/app");
    const active = $$('.topnav a[data-route="landing"]')[0];
    if (active) active.classList.add("active");
  }

  function showToast(message, duration = 3600) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.remove("hidden");
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => toast.classList.add("hidden"), duration);
  }

  function setError(element, message) {
    element.textContent = message;
    element.classList.toggle("hidden", !message);
  }

  async function api(path, options = {}) {
    if (IS_NATIVE_APP && !API_BASE_URL) {
      throw new Error("This native build has no Aureon API URL configured. Publish a native build with AUREON_API_BASE_URL set.");
    }
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
    if (options.body && !(options.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetch(apiUrl(path), { ...options, headers });
    const type = response.headers.get("content-type") || "";
    let payload = null;
    if (type.includes("application/json")) {
      payload = await response.json();
    } else if (!response.ok) {
      payload = { detail: await response.text() };
    }
    if (!response.ok) {
      const message = payload?.detail || `Request failed (${response.status})`;
      throw new Error(typeof message === "string" ? message : JSON.stringify(message));
    }
    return payload;
  }

  function authModal(open = true) {
    $("#auth-modal").classList.toggle("hidden", !open);
    if (open) {
      $("#account-identifier").focus();
      document.body.classList.add("modal-open");
    } else if ($("#admin-modal").classList.contains("hidden")) {
      document.body.classList.remove("modal-open");
    }
  }

  function switchAuthMode(mode) {
    state.authMode = mode;
    state.authStep = "credentials";
    $("#auth-title").innerHTML = mode === "signup" ? "Start with<br><span>Aureon.</span>" : "Welcome to<br><span>Aureon.</span>";
    $("#auth-submit").textContent = mode === "signup" ? "Create account" : "Continue";
    $("#account-password").autocomplete = mode === "signup" ? "new-password" : "current-password";
    $("#account-password").minLength = mode === "signup" ? 12 : 1;
    $("#account-password").placeholder = mode === "signup" ? "At least 12 characters" : "Enter your password";
    $("#coupon-field").classList.toggle("hidden", mode !== "signup");
    const privacyConsent = $("#privacy-consent");
    $("#privacy-consent-field").classList.toggle("hidden", mode !== "signup");
    privacyConsent.required = mode === "signup";
    if (mode !== "signup") privacyConsent.checked = false;
    $("#verification-field").classList.add("hidden");
    $("#account-identifier").disabled = false;
    $("#account-password").disabled = false;
    $$(".auth-tabs button").forEach((button) => button.classList.toggle("active", button.dataset.authTab === mode));
    setError($("#auth-error"), "");
  }

  function showVerificationStep(identifier) {
    state.authStep = "verification";
    state.pendingIdentifier = identifier;
    $("#verification-field").classList.remove("hidden");
    $("#account-password").classList.add("hidden");
    $("#coupon-field").classList.add("hidden");
    $("#account-identifier").disabled = true;
    $("#auth-submit").textContent = "Verify account";
    $("#privacy-consent-field").classList.add("hidden");
    $("#privacy-consent").required = false;
    setError($("#auth-error"), "Enter the verification code sent to your email or mobile number.");
    $("#verification-code").focus();
  }

  function showPasswordInitializeStep() {
    state.authStep = "initialize-password";
    $("#auth-title").innerHTML = "One last step<span>.</span>";
    $('label[for="account-identifier"]').classList.add("hidden");
    $("#account-identifier").classList.add("hidden");
    $("#account-password").classList.remove("hidden");
    $("#account-password").disabled = false;
    $("#account-password").value = "";
    $("#account-password").autocomplete = "new-password";
    $("#account-password").minLength = 12;
    $("#account-password").placeholder = "At least 12 characters";
    $("#coupon-field").classList.add("hidden");
    $("#privacy-consent-field").classList.add("hidden");
    $("#privacy-consent").required = false;
    $("#verification-field").classList.add("hidden");
    $("#auth-submit").textContent = "Set password & continue";
    setError($("#auth-error"), "Your verified GitHub account needs a private Aureon password before workspace access.");
  }

  function restoreAuthForm() {
    $('label[for="account-identifier"]').classList.remove("hidden");
    $("#account-identifier").classList.remove("hidden");
    $("#account-password").classList.remove("hidden");
    $("#account-identifier").disabled = false;
    $("#account-password").disabled = false;
    $("#account-password").value = "";
    $("#verification-code").value = "";
    $("#verification-field").classList.add("hidden");
    $("#privacy-consent-field").classList.toggle("hidden", state.authMode !== "signup");
    $("#privacy-consent").required = state.authMode === "signup";
  }

  function saveSession(payload) {
    state.token = payload.access_token;
    state.userId = payload.user_id || "";
    state.premium = Boolean(payload.is_premium_whitelisted);
    localStorage.setItem(TOKEN_KEY, state.token);
    if (payload.refresh_token) {
      localStorage.setItem(REFRESH_KEY, payload.refresh_token);
    }
    if (state.userId) localStorage.setItem(USER_KEY, state.userId);
    localStorage.setItem(PREMIUM_KEY, String(state.premium));
  }

  function clearSession(message = "You have been signed out.") {
    if (state.socket) {
      state.socket.onclose = null;
      state.socket.close();
      state.socket = null;
    }
    [TOKEN_KEY, REFRESH_KEY, USER_KEY, PREMIUM_KEY].forEach((key) => localStorage.removeItem(key));
    sessionStorage.removeItem(ADMIN_KEY);
    state.token = "";
    state.userId = "";
    state.premium = false;
    state.adminToken = "";
    state.attachments = [];
    if (onAppRoute) authModal(true);
    showToast(message);
  }

  function consumeOAuthCallback() {
    if (!location.hash.includes("access_token=")) return false;
    const fragment = new URLSearchParams(location.hash.slice(1));
    const accessToken = fragment.get("access_token");
    if (!accessToken) return false;
    const refreshToken = fragment.get("refresh_token");
    state.token = accessToken;
    localStorage.setItem(TOKEN_KEY, accessToken);
    if (refreshToken) localStorage.setItem(REFRESH_KEY, refreshToken);
    history.replaceState(null, "", `${location.pathname}${location.search}`);
    authModal(false);
    void finishOAuthSignIn();
    return true;
  }

  async function finishOAuthSignIn() {
    try {
      const metrics = await api("/api/account/metrics");
      await persistPendingOAuthConsent();
      state.userId = metrics.account.user_id;
      state.premium = Boolean(metrics.account.is_premium_whitelisted);
      localStorage.setItem(USER_KEY, state.userId);
      localStorage.setItem(PREMIUM_KEY, String(state.premium));
      await initializeWorkspace();
    } catch (error) {
      if (error.message.toLowerCase().includes("initialize a private account password")) {
        authModal(true);
        showPasswordInitializeStep();
        return;
      }
      authModal(true);
      setError($("#auth-error"), error.message);
    }
  }

  async function persistPendingOAuthConsent() {
    if (sessionStorage.getItem(OAUTH_CONSENT_KEY) !== "true") return;
    await api("/api/account/privacy-consent", { method: "POST" });
    sessionStorage.removeItem(OAUTH_CONSENT_KEY);
  }

  function escapeHTML(value) {
    return String(value).replace(/[&<>"']/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[character]);
  }

  function safeHref(value) {
    try {
      const url = new URL(value, location.origin);
      return ["https:", "http:", "mailto:"].includes(url.protocol) ? escapeHTML(url.href) : "";
    } catch {
      return "";
    }
  }

  function renderLatex(source) {
    let math = escapeHTML(source);
    const symbols = {
      alpha: "α", beta: "β", gamma: "γ", delta: "δ", theta: "θ", lambda: "λ",
      mu: "μ", pi: "π", sigma: "σ", phi: "φ", omega: "ω", sum: "∑", int: "∫",
      times: "×", cdot: "·", leq: "≤", geq: "≥", neq: "≠", approx: "≈",
      infty: "∞", to: "→", rightarrow: "→", leftarrow: "←",
    };
    math = math.replace(/\\([a-zA-Z]+)/g, (match, name) => symbols[name] || match);
    math = math.replace(/\\sqrt\{([^{}]+)\}/g, "√($1)");
    math = math.replace(/\\frac\{([^{}]+)\}\{([^{}]+)\}/g, '<span class="math-fraction"><span>$1</span><span>$2</span></span>');
    math = math.replace(/\^\{([^{}]+)\}|\^([a-zA-Z0-9])/g, (_match, grouped, single) => `<sup>${grouped || single}</sup>`);
    math = math.replace(/_\{([^{}]+)\}|_([a-zA-Z0-9])/g, (_match, grouped, single) => `<sub>${grouped || single}</sub>`);
    return math;
  }

  function inlineMarkdown(source) {
    let text = escapeHTML(source);
    text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
    text = text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/\*(.+?)\*/g, "<em>$1</em>");
    text = text.replace(/\\\((.+?)\\\)/g, (_match, expression) => `<span class="math-inline">${renderLatex(expression)}</span>`);
    text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_all, label, url) => {
      const href = safeHref(url);
      return href ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
    });
    return text;
  }

  function renderMarkdown(source) {
    const chunks = String(source).split(/(```[\s\S]*?```|\$\$[\s\S]*?\$\$)/g);
    return chunks.map((chunk) => {
      if (chunk.startsWith("```")) {
        const code = chunk.replace(/^```[^\n]*\n?/, "").replace(/```$/, "");
        return `<pre><code>${escapeHTML(code)}</code></pre>`;
      }
      if (chunk.startsWith("$$") && chunk.endsWith("$$")) {
        return `<div class="math-block">${renderLatex(chunk.slice(2, -2).trim())}</div>`;
      }
      const lines = chunk.split("\n");
      const html = [];
      let paragraph = [];
      let list = [];
      const flushParagraph = () => {
        if (paragraph.length) html.push(`<p>${inlineMarkdown(paragraph.join("<br>"))}</p>`);
        paragraph = [];
      };
      const flushList = () => {
        if (list.length) html.push(`<ul>${list.map((line) => `<li>${inlineMarkdown(line)}</li>`).join("")}</ul>`);
        list = [];
      };
      for (const line of lines) {
        const heading = line.match(/^(#{1,3})\s+(.+)$/);
        const bullet = line.match(/^\s*[-*]\s+(.+)$/);
        if (heading) {
          flushParagraph(); flushList();
          const level = Math.min(heading[1].length, 3);
          html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
        } else if (bullet) {
          flushParagraph(); list.push(bullet[1]);
        } else if (!line.trim()) {
          flushParagraph(); flushList();
        } else {
          flushList(); paragraph.push(line);
        }
      }
      flushParagraph(); flushList();
      return html.join("");
    }).join("");
  }

  function addMessage(role, text, { typing = false } = {}) {
    $("#welcome-panel").classList.add("hidden");
    const row = document.createElement("article");
    row.className = `message-row ${role}`;
    const avatar = document.createElement("span");
    avatar.className = "message-avatar";
    avatar.textContent = role === "user" ? "Y" : "✳";
    const body = document.createElement("div");
    body.className = "message-body";
    body.innerHTML = renderMarkdown(text);
    if (typing) body.insertAdjacentHTML("beforeend", '<span class="typing-caret"></span>');
    row.append(avatar, body);
    $("#message-list").append(row);
    $("#conversation").scrollTop = $("#conversation").scrollHeight;
    return body;
  }

  function setConnection(label, connected = false) {
    $("#connection-label").textContent = label;
    $(".connection-state").classList.toggle("connected", connected);
  }

  function websocketUrl(sessionId) {
    const query = new URLSearchParams({ access_token: state.token });
    const base = API_BASE_URL || location.origin;
    const url = new URL(`/ws/chat/${encodeURIComponent(sessionId)}`, base);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.search = query.toString();
    return url.toString();
  }

  async function sendMessage(text) {
    if (!text.trim() || state.sending) return;
    if (!state.token) { authModal(true); return; }
    const modelId = $("#model-select").value;
    if (!modelId || modelId === "loading") {
      showToast("No model is currently available for this account.");
      return;
    }
    state.sending = true;
    $("#send-button").disabled = true;
    const attachmentNote = state.attachments.length
      ? `\n\n[Local files staged in your browser: ${state.attachments.map((file) => file.name).join(", ")}. File contents are not sent by the current API.]`
      : "";
    addMessage("user", text + (attachmentNote ? `\n\n${attachmentNote.trim()}` : ""));
    const assistantBody = addMessage("assistant", "", { typing: true });
    const content = text;
    const sessionId = state.sessionId;
    state.attachments = [];
    renderAttachments();
    try {
      const socket = new WebSocket(websocketUrl(sessionId));
      state.socket = socket;
      setConnection("Connecting…");
      socket.addEventListener("open", () => {
        setConnection("Connected", true);
        socket.send(JSON.stringify({ type: "message", content, model: modelId }));
      });
      socket.addEventListener("message", (event) => {
        const message = JSON.parse(event.data);
        if (message.type === "token") {
          assistantBody.dataset.raw = (assistantBody.dataset.raw || "") + message.content;
          assistantBody.innerHTML = `${renderMarkdown(assistantBody.dataset.raw)}<span class="typing-caret"></span>`;
          $("#conversation").scrollTop = $("#conversation").scrollHeight;
        } else if (message.type === "done") {
          assistantBody.innerHTML = renderMarkdown(assistantBody.dataset.raw || "");
          setConnection("Ready", true);
          state.sending = false;
          $("#send-button").disabled = false;
          state.socket = null;
          void refreshHistory();
        } else if (message.type === "error") {
          assistantBody.innerHTML = renderMarkdown(`**Unable to complete:** ${message.detail}`);
          setConnection("Ready");
          state.sending = false;
          $("#send-button").disabled = false;
          state.socket = null;
        }
      });
      socket.addEventListener("error", () => {
        assistantBody.innerHTML = renderMarkdown("**Connection error.** Please try again.");
        setConnection("Offline");
        state.sending = false;
        $("#send-button").disabled = false;
        state.socket = null;
      });
      socket.addEventListener("close", (event) => {
        if (state.sending && event.code !== 1000) {
          assistantBody.innerHTML = renderMarkdown(`**Connection closed** (${event.code}). Please try again.`);
          state.sending = false;
          $("#send-button").disabled = false;
          setConnection("Disconnected");
        }
        if (state.socket === socket) state.socket = null;
      });
    } catch (error) {
      assistantBody.innerHTML = renderMarkdown(`**Unable to connect:** ${error.message}`);
      state.sending = false;
      $("#send-button").disabled = false;
      setConnection("Offline");
    }
  }

  async function refreshHistory() {
    if (!state.token) return;
    try {
      const rows = await api("/api/history?limit=100");
      const list = $("#thread-list");
      list.replaceChildren();
      if (!rows.length) {
        list.innerHTML = '<div class="side-empty">Your conversations will appear here.</div>';
        return;
      }
      rows.forEach((thread) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `thread-row${thread.session_id === state.sessionId ? " active" : ""}`;
        button.innerHTML = `<span class="thread-icon">◌</span><span class="thread-title"></span>`;
        $(".thread-title", button).textContent = thread.title || "New conversation";
        button.addEventListener("click", () => void openThread(thread.session_id));
        list.append(button);
      });
    } catch (error) {
      if (error.message.includes("401") || error.message.toLowerCase().includes("token")) {
        clearSession();
      } else {
        showToast(`Could not load conversations: ${error.message}`);
      }
    }
  }

  async function openThread(sessionId) {
    try {
      const messages = await api(`/api/history/${encodeURIComponent(sessionId)}`);
      if (state.socket) state.socket.close();
      state.sessionId = sessionId;
      $("#message-list").replaceChildren();
      $("#welcome-panel").classList.toggle("hidden", messages.length > 0);
      messages.forEach((message) => {
        if (message.content) addMessage(message.role === "user" ? "user" : "assistant", message.content);
      });
      await refreshHistory();
    } catch (error) {
      showToast(`Could not open conversation: ${error.message}`);
    }
  }

  function newConversation() {
    if (state.socket) state.socket.close();
    state.sessionId = crypto.randomUUID();
    $("#message-list").replaceChildren();
    $("#welcome-panel").classList.remove("hidden");
    $$(".thread-row").forEach((row) => row.classList.remove("active"));
    $("#prompt-input").focus();
  }

  function renderAttachments() {
    const tray = $("#attachment-tray");
    const assets = $("#asset-list");
    tray.replaceChildren();
    assets.replaceChildren();
    if (!state.attachments.length) {
      tray.classList.add("hidden");
      assets.innerHTML = '<div class="side-empty">Files you stage appear here.</div>';
      return;
    }
    tray.classList.remove("hidden");
    state.attachments.forEach((file, index) => {
      const chip = document.createElement("div");
      chip.className = "attachment-chip";
      chip.innerHTML = `<span>▧</span><span></span><button type="button" aria-label="Remove file">×</button>`;
      $("span:nth-child(2)", chip).textContent = file.name;
      $("button", chip).addEventListener("click", () => {
        state.attachments.splice(index, 1);
        renderAttachments();
      });
      tray.append(chip);
      const row = document.createElement("div");
      row.className = "asset-row";
      row.innerHTML = '<span class="asset-icon">▧</span><span></span>';
      $("span:last-child", row).textContent = file.name;
      assets.append(row);
    });
  }

  async function loadModels() {
    try {
      const response = await api("/api/models");
      const select = $("#model-select");
      select.replaceChildren();
      const models = (response.models || []).filter((model) => model.configured);
      if (!models.length) {
        select.add(new Option("No models configured", "loading"));
        return;
      }
      models.forEach((model) => select.add(new Option(model.label, model.id)));
      const previous = localStorage.getItem("aureon_selected_model");
      if (models.some((model) => model.id === previous)) select.value = previous;
      select.addEventListener("change", () => localStorage.setItem("aureon_selected_model", select.value));
    } catch (error) {
      $("#model-select").replaceChildren(new Option("Models unavailable", "loading"));
      showToast(`Model list unavailable: ${error.message}`);
    }
  }

  async function loadMetrics() {
    try {
      const response = await api("/api/account/metrics");
      const profile = response.account;
      state.plan = response.plan;
      state.premium = Boolean(profile.is_premium_whitelisted);
      localStorage.setItem(PREMIUM_KEY, String(state.premium));
      $("#credit-value").classList.toggle("premium", state.premium);
      $("#credit-value").innerHTML = state.premium
        ? "UNLIMITED <small>/ PREMIUM</small>"
        : `${Number(profile.credit_balance || 0).toLocaleString()} <small>credits</small>`;
      $("#credit-caption").textContent = state.premium
        ? "Premium access is active"
        : `${Number(response.plan.daily_renewal_credits).toLocaleString()} credits renew daily`;
      $("#credit-progress").style.width = state.premium
        ? "100%"
        : `${Math.max(4, Math.min(100, (Number(profile.credit_balance) / 20000) * 100))}%`;
      $("#profile-plan").textContent = state.premium ? "Premium whitelist" : "Standard workspace";
      $("#profile-name").textContent = (state.userId || profile.user_id || "Aureon account").slice(0, 8);
      $("#profile-avatar").textContent = (state.userId || "A").slice(0, 1).toUpperCase();
    } catch (error) {
      $("#credit-caption").textContent = "Account metrics unavailable";
      showToast(`Account status unavailable: ${error.message}`);
    }
  }

  async function loadDomains() {
    try {
      state.domains = await api("/api/domains");
      const target = $("#domain-list");
      target.replaceChildren();
      if (!state.domains.length) {
        target.innerHTML = '<div class="side-empty">No published routes yet.</div>';
        return;
      }
      state.domains.forEach((domain) => {
        const row = document.createElement("div");
        row.className = "domain-row";
        row.innerHTML = "<span></span>";
        const link = document.createElement("a");
        link.href = domain.site_path;
        link.textContent = domain.domain;
        link.title = domain.site_path;
        link.addEventListener("click", (event) => {
          event.preventDefault();
          showCanvas();
          previewSite(domain.site_path);
        });
        row.append(link);
        target.append(row);
      });
    } catch (error) {
      $("#domain-list").innerHTML = `<div class="side-empty">${escapeHTML(error.message)}</div>`;
    }
  }

  function showCanvas() {
    $("#canvas-panel").classList.remove("hidden");
    appPage.classList.add("with-canvas");
  }

  function hideCanvas() {
    $("#canvas-panel").classList.add("hidden");
    appPage.classList.remove("with-canvas");
  }

  function previewSite(path) {
    if (!path.startsWith("/sites/")) {
      showToast("Only Aureon-published paths can be previewed here.");
      return;
    }
    const base = API_BASE_URL || location.origin;
    $("#preview-url").value = `${base}${path}`;
    $("#preview-frame").replaceChildren();
    const iframe = document.createElement("iframe");
    iframe.title = "Published Aureon website preview";
    iframe.sandbox = "allow-scripts allow-forms allow-popups";
    iframe.referrerPolicy = "no-referrer";
    iframe.src = apiUrl(path);
    $("#preview-frame").append(iframe);
  }

  async function initializeWorkspace() {
    if (!state.token) {
      authModal(true);
      return;
    }
    authModal(false);
    try {
      await Promise.all([loadModels(), loadMetrics(), refreshHistory(), loadDomains()]);
      setConnection("Ready", true);
    } catch (error) {
      showToast(error.message);
    }
  }

  async function loadLandingContent() {
    try {
      const [downloadsResponse, configResponse] = await Promise.all([
        fetch(apiUrl("/api/downloads")).then((response) => response.ok ? response.json() : Promise.reject(new Error("Download catalog unavailable"))),
        fetch(apiUrl("/api/public/landing")).then((response) => response.ok ? response.json() : Promise.reject(new Error("Landing content unavailable"))),
      ]);
      state.downloads = downloadsResponse.downloads || [];
      state.landingText = configResponse.landing_text || {};
      renderDownloads();
      renderFooter();
    } catch {
      renderDownloads();
      renderFooter();
    }
  }

  function renderDownloads() {
    const grid = $("#download-grid");
    if (!grid) return;
    grid.replaceChildren();
    const validDownloads = state.downloads.filter((release) => {
      if (!release || typeof release.url !== "string") return false;
      const localPath = release.url.startsWith("/") && !release.url.startsWith("//");
      const secureRemote = /^https:\/\//i.test(release.url);
      return localPath || secureRemote;
    });
    if (!validDownloads.length) {
      grid.innerHTML = '<div class="download-empty"><span>✳</span> New Aureon releases will appear here.</div>';
    }
    const glyphs = { windows: "⊞", macos: "⌘", mac: "⌘", android: "◈", linux: "⌁", web: "◌" };
    validDownloads.forEach((release) => {
      const link = document.createElement("a");
      link.className = "download-card";
      link.href = new URL(release.url, API_BASE_URL || location.origin).href;
      link.target = release.url?.startsWith("https://") ? "_blank" : "_self";
      link.rel = "noopener noreferrer";
      link.innerHTML = `<span class="platform-icon"></span><span class="platform-version"></span><h3></h3><p></p><span class="download-cta">Get Aureon <span>↗</span></span>`;
      $(".platform-icon", link).textContent = glyphs[release.platform] || "✳";
      $(".platform-version", link).textContent = release.version || "";
      $("h3", link).textContent = release.label || release.platform;
      $("p", link).textContent = release.detail || "Official Aureon release";
      grid.append(link);
    });
    if (!validDownloads.some((release) => release.platform.toLowerCase() === "android")) {
      const placeholder = document.createElement("article");
      placeholder.className = "download-card download-card-pending";
      placeholder.setAttribute("aria-disabled", "true");
      placeholder.innerHTML = '<span class="platform-icon">◈</span><span class="platform-version">ANDROID</span><h3>Aureon for Android</h3><p>Signed app release is being prepared.</p><span class="download-cta">APK coming soon <span>·</span></span>';
      grid.append(placeholder);
    }
  }

  function attachmentLimit() {
    if (state.premium) return 300;
    return Math.min(MAX_FILES, Number(state.plan?.max_file_uploads ?? MAX_FILES));
  }

  function renderFooter() {
    const content = state.landingText;
    if (content.creator_name) $("#creator-name").textContent = content.creator_name;
    if (content.copyright_notice) $("#copyright-notice").textContent = content.copyright_notice;
    if (content.footer_motto) $("#footer-motto").textContent = content.footer_motto;
    if (content.support_email && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(content.support_email)) {
      $("#support-link").href = `mailto:${content.support_email}`;
      $("#support-link").textContent = content.support_email;
    }
    if (content.footer_note) $(".footer-bottom span").textContent = content.footer_note;
  }

  async function submitAuth(event) {
    event.preventDefault();
    const errorBox = $("#auth-error");
    setError(errorBox, "");
    if (
      state.authStep === "credentials"
      && state.authMode === "signup"
      && !$("#privacy-consent").checked
    ) {
      setError(errorBox, "You must accept the privacy framework to register an account.");
      $("#privacy-consent").focus();
      return;
    }
    const identifierInput = $("#account-identifier");
    const identifier = state.pendingIdentifier || identifierInput.value.trim();
    const password = $("#account-password").value;
    const submit = $("#auth-submit");
    submit.disabled = true;
    try {
      if (state.authStep === "initialize-password") {
        await api("/api/auth/password/initialize", {
          method: "POST",
          body: JSON.stringify({ password }),
        });
        const profile = await api("/api/account/metrics");
        await persistPendingOAuthConsent();
        saveSession({ access_token: state.token, user_id: profile.account.user_id, is_premium_whitelisted: profile.account.is_premium_whitelisted });
        restoreAuthForm();
        await initializeWorkspace();
        return;
      }
      if (state.authStep === "verification") {
        const result = await api("/api/auth/verify", {
          method: "POST",
          body: JSON.stringify({
            account_identifier: state.pendingIdentifier,
            code: $("#verification-code").value.trim(),
          }),
        });
        if (result.access_token) {
          saveSession(result);
          restoreAuthForm();
          await initializeWorkspace();
          return;
        }
        state.authMode = "login";
        state.authStep = "credentials";
        restoreAuthForm();
        $("#account-identifier").value = state.pendingIdentifier;
        switchAuthMode("login");
        setError(errorBox, "Your account is verified. Sign in with your password to continue.");
        return;
      }
      if (state.authMode === "signup") {
        const result = await api("/api/auth/register", {
          method: "POST",
          body: JSON.stringify({
            account_identifier: identifier,
            password,
            coupon_code: $("#coupon-code").value.trim() || null,
            privacy_policy_accepted: $("#privacy-consent").checked,
          }),
        });
        if (result.verification_required) {
          showVerificationStep(identifier);
          return;
        }
        showVerificationStep(identifier);
        return;
      }
      if (
        state.authMode === "login"
        && identifier.toLowerCase() === ADMIN_IDENTIFIER
      ) {
        await authenticateAdmin(ADMIN_IDENTIFIER, password);
        return;
      }
      const result = await api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ account_identifier: identifier, password }),
      });
      saveSession(result);
      restoreAuthForm();
      await initializeWorkspace();
    } catch (error) {
      const activeErrorBox = $("#admin-modal").classList.contains("hidden")
        ? errorBox
        : $("#admin-error");
      setError(activeErrorBox, error.message);
    } finally {
      submit.disabled = false;
    }
  }

  async function deleteAccount() {
    if (!window.confirm("Permanently delete your Aureon account, conversations, published projects, and generated media? This cannot be undone.")) {
      return;
    }
    try {
      await api("/api/user/delete-account", { method: "DELETE" });
      clearSession("Your account and associated data have been deleted.");
    } catch (error) {
      showToast(`Account deletion failed: ${error.message}`, 6000);
    }
  }

  async function authenticateAdmin(username, password) {
    const response = await fetch(apiUrl("/api/admin/login"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.detail || "Administrator sign-in failed");
    }
    if (!result.access_token) {
      throw new Error("Administrator authentication returned no access token");
    }
    state.adminToken = result.access_token;
    sessionStorage.setItem(ADMIN_KEY, state.adminToken);
    state.userId = "";
    $("#auth-modal").classList.add("hidden");
    $("#admin-modal").classList.remove("hidden");
    $("#admin-modal").classList.add("admin-folio");
    appPage.classList.add("admin-mode");
    document.body.classList.add("modal-open");
    $("#admin-login-form").classList.add("hidden");
    $("#admin-content-form").classList.remove("hidden");
    try {
      await loadAdminEditor();
    } catch (error) {
      sessionStorage.removeItem(ADMIN_KEY);
      state.adminToken = "";
      $("#admin-content-form").classList.add("hidden");
      $("#admin-login-form").classList.remove("hidden");
      setError($("#admin-error"), error.message);
      throw error;
    }
  }

  async function adminLogin(event) {
    event.preventDefault();
    const errorBox = $("#admin-error");
    setError(errorBox, "");
    try {
      await authenticateAdmin($("#admin-username").value, $("#admin-password").value);
    } catch (error) {
      setError(errorBox, error.message);
    }
  }

  async function adminApi(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${state.adminToken}`);
    if (options.body) headers.set("Content-Type", "application/json");
    const response = await fetch(apiUrl(path), { ...options, headers });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
    return payload;
  }

  async function loadAdminEditor() {
    const [config, downloads] = await Promise.all([
      adminApi("/api/admin/config"),
      fetch(apiUrl("/api/downloads")).then((response) => response.json()),
    ]);
    const text = config.landing_text || {};
    $("#cms-creator").value = text.creator_name || "";
    $("#cms-support").value = text.support_email || "";
    $("#cms-copyright").value = text.copyright_notice || "";
    $("#cms-motto").value = text.footer_motto || "";
    $("#cms-downloads").value = JSON.stringify(downloads.downloads || [], null, 2);
  }

  async function saveAdminContent(event) {
    event.preventDefault();
    const errorBox = $("#cms-error");
    setError(errorBox, "");
    try {
      const downloads = JSON.parse($("#cms-downloads").value || "[]");
      await adminApi("/api/admin/landing-text", {
        method: "PATCH",
        body: JSON.stringify({
          text: {
            creator_name: $("#cms-creator").value.trim(),
            support_email: $("#cms-support").value.trim(),
            copyright_notice: $("#cms-copyright").value.trim(),
            footer_motto: $("#cms-motto").value.trim(),
          },
        }),
      });
      await adminApi("/api/admin/downloads", {
        method: "POST",
        body: JSON.stringify({ downloads }),
      });
      state.landingText = {
        creator_name: $("#cms-creator").value.trim(),
        support_email: $("#cms-support").value.trim(),
        copyright_notice: $("#cms-copyright").value.trim(),
        footer_motto: $("#cms-motto").value.trim(),
      };
      state.downloads = downloads;
      renderFooter();
      renderDownloads();
      showToast("Aureon site content has been updated.");
    } catch (error) {
      setError(errorBox, error.message);
    }
  }

  function openAdminModal() {
    $("#admin-modal").classList.remove("hidden");
    $("#admin-modal").classList.remove("admin-folio");
    document.body.classList.add("modal-open");
    if (state.adminToken) {
      appPage.classList.add("admin-mode");
      $("#admin-login-form").classList.add("hidden");
      $("#admin-content-form").classList.remove("hidden");
      void loadAdminEditor().catch((error) => {
        sessionStorage.removeItem(ADMIN_KEY);
        state.adminToken = "";
        appPage.classList.remove("admin-mode");
        $("#admin-content-form").classList.add("hidden");
        $("#admin-login-form").classList.remove("hidden");
        setError($("#admin-error"), error.message);
      });
    } else {
      appPage.classList.remove("admin-mode");
      $("#admin-login-form").classList.remove("hidden");
      $("#admin-content-form").classList.add("hidden");
    }
  }

  function setupInstallPrompt() {
    let installPrompt = null;
    window.addEventListener("beforeinstallprompt", (event) => {
      event.preventDefault();
      installPrompt = event;
      $("#install-button")?.classList.remove("hidden");
    });
    $("#install-button")?.addEventListener("click", async () => {
      if (!installPrompt) {
        showToast("Use your browser menu and choose “Install app” or “Add to Home Screen”.");
        return;
      }
      installPrompt.prompt();
      await installPrompt.userChoice;
      installPrompt = null;
    });
    if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        // The app remains usable when offline installation is unavailable.
      });
    }
  }

  function bindEvents() {
    $(".github-button").href = apiUrl("/api/auth/github");
    $(".github-button").addEventListener("click", (event) => {
      event.preventDefault();
      const consent = $("#privacy-consent");
      if (!consent.checked) {
        $("#privacy-consent-field").classList.remove("hidden");
        consent.required = true;
        setError($("#auth-error"), "You must accept the privacy framework to register an account.");
        consent.focus();
        return;
      }
      consent.required = false;
      sessionStorage.setItem(OAUTH_CONSENT_KEY, "true");
      location.assign(apiUrl("/api/auth/github?privacy_policy_accepted=true"));
    });
    if (IS_NATIVE_APP) {
      $(".github-button").classList.add("hidden");
      $(".auth-divider").classList.add("hidden");
      $(".password-note").textContent = "Sign in with your verified email or mobile number. GitHub app sign-in requires OAuth deep-link configuration.";
      $(".password-note").prepend(Object.assign(document.createElement("span"), { textContent: "⌑" }));
    }
    $("#nav-login").addEventListener("click", () => {
      switchAuthMode("login");
      authModal(true);
    });
    $$(".auth-tabs button").forEach((button) => button.addEventListener("click", () => {
      restoreAuthForm();
      switchAuthMode(button.dataset.authTab);
    }));
    $$("[data-close-modal]").forEach((button) => button.addEventListener("click", () => {
      $("#auth-modal").classList.add("hidden");
      $("#admin-modal").classList.add("hidden");
      $("#admin-modal").classList.remove("admin-folio");
      appPage.classList.remove("admin-mode");
      document.body.classList.remove("modal-open");
    }));
    $("#auth-form").addEventListener("submit", (event) => void submitAuth(event));
    $("#privacy-consent").addEventListener("invalid", () => {
      setError($("#auth-error"), "You must accept the privacy framework to register an account.");
    });
    const closePrivacyModal = () => {
      $("#privacy-modal").classList.add("hidden");
      if ($("#auth-modal").classList.contains("hidden") && $("#admin-modal").classList.contains("hidden")) {
        document.body.classList.remove("modal-open");
      }
    };
    $("#privacy-policy-open").addEventListener("click", () => {
      $("#privacy-modal").classList.remove("hidden");
      document.body.classList.add("modal-open");
      $("#privacy-close").focus();
    });
    $("#privacy-close").addEventListener("click", closePrivacyModal);
    $("#privacy-accept-close").addEventListener("click", closePrivacyModal);
    $("#admin-open").addEventListener("click", openAdminModal);
    $("#admin-login-form").addEventListener("submit", (event) => void adminLogin(event));
    $("#admin-content-form").addEventListener("submit", (event) => void saveAdminContent(event));
    $("#admin-logout").addEventListener("click", () => {
      sessionStorage.removeItem(ADMIN_KEY);
      state.adminToken = "";
      appPage.classList.remove("admin-mode");
      $("#admin-modal").classList.remove("admin-folio");
      $("#admin-content-form").classList.add("hidden");
      $("#admin-login-form").classList.remove("hidden");
    });
    $("#composer-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const prompt = $("#prompt-input");
      const value = prompt.value.trim();
      if (!value) return;
      prompt.value = "";
      prompt.style.height = "auto";
      void sendMessage(value);
    });
    $("#prompt-input").addEventListener("input", (event) => {
      event.currentTarget.style.height = "auto";
      event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 180)}px`;
    });
    $("#prompt-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        $("#composer-form").requestSubmit();
      }
    });
    $("#file-input").addEventListener("change", (event) => {
      const files = [...event.currentTarget.files];
      const limit = attachmentLimit();
      const available = Math.max(0, limit - state.attachments.length);
      if (files.length > available) showToast(`Your current plan supports up to ${limit} staged files.`);
      state.attachments.push(...files.slice(0, available));
      renderAttachments();
      event.currentTarget.value = "";
    });
    $$(".starter-card").forEach((card) => card.addEventListener("click", () => {
      $("#prompt-input").value = card.dataset.prompt;
      $("#prompt-input").focus();
    }));
    $("#new-chat").addEventListener("click", newConversation);
    $("#refresh-history").addEventListener("click", () => void refreshHistory());
    $("#profile-button").addEventListener("click", () => $("#profile-menu").classList.toggle("hidden"));
    $("#logout-button").addEventListener("click", () => clearSession());
    $("#delete-account-button").addEventListener("click", () => void deleteAccount());
    $("#canvas-toggle").addEventListener("click", () => {
      if (appPage.classList.contains("with-canvas")) hideCanvas();
      else showCanvas();
    });
    $("#close-canvas").addEventListener("click", hideCanvas);
    $("#mobile-menu").addEventListener("click", () => appPage.classList.toggle("sidebar-open"));
    $("#preview-go").addEventListener("click", () => {
      try {
        const url = new URL($("#preview-url").value);
        const allowedOrigin = new URL(API_BASE_URL || location.origin).origin;
        if (url.origin !== allowedOrigin) throw new Error("Preview only supports this Aureon host.");
        previewSite(`${url.pathname}${url.search}`);
      } catch (error) {
        showToast(error.message || "Enter a valid Aureon site path.");
      }
    });
    $("#preview-current-site").addEventListener("click", () => {
      const target = state.domains[0];
      if (target) previewSite(target.site_path);
      else showToast("Publish a website from your workspace first.");
    });
    $$(".canvas-tab").forEach((button) => button.addEventListener("click", () => {
      state.activeTab = button.dataset.canvasTab;
      $$(".canvas-tab").forEach((tab) => tab.classList.toggle("active", tab === button));
      $("#canvas-preview").classList.toggle("hidden", state.activeTab !== "preview");
      $("#canvas-activity").classList.toggle("hidden", state.activeTab !== "activity");
    }));
    $$("a[data-scroll]").forEach((link) => link.addEventListener("click", (event) => {
      if (!landing.classList.contains("hidden")) {
        event.preventDefault();
        document.getElementById(link.dataset.scroll)?.scrollIntoView({ behavior: "smooth" });
      }
    }));
    window.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        newConversation();
      }
      if (event.key === "Escape") {
        $("#auth-modal").classList.add("hidden");
        $("#admin-modal").classList.add("hidden");
        hideCanvas();
        document.body.classList.remove("modal-open");
      }
    });
  }

  bindEvents();
  setupInstallPrompt();
  void loadLandingContent();
  if (onAppRoute) {
    const hasOAuthToken = consumeOAuthCallback();
    if (!hasOAuthToken) {
      if (state.token) void initializeWorkspace();
      else {
        switchAuthMode("login");
        authModal(true);
      }
    }
  }
})();
