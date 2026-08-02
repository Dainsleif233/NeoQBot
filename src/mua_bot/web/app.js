(function () {
  "use strict";

  var state = {
    csrf: "",
    username: "",
    activeView: "dashboard",
    config: null,
    qqUrls: {},
    qqLoginBotId: "",
    qqLoginTimer: null,
    qqLoginPolling: false,
    qqQrRefreshing: false,
    qqQrLastRefresh: 0,
    settingsStep: "qq"
  };

  function $(id) { return document.getElementById(id); }
  function clone(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }
  function listValue(value) { return (value || []).join("\n"); }
  function splitList(value) {
    return String(value || "").split(/[\n,]/).map(function (item) { return item.trim(); }).filter(Boolean);
  }
  function numberValue(value, fallback) {
    var parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }
  function pretty(value) { return JSON.stringify(value || {}, null, 2); }
  function parseJsonText(value, label) {
    try { return JSON.parse(value || "{}"); }
    catch (_) { throw new Error(label + "不是有效 JSON"); }
  }
  function errorText(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map(function (item) { return item.msg || JSON.stringify(item); }).join("；");
    return JSON.stringify(detail || "请求失败");
  }

  function showToast(message, isError) {
    var toast = $("toast");
    toast.textContent = message;
    toast.className = "toast show" + (isError ? " error" : "");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(function () { toast.className = "toast"; }, 3200);
  }

  async function api(path, options) {
    options = options || {};
    options.headers = options.headers || {};
    if (options.body && typeof options.body !== "string") {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    if (state.csrf && (options.method || "GET") !== "GET") {
      options.headers["X-CSRF-Token"] = state.csrf;
    }
    var response = await fetch(path, options);
    var data = {};
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) {
      if (response.status === 401) showLogin();
      var error = new Error(errorText(data.detail || data.error || "请求失败"));
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $("theme-button").textContent = theme === "dark" ? "切换纯白" : "切换纯黑";
    try { window.localStorage.setItem("mua-theme", theme); } catch (_) {}
  }

  function initializeTheme() {
    var theme = "dark";
    try { theme = window.localStorage.getItem("mua-theme") || "dark"; } catch (_) {}
    applyTheme(theme === "light" ? "light" : "dark");
  }

  function hideAllRoots() {
    ["login-view", "password-view", "app-view"].forEach(function (id) { $(id).classList.add("hidden"); });
  }

  function showLogin() {
    state.csrf = "";
    hideAllRoots();
    $("login-view").classList.remove("hidden");
    window.setTimeout(function () { $("login-password").focus(); }, 50);
  }

  function showPasswordChange() {
    hideAllRoots();
    $("password-view").classList.remove("hidden");
    window.setTimeout(function () { $("current-password").focus(); }, 50);
  }

  async function showApp() {
    hideAllRoots();
    $("app-view").classList.remove("hidden");
    $("current-user").textContent = state.username;
    await loadDashboard();
  }

  async function initialize() {
    initializeTheme();
    try {
      var session = await api("/api/gui/auth/session");
      state.csrf = session.csrf_token;
      state.username = session.username;
      if (session.must_change_password) showPasswordChange();
      else await showApp();
    } catch (_) {
      showLogin();
    }
  }

  $("login-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var button = event.submitter;
    button.disabled = true;
    try {
      var result = await api("/api/gui/auth/login", {
        method: "POST",
        body: { username: $("login-username").value.trim(), password: $("login-password").value }
      });
      state.csrf = result.csrf_token;
      state.username = result.username;
      $("login-password").value = "";
      if (result.must_change_password) showPasswordChange();
      else await showApp();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  $("password-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var current = $("current-password").value;
    var password = $("new-password").value;
    if (password !== $("confirm-password").value) {
      showToast("两次输入的新密码不一致", true);
      return;
    }
    var button = event.submitter;
    button.disabled = true;
    try {
      await api("/api/gui/auth/password", {
        method: "POST",
        body: { current_password: current, new_password: password }
      });
      var session = await api("/api/gui/auth/session");
      state.csrf = session.csrf_token;
      state.username = session.username;
      $("password-form").reset();
      showToast("密码已更新");
      await showApp();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  $("logout-button").addEventListener("click", async function () {
    try { await api("/api/gui/auth/logout", { method: "POST" }); } catch (_) {}
    showLogin();
  });

  $("theme-button").addEventListener("click", function () {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });

  var titles = {
    dashboard: ["Overview", "运行概览"],
    integrations: ["Orchestration", "Bot 编排"],
    settings: ["Configuration", "系统设置"],
    records: ["Audit trail", "记录与审计"]
  };

  async function switchView(name) {
    state.activeView = name;
    document.querySelectorAll(".view").forEach(function (node) { node.classList.remove("active"); });
    document.querySelectorAll(".nav-item").forEach(function (node) { node.classList.toggle("active", node.dataset.view === name); });
    $("view-" + name).classList.add("active");
    $("page-kicker").textContent = titles[name][0];
    $("page-title").textContent = titles[name][1];
    document.querySelector(".sidebar").classList.remove("open");
    try {
      if (name === "dashboard") await loadDashboard();
      if (name === "integrations") await loadIntegrationStatus();
      if (name === "settings") await loadSettings();
      if (name === "records") await loadRecords();
    } catch (error) {
      showToast(error.message, true);
    }
  }

  document.querySelectorAll(".nav-item").forEach(function (button) {
    button.addEventListener("click", function () { switchView(button.dataset.view); });
  });
  $("menu-button").addEventListener("click", function () { document.querySelector(".sidebar").classList.toggle("open"); });
  $("refresh-button").addEventListener("click", function () { switchView(state.activeView); });

  function renderDiagnostics(diagnostics) {
    var target = $("diagnostics-list");
    target.replaceChildren();
    var items = [];
    (diagnostics.errors || []).forEach(function (value) { items.push(["error", value]); });
    (diagnostics.warnings || []).forEach(function (value) { items.push(["warning", value]); });
    if (!items.length) items.push(["ok", "配置检查通过，没有发现问题。"]);
    items.forEach(function (item) {
      var node = document.createElement("div");
      node.className = "diagnostic " + item[0];
      node.textContent = item[1];
      target.appendChild(node);
    });
    var banner = $("security-banner");
    var important = (diagnostics.warnings || []).filter(function (text) {
      return text.indexOf("默认初始凭据") >= 0 || text.indexOf("Webhook") >= 0;
    });
    if (important.length) {
      banner.textContent = important.join(" · ");
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }
  }

  function taskTag(label, enabled) {
    var node = document.createElement("span");
    node.className = "task-tag" + (enabled ? " on" : "");
    node.textContent = label + (enabled ? " 开" : " 关");
    return node;
  }

  function renderBotSummary(bots) {
    var target = $("bot-summary");
    target.replaceChildren();
    if (!bots.length) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "还没有配置 QQ Bot。";
      target.appendChild(empty);
      return;
    }
    bots.forEach(function (bot) {
      var row = document.createElement("div");
      row.className = "summary-row";
      var title = document.createElement("div");
      var strong = document.createElement("strong");
      strong.textContent = bot.name;
      var small = document.createElement("div");
      small.className = "muted";
      small.textContent = bot.enabled ? bot.groups.length + " 个群" : "账号已停用";
      title.append(strong, small);
      var tags = document.createElement("div");
      tags.className = "task-tags";
      tags.append(
        taskTag("入群管理", bot.enabled && bot.tasks.join_management.enabled),
        taskTag("消息检测", bot.enabled && bot.tasks.message_detection.enabled),
        taskTag("公告同步", bot.enabled && bot.tasks.announcement_sync.enabled)
      );
      row.append(title, tags);
      target.appendChild(row);
    });
  }

  async function loadDashboard() {
    var data = await api("/api/gui/dashboard");
    $("count-joins").textContent = data.counts.join_requests || 0;
    $("count-messages").textContent = data.counts.group_messages || 0;
    $("count-runs").textContent = data.counts.moderation_runs || 0;
    $("count-notices").textContent = data.counts.announcements || 0;
    $("managed-groups").textContent = (data.managed_groups || []).length + " 个群";
    $("dry-run-badge").textContent = data.dry_run ? "安全演练中" : "真实动作已启用";
    $("dry-run-badge").className = "pill" + (data.dry_run ? "" : " solid");
    renderBotSummary(data.bots || []);
    renderDiagnostics(data.diagnostics);
  }

  async function runJob(job, botId, button) {
    if (button) button.disabled = true;
    $("job-output").textContent = "任务执行中…";
    try {
      var suffix = botId ? "?bot_id=" + encodeURIComponent(botId) : "";
      var result = await api("/api/gui/jobs/" + job + suffix, { method: "POST" });
      $("job-output").textContent = JSON.stringify(result.result, null, 2);
      showToast("任务执行完成");
      await loadDashboard();
    } catch (error) {
      $("job-output").textContent = error.message;
      showToast(error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  document.querySelectorAll("[data-job]").forEach(function (button) {
    button.addEventListener("click", function () { runJob(button.dataset.job, "", button); });
  });

  function statusText(status) {
    if (status && status.ok) return "已连接 · " + JSON.stringify(status);
    return "未连接 · " + (status && status.error ? status.error : JSON.stringify(status || {}));
  }

  function integrationButton(label, action) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "button secondary";
    button.textContent = label;
    button.addEventListener("click", action);
    return button;
  }

  function qqPublicUrl(bot) {
    return bot.webui_public_url || (window.location.protocol + "//" + window.location.hostname + ":" + bot.webui_public_port);
  }

  async function refreshQQCode(requestNewCode) {
    if (!state.qqLoginBotId || state.qqQrRefreshing) return;
    state.qqQrRefreshing = true;
    var image = $("qq-qrcode");
    var placeholder = $("qq-qr-placeholder");
    var refreshFailure = "";
    placeholder.classList.remove("hidden");
    $("qq-qr-caption").textContent = requestNewCode ? "正在请求 NapCat 生成新二维码…" : "正在读取二维码…";
    image.onload = function () {
      placeholder.classList.add("hidden");
      image.classList.add("loaded");
      $("qq-qr-caption").textContent = refreshFailure
        ? "新二维码申请失败，当前显示缓存图片：" + refreshFailure
        : "请在约 2 分钟内扫码；临近过期时页面会自动申请新二维码。";
    };
    image.onerror = function () {
      image.classList.remove("loaded");
      placeholder.classList.remove("hidden");
      $("qq-qr-caption").textContent = "二维码尚未生成，请确认 qq-bridge 正在运行。";
    };
    var refreshStamp = Date.now();
    state.qqQrLastRefresh = refreshStamp;
    try {
      if (requestNewCode) {
        await api("/api/gui/integrations/qq/qrcode/refresh?bot_id=" + encodeURIComponent(state.qqLoginBotId), { method: "POST" });
      }
      image.src = "/api/gui/integrations/qq/qrcode?bot_id=" + encodeURIComponent(state.qqLoginBotId) + "&t=" + refreshStamp;
    } catch (error) {
      refreshFailure = error.message;
      $("qq-qr-caption").textContent = error.message;
      showToast(error.message, true);
      image.src = "/api/gui/integrations/qq/qrcode?bot_id=" + encodeURIComponent(state.qqLoginBotId) + "&t=" + refreshStamp;
    } finally {
      state.qqQrRefreshing = false;
    }
  }

  function qqLoginStatusText(bot) {
    if (!bot) return "未找到 QQ Bot 状态";
    var onebot = bot.onebot_status || bot.status || {};
    var napcat = bot.napcat_status || {};
    if (bot.connection_state === "connected") {
      var info = onebot.login_info || {};
      return "已登录" + (info.nickname ? " · " + info.nickname : "") + (info.user_id ? "（" + info.user_id + "）" : "");
    }
    if (bot.connection_state === "qq_logged_in_onebot_unavailable") {
      return "QQ 已登录，但 OneBot HTTP 服务异常：" + (onebot.error || "请重新部署以应用自动配置");
    }
    if (bot.connection_state === "qq_offline") return "QQ 登录态已离线，请刷新二维码重新登录。";
    if (bot.connection_state === "waiting_for_scan") return "NapCat 已连接，等待扫码确认…";
    if (bot.connection_state === "disabled") return "此 QQ Bot 已停用。";
    return "NapCat 无法连接：" + (napcat.error || onebot.error || "请检查 qq-bridge 容器");
  }

  async function refreshQQLoginState() {
    if (!state.qqLoginBotId || state.qqLoginPolling) return;
    state.qqLoginPolling = true;
    try {
      var data = await api("/api/gui/integrations/qq?bot_id=" + encodeURIComponent(state.qqLoginBotId));
      var bot = (data.bots || [])[0];
      $("qq-login-state").textContent = qqLoginStatusText(bot);
      if (bot && bot.connection_state === "connected") {
        $("qq-login-state").classList.add("success");
        $("qq-qr-stage").classList.add("success");
        $("qq-qr-caption").textContent = "登录成功，可以关闭此窗口。";
      } else {
        $("qq-login-state").classList.remove("success");
        $("qq-qr-stage").classList.remove("success");
        if (Date.now() - state.qqQrLastRefresh > 105000) refreshQQCode(true);
      }
    } catch (error) {
      $("qq-login-state").textContent = "状态检查失败：" + error.message;
      $("qq-login-state").classList.remove("success");
      $("qq-qr-stage").classList.remove("success");
    } finally {
      state.qqLoginPolling = false;
    }
  }

  function openQQLogin(bot) {
    state.qqLoginBotId = bot.id;
    state.qqUrls[bot.id] = qqPublicUrl(bot);
    $("qq-login-title").textContent = bot.name + " · 扫码登录";
    $("qq-login-state").textContent = "正在连接 NapCat…";
    $("qq-login-state").classList.remove("success");
    $("qq-qr-stage").classList.remove("success");
    state.qqQrLastRefresh = 0;
    $("qq-login-dialog").showModal();
    refreshQQCode(true);
    refreshQQLoginState();
    window.clearInterval(state.qqLoginTimer);
    state.qqLoginTimer = window.setInterval(refreshQQLoginState, 3000);
  }

  function closeQQLogin() {
    window.clearInterval(state.qqLoginTimer);
    state.qqLoginTimer = null;
    state.qqLoginBotId = "";
    state.qqLoginPolling = false;
    state.qqQrRefreshing = false;
    $("qq-qrcode").removeAttribute("src");
    $("qq-qrcode").classList.remove("loaded");
    $("qq-login-dialog").close();
  }

  function renderQQIntegrations(bots) {
    var target = $("qq-integrations");
    target.replaceChildren();
    bots.forEach(function (bot) {
      var card = document.createElement("article");
      card.className = "integration-card";
      card.innerHTML = '<div class="integration-top"><div class="integration-logo">QQ</div><span class="pill"></span></div><h3></h3><div class="integration-meta"></div><div class="task-tags"></div><div class="integration-status"></div><div class="button-row"></div>';
      card.querySelector(".pill").textContent = bot.enabled ? "已启用" : "已停用";
      card.querySelector("h3").textContent = bot.name;
      card.querySelector(".integration-meta").textContent = "Webhook " + bot.webhook_url;
      var tasks = card.querySelector(".task-tags");
      tasks.append(
        taskTag("入群", bot.tasks.join_management.enabled),
        taskTag(bot.tasks.message_detection.record_only ? "纯记录" : "消息", bot.tasks.message_detection.enabled),
        taskTag("公告", bot.tasks.announcement_sync.enabled)
      );
      card.querySelector(".integration-status").textContent = bot.connection_message || statusText(bot.status);
      state.qqUrls[bot.id] = qqPublicUrl(bot);
      var buttons = card.querySelector(".button-row");
      buttons.append(
        integrationButton("刷新状态", function () { loadIntegrationStatus().catch(function (error) { showToast(error.message, true); }); }),
        integrationButton("扫码登录", function () { openQQLogin(bot); }),
        integrationButton("NapCat 设置", function () { window.open(state.qqUrls[bot.id], "_blank", "noopener"); })
      );
      if (bot.tasks.message_detection.enabled && bot.tasks.message_detection.analyze) {
        buttons.append(integrationButton("立即分析", function () { runJob("moderation", bot.id, this); }));
      }
      if (bot.tasks.announcement_sync.enabled) {
        buttons.append(integrationButton("一键同步公告", function () { runJob("announcements", bot.id, this); }));
      }
      target.appendChild(card);
    });
    if (!bots.length) {
      var empty = document.createElement("div");
      empty.className = "empty-editor";
      empty.textContent = "暂无 QQ Bot，请先到系统设置中添加。";
      target.appendChild(empty);
    }
  }

  async function feishuAction(botId, action, button, output) {
    button.disabled = true;
    output.textContent = action === "login" ? "等待飞书 CLI 返回授权信息…" : "正在退出飞书…";
    try {
      var data = await api("/api/gui/integrations/feishu/" + action + "?bot_id=" + encodeURIComponent(botId), { method: "POST" });
      output.textContent = typeof data.result === "string" ? data.result : JSON.stringify(data.result, null, 2);
      showToast("飞书操作已完成");
    } catch (error) {
      output.textContent = error.message;
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function renderFeishuIntegrations(bots) {
    var target = $("feishu-integrations");
    target.replaceChildren();
    bots.forEach(function (bot) {
      var card = document.createElement("article");
      card.className = "integration-card";
      card.innerHTML = '<div class="integration-top"><div class="integration-logo">FS</div><span class="pill"></span></div><h3></h3><div class="integration-meta">飞书 CLI 独立账号</div><div class="integration-status"></div><div class="button-row"></div>';
      card.querySelector(".pill").textContent = bot.enabled ? "已启用" : "已停用";
      card.querySelector("h3").textContent = bot.name;
      var output = card.querySelector(".integration-status");
      output.textContent = statusText(bot.status);
      var buttons = card.querySelector(".button-row");
      buttons.append(
        integrationButton("刷新状态", function () { loadIntegrationStatus().catch(function (error) { showToast(error.message, true); }); }),
        integrationButton("登录", function () { feishuAction(bot.id, "login", this, output); }),
        integrationButton("退出", function () { feishuAction(bot.id, "logout", this, output); })
      );
      target.appendChild(card);
    });
    if (!bots.length) {
      var empty = document.createElement("div");
      empty.className = "empty-editor";
      empty.textContent = "暂无飞书 Bot，请先到系统设置中添加。";
      target.appendChild(empty);
    }
  }

  async function loadIntegrationStatus() {
    var results = await Promise.all([api("/api/gui/integrations/qq"), api("/api/gui/integrations/feishu")]);
    renderQQIntegrations(results[0].bots || []);
    renderFeishuIntegrations(results[1].bots || []);
  }

  $("qq-login-close").addEventListener("click", closeQQLogin);
  $("qq-qrcode-refresh").addEventListener("click", function () { refreshQQCode(true); });
  $("qq-token-copy").addEventListener("click", async function () {
    if (!state.qqLoginBotId) return;
    try {
      var data = await api("/api/gui/integrations/qq/webui-token?bot_id=" + encodeURIComponent(state.qqLoginBotId), { method: "POST" });
      try {
        await navigator.clipboard.writeText(data.token);
        showToast("NapCat Token 已复制");
      } catch (_) {
        window.prompt("NapCat Token（请复制）", data.token);
      }
    } catch (error) {
      showToast(error.message, true);
    }
  });
  $("qq-login-new-window").addEventListener("click", function () {
    if (state.qqLoginBotId) window.open(state.qqUrls[state.qqLoginBotId], "_blank", "noopener");
  });
  $("qq-login-dialog").addEventListener("cancel", function (event) {
    event.preventDefault();
    closeQQLogin();
  });

  var settingsSteps = ["qq", "tasks", "feishu", "llm", "policy", "storage", "system", "review"];

  function activateSettingsStep(name) {
    var index = settingsSteps.indexOf(name);
    if (index < 0) return;
    state.settingsStep = name;
    document.querySelectorAll(".settings-step").forEach(function (button) {
      var buttonIndex = settingsSteps.indexOf(button.dataset.section);
      button.classList.toggle("active", buttonIndex === index);
      button.classList.toggle("completed", buttonIndex < index);
    });
    document.querySelectorAll(".settings-section").forEach(function (section) {
      section.classList.toggle("active", section.dataset.settingsSection === name);
    });
    $("settings-prev").disabled = index === 0;
    $("settings-next").classList.toggle("hidden", index === settingsSteps.length - 1);
    if (name === "review") updateSettingsReview();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  document.querySelectorAll(".settings-step").forEach(function (button) {
    button.addEventListener("click", function () { activateSettingsStep(button.dataset.section); });
  });
  $("settings-prev").addEventListener("click", function () {
    var index = settingsSteps.indexOf(state.settingsStep);
    activateSettingsStep(settingsSteps[Math.max(0, index - 1)]);
  });
  $("settings-next").addEventListener("click", function () {
    var index = settingsSteps.indexOf(state.settingsStep);
    activateSettingsStep(settingsSteps[Math.min(settingsSteps.length - 1, index + 1)]);
  });

  function defaultQQBot(index) {
    return {
      id: "qq-bot-" + (index + 1),
      name: "QQ Bot " + (index + 1),
      enabled: false,
      onebot_base_url: "http://qq-bridge:3000",
      access_token: "",
      access_token_file: "/app/data/secrets/napcat-onebot.token",
      webhook_secret: "",
      request_timeout_seconds: 15,
      webui_base_url: "http://qq-bridge:6099",
      webui_token: "",
      webui_token_file: "/app/data/secrets/napcat-webui.token",
      webui_public_url: "",
      webui_public_port: 6099,
      qrcode_path: "/app/napcat-cache/qrcode.png",
      managed_group_ids: [],
      administrator_qq_ids: [],
      announcement_actions: ["get_group_notice", "_get_group_notice"],
      search_feishu_bot_id: "",
      tasks: {
        join_management: { enabled: false, detect_requests: false, execute_management: false, auto_approve: false, auto_reject: false, minimum_confidence: 0.88 },
        message_detection: { enabled: false, record_only: false, realtime_detection: false, polling_detection: false, analyze: false, handle: false, interval_minutes: 30, window_minutes: 5, risk_threshold: 0.7, max_messages_per_run: 300 },
        announcement_sync: { enabled: false, auto_sync: false, sync_interval_minutes: 30, sync_on_startup: false, feishu_bot_id: "" }
      }
    };
  }

  function defaultFeishuBot(index) {
    return {
      id: "feishu-bot-" + (index + 1),
      name: "飞书 Bot " + (index + 1),
      enabled: false,
      driver: "disabled",
      executable: "feishu",
      timeout_seconds: 60,
      search_prefixes: ["搜索 ", "查询 ", "/search "],
      max_search_results: 5,
      command_templates: {},
      archive_payload_stdin: false,
      extra_environment: {}
    };
  }

  function normalizeBotConfig(c) {
    c.qq.bots = c.qq.bots || [];
    if (!c.qq.bots.length) {
      var legacyQQ = defaultQQBot(0);
      legacyQQ.id = "default";
      legacyQQ.name = "默认 QQ Bot";
      ["enabled", "onebot_base_url", "access_token", "access_token_file", "webhook_secret", "request_timeout_seconds", "webui_base_url", "webui_token", "webui_token_file", "webui_public_url", "webui_public_port", "qrcode_path", "managed_group_ids", "administrator_qq_ids", "announcement_actions"].forEach(function (key) { legacyQQ[key] = clone(c.qq[key]); });
      legacyQQ.qrcode_path = legacyQQ.qrcode_path || "/app/napcat-cache/qrcode.png";
      legacyQQ.tasks.join_management = {
        enabled: c.join_approval.enabled,
        detect_requests: c.join_approval.enabled,
        execute_management: c.join_approval.enabled,
        auto_approve: c.join_approval.auto_approve,
        auto_reject: c.join_approval.auto_reject,
        minimum_confidence: c.join_approval.minimum_confidence
      };
      legacyQQ.tasks.message_detection = {
        enabled: c.moderation.enabled,
        record_only: false,
        realtime_detection: c.moderation.enabled,
        polling_detection: c.moderation.enabled,
        analyze: c.moderation.enabled,
        handle: c.moderation.enabled,
        interval_minutes: c.moderation.interval_minutes,
        window_minutes: c.moderation.window_minutes,
        risk_threshold: c.moderation.risk_threshold,
        max_messages_per_run: c.moderation.max_messages_per_run
      };
      legacyQQ.tasks.announcement_sync = {
        enabled: c.announcements.enabled,
        auto_sync: c.announcements.enabled,
        sync_interval_minutes: c.announcements.sync_interval_minutes,
        sync_on_startup: c.announcements.sync_on_startup,
        feishu_bot_id: ""
      };
      c.qq.bots.push(legacyQQ);
    }
    c.feishu.bots = c.feishu.bots || [];
    if (!c.feishu.bots.length) {
      var legacyFeishu = defaultFeishuBot(0);
      legacyFeishu.id = "default";
      legacyFeishu.name = "默认飞书 Bot";
      ["enabled", "driver", "executable", "timeout_seconds", "search_prefixes", "max_search_results", "command_templates", "archive_payload_stdin", "extra_environment"].forEach(function (key) { legacyFeishu[key] = clone(c.feishu[key]); });
      c.feishu.bots.push(legacyFeishu);
    }
    c.qq.bots.forEach(function (bot) {
      if (bot.tasks && bot.tasks.message_detection && bot.tasks.message_detection.record_only == null) {
        bot.tasks.message_detection.record_only = false;
      }
    });
  }

  function setField(card, name, value) {
    var input = card.querySelector('[data-field="' + name + '"]');
    if (!input) return;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value == null ? "" : value;
  }

  function field(card, name) { return card.querySelector('[data-field="' + name + '"]'); }

  function bindTaskCard(taskCard) {
    var master = taskCard.querySelector('[data-role="master"]');
    function refresh() { taskCard.classList.toggle("active", master.checked); }
    master.addEventListener("change", function () {
      if (!master.checked) {
        taskCard.querySelectorAll('.task-detail input[type="checkbox"]').forEach(function (input) { input.checked = false; });
      }
      refresh();
    });
    refresh();
  }

  function renderQQEditors() {
    var target = $("qq-bot-editor");
    target.replaceChildren();
    state.config.qq.bots.forEach(function (bot, index) {
      var card = document.createElement("article");
      card.className = "editor-card qq-editor";
      card.dataset.index = index;
      card.innerHTML = [
        '<div class="editor-head"><div class="editor-title"><span class="editor-index"></span><div><strong></strong><small></small></div></div><div class="button-row"><button type="button" class="button danger" data-remove>删除</button></div></div>',
        '<div class="editor-body">',
        '<div class="form-grid connection-fields">',
        '<label>Bot ID<input data-field="id" pattern="[A-Za-z0-9_-]+" required></label>',
        '<label>显示名称<input data-field="name" required></label>',
        '<label class="switch-field span-2"><span><strong>启用这个 QQ Bot</strong><small>关闭后不接收事件，也不运行事务</small></span><input data-field="enabled" type="checkbox"></label>',
        '<label class="span-2">OneBot HTTP 地址<input data-field="onebot_base_url"></label>',
        '<label>Access Token<input data-field="access_token" type="password" placeholder="留空保持原值"></label>',
        '<label>Webhook Secret<input data-field="webhook_secret" type="password" placeholder="留空保持原值"></label>',
        '<label class="span-2">托管群号<textarea data-field="managed_group_ids" rows="2"></textarea></label>',
        '<label class="span-2">管理员 QQ<textarea data-field="administrator_qq_ids" rows="2"></textarea></label>',
        '<label>NapCat 公网端口<input data-field="webui_public_port" type="number"></label>',
        '<label>NapCat 公网 URL<input data-field="webui_public_url" placeholder="留空自动按端口生成"></label>',
        '<label class="span-2">公告动作名<textarea data-field="announcement_actions" rows="2"></textarea></label>',
        '<label class="span-2">管理员搜索使用的飞书 Bot ID<input data-field="search_feishu_bot_id" placeholder="留空使用默认飞书 Bot"></label>',
        '</div>',
        '<div class="task-list">',
        '<section class="task-card" data-task="join"><div class="task-master"><div><strong>入群管理</strong><small>检测申请与实际执行完全分离</small></div><input data-role="master" data-field="join.enabled" type="checkbox"></div><div class="task-detail">',
        '<label class="switch-field span-2"><span><strong>检测入群消息</strong><small>只登记到 GUI，不分析、不处理</small></span><input data-field="join.detect_requests" type="checkbox"></label>',
        '<label class="switch-field span-2"><span><strong>执行入群管理</strong><small>开启后自动开启检测，并进入判断与处理链路</small></span><input data-field="join.execute_management" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>自动同意</strong><small>高置信度 approve</small></span><input data-field="join.auto_approve" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>自动拒绝</strong><small>建议谨慎启用</small></span><input data-field="join.auto_reject" type="checkbox"></label>',
        '<label>最低置信度<input data-field="join.minimum_confidence" type="number" min="0" max="1" step="0.01"></label>',
        '<p class="task-note">只开“检测入群消息”时，申请只会进入记录页；不会调用模型，也不会向 QQ 发出同意或拒绝动作。</p>',
        '</div></section>',
        '<section class="task-card" data-task="message"><div class="task-master"><div><strong>消息检测</strong><small>长期登记、轮询窗口、分析与处理可独立组合</small></div><input data-role="master" data-field="message.enabled" type="checkbox"></div><div class="task-detail">',
        '<label class="switch-field"><span><strong>长期检测</strong><small>消息到达即登记 GUI</small></span><input data-field="message.realtime_detection" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>轮询检测</strong><small>按间隔检查最近窗口</small></span><input data-field="message.polling_detection" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>分析</strong><small>生成五分钟消息报告</small></span><input data-field="message.analyze" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>处理</strong><small>按报告判决并通知管理员</small></span><input data-field="message.handle" type="checkbox"></label>',
        '<label>轮询间隔（分钟）<input data-field="message.interval_minutes" type="number" min="1"></label>',
        '<label>分析窗口（分钟）<input data-field="message.window_minutes" type="number" min="1"></label>',
        '<label>风险阈值<input data-field="message.risk_threshold" type="number" min="0" max="1" step="0.01"></label>',
        '<label>单次最大消息数<input data-field="message.max_messages_per_run" type="number" min="1"></label>',
        '<p class="task-note">开启“分析”会自动开启轮询检测；开启“处理”会自动开启轮询检测和分析。处理只向管理员发送判决，不会自动禁言或踢人。</p>',
        '</div></section>',
        '<section class="task-card" data-task="announcement"><div class="task-master"><div><strong>公告同步</strong><small>一键全量同步与自动同步</small></div><input data-role="master" data-field="announcement.enabled" type="checkbox"></div><div class="task-detail">',
        '<label class="switch-field"><span><strong>自动同步</strong><small>定期抓取新公告入库</small></span><input data-field="announcement.auto_sync" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>启动时同步</strong><small>服务启动后立即抓取一次</small></span><input data-field="announcement.sync_on_startup" type="checkbox"></label>',
        '<label>同步间隔（分钟）<input data-field="announcement.sync_interval_minutes" type="number" min="1"></label>',
        '<label>归档到飞书 Bot ID<input data-field="announcement.feishu_bot_id" placeholder="留空使用默认飞书 Bot"></label>',
        '<p class="task-note">建议保存后先到“Bot 编排”执行一次“一键同步公告”，确认公告库完整，再开启自动同步。</p>',
        '</div></section>',
        '</div></div>'
      ].join("");
      card.querySelector(".editor-index").textContent = String(index + 1).padStart(2, "0");
      card.querySelector(".editor-title strong").textContent = bot.name;
      card.querySelector(".editor-title small").textContent = bot.id;
      setField(card, "id", bot.id);
      setField(card, "name", bot.name);
      setField(card, "enabled", bot.enabled);
      setField(card, "onebot_base_url", bot.onebot_base_url);
      setField(card, "access_token", "");
      setField(card, "webhook_secret", "");
      setField(card, "managed_group_ids", listValue(bot.managed_group_ids));
      setField(card, "administrator_qq_ids", listValue(bot.administrator_qq_ids));
      setField(card, "webui_public_port", bot.webui_public_port);
      setField(card, "webui_public_url", bot.webui_public_url || "");
      setField(card, "announcement_actions", listValue(bot.announcement_actions));
      setField(card, "search_feishu_bot_id", bot.search_feishu_bot_id || "");
      var join = bot.tasks.join_management;
      setField(card, "join.enabled", join.enabled);
      setField(card, "join.detect_requests", join.detect_requests);
      setField(card, "join.execute_management", join.execute_management);
      setField(card, "join.auto_approve", join.auto_approve);
      setField(card, "join.auto_reject", join.auto_reject);
      setField(card, "join.minimum_confidence", join.minimum_confidence);
      var message = bot.tasks.message_detection;
      setField(card, "message.enabled", message.enabled);
      setField(card, "message.realtime_detection", message.realtime_detection);
      setField(card, "message.polling_detection", message.polling_detection);
      setField(card, "message.analyze", message.analyze);
      setField(card, "message.handle", message.handle);
      setField(card, "message.interval_minutes", message.interval_minutes);
      setField(card, "message.window_minutes", message.window_minutes);
      setField(card, "message.risk_threshold", message.risk_threshold);
      setField(card, "message.max_messages_per_run", message.max_messages_per_run);
      var announcement = bot.tasks.announcement_sync;
      setField(card, "announcement.enabled", announcement.enabled);
      setField(card, "announcement.auto_sync", announcement.auto_sync);
      setField(card, "announcement.sync_on_startup", announcement.sync_on_startup);
      setField(card, "announcement.sync_interval_minutes", announcement.sync_interval_minutes);
      setField(card, "announcement.feishu_bot_id", announcement.feishu_bot_id || "");
      card.querySelectorAll(".task-card").forEach(bindTaskCard);

      var joinMaster = field(card, "join.enabled");
      field(card, "join.execute_management").addEventListener("change", function () {
        if (this.checked) { joinMaster.checked = true; field(card, "join.detect_requests").checked = true; }
        joinMaster.dispatchEvent(new Event("change"));
      });
      field(card, "join.detect_requests").addEventListener("change", function () {
        if (this.checked) { joinMaster.checked = true; joinMaster.dispatchEvent(new Event("change")); }
      });
      var messageMaster = field(card, "message.enabled");
      field(card, "message.analyze").addEventListener("change", function () {
        if (this.checked) { messageMaster.checked = true; field(card, "message.polling_detection").checked = true; }
        messageMaster.dispatchEvent(new Event("change"));
      });
      field(card, "message.handle").addEventListener("change", function () {
        if (this.checked) {
          messageMaster.checked = true;
          field(card, "message.polling_detection").checked = true;
          field(card, "message.analyze").checked = true;
        }
        messageMaster.dispatchEvent(new Event("change"));
      });
      ["message.realtime_detection", "message.polling_detection"].forEach(function (name) {
        field(card, name).addEventListener("change", function () {
          if (this.checked) { messageMaster.checked = true; messageMaster.dispatchEvent(new Event("change")); }
        });
      });
      var announcementMaster = field(card, "announcement.enabled");
      ["announcement.auto_sync", "announcement.sync_on_startup"].forEach(function (name) {
        field(card, name).addEventListener("change", function () {
          if (this.checked) { announcementMaster.checked = true; announcementMaster.dispatchEvent(new Event("change")); }
        });
      });
      card.querySelector("[data-remove]").addEventListener("click", function () {
        collectQQBots();
        state.config.qq.bots.splice(index, 1);
        renderQQEditors();
      });
      target.appendChild(card);
    });
    if (!state.config.qq.bots.length) {
      var empty = document.createElement("div");
      empty.className = "empty-editor";
      empty.textContent = "点击“添加 QQ Bot”开始配置。";
      target.appendChild(empty);
    }
  }

  function collectQQBots() {
    var bots = [];
    document.querySelectorAll(".qq-editor").forEach(function (card) {
      var original = state.config.qq.bots[Number(card.dataset.index)] || defaultQQBot(bots.length);
      var joinEnabled = field(card, "join.enabled").checked;
      var messageEnabled = field(card, "message.enabled").checked;
      var announcementEnabled = field(card, "announcement.enabled").checked;
      bots.push({
        id: field(card, "id").value.trim(),
        name: field(card, "name").value.trim(),
        enabled: field(card, "enabled").checked,
        onebot_base_url: field(card, "onebot_base_url").value.trim(),
        access_token: field(card, "access_token").value || original.access_token || "",
        access_token_file: original.access_token_file || "/app/data/secrets/napcat-onebot.token",
        webhook_secret: field(card, "webhook_secret").value || original.webhook_secret || "",
        request_timeout_seconds: original.request_timeout_seconds || 15,
        webui_base_url: original.webui_base_url || "http://qq-bridge:6099",
        webui_token: original.webui_token || "",
        webui_token_file: original.webui_token_file || "/app/data/secrets/napcat-webui.token",
        webui_public_url: field(card, "webui_public_url").value.trim(),
        webui_public_port: numberValue(field(card, "webui_public_port").value, 6099),
        managed_group_ids: splitList(field(card, "managed_group_ids").value),
        administrator_qq_ids: splitList(field(card, "administrator_qq_ids").value),
        announcement_actions: splitList(field(card, "announcement_actions").value),
        search_feishu_bot_id: field(card, "search_feishu_bot_id").value.trim(),
        tasks: {
          join_management: {
            enabled: joinEnabled,
            detect_requests: joinEnabled && field(card, "join.detect_requests").checked,
            execute_management: joinEnabled && field(card, "join.execute_management").checked,
            auto_approve: joinEnabled && field(card, "join.auto_approve").checked,
            auto_reject: joinEnabled && field(card, "join.auto_reject").checked,
            minimum_confidence: numberValue(field(card, "join.minimum_confidence").value, 0.88)
          },
          message_detection: {
            enabled: messageEnabled,
            realtime_detection: messageEnabled && field(card, "message.realtime_detection").checked,
            polling_detection: messageEnabled && field(card, "message.polling_detection").checked,
            analyze: messageEnabled && field(card, "message.analyze").checked,
            handle: messageEnabled && field(card, "message.handle").checked,
            interval_minutes: numberValue(field(card, "message.interval_minutes").value, 30),
            window_minutes: numberValue(field(card, "message.window_minutes").value, 5),
            risk_threshold: numberValue(field(card, "message.risk_threshold").value, 0.7),
            max_messages_per_run: numberValue(field(card, "message.max_messages_per_run").value, 300)
          },
          announcement_sync: {
            enabled: announcementEnabled,
            auto_sync: announcementEnabled && field(card, "announcement.auto_sync").checked,
            sync_interval_minutes: numberValue(field(card, "announcement.sync_interval_minutes").value, 30),
            sync_on_startup: announcementEnabled && field(card, "announcement.sync_on_startup").checked,
            feishu_bot_id: field(card, "announcement.feishu_bot_id").value.trim()
          }
        }
      });
    });
    state.config.qq.bots = bots;
    return bots;
  }

  function renderQQAccountEditors() {
    var target = $("qq-bot-editor");
    target.replaceChildren();
    state.config.qq.bots.forEach(function (bot, index) {
      var card = document.createElement("article");
      card.className = "editor-card qq-account-editor";
      card.dataset.index = index;
      card.innerHTML = [
        '<div class="editor-head"><div class="editor-title"><span class="editor-index"></span><div><strong></strong><small></small></div></div><button type="button" class="button danger" data-remove>删除</button></div>',
        '<div class="editor-body"><div class="form-grid">',
        '<label>Bot ID<input data-field="id" pattern="[A-Za-z0-9_-]+" required></label>',
        '<label>显示名称<input data-field="name" required></label>',
        '<label class="switch-field span-2"><span><strong>启用这个 QQ Bot</strong><small>关闭后不接收事件，也不运行事务</small></span><input data-field="enabled" type="checkbox"></label>',
        '<label class="span-2">OneBot HTTP 地址<input data-field="onebot_base_url"></label>',
        '<label>Access Token<input data-field="access_token" type="password" placeholder="留空保持原值"></label>',
        '<label>Webhook Secret<input data-field="webhook_secret" type="password" placeholder="留空保持原值"></label>',
        '<label class="span-2">托管群号<textarea data-field="managed_group_ids" rows="2" placeholder="每行一个群号"></textarea></label>',
        '<label class="span-2">管理员 QQ<textarea data-field="administrator_qq_ids" rows="2" placeholder="每行一个 QQ 号"></textarea></label>',
        '<label>NapCat 公网端口<input data-field="webui_public_port" type="number"></label>',
        '<label>NapCat 公网 URL<input data-field="webui_public_url" placeholder="留空自动按端口生成"></label>',
        '<label class="span-2">二维码挂载路径<input data-field="qrcode_path" placeholder="/app/napcat-cache/qrcode.png"><small>Compose 默认已配置，一般无需修改</small></label>',
        '<label class="span-2">公告动作名<textarea data-field="announcement_actions" rows="2"></textarea></label>',
        '<label class="span-2">管理员搜索使用的飞书 Bot ID<input data-field="search_feishu_bot_id" placeholder="留空使用默认飞书 Bot"></label>',
        '</div><div class="connection-hint"><strong>扫码登录</strong><span>保存连接后，到“Bot 编排”点击“扫码登录”，扫描对应 NapCat 实例生成的二维码。</span></div></div>'
      ].join("");
      card.querySelector(".editor-index").textContent = String(index + 1).padStart(2, "0");
      card.querySelector(".editor-title strong").textContent = bot.name;
      card.querySelector(".editor-title small").textContent = bot.id;
      setField(card, "id", bot.id);
      setField(card, "name", bot.name);
      setField(card, "enabled", bot.enabled);
      setField(card, "onebot_base_url", bot.onebot_base_url);
      setField(card, "access_token", "");
      setField(card, "webhook_secret", "");
      setField(card, "managed_group_ids", listValue(bot.managed_group_ids));
      setField(card, "administrator_qq_ids", listValue(bot.administrator_qq_ids));
      setField(card, "webui_public_port", bot.webui_public_port);
      setField(card, "webui_public_url", bot.webui_public_url || "");
      setField(card, "qrcode_path", bot.qrcode_path || "/app/napcat-cache/qrcode.png");
      setField(card, "announcement_actions", listValue(bot.announcement_actions));
      setField(card, "search_feishu_bot_id", bot.search_feishu_bot_id || "");
      card.querySelector("[data-remove]").addEventListener("click", function () {
        collectQQConfiguration();
        state.config.qq.bots.splice(index, 1);
        renderQQAccountEditors();
        renderQQWorkflowEditors();
      });
      target.appendChild(card);
    });
    if (!state.config.qq.bots.length) {
      var empty = document.createElement("div");
      empty.className = "empty-editor";
      empty.textContent = "点击“添加 QQ Bot”开始配置。";
      target.appendChild(empty);
    }
  }

  function renderQQWorkflowEditors() {
    var target = $("qq-task-editor");
    target.replaceChildren();
    state.config.qq.bots.forEach(function (bot, index) {
      var card = document.createElement("article");
      card.className = "editor-card qq-workflow-editor";
      card.dataset.index = index;
      card.innerHTML = [
        '<div class="editor-head"><div class="editor-title"><span class="editor-index"></span><div><strong></strong><small></small></div></div><span class="pill">独立事务</span></div>',
        '<div class="workflow-list">',
        '<section class="task-card"><div class="task-master"><div><strong>入群管理</strong><small>检测申请与执行管理分离</small></div><input data-role="master" data-field="join.enabled" type="checkbox"></div><div class="task-detail">',
        '<label class="switch-field span-2"><span><strong>检测入群消息</strong><small>只登记到 GUI，不分析、不处理</small></span><input data-field="join.detect_requests" type="checkbox"></label>',
        '<label class="switch-field span-2"><span><strong>执行入群管理</strong><small>进入模型判断与处理链路</small></span><input data-field="join.execute_management" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>自动同意</strong><small>高置信度 approve</small></span><input data-field="join.auto_approve" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>自动拒绝</strong><small>建议谨慎启用</small></span><input data-field="join.auto_reject" type="checkbox"></label>',
        '<label>最低置信度<input data-field="join.minimum_confidence" type="number" min="0" max="1" step="0.01"></label>',
        '<p class="task-note">只开启检测时，不调用模型，也不向 QQ 发出同意或拒绝动作。</p></div></section>',
        '<section class="task-card"><div class="task-master"><div><strong>群消息记录与分析</strong><small>原始记录、窗口分析与管理员通知分离</small></div><input data-role="master" data-field="message.enabled" type="checkbox"></div><div class="task-detail">',
        '<label class="switch-field span-2 featured-switch"><span><strong>纯记录群消息</strong><small>不调用模型；写入 SQLite，并按日追加到本地挂载卷 JSONL</small></span><input data-field="message.record_only" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>实时登记</strong><small>消息到达即进入 GUI 记录</small></span><input data-field="message.realtime_detection" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>轮询检测</strong><small>按间隔读取最近窗口</small></span><input data-field="message.polling_detection" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>分析</strong><small>生成消息风险报告</small></span><input data-field="message.analyze" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>处理</strong><small>命中风险时通知管理员</small></span><input data-field="message.handle" type="checkbox"></label>',
        '<label>轮询间隔（分钟）<input data-field="message.interval_minutes" type="number" min="1"></label>',
        '<label>分析窗口（分钟）<input data-field="message.window_minutes" type="number" min="1"></label>',
        '<label>风险阈值<input data-field="message.risk_threshold" type="number" min="0" max="1" step="0.01"></label>',
        '<label>单次最大消息数<input data-field="message.max_messages_per_run" type="number" min="1"></label>',
        '<p class="task-note">纯记录不会触发分析。分析会自动启用轮询；处理会自动启用分析。</p></div></section>',
        '<section class="task-card"><div class="task-master"><div><strong>公告同步</strong><small>本地版本库与飞书归档</small></div><input data-role="master" data-field="announcement.enabled" type="checkbox"></div><div class="task-detail">',
        '<label class="switch-field"><span><strong>自动同步</strong><small>定期抓取新公告</small></span><input data-field="announcement.auto_sync" type="checkbox"></label>',
        '<label class="switch-field"><span><strong>启动时同步</strong><small>服务启动立即抓取</small></span><input data-field="announcement.sync_on_startup" type="checkbox"></label>',
        '<label>同步间隔（分钟）<input data-field="announcement.sync_interval_minutes" type="number" min="1"></label>',
        '<label>归档到飞书 Bot ID<input data-field="announcement.feishu_bot_id" placeholder="留空使用默认飞书 Bot"></label>',
        '<p class="task-note">建议先手动全量同步并确认公告库，再开启自动同步。</p></div></section>',
        '</div>'
      ].join("");
      card.querySelector(".editor-index").textContent = String(index + 1).padStart(2, "0");
      card.querySelector(".editor-title strong").textContent = bot.name;
      card.querySelector(".editor-title small").textContent = bot.id;
      var join = bot.tasks.join_management;
      setField(card, "join.enabled", join.enabled);
      setField(card, "join.detect_requests", join.detect_requests);
      setField(card, "join.execute_management", join.execute_management);
      setField(card, "join.auto_approve", join.auto_approve);
      setField(card, "join.auto_reject", join.auto_reject);
      setField(card, "join.minimum_confidence", join.minimum_confidence);
      var message = bot.tasks.message_detection;
      setField(card, "message.enabled", message.enabled);
      setField(card, "message.record_only", message.record_only);
      setField(card, "message.realtime_detection", message.realtime_detection);
      setField(card, "message.polling_detection", message.polling_detection);
      setField(card, "message.analyze", message.analyze);
      setField(card, "message.handle", message.handle);
      setField(card, "message.interval_minutes", message.interval_minutes);
      setField(card, "message.window_minutes", message.window_minutes);
      setField(card, "message.risk_threshold", message.risk_threshold);
      setField(card, "message.max_messages_per_run", message.max_messages_per_run);
      var announcement = bot.tasks.announcement_sync;
      setField(card, "announcement.enabled", announcement.enabled);
      setField(card, "announcement.auto_sync", announcement.auto_sync);
      setField(card, "announcement.sync_on_startup", announcement.sync_on_startup);
      setField(card, "announcement.sync_interval_minutes", announcement.sync_interval_minutes);
      setField(card, "announcement.feishu_bot_id", announcement.feishu_bot_id || "");
      card.querySelectorAll(".task-card").forEach(bindTaskCard);
      var joinMaster = field(card, "join.enabled");
      field(card, "join.execute_management").addEventListener("change", function () {
        if (this.checked) { joinMaster.checked = true; field(card, "join.detect_requests").checked = true; }
        joinMaster.dispatchEvent(new Event("change"));
      });
      field(card, "join.detect_requests").addEventListener("change", function () {
        if (this.checked) { joinMaster.checked = true; joinMaster.dispatchEvent(new Event("change")); }
      });
      var messageMaster = field(card, "message.enabled");
      field(card, "message.analyze").addEventListener("change", function () {
        if (this.checked) { messageMaster.checked = true; field(card, "message.polling_detection").checked = true; }
        messageMaster.dispatchEvent(new Event("change"));
      });
      field(card, "message.handle").addEventListener("change", function () {
        if (this.checked) { messageMaster.checked = true; field(card, "message.polling_detection").checked = true; field(card, "message.analyze").checked = true; }
        messageMaster.dispatchEvent(new Event("change"));
      });
      ["message.record_only", "message.realtime_detection", "message.polling_detection"].forEach(function (name) {
        field(card, name).addEventListener("change", function () {
          if (this.checked) { messageMaster.checked = true; messageMaster.dispatchEvent(new Event("change")); }
        });
      });
      var announcementMaster = field(card, "announcement.enabled");
      ["announcement.auto_sync", "announcement.sync_on_startup"].forEach(function (name) {
        field(card, name).addEventListener("change", function () {
          if (this.checked) { announcementMaster.checked = true; announcementMaster.dispatchEvent(new Event("change")); }
        });
      });
      target.appendChild(card);
    });
    if (!state.config.qq.bots.length) {
      var empty = document.createElement("div");
      empty.className = "empty-editor";
      empty.textContent = "先在“QQ 账号”步骤添加 Bot，再为它分配事务。";
      target.appendChild(empty);
    }
  }

  function collectQQConfiguration() {
    var bots = clone(state.config.qq.bots || []);
    document.querySelectorAll(".qq-account-editor").forEach(function (card) {
      var index = Number(card.dataset.index);
      var original = bots[index] || defaultQQBot(index);
      bots[index] = Object.assign(original, {
        id: field(card, "id").value.trim(),
        name: field(card, "name").value.trim(),
        enabled: field(card, "enabled").checked,
        onebot_base_url: field(card, "onebot_base_url").value.trim(),
        access_token: field(card, "access_token").value || original.access_token || "",
        access_token_file: original.access_token_file || "/app/data/secrets/napcat-onebot.token",
        webhook_secret: field(card, "webhook_secret").value || original.webhook_secret || "",
        request_timeout_seconds: original.request_timeout_seconds || 15,
        webui_base_url: original.webui_base_url || "http://qq-bridge:6099",
        webui_token: original.webui_token || "",
        webui_token_file: original.webui_token_file || "/app/data/secrets/napcat-webui.token",
        webui_public_url: field(card, "webui_public_url").value.trim(),
        webui_public_port: numberValue(field(card, "webui_public_port").value, 6099),
        qrcode_path: field(card, "qrcode_path").value.trim() || "/app/napcat-cache/qrcode.png",
        managed_group_ids: splitList(field(card, "managed_group_ids").value),
        administrator_qq_ids: splitList(field(card, "administrator_qq_ids").value),
        announcement_actions: splitList(field(card, "announcement_actions").value),
        search_feishu_bot_id: field(card, "search_feishu_bot_id").value.trim()
      });
    });
    document.querySelectorAll(".qq-workflow-editor").forEach(function (card) {
      var index = Number(card.dataset.index);
      var bot = bots[index] || defaultQQBot(index);
      var joinEnabled = field(card, "join.enabled").checked;
      var messageEnabled = field(card, "message.enabled").checked;
      var announcementEnabled = field(card, "announcement.enabled").checked;
      bot.tasks = {
        join_management: {
          enabled: joinEnabled,
          detect_requests: joinEnabled && field(card, "join.detect_requests").checked,
          execute_management: joinEnabled && field(card, "join.execute_management").checked,
          auto_approve: joinEnabled && field(card, "join.auto_approve").checked,
          auto_reject: joinEnabled && field(card, "join.auto_reject").checked,
          minimum_confidence: numberValue(field(card, "join.minimum_confidence").value, 0.88)
        },
        message_detection: {
          enabled: messageEnabled,
          record_only: messageEnabled && field(card, "message.record_only").checked,
          realtime_detection: messageEnabled && field(card, "message.realtime_detection").checked,
          polling_detection: messageEnabled && field(card, "message.polling_detection").checked,
          analyze: messageEnabled && field(card, "message.analyze").checked,
          handle: messageEnabled && field(card, "message.handle").checked,
          interval_minutes: numberValue(field(card, "message.interval_minutes").value, 30),
          window_minutes: numberValue(field(card, "message.window_minutes").value, 5),
          risk_threshold: numberValue(field(card, "message.risk_threshold").value, 0.7),
          max_messages_per_run: numberValue(field(card, "message.max_messages_per_run").value, 300)
        },
        announcement_sync: {
          enabled: announcementEnabled,
          auto_sync: announcementEnabled && field(card, "announcement.auto_sync").checked,
          sync_interval_minutes: numberValue(field(card, "announcement.sync_interval_minutes").value, 30),
          sync_on_startup: announcementEnabled && field(card, "announcement.sync_on_startup").checked,
          feishu_bot_id: field(card, "announcement.feishu_bot_id").value.trim()
        }
      };
      bots[index] = bot;
    });
    state.config.qq.bots = bots;
    return bots;
  }

  function renderFeishuEditors() {
    var target = $("feishu-bot-editor");
    target.replaceChildren();
    state.config.feishu.bots.forEach(function (bot, index) {
      var card = document.createElement("article");
      card.className = "editor-card feishu-editor";
      card.dataset.index = index;
      card.innerHTML = [
        '<div class="editor-head"><div class="editor-title"><span class="editor-index"></span><div><strong></strong><small></small></div></div><button type="button" class="button danger" data-remove>删除</button></div>',
        '<div class="editor-body"><div class="form-grid">',
        '<label>Bot ID<input data-field="id" pattern="[A-Za-z0-9_-]+" required></label>',
        '<label>显示名称<input data-field="name" required></label>',
        '<label class="switch-field span-2"><span><strong>启用这个飞书 Bot</strong><small>使用独立 CLI 登录态和环境变量</small></span><input data-field="enabled" type="checkbox"></label>',
        '<label>可执行文件<input data-field="executable"></label>',
        '<label>超时（秒）<input data-field="timeout_seconds" type="number" min="1"></label>',
        '<label>最大搜索结果<input data-field="max_search_results" type="number" min="1" max="20"></label>',
        '<label class="switch-field"><span><strong>公告 JSON 走 stdin</strong><small>避免超长命令行</small></span><input data-field="archive_payload_stdin" type="checkbox"></label>',
        '<label class="span-2">管理员搜索前缀<textarea data-field="search_prefixes" rows="2"></textarea></label>',
        '<label class="span-2">命令模板（JSON）<textarea data-field="command_templates" class="code-input" rows="10"></textarea></label>',
        '<label class="span-2">额外环境变量（JSON）<textarea data-field="extra_environment" class="code-input" rows="5"></textarea></label>',
        '</div></div>'
      ].join("");
      card.querySelector(".editor-index").textContent = String(index + 1).padStart(2, "0");
      card.querySelector(".editor-title strong").textContent = bot.name;
      card.querySelector(".editor-title small").textContent = bot.id;
      setField(card, "id", bot.id);
      setField(card, "name", bot.name);
      setField(card, "enabled", bot.enabled);
      setField(card, "executable", bot.executable);
      setField(card, "timeout_seconds", bot.timeout_seconds);
      setField(card, "max_search_results", bot.max_search_results);
      setField(card, "archive_payload_stdin", bot.archive_payload_stdin);
      setField(card, "search_prefixes", listValue(bot.search_prefixes));
      setField(card, "command_templates", pretty(bot.command_templates));
      setField(card, "extra_environment", pretty(bot.extra_environment));
      card.querySelector("[data-remove]").addEventListener("click", function () {
        collectFeishuBots();
        state.config.feishu.bots.splice(index, 1);
        renderFeishuEditors();
      });
      target.appendChild(card);
    });
    if (!state.config.feishu.bots.length) {
      var empty = document.createElement("div");
      empty.className = "empty-editor";
      empty.textContent = "点击“添加飞书 Bot”开始配置。";
      target.appendChild(empty);
    }
  }

  function collectFeishuBots() {
    var bots = [];
    document.querySelectorAll(".feishu-editor").forEach(function (card) {
      var original = state.config.feishu.bots[Number(card.dataset.index)] || defaultFeishuBot(bots.length);
      var enabled = field(card, "enabled").checked;
      var incomingEnvironment = parseJsonText(field(card, "extra_environment").value, "飞书环境变量");
      Object.keys(incomingEnvironment).forEach(function (key) {
        if (["", "***"].indexOf(incomingEnvironment[key]) >= 0 && original.extra_environment && original.extra_environment[key]) {
          incomingEnvironment[key] = original.extra_environment[key];
        }
      });
      bots.push({
        id: field(card, "id").value.trim(),
        name: field(card, "name").value.trim(),
        enabled: enabled,
        driver: enabled ? "cli" : "disabled",
        executable: field(card, "executable").value.trim(),
        timeout_seconds: numberValue(field(card, "timeout_seconds").value, 60),
        search_prefixes: splitList(field(card, "search_prefixes").value),
        max_search_results: numberValue(field(card, "max_search_results").value, 5),
        command_templates: parseJsonText(field(card, "command_templates").value, "飞书命令模板"),
        archive_payload_stdin: field(card, "archive_payload_stdin").checked,
        extra_environment: incomingEnvironment
      });
    });
    state.config.feishu.bots = bots;
    return bots;
  }

  $("add-qq-bot").addEventListener("click", function () {
    collectQQConfiguration();
    state.config.qq.bots.push(defaultQQBot(state.config.qq.bots.length));
    renderQQAccountEditors();
    renderQQWorkflowEditors();
  });

  $("add-feishu-bot").addEventListener("click", function () {
    collectFeishuBots();
    state.config.feishu.bots.push(defaultFeishuBot(state.config.feishu.bots.length));
    renderFeishuEditors();
  });

  function populateSettings(c) {
    $("cfg-environment").value = c.app.environment;
    $("cfg-log-level").value = c.app.log_level;
    $("cfg-dry-run").checked = c.app.dry_run;
    $("cfg-admin-token").value = "";
    $("cfg-secure-cookie").checked = c.gui.secure_cookie;
    $("cfg-message-archive-path").value = c.app.message_archive_path || "data/group-message-records";
    $("cfg-llm-driver").value = c.llm.driver;
    $("cfg-llm-model").value = c.llm.model;
    $("cfg-llm-url").value = c.llm.base_url;
    $("cfg-llm-key").value = "";
    $("cfg-llm-timeout").value = c.llm.timeout_seconds;
    $("cfg-llm-retries").value = c.llm.max_retries;
    $("cfg-llm-json").checked = c.llm.json_response_format;
    $("cfg-required-keywords").value = listValue(c.join_approval.required_keywords);
    $("cfg-forbidden-keywords").value = listValue(c.join_approval.forbidden_keywords);
    $("cfg-join-policy").value = c.join_approval.policy;
    $("cfg-mod-policy").value = c.moderation.policy;
    $("cfg-rule-keywords").value = pretty(c.moderation.rule_keywords);
    $("cfg-retention-enabled").checked = c.retention.enabled;
    $("cfg-message-days").value = c.retention.message_days;
    $("cfg-join-days").value = c.retention.join_request_days;
    $("cfg-mod-days").value = c.retention.moderation_run_days;
    $("cfg-audit-days").value = c.retention.audit_days;
    renderQQAccountEditors();
    renderQQWorkflowEditors();
    renderFeishuEditors();
    activateSettingsStep(state.settingsStep || "qq");
  }

  function updateSettingsReview() {
    if (!state.config) return;
    var bots = collectQQConfiguration();
    var enabled = bots.filter(function (bot) { return bot.enabled; });
    var recorders = enabled.filter(function (bot) { return bot.tasks.message_detection.record_only; });
    var analyzers = enabled.filter(function (bot) { return bot.tasks.message_detection.analyze; });
    var notices = enabled.filter(function (bot) { return bot.tasks.announcement_sync.enabled; });
    var target = $("settings-review");
    target.replaceChildren();
    [
      ["QQ 账号", enabled.length + " / " + bots.length + " 已启用", "每个账号拥有独立连接与 Webhook"],
      ["纯记录", recorders.length + " 个 Bot", $("cfg-message-archive-path").value || "data/group-message-records"],
      ["消息分析", analyzers.length + " 个 Bot", "模型驱动：" + $("cfg-llm-driver").value],
      ["公告同步", notices.length + " 个 Bot", "飞书账号：" + state.config.feishu.bots.length + " 个"],
      ["安全模式", $("cfg-dry-run").checked ? "安全演练已开启" : "真实出站动作已允许", "保存后立即生效"],
      ["数据保留", $("cfg-retention-enabled").checked ? $("cfg-message-days").value + " 天" : "不自动清理", "SQLite 与按日 JSONL 使用同一期限"]
    ].forEach(function (item) {
      var card = document.createElement("article");
      card.innerHTML = "<span></span><strong></strong><small></small>";
      card.querySelector("span").textContent = item[0];
      card.querySelector("strong").textContent = item[1];
      card.querySelector("small").textContent = item[2];
      target.appendChild(card);
    });
  }

  async function loadSettings() {
    var data = await api("/api/gui/settings");
    state.config = data.config;
    normalizeBotConfig(state.config);
    populateSettings(state.config);
  }

  $("settings-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var button = event.submitter;
    button.disabled = true;
    $("settings-state").textContent = "正在校验并热加载…";
    try {
      var c = clone(state.config);
      c.qq.bots = collectQQConfiguration();
      c.feishu.bots = collectFeishuBots();
      c.qq.enabled = c.qq.bots.some(function (bot) { return bot.enabled; });
      c.feishu.enabled = c.feishu.bots.some(function (bot) { return bot.enabled; });
      c.app.environment = $("cfg-environment").value.trim();
      c.app.log_level = $("cfg-log-level").value;
      c.app.dry_run = $("cfg-dry-run").checked;
      c.app.message_archive_path = $("cfg-message-archive-path").value.trim() || "data/group-message-records";
      c.app.admin_api_token = $("cfg-admin-token").value;
      c.gui.secure_cookie = $("cfg-secure-cookie").checked;
      c.llm.driver = $("cfg-llm-driver").value;
      c.llm.model = $("cfg-llm-model").value.trim();
      c.llm.base_url = $("cfg-llm-url").value.trim();
      c.llm.api_key = $("cfg-llm-key").value;
      c.llm.timeout_seconds = numberValue($("cfg-llm-timeout").value, 60);
      c.llm.max_retries = numberValue($("cfg-llm-retries").value, 2);
      c.llm.json_response_format = $("cfg-llm-json").checked;
      c.join_approval.required_keywords = splitList($("cfg-required-keywords").value);
      c.join_approval.forbidden_keywords = splitList($("cfg-forbidden-keywords").value);
      c.join_approval.policy = $("cfg-join-policy").value;
      c.moderation.policy = $("cfg-mod-policy").value;
      c.moderation.rule_keywords = parseJsonText($("cfg-rule-keywords").value, "离线关键词规则");
      c.retention.enabled = $("cfg-retention-enabled").checked;
      c.retention.message_days = numberValue($("cfg-message-days").value, 30);
      c.retention.join_request_days = numberValue($("cfg-join-days").value, 180);
      c.retention.moderation_run_days = numberValue($("cfg-mod-days").value, 365);
      c.retention.audit_days = numberValue($("cfg-audit-days").value, 365);
      var result = await api("/api/gui/settings", { method: "PUT", body: { config: c } });
      $("settings-state").textContent = result.restart_required.length ? "已保存；这些字段需重启：" + result.restart_required.join(", ") : "已保存并热加载";
      showToast("配置已保存");
      await loadSettings();
    } catch (error) {
      $("settings-state").textContent = "保存失败";
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });

  function recordTitle(kind, item) {
    var bot = item.bot_id ? "[" + item.bot_id + "] " : "";
    if (kind === "joins") return bot + "群 " + item.group_id + " · 申请人 " + item.user_id;
    if (kind === "messages") return bot + "群 " + item.group_id + " · 发言人 " + item.user_id;
    if (kind === "moderation") return bot + "群 " + item.group_id + " · 风险 " + item.max_risk;
    if (kind === "announcements") return bot + "群 " + item.group_id + " · " + (item.title || item.announcement_id);
    return item.action + " · " + item.status;
  }

  function recordTime(item) { return item.received_at || item.created_at || item.last_seen_at || item.window_end || ""; }

  async function loadRecords() {
    var kind = $("record-kind").value;
    var data = await api("/api/gui/records/" + kind + "?limit=100");
    var target = $("records-table");
    target.replaceChildren();
    if (!data.records.length) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "还没有相关记录。";
      target.appendChild(empty);
      return;
    }
    var list = document.createElement("div");
    list.className = "record-list";
    data.records.forEach(function (item) {
      var card = document.createElement("article");
      card.className = "record-item";
      var summary = document.createElement("div");
      summary.className = "record-summary";
      var title = document.createElement("strong");
      title.textContent = recordTitle(kind, item);
      var timeNode = document.createElement("span");
      timeNode.textContent = recordTime(item);
      var body = document.createElement("pre");
      body.textContent = JSON.stringify(item, null, 2);
      summary.append(title, timeNode);
      card.append(summary, body);
      list.appendChild(card);
    });
    target.appendChild(list);
  }

  $("record-kind").addEventListener("change", function () { loadRecords().catch(function (error) { showToast(error.message, true); }); });
  $("records-refresh").addEventListener("click", function () { loadRecords().catch(function (error) { showToast(error.message, true); }); });

  initialize();
})();
