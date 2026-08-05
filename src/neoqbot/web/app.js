(function () {
  "use strict";

  var state = {
    csrf: "",
    username: "",
    activeView: "dashboard",
    config: null,
    settingsRevision: "",
    qqUrls: {},
    qqLoginBotId: "",
    qqLoginTimer: null,
    qqLoginPolling: false,
    qqQrRefreshing: false,
    qqQrLastRefresh: 0,
    settingsStep: "qq",
    settingsDirty: false,
    orchestrationNodes: [],
    orchestrationEdges: [],
    orchestrationSelected: "",
    orchestrationDirty: false,
    orchestrationMenuPoint: { x: 120, y: 120 },
    orchestrationDrag: null,
    orchestrationConnect: null,
    orchestrationSearch: "",
    orchestrationStatuses: { qq: [], feishu: [] },
    recordsOffset: 0,
    recordsHasMore: false,
    recordsSearchTimer: null,
    commandIndex: 0
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
    $("theme-button").textContent = theme === "dark" ? "浅色" : "深色";
    try { window.localStorage.setItem("neoqbot-theme", theme); } catch (_) {}
  }

  function initializeTheme() {
    var theme = "dark";
    try { theme = window.localStorage.getItem("neoqbot-theme") || "dark"; } catch (_) {}
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
    integrations: ["Orchestration", "资源编排"],
    settings: ["Configuration", "系统设置"],
    records: ["Audit trail", "记录与审计"]
  };

  async function switchView(name) {
    if (state.activeView === "integrations" && name !== "integrations" && state.orchestrationDirty) {
      if (!window.confirm("编排还有未保存的更改，离开后将丢失。仍要离开吗？")) return;
      state.orchestrationDirty = false;
    }
    if (state.activeView === "settings" && name !== "settings" && state.settingsDirty) {
      if (!window.confirm("系统设置还有未保存的更改，离开后将丢失。仍要离开吗？")) return;
      state.settingsDirty = false;
    }
    state.activeView = name;
    document.querySelectorAll(".view").forEach(function (node) { node.classList.remove("active"); });
    document.querySelectorAll(".nav-item").forEach(function (node) { node.classList.toggle("active", node.dataset.view === name); });
    $("view-" + name).classList.add("active");
    $("page-kicker").textContent = titles[name][0];
    $("page-title").textContent = titles[name][1];
    document.querySelector(".sidebar").classList.remove("open");
    try {
      if (name === "dashboard") await loadDashboard();
      if (name === "integrations") await loadOrchestration();
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
      return text.indexOf("初始密码") >= 0 || text.indexOf("Webhook") >= 0;
    });
    if (important.length) {
      banner.textContent = important.join(" · ");
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }
    var connection = $("connection-pill");
    if ((diagnostics.errors || []).length) {
      connection.textContent = diagnostics.errors.length + " 项配置错误";
      connection.className = "pill health-error";
    } else if ((diagnostics.warnings || []).length) {
      connection.textContent = diagnostics.warnings.length + " 项提醒";
      connection.className = "pill health-warning";
    } else {
      connection.textContent = "服务正常";
      connection.className = "pill health-ok";
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
    var graph = data.orchestration || {};
    $("managed-groups").textContent = (data.managed_groups || []).length + " 个群 · " + (graph.edges || 0) + " 条连接";
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
    return bot.webui_public_url || "";
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
    $("qq-login-new-window").disabled = !state.qqUrls[bot.id];
    $("qq-login-new-window").title = state.qqUrls[bot.id]
      ? "打开显式配置的 NapCat WebUI 地址"
      : "NapCat WebUI 默认仅在容器内部可用";
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

  function safeIdentifier(value, fallback) {
    var cleaned = String(value || "").toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
    return (cleaned || fallback || "resource").slice(0, 52);
  }

  function uniqueIdentifier(prefix, values) {
    var used = new Set(values || []);
    var index = 1;
    var candidate = prefix;
    while (used.has(candidate)) candidate = prefix + "-" + (++index);
    return candidate;
  }

  function graphNode(id) {
    return state.orchestrationNodes.find(function (node) { return node.id === id; });
  }

  function graphResource(id) {
    return (state.config.orchestration.resources || []).find(function (resource) { return resource.id === id; });
  }

  function removeBotFromOrchestration(kind, botId) {
    if (!state.config.orchestration) return;
    var nodeId = kind + ":" + botId;
    state.config.orchestration.edges = (state.config.orchestration.edges || []).filter(function (edge) {
      return edge.source !== nodeId && edge.target !== nodeId;
    });
    if (state.config.orchestration.layout) delete state.config.orchestration.layout[nodeId];
    if (kind === "feishu-bot") {
      (state.config.qq.bots || []).forEach(function (bot) {
        if (bot.search_feishu_bot_id === botId) bot.search_feishu_bot_id = "";
        if (bot.tasks.announcement_sync.feishu_bot_id === botId) bot.tasks.announcement_sync.feishu_bot_id = "";
      });
    }
  }

  function renameBotInOrchestration(kind, previousId, nextId) {
    if (!state.config.orchestration || previousId === nextId) return;
    var previousNodeId = kind + ":" + previousId;
    var nextNodeId = kind + ":" + nextId;
    (state.config.orchestration.edges || []).forEach(function (edge) {
      if (edge.source === previousNodeId) edge.source = nextNodeId;
      if (edge.target === previousNodeId) edge.target = nextNodeId;
    });
    if (state.config.orchestration.layout && state.config.orchestration.layout[previousNodeId]) {
      state.config.orchestration.layout[nextNodeId] = state.config.orchestration.layout[previousNodeId];
      delete state.config.orchestration.layout[previousNodeId];
    }
    if (kind === "feishu-bot") {
      (state.config.qq.bots || []).forEach(function (bot) {
        if (bot.search_feishu_bot_id === previousId) bot.search_feishu_bot_id = nextId;
        if (bot.tasks.announcement_sync.feishu_bot_id === previousId) {
          bot.tasks.announcement_sync.feishu_bot_id = nextId;
        }
      });
    }
  }

  function defaultNodePosition(kind, index) {
    var columns = { qq_bot: 80, feishu_bot: 80, qq_group: 430, feishu_group: 430, knowledge_base: 790 };
    var laneOffset = kind === "feishu_bot" || kind === "feishu_group" ? 300 : 70;
    return { x: columns[kind] || 80, y: laneOffset + (index % 5) * 145 };
  }

  function nextEdgeId(source, target, relation, edges) {
    var prefix = safeIdentifier("edge-" + source + "-" + target + "-" + relation, "edge").slice(0, 82);
    return uniqueIdentifier(prefix, (edges || state.orchestrationEdges).map(function (edge) { return edge.id; }));
  }

  function normalizeOrchestration(config) {
    normalizeBotConfig(config);
    config.orchestration = config.orchestration || {};
    config.orchestration.resources = config.orchestration.resources || [];
    config.orchestration.edges = config.orchestration.edges || [];
    config.orchestration.layout = config.orchestration.layout || {};

    var nodes = [];
    config.qq.bots.forEach(function (bot, index) {
      nodes.push({ id: "qq-bot:" + bot.id, kind: "qq_bot", name: bot.name, enabled: bot.enabled, ref: bot });
      if (!config.orchestration.layout["qq-bot:" + bot.id]) config.orchestration.layout["qq-bot:" + bot.id] = defaultNodePosition("qq_bot", index);
    });
    config.feishu.bots.forEach(function (bot, index) {
      nodes.push({ id: "feishu-bot:" + bot.id, kind: "feishu_bot", name: bot.name, enabled: bot.enabled, ref: bot });
      if (!config.orchestration.layout["feishu-bot:" + bot.id]) config.orchestration.layout["feishu-bot:" + bot.id] = defaultNodePosition("feishu_bot", index);
    });
    config.orchestration.resources.forEach(function (resource, index) {
      nodes.push({ id: resource.id, kind: resource.kind, name: resource.name, enabled: resource.enabled, ref: resource });
      if (!config.orchestration.layout[resource.id]) config.orchestration.layout[resource.id] = defaultNodePosition(resource.kind, index);
    });
    state.orchestrationNodes = nodes;
    state.orchestrationEdges = config.orchestration.edges;
  }

  function nodeKindLabel(kind) {
    return ({ qq_bot: "QQ / ONEBOT", feishu_bot: "FEISHU BOT", qq_group: "QQ GROUP", feishu_group: "FEISHU GROUP", knowledge_base: "KNOWLEDGE" })[kind] || kind;
  }

  function nodeGlyph(kind) {
    return ({ qq_bot: "Q", feishu_bot: "F", qq_group: "#", feishu_group: "群", knowledge_base: "▤" })[kind] || "·";
  }

  function relationLabel(relation) {
    return ({ manages: "管理", observes: "监听", archives_to: "归档", searches: "检索", syncs: "同步" })[relation] || relation;
  }

  function defaultRelation(source, target) {
    if (!source || !target) return "syncs";
    if (/bot$/.test(source.kind) && /group$/.test(target.kind)) return "manages";
    if (target.kind === "knowledge_base") return source.kind === "feishu_bot" ? "searches" : "archives_to";
    if (target.kind === "feishu_bot") return "archives_to";
    return "syncs";
  }

  function graphPath(startX, startY, endX, endY) {
    var bend = Math.max(70, Math.abs(endX - startX) * 0.48);
    return "M " + startX + " " + startY + " C " + (startX + bend) + " " + startY + ", " + (endX - bend) + " " + endY + ", " + endX + " " + endY;
  }

  function renderOrchestrationEdges() {
    var layer = $("orchestration-edge-layer");
    layer.replaceChildren();
    var canvas = $("orchestration-canvas");
    var canvasRect = canvas.getBoundingClientRect();
    state.orchestrationEdges.forEach(function (edge) {
      var sourceNode = document.querySelector('.graph-node[data-node-id="' + CSS.escape(edge.source) + '"]');
      var targetNode = document.querySelector('.graph-node[data-node-id="' + CSS.escape(edge.target) + '"]');
      if (!sourceNode || !targetNode) return;
      if (sourceNode.classList.contains("search-muted") || targetNode.classList.contains("search-muted")) return;
      var sourceRect = sourceNode.getBoundingClientRect();
      var targetRect = targetNode.getBoundingClientRect();
      var startX = sourceRect.right - canvasRect.left;
      var startY = sourceRect.top + sourceRect.height / 2 - canvasRect.top;
      var endX = targetRect.left - canvasRect.left;
      var endY = targetRect.top + targetRect.height / 2 - canvasRect.top;
      var group = document.createElementNS("http://www.w3.org/2000/svg", "g");
      group.classList.add("edge-group");
      group.dataset.edgeId = edge.id;
      var hit = document.createElementNS("http://www.w3.org/2000/svg", "path");
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      var d = graphPath(startX, startY, endX, endY);
      hit.setAttribute("d", d);
      hit.setAttribute("class", "graph-edge-hit");
      path.setAttribute("d", d);
      path.setAttribute("class", "graph-edge" + (edge.enabled ? "" : " disabled"));
      path.setAttribute("marker-end", "url(#edge-arrow)");
      var label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", String((startX + endX) / 2));
      label.setAttribute("y", String((startY + endY) / 2 - 8));
      label.setAttribute("class", "graph-edge-label");
      label.textContent = relationLabel(edge.relation);
      hit.addEventListener("dblclick", function () {
        state.orchestrationEdges.splice(state.orchestrationEdges.indexOf(edge), 1);
        markOrchestrationDirty();
        renderOrchestration();
      });
      group.append(hit, path, label);
      layer.appendChild(group);
    });
  }

  function graphConnectionCount(nodeId) {
    return state.orchestrationEdges.filter(function (edge) { return edge.source === nodeId || edge.target === nodeId; }).length;
  }

  function renderOrchestration() {
    var layer = $("orchestration-node-layer");
    layer.replaceChildren();
    var search = state.orchestrationSearch.trim().toLowerCase();
    var matchCount = 0;
    state.orchestrationNodes.forEach(function (node) {
      var position = state.config.orchestration.layout[node.id] || { x: 80, y: 80 };
      var searchable = [node.name, node.id, node.ref.external_id, nodeKindLabel(node.kind)].join(" ").toLowerCase();
      var matchesSearch = !search || searchable.indexOf(search) >= 0;
      if (matchesSearch) matchCount++;
      var card = document.createElement("article");
      card.className = "graph-node " + node.kind + (node.enabled ? "" : " disabled") + (state.orchestrationSelected === node.id ? " selected" : "") + (matchesSearch ? "" : " search-muted") + (search && matchesSearch ? " search-match" : "");
      card.dataset.nodeId = node.id;
      card.tabIndex = 0;
      card.style.left = position.x + "px";
      card.style.top = position.y + "px";
      card.innerHTML = '<div class="node-accent"></div><div class="node-head"><span class="node-glyph"></span><div><span class="node-kind"></span><strong class="node-name"></strong></div><i class="node-state"></i></div><div class="node-meta"><span class="node-detail"></span><span class="node-links"></span></div><button class="node-port" type="button" title="拖动以连接"></button>';
      card.querySelector(".node-glyph").textContent = nodeGlyph(node.kind);
      card.querySelector(".node-kind").textContent = nodeKindLabel(node.kind);
      card.querySelector(".node-name").textContent = node.name;
      card.querySelector(".node-detail").textContent = node.ref.external_id || node.ref.id || "未配置标识";
      card.querySelector(".node-links").textContent = graphConnectionCount(node.id) + " links";
      card.querySelector(".node-port").setAttribute("aria-label", "从 " + node.name + " 创建连接");
      card.addEventListener("click", function () {
        state.orchestrationSelected = node.id;
        renderOrchestration();
        renderOrchestrationInspector(node.id);
      });
      card.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        state.orchestrationSelected = node.id;
        renderOrchestration();
        renderOrchestrationInspector(node.id);
      });
      card.addEventListener("pointerdown", function (event) {
        if (event.button !== 0 || event.target.closest("button, input, textarea, select")) return;
        state.orchestrationDrag = { id: node.id, startX: event.clientX, startY: event.clientY, x: position.x, y: position.y, moved: false };
        card.setPointerCapture(event.pointerId);
      });
      card.querySelector(".node-port").addEventListener("pointerdown", function (event) {
        event.preventDefault();
        event.stopPropagation();
        var rect = card.getBoundingClientRect();
        var canvasRect = $("orchestration-canvas").getBoundingClientRect();
        state.orchestrationConnect = { source: node.id, startX: rect.right - canvasRect.left, startY: rect.top + rect.height / 2 - canvasRect.top };
        this.setPointerCapture(event.pointerId);
      });
      layer.appendChild(card);
    });
    $("orchestration-empty").classList.toggle("hidden", state.orchestrationNodes.length > 0);
    $("orchestration-counts").textContent = search ? matchCount + " / " + state.orchestrationNodes.length + " 个节点匹配" : state.orchestrationNodes.length + " 个节点 · " + state.orchestrationEdges.length + " 条连接";
    window.requestAnimationFrame(renderOrchestrationEdges);
  }

  function focusOrchestrationNode(nodeId) {
    var node = graphNode(nodeId);
    if (!node) return;
    state.orchestrationSelected = nodeId;
    renderOrchestration();
    renderOrchestrationInspector(nodeId);
    var position = state.config.orchestration.layout[nodeId] || { x: 0, y: 0 };
    var viewport = $("orchestration-viewport");
    viewport.scrollTo({
      left: Math.max(0, position.x - viewport.clientWidth / 2 + 110),
      top: Math.max(0, position.y - viewport.clientHeight / 2 + 55),
      behavior: "smooth"
    });
  }

  function canvasPoint(clientX, clientY) {
    var canvas = $("orchestration-canvas");
    var rect = canvas.getBoundingClientRect();
    return { x: clientX - rect.left, y: clientY - rect.top };
  }

  function markOrchestrationDirty() {
    state.orchestrationDirty = true;
    $("orchestration-state").textContent = "有未保存的更改";
    $("orchestration-save").disabled = false;
  }

  function syncOrchestrationToBots() {
    var resources = new Map((state.config.orchestration.resources || []).map(function (resource) { return [resource.id, resource]; }));
    state.config.qq.bots.forEach(function (bot) {
      var nodeId = "qq-bot:" + bot.id;
      bot.managed_group_ids = state.orchestrationEdges.filter(function (edge) {
        var resource = resources.get(edge.target);
        return edge.enabled && edge.source === nodeId && ["manages", "observes"].indexOf(edge.relation) >= 0 && resource && resource.kind === "qq_group" && resource.external_id;
      }).map(function (edge) { return resources.get(edge.target).external_id; }).filter(function (value, index, values) { return values.indexOf(value) === index; });
      var archive = state.orchestrationEdges.find(function (edge) {
        return edge.enabled && edge.source === nodeId && edge.target.indexOf("feishu-bot:") === 0 && ["archives_to", "syncs"].indexOf(edge.relation) >= 0;
      });
      if (archive) bot.tasks.announcement_sync.feishu_bot_id = archive.target.replace("feishu-bot:", "");
      var search = state.orchestrationEdges.find(function (edge) {
        return edge.enabled && edge.source === nodeId && edge.target.indexOf("feishu-bot:") === 0 && edge.relation === "searches";
      });
      if (search) bot.search_feishu_bot_id = search.target.replace("feishu-bot:", "");
    });
    state.config.orchestration.edges = state.orchestrationEdges;
    state.config.qq.enabled = state.config.qq.bots.some(function (bot) { return bot.enabled; });
    state.config.feishu.enabled = state.config.feishu.bots.some(function (bot) { return bot.enabled; });
  }

  function statusForNode(node) {
    var list = node.kind === "qq_bot" ? state.orchestrationStatuses.qq : state.orchestrationStatuses.feishu;
    return list.find(function (item) { return item.id === node.ref.id; });
  }

  function inspectorSection(title) {
    var section = document.createElement("section");
    section.className = "inspector-section";
    var heading = document.createElement("h4");
    heading.textContent = title;
    section.appendChild(heading);
    return section;
  }

  function appendMiniRecords(section, records, emptyText) {
    var list = document.createElement("div");
    list.className = "inspector-records";
    (records || []).slice(0, 8).forEach(function (record) {
      var item = document.createElement("article");
      var title = document.createElement("strong");
      var text = document.createElement("p");
      var time = document.createElement("small");
      title.textContent = record.title || record.user_id || record.announcement_id || record.message_id || "记录";
      text.textContent = record.text || record.content || (record.result_json && JSON.stringify(record.result_json)) || "";
      time.textContent = record.sent_at || record.last_seen_at || record.created_at || record.received_at || "";
      item.append(title, text, time);
      list.appendChild(item);
    });
    if (!list.children.length) {
      var empty = document.createElement("p");
      empty.className = "inspector-muted";
      empty.textContent = emptyText;
      list.appendChild(empty);
    }
    section.appendChild(list);
  }

  async function renderGroupActivity(node, host) {
    if (!node.ref.external_id) {
      host.textContent = "填写群号并保存编排后，即可读取这个群的聊天、公告和分析记录。";
      return;
    }
    host.textContent = "正在读取群数据…";
    try {
      var url = "/api/gui/orchestration/group?group_id=" + encodeURIComponent(node.ref.external_id) + "&resource_id=" + encodeURIComponent(node.ref.id) + "&limit=30";
      var data = await api(url);
      host.replaceChildren();
      var metrics = document.createElement("div");
      metrics.className = "inspector-metrics";
      [["消息", data.counts.messages], ["公告", data.counts.announcements], ["分析", data.counts.moderation], ["申请", data.counts.joins]].forEach(function (item) {
        var metric = document.createElement("div");
        metric.innerHTML = "<span></span><strong></strong>";
        metric.querySelector("span").textContent = item[0];
        metric.querySelector("strong").textContent = item[1] || 0;
        metrics.appendChild(metric);
      });
      host.appendChild(metrics);
      var manager = document.createElement("p");
      manager.className = "inspector-muted";
      manager.textContent = data.managers.length ? "管理 Bot：" + data.managers.map(function (bot) { return bot.name; }).join("、") : "尚未连接管理 Bot";
      host.appendChild(manager);
      var messages = inspectorSection("最近消息");
      appendMiniRecords(messages, data.records.messages, "还没有采集到群消息。");
      var announcements = inspectorSection("群公告");
      appendMiniRecords(announcements, data.records.announcements, "还没有归档群公告。");
      host.append(messages, announcements);
    } catch (error) {
      host.textContent = error.message;
    }
  }

  function renderOrchestrationInspector(nodeId) {
    var node = graphNode(nodeId);
    var empty = $("inspector-empty");
    var content = $("inspector-content");
    if (!node) {
      empty.classList.remove("hidden");
      content.classList.add("hidden");
      return;
    }
    empty.classList.add("hidden");
    content.classList.remove("hidden");
    content.replaceChildren();
    var header = document.createElement("header");
    header.className = "inspector-header";
    header.innerHTML = '<span class="node-glyph"></span><div><small></small><h3></h3></div><button class="inspector-close" type="button">×</button>';
    header.querySelector(".node-glyph").textContent = nodeGlyph(node.kind);
    header.querySelector("small").textContent = nodeKindLabel(node.kind);
    header.querySelector("h3").textContent = node.name;
    header.querySelector(".inspector-close").addEventListener("click", function () {
      state.orchestrationSelected = "";
      renderOrchestration();
      renderOrchestrationInspector("");
    });
    content.appendChild(header);

    var form = document.createElement("div");
    form.className = "inspector-form";
    form.innerHTML = '<label>显示名称<input data-inspector="name"></label>' +
      (node.kind === "qq_bot" || node.kind === "feishu_bot" ? '<label>内部 ID<input data-inspector="id" disabled></label>' : '<label>平台标识<input data-inspector="external_id" placeholder="群号、chat_id 或知识库 ID"></label>') +
      '<label class="switch-field"><span><strong>启用资源</strong><small>停用后保留配置与连接</small></span><input data-inspector="enabled" type="checkbox"></label>';
    form.querySelector('[data-inspector="name"]').value = node.ref.name;
    var idField = form.querySelector('[data-inspector="id"]');
    if (idField) idField.value = node.ref.id;
    var externalField = form.querySelector('[data-inspector="external_id"]');
    if (externalField) externalField.value = node.ref.external_id || "";
    form.querySelector('[data-inspector="enabled"]').checked = node.ref.enabled;
    form.querySelectorAll("input").forEach(function (input) {
      if (input.disabled) return;
      input.addEventListener("input", function () {
        if (input.dataset.inspector === "enabled") node.ref.enabled = input.checked;
        else node.ref[input.dataset.inspector] = input.value;
        node.name = node.ref.name;
        node.enabled = node.ref.enabled;
        markOrchestrationDirty();
        renderOrchestration();
      });
      input.addEventListener("change", function () {
        if (input.dataset.inspector === "enabled") {
          node.ref.enabled = input.checked;
          node.enabled = input.checked;
          markOrchestrationDirty();
          renderOrchestration();
        }
      });
    });
    content.appendChild(form);

    if (node.kind === "qq_bot" || node.kind === "feishu_bot") {
      var status = statusForNode(node);
      var statusSection = inspectorSection("运行状态");
      var output = document.createElement("pre");
      output.className = "inspector-status";
      output.textContent = node.kind === "qq_bot" ? (status ? status.connection_message : "状态读取中…") : (status ? statusText(status.status) : "状态读取中…");
      var actions = document.createElement("div");
      actions.className = "button-row";
      if (node.kind === "qq_bot") {
        var login = integrationButton("扫码登录", function () { if (status) openQQLogin(status); });
        actions.append(login);
      } else {
        actions.append(
          integrationButton("登录", function () { feishuAction(node.ref.id, "login", this, output); }),
          integrationButton("退出", function () { feishuAction(node.ref.id, "logout", this, output); })
        );
      }
      var configure = integrationButton("打开完整设置", function () {
        state.settingsStep = node.kind === "qq_bot" ? "qq" : "feishu";
        switchView("settings");
      });
      actions.append(configure);
      statusSection.append(output, actions);
      content.appendChild(statusSection);
    } else {
      var descriptionSection = inspectorSection("资源说明");
      var description = document.createElement("textarea");
      description.rows = 3;
      description.placeholder = "记录用途、负责人或数据边界";
      description.value = node.ref.description || "";
      description.addEventListener("input", function () { node.ref.description = description.value; markOrchestrationDirty(); });
      descriptionSection.appendChild(description);
      content.appendChild(descriptionSection);
      if (node.kind === "knowledge_base") {
        var metadataSection = inspectorSection("知识库来源");
        metadataSection.innerHTML += '<label>Provider<input data-meta="provider" placeholder="Feishu / Notion / Local"></label><label>访问地址<input data-meta="url" placeholder="https://..."></label>';
        node.ref.metadata = node.ref.metadata || {};
        metadataSection.querySelectorAll("input").forEach(function (input) {
          input.value = node.ref.metadata[input.dataset.meta] || "";
          input.addEventListener("input", function () { node.ref.metadata[input.dataset.meta] = input.value; markOrchestrationDirty(); });
        });
        content.appendChild(metadataSection);
      } else {
        var activity = inspectorSection("群数据");
        var activityHost = document.createElement("div");
        activityHost.className = "group-activity";
        activity.appendChild(activityHost);
        content.appendChild(activity);
        renderGroupActivity(node, activityHost);
      }
    }

    var connections = inspectorSection("连接");
    var connectionList = document.createElement("div");
    connectionList.className = "connection-list";
    state.orchestrationEdges.filter(function (edge) { return edge.source === node.id || edge.target === node.id; }).forEach(function (edge) {
      var other = graphNode(edge.source === node.id ? edge.target : edge.source);
      var row = document.createElement("div");
      row.innerHTML = '<span></span><select><option value="manages">管理</option><option value="observes">监听</option><option value="archives_to">归档</option><option value="searches">检索</option><option value="syncs">同步</option></select><button type="button" title="删除连接">×</button>';
      row.querySelector("span").textContent = other ? other.name : "未知节点";
      row.querySelector("select").value = edge.relation;
      row.querySelector("select").addEventListener("change", function () { edge.relation = this.value; markOrchestrationDirty(); renderOrchestrationEdges(); });
      row.querySelector("button").addEventListener("click", function () {
        state.orchestrationEdges.splice(state.orchestrationEdges.indexOf(edge), 1);
        markOrchestrationDirty();
        renderOrchestration();
        renderOrchestrationInspector(node.id);
      });
      connectionList.appendChild(row);
    });
    if (!connectionList.children.length) {
      var hint = document.createElement("p");
      hint.className = "inspector-muted";
      hint.textContent = "从节点右侧端口拖到另一个节点即可创建连接。";
      connectionList.appendChild(hint);
    }
    connections.appendChild(connectionList);
    content.appendChild(connections);

    var danger = document.createElement("button");
    danger.type = "button";
    danger.className = "button danger full";
    danger.textContent = "从编排中删除";
    danger.addEventListener("click", deleteSelectedOrchestrationNode);
    content.appendChild(danger);
  }

  function deleteSelectedOrchestrationNode() {
    var node = graphNode(state.orchestrationSelected);
    if (!node) return;
    if (node.kind === "qq_bot" && state.config.qq.bots.length <= 1) return showToast("至少保留一个 QQ Bot，可将其停用", true);
    if (node.kind === "feishu_bot" && state.config.feishu.bots.length <= 1) return showToast("至少保留一个飞书 Bot，可将其停用", true);
    if (!window.confirm("删除“" + node.name + "”及其所有连接？更改将在保存编排后生效。")) return;
    if (node.kind === "qq_bot") state.config.qq.bots = state.config.qq.bots.filter(function (bot) { return "qq-bot:" + bot.id !== node.id; });
    else if (node.kind === "feishu_bot") state.config.feishu.bots = state.config.feishu.bots.filter(function (bot) { return "feishu-bot:" + bot.id !== node.id; });
    else state.config.orchestration.resources = state.config.orchestration.resources.filter(function (resource) { return resource.id !== node.id; });
    state.config.orchestration.edges = state.config.orchestration.edges.filter(function (edge) { return edge.source !== node.id && edge.target !== node.id; });
    delete state.config.orchestration.layout[node.id];
    state.orchestrationSelected = "";
    normalizeOrchestration(state.config);
    markOrchestrationDirty();
    renderOrchestration();
    renderOrchestrationInspector("");
  }

  function createOrchestrationNode(kind) {
    var point = state.orchestrationMenuPoint;
    if (kind === "qq_bot") {
      var qq = defaultQQBot(state.config.qq.bots.length);
      qq.id = uniqueIdentifier("qq-bot-" + (state.config.qq.bots.length + 1), state.config.qq.bots.map(function (bot) { return bot.id; }));
      qq.name = "新 QQ Bot";
      state.config.qq.bots.push(qq);
      state.config.orchestration.layout["qq-bot:" + qq.id] = point;
      state.orchestrationSelected = "qq-bot:" + qq.id;
    } else if (kind === "feishu_bot") {
      var fs = defaultFeishuBot(state.config.feishu.bots.length);
      fs.id = uniqueIdentifier("feishu-bot-" + (state.config.feishu.bots.length + 1), state.config.feishu.bots.map(function (bot) { return bot.id; }));
      fs.name = "新飞书 Bot";
      state.config.feishu.bots.push(fs);
      state.config.orchestration.layout["feishu-bot:" + fs.id] = point;
      state.orchestrationSelected = "feishu-bot:" + fs.id;
    } else {
      var labels = { qq_group: "新 QQ 群", feishu_group: "新飞书群", knowledge_base: "新知识库" };
      var resource = {
        id: uniqueIdentifier(kind.replace("_", "-") + "-1", state.config.orchestration.resources.map(function (item) { return item.id; })),
        kind: kind,
        name: labels[kind],
        external_id: "",
        description: "",
        enabled: true,
        metadata: {}
      };
      state.config.orchestration.resources.push(resource);
      state.config.orchestration.layout[resource.id] = point;
      state.orchestrationSelected = resource.id;
    }
    normalizeOrchestration(state.config);
    markOrchestrationDirty();
    renderOrchestration();
    renderOrchestrationInspector(state.orchestrationSelected);
    $("orchestration-menu").classList.add("hidden");
  }

  function autoLayoutOrchestration() {
    var lanes = { qq_bot: [], feishu_bot: [], qq_group: [], feishu_group: [], knowledge_base: [] };
    state.orchestrationNodes.forEach(function (node) { lanes[node.kind].push(node); });
    Object.keys(lanes).forEach(function (kind) {
      lanes[kind].forEach(function (node, index) { state.config.orchestration.layout[node.id] = defaultNodePosition(kind, index); });
    });
    markOrchestrationDirty();
    renderOrchestration();
  }

  function fitOrchestration() {
    var viewport = $("orchestration-viewport");
    var positions = Object.values(state.config.orchestration.layout || {});
    if (!positions.length) return;
    var minX = Math.max(0, Math.min.apply(null, positions.map(function (position) { return position.x; })) - 50);
    var minY = Math.max(0, Math.min.apply(null, positions.map(function (position) { return position.y; })) - 50);
    viewport.scrollTo({ left: minX, top: minY, behavior: "smooth" });
  }

  async function saveOrchestration() {
    var button = $("orchestration-save");
    button.disabled = true;
    $("orchestration-state").textContent = "正在校验并热加载…";
    try {
      syncOrchestrationToBots();
      var result = await api("/api/gui/settings", {
        method: "PUT",
        body: { config: state.config, revision: state.settingsRevision }
      });
      state.settingsRevision = result.revision || state.settingsRevision;
      state.orchestrationDirty = false;
      $("orchestration-state").textContent = result.restart_required.length ? "已保存，部分连接参数需重启" : "配置已同步";
      showToast("编排已保存并应用");
      await loadOrchestration();
    } catch (error) {
      $("orchestration-state").textContent = error.status === 409 ? "检测到配置冲突，请刷新后重试" : "保存失败";
      showToast(error.message, true);
      button.disabled = false;
    }
  }

  async function loadOrchestration() {
    var settings = await api("/api/gui/settings");
    state.config = settings.config;
    state.settingsRevision = settings.revision || "";
    normalizeOrchestration(state.config);
    state.orchestrationDirty = false;
    $("orchestration-state").textContent = "配置已同步";
    $("orchestration-save").disabled = true;
    renderOrchestration();
    if (state.orchestrationSelected && graphNode(state.orchestrationSelected)) renderOrchestrationInspector(state.orchestrationSelected);
    var statuses = await Promise.allSettled([api("/api/gui/integrations/qq"), api("/api/gui/integrations/feishu")]);
    state.orchestrationStatuses.qq = statuses[0].status === "fulfilled" ? statuses[0].value.bots || [] : [];
    state.orchestrationStatuses.feishu = statuses[1].status === "fulfilled" ? statuses[1].value.bots || [] : [];
    renderOrchestration();
    if (state.orchestrationSelected) renderOrchestrationInspector(state.orchestrationSelected);
  }

  $("orchestration-canvas").addEventListener("contextmenu", function (event) {
    if (event.target.closest(".graph-node")) return;
    event.preventDefault();
    state.orchestrationMenuPoint = canvasPoint(event.clientX, event.clientY);
    var menu = $("orchestration-menu");
    menu.style.left = Math.min(event.clientX, window.innerWidth - 250) + "px";
    menu.style.top = Math.min(event.clientY, window.innerHeight - 340) + "px";
    menu.classList.remove("hidden");
  });
  $("orchestration-add").addEventListener("click", function () {
    var viewport = $("orchestration-viewport");
    state.orchestrationMenuPoint = { x: viewport.scrollLeft + 150, y: viewport.scrollTop + 120 };
    var rect = this.getBoundingClientRect();
    var menu = $("orchestration-menu");
    menu.style.left = rect.left + "px";
    menu.style.top = (rect.bottom + 8) + "px";
    menu.classList.toggle("hidden");
  });
  document.querySelectorAll("[data-create-kind]").forEach(function (button) {
    button.addEventListener("click", function () { createOrchestrationNode(button.dataset.createKind); });
  });
  document.addEventListener("pointerdown", function (event) {
    if (!event.target.closest("#orchestration-menu, #orchestration-add")) $("orchestration-menu").classList.add("hidden");
  });
  window.addEventListener("pointermove", function (event) {
    if (state.orchestrationDrag) {
      var drag = state.orchestrationDrag;
      var position = state.config.orchestration.layout[drag.id];
      position.x = Math.max(20, drag.x + event.clientX - drag.startX);
      position.y = Math.max(20, drag.y + event.clientY - drag.startY);
      drag.moved = true;
      var card = document.querySelector('.graph-node[data-node-id="' + CSS.escape(drag.id) + '"]');
      if (card) { card.style.left = position.x + "px"; card.style.top = position.y + "px"; }
      renderOrchestrationEdges();
    }
    if (state.orchestrationConnect) {
      var point = canvasPoint(event.clientX, event.clientY);
      $("orchestration-temp-edge").setAttribute("d", graphPath(state.orchestrationConnect.startX, state.orchestrationConnect.startY, point.x, point.y));
    }
  });
  window.addEventListener("pointerup", function (event) {
    if (state.orchestrationDrag) {
      if (state.orchestrationDrag.moved) markOrchestrationDirty();
      state.orchestrationDrag = null;
    }
    if (state.orchestrationConnect) {
      var targetElement = document.elementFromPoint(event.clientX, event.clientY);
      var targetCard = targetElement && targetElement.closest(".graph-node");
      var sourceId = state.orchestrationConnect.source;
      if (targetCard && targetCard.dataset.nodeId !== sourceId) {
        var targetId = targetCard.dataset.nodeId;
        var duplicate = state.orchestrationEdges.some(function (edge) { return edge.source === sourceId && edge.target === targetId; });
        if (!duplicate) {
          var relation = defaultRelation(graphNode(sourceId), graphNode(targetId));
          state.orchestrationEdges.push({ id: nextEdgeId(sourceId, targetId, relation), source: sourceId, target: targetId, relation: relation, enabled: true });
          markOrchestrationDirty();
        } else showToast("这两个节点已经连接", true);
      }
      state.orchestrationConnect = null;
      $("orchestration-temp-edge").setAttribute("d", "");
      renderOrchestration();
      if (state.orchestrationSelected) renderOrchestrationInspector(state.orchestrationSelected);
    }
  });
  window.addEventListener("resize", function () { if (state.activeView === "integrations") renderOrchestrationEdges(); });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") $("orchestration-menu").classList.add("hidden");
    if (state.activeView !== "integrations" || !state.orchestrationSelected || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    if (event.key === "Delete" || event.key === "Backspace") deleteSelectedOrchestrationNode();
  });
  $("orchestration-auto-layout").addEventListener("click", autoLayoutOrchestration);
  $("orchestration-fit").addEventListener("click", fitOrchestration);
  $("orchestration-save").addEventListener("click", saveOrchestration);
  $("orchestration-search").addEventListener("input", function () {
    state.orchestrationSearch = this.value;
    renderOrchestration();
  });
  $("orchestration-search").addEventListener("keydown", function (event) {
    if (event.key !== "Enter") return;
    var query = state.orchestrationSearch.trim().toLowerCase();
    if (!query) return;
    var match = state.orchestrationNodes.find(function (node) {
      return [node.name, node.id, node.ref.external_id, nodeKindLabel(node.kind)].join(" ").toLowerCase().indexOf(query) >= 0;
    });
    if (match) focusOrchestrationNode(match.id);
    else showToast("没有找到匹配的编排节点", true);
  });
  window.addEventListener("beforeunload", function (event) {
    if (!state.orchestrationDirty && !state.settingsDirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

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
    if (!state.qqLoginBotId || !state.qqUrls[state.qqLoginBotId]) {
      showToast("NapCat WebUI 默认未发布到宿主机；扫码登录不受影响", true);
      return;
    }
    window.open(state.qqUrls[state.qqLoginBotId], "_blank", "noopener");
  });
  $("qq-login-dialog").addEventListener("cancel", function (event) {
    event.preventDefault();
    closeQQLogin();
  });

  var settingsSteps = ["qq", "tasks", "feishu", "llm", "policy", "storage", "system", "review"];

  function markSettingsDirty() {
    state.settingsDirty = true;
    $("settings-state").textContent = "有未保存的更改";
  }

  $("settings-form").addEventListener("input", markSettingsDirty);
  $("settings-form").addEventListener("change", markSettingsDirty);

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
    c.feishu.bots = c.feishu.bots || [];
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

  function sensitiveSettingsLocked() {
    return !state.config || !state.config.gui || !state.config.gui.allow_sensitive_settings_edits;
  }

  function lockSensitiveControl(control) {
    if (!control || !sensitiveSettingsLocked()) return;
    control.disabled = true;
    control.title = "该部署安全字段已锁定，请在服务器配置文件中修改";
  }

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
        '<div class="orchestration-link-card span-2"><div><strong>托管群与协作关系</strong><small data-managed-summary>尚未连接群</small></div><button type="button" class="button secondary" data-open-orchestration>打开资源编排</button></div>',
        '<label class="span-2">管理员 QQ<textarea data-field="administrator_qq_ids" rows="2" placeholder="每行一个 QQ 号"></textarea></label>',
        '<label>NapCat 公网端口<input data-field="webui_public_port" type="number"></label>',
        '<label>NapCat 公网 URL<input data-field="webui_public_url" placeholder="留空自动按端口生成"></label>',
        '<label class="span-2">二维码挂载路径<input data-field="qrcode_path" placeholder="/app/napcat-cache/qrcode.png"><small>Compose 默认已配置，一般无需修改</small></label>',
        '<label class="span-2">公告动作名<textarea data-field="announcement_actions" rows="2"></textarea></label>',
        '<label class="span-2">管理员搜索使用的飞书 Bot ID<input data-field="search_feishu_bot_id" placeholder="留空使用默认飞书 Bot"></label>',
        '</div><div class="connection-hint"><strong>扫码登录</strong><span>保存连接后，到“资源编排”点击“扫码登录”，扫描对应 NapCat 实例生成的二维码。</span></div></div>'
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
      card.querySelector("[data-managed-summary]").textContent = (bot.managed_group_ids || []).length ? "已连接 " + bot.managed_group_ids.length + " 个 QQ 群" : "尚未连接 QQ 群";
      setField(card, "administrator_qq_ids", listValue(bot.administrator_qq_ids));
      setField(card, "webui_public_port", bot.webui_public_port);
      setField(card, "webui_public_url", bot.webui_public_url || "");
      setField(card, "qrcode_path", bot.qrcode_path || "/app/napcat-cache/qrcode.png");
      setField(card, "announcement_actions", listValue(bot.announcement_actions));
      setField(card, "search_feishu_bot_id", bot.search_feishu_bot_id || "");
      [
        "onebot_base_url", "access_token", "webhook_secret", "webui_public_port",
        "webui_public_url", "qrcode_path", "announcement_actions"
      ].forEach(function (name) { lockSensitiveControl(field(card, name)); });
      card.querySelector("[data-open-orchestration]").addEventListener("click", function () {
        switchView("integrations");
      });
      card.querySelector("[data-remove]").addEventListener("click", function () {
        if (state.config.qq.bots.length <= 1) return showToast("至少保留一个 QQ Bot，可将其停用", true);
        if (!window.confirm("删除“" + bot.name + "”及其编排连接？保存设置后生效。")) return;
        collectQQConfiguration();
        removeBotFromOrchestration("qq-bot", bot.id);
        state.config.qq.bots.splice(index, 1);
        renderQQAccountEditors();
        renderQQWorkflowEditors();
        markSettingsDirty();
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
      var nextId = field(card, "id").value.trim();
      renameBotInOrchestration("qq-bot", original.id, nextId);
      bots[index] = Object.assign(original, {
        id: nextId,
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
        managed_group_ids: original.managed_group_ids || [],
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
      ["executable", "command_templates", "archive_payload_stdin", "extra_environment"].forEach(
        function (name) { lockSensitiveControl(field(card, name)); }
      );
      card.querySelector("[data-remove]").addEventListener("click", function () {
        if (state.config.feishu.bots.length <= 1) return showToast("至少保留一个飞书 Bot，可将其停用", true);
        if (!window.confirm("删除“" + bot.name + "”及其编排连接？保存设置后生效。")) return;
        collectFeishuBots();
        removeBotFromOrchestration("feishu-bot", bot.id);
        state.config.feishu.bots.splice(index, 1);
        renderFeishuEditors();
        markSettingsDirty();
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
      var nextId = field(card, "id").value.trim();
      renameBotInOrchestration("feishu-bot", original.id, nextId);
      var incomingEnvironment = parseJsonText(field(card, "extra_environment").value, "飞书环境变量");
      Object.keys(incomingEnvironment).forEach(function (key) {
        if (["", "***"].indexOf(incomingEnvironment[key]) >= 0 && original.extra_environment && original.extra_environment[key]) {
          incomingEnvironment[key] = original.extra_environment[key];
        }
      });
      bots.push({
        id: nextId,
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
    markSettingsDirty();
  });

  $("add-feishu-bot").addEventListener("click", function () {
    collectFeishuBots();
    state.config.feishu.bots.push(defaultFeishuBot(state.config.feishu.bots.length));
    renderFeishuEditors();
    markSettingsDirty();
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
    [
      "cfg-environment", "cfg-log-level", "cfg-secure-cookie", "cfg-admin-token",
      "cfg-message-archive-path", "cfg-llm-url", "cfg-llm-key"
    ].forEach(function (id) { lockSensitiveControl($(id)); });
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
    state.settingsRevision = data.revision || "";
    normalizeBotConfig(state.config);
    populateSettings(state.config);
    state.settingsDirty = false;
    $("settings-state").textContent = sensitiveSettingsLocked()
      ? "配置已同步；部署安全字段已锁定"
      : "配置已同步";
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
      c.orchestration = clone(state.config.orchestration);
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
      var result = await api("/api/gui/settings", {
        method: "PUT",
        body: { config: c, revision: state.settingsRevision }
      });
      state.settingsRevision = result.revision || state.settingsRevision;
      state.settingsDirty = false;
      $("settings-state").textContent = result.restart_required.length ? "已保存；这些字段需重启：" + result.restart_required.join(", ") : "已保存并热加载";
      showToast("配置已保存");
      await loadSettings();
    } catch (error) {
      $("settings-state").textContent = error.status === 409 ? "其他会话已更新配置，请刷新后重新应用" : "保存失败";
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

  function recordPreview(kind, item) {
    if (kind === "joins") return item.comment || item.reason || item.decision || "等待处理";
    if (kind === "messages") return item.text || "空消息";
    if (kind === "moderation") {
      var result = item.result_json || {};
      return result.summary || item.status || (item.max_risk != null ? "最高风险 " + item.max_risk : "分析记录");
    }
    if (kind === "announcements") return item.content || item.sync_status || "公告记录";
    return [item.subject_type, item.subject_id].filter(Boolean).join(" / ") || "审计事件";
  }

  async function populateRecordBotFilter() {
    var select = $("record-bot");
    var currentValue = select.value;
    var settings = await api("/api/gui/settings");
    var config = settings.config;
    normalizeBotConfig(config);
    select.replaceChildren();
    var all = document.createElement("option");
    all.value = "";
    all.textContent = "全部 Bot";
    select.appendChild(all);
    config.qq.bots.forEach(function (bot) {
      var option = document.createElement("option");
      option.value = bot.id;
      option.textContent = bot.name + " · " + bot.id;
      select.appendChild(option);
    });
    if (Array.from(select.options).some(function (option) { return option.value === currentValue; })) select.value = currentValue;
  }

  function updateRecordFilterAvailability() {
    var audit = $("record-kind").value === "audit";
    $("record-bot").disabled = audit;
    $("record-group").disabled = audit;
  }

  function updateRecordPagination(data) {
    var count = data.records.length;
    var start = count ? data.offset + 1 : 0;
    var end = data.offset + count;
    state.recordsHasMore = Boolean(data.has_more);
    $("records-range").textContent = count ? "显示第 " + start + "–" + end + " 条" : "0 条记录";
    $("records-page").textContent = "第 " + (Math.floor(data.offset / data.limit) + 1) + " 页";
    $("records-prev").disabled = data.offset <= 0;
    $("records-next").disabled = !data.has_more;
  }

  async function loadRecords(resetOffset) {
    if (resetOffset) state.recordsOffset = 0;
    var kind = $("record-kind").value;
    updateRecordFilterAvailability();
    if ($("record-bot").options.length <= 1) await populateRecordBotFilter();
    var limit = numberValue($("record-limit").value, 50);
    var parameters = new URLSearchParams({
      limit: String(limit),
      offset: String(state.recordsOffset)
    });
    if (kind !== "audit" && $("record-bot").value) parameters.set("bot_id", $("record-bot").value);
    if (kind !== "audit" && $("record-group").value.trim()) parameters.set("group_id", $("record-group").value.trim());
    if ($("record-query").value.trim()) parameters.set("search", $("record-query").value.trim());
    var data = await api("/api/gui/records/" + kind + "?" + parameters.toString());
    var target = $("records-table");
    target.replaceChildren();
    updateRecordPagination(data);
    if (!data.records.length) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = state.recordsOffset ? "这一页没有记录，请返回上一页。" : "没有符合当前筛选条件的记录。";
      target.appendChild(empty);
      return;
    }
    var list = document.createElement("div");
    list.className = "record-list";
    data.records.forEach(function (item) {
      var card = document.createElement("details");
      card.className = "record-item";
      var summary = document.createElement("summary");
      summary.className = "record-summary";
      var summaryText = document.createElement("div");
      var title = document.createElement("strong");
      var preview = document.createElement("small");
      title.textContent = recordTitle(kind, item);
      preview.textContent = recordPreview(kind, item);
      summaryText.append(title, preview);
      var timeNode = document.createElement("span");
      timeNode.textContent = recordTime(item);
      var body = document.createElement("pre");
      body.textContent = JSON.stringify(item, null, 2);
      summary.append(summaryText, timeNode);
      card.append(summary, body);
      list.appendChild(card);
    });
    target.appendChild(list);
  }

  function reloadRecordsFromStart() {
    loadRecords(true).catch(function (error) { showToast(error.message, true); });
  }

  ["record-kind", "record-bot", "record-limit"].forEach(function (id) {
    $(id).addEventListener("change", reloadRecordsFromStart);
  });
  $("record-group").addEventListener("change", reloadRecordsFromStart);
  $("record-query").addEventListener("input", function () {
    window.clearTimeout(state.recordsSearchTimer);
    state.recordsSearchTimer = window.setTimeout(reloadRecordsFromStart, 280);
  });
  $("records-refresh").addEventListener("click", function () {
    loadRecords(false).catch(function (error) { showToast(error.message, true); });
  });
  $("records-reset").addEventListener("click", function () {
    $("record-bot").value = "";
    $("record-group").value = "";
    $("record-query").value = "";
    reloadRecordsFromStart();
  });
  $("records-prev").addEventListener("click", function () {
    var limit = numberValue($("record-limit").value, 50);
    state.recordsOffset = Math.max(0, state.recordsOffset - limit);
    loadRecords(false).catch(function (error) { showToast(error.message, true); });
  });
  $("records-next").addEventListener("click", function () {
    if (!state.recordsHasMore) return;
    state.recordsOffset += numberValue($("record-limit").value, 50);
    loadRecords(false).catch(function (error) { showToast(error.message, true); });
  });

  async function commandCreateResource(kind) {
    await switchView("integrations");
    if (state.activeView !== "integrations") return;
    var viewport = $("orchestration-viewport");
    state.orchestrationMenuPoint = {
      x: viewport.scrollLeft + Math.max(140, viewport.clientWidth / 2 - 110),
      y: viewport.scrollTop + Math.max(90, viewport.clientHeight / 2 - 55)
    };
    createOrchestrationNode(kind);
  }

  async function commandRunJob(job) {
    await switchView("dashboard");
    if (state.activeView === "dashboard") await runJob(job, "", null);
  }

  function commandDefinitions() {
    return [
      { label: "打开运行概览", detail: "查看计数、诊断和手动任务", category: "导航", action: function () { return switchView("dashboard"); } },
      { label: "打开资源编排", detail: "管理 Bot、群、知识库和连接", category: "导航", action: function () { return switchView("integrations"); } },
      { label: "打开系统设置", detail: "连接、事务、模型和保留策略", category: "导航", action: function () { return switchView("settings"); } },
      { label: "打开记录与审计", detail: "搜索消息、公告、分析和审计事件", category: "导航", action: function () { return switchView("records"); } },
      { label: "新建 QQ Bot", detail: "在资源编排中创建 OneBot 账号", category: "编排", action: function () { return commandCreateResource("qq_bot"); } },
      { label: "新建飞书 Bot", detail: "在资源编排中创建飞书 CLI 账号", category: "编排", action: function () { return commandCreateResource("feishu_bot"); } },
      { label: "新建 QQ 群", detail: "创建可管理或监听的 QQ 群节点", category: "编排", action: function () { return commandCreateResource("qq_group"); } },
      { label: "新建飞书群", detail: "创建飞书协作目标", category: "编排", action: function () { return commandCreateResource("feishu_group"); } },
      { label: "新建知识库", detail: "创建飞书、Notion 或本地知识资源", category: "编排", action: function () { return commandCreateResource("knowledge_base"); } },
      { label: "运行消息分析", detail: "立即执行所有已启用的分析事务", category: "任务", action: function () { return commandRunJob("moderation"); } },
      { label: "同步全部公告", detail: "立即抓取并归档所有启用群的公告", category: "任务", action: function () { return commandRunJob("announcements"); } },
      { label: "清理过期数据", detail: "立即应用消息和审计保留策略", category: "任务", action: function () { return commandRunJob("maintenance"); } },
      { label: "切换深浅主题", detail: "在纯黑和纯白工作台之间切换", category: "界面", action: function () { applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"); } },
      { label: "刷新当前页面", detail: "重新读取当前页面的最新状态", category: "界面", action: function () { return switchView(state.activeView); } }
    ];
  }

  function filteredCommands() {
    var query = $("command-query").value.trim().toLowerCase();
    return commandDefinitions().filter(function (command) {
      return !query || [command.label, command.detail, command.category].join(" ").toLowerCase().indexOf(query) >= 0;
    });
  }

  function renderCommandPalette() {
    var commands = filteredCommands();
    state.commandIndex = Math.max(0, Math.min(state.commandIndex, Math.max(0, commands.length - 1)));
    var target = $("command-results");
    target.replaceChildren();
    commands.forEach(function (command, index) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "command-item" + (index === state.commandIndex ? " active" : "");
      button.innerHTML = '<span class="command-category"></span><div><strong></strong><small></small></div><span class="command-arrow">↵</span>';
      button.querySelector(".command-category").textContent = command.category;
      button.querySelector("strong").textContent = command.label;
      button.querySelector("small").textContent = command.detail;
      button.addEventListener("pointerenter", function () {
        state.commandIndex = index;
        target.querySelectorAll(".command-item").forEach(function (item, itemIndex) {
          item.classList.toggle("active", itemIndex === index);
        });
      });
      button.addEventListener("click", function () { executeCommand(command); });
      target.appendChild(button);
    });
    if (!commands.length) {
      var empty = document.createElement("div");
      empty.className = "command-empty";
      empty.textContent = "没有匹配的命令";
      target.appendChild(empty);
    }
  }

  function openCommandPalette() {
    if ($("app-view").classList.contains("hidden")) return;
    var openDialog = document.querySelector("dialog[open]");
    if (openDialog && openDialog !== $("command-dialog")) return;
    state.commandIndex = 0;
    $("command-query").value = "";
    renderCommandPalette();
    $("command-dialog").showModal();
    window.setTimeout(function () { $("command-query").focus(); }, 20);
  }

  function closeCommandPalette() {
    if ($("command-dialog").open) $("command-dialog").close();
  }

  async function executeCommand(command) {
    closeCommandPalette();
    try {
      await command.action();
    } catch (error) {
      showToast(error.message, true);
    }
  }

  $("command-button").addEventListener("click", openCommandPalette);
  $("command-close").addEventListener("click", closeCommandPalette);
  $("command-query").addEventListener("input", function () { state.commandIndex = 0; renderCommandPalette(); });
  $("command-query").addEventListener("keydown", function (event) {
    var commands = filteredCommands();
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!commands.length) return;
      event.preventDefault();
      var direction = event.key === "ArrowDown" ? 1 : -1;
      state.commandIndex = (state.commandIndex + direction + commands.length) % commands.length;
      renderCommandPalette();
    } else if (event.key === "Enter" && commands[state.commandIndex]) {
      event.preventDefault();
      executeCommand(commands[state.commandIndex]);
    }
  });
  $("command-dialog").addEventListener("cancel", function (event) { event.preventDefault(); closeCommandPalette(); });
  document.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if ($("command-dialog").open) closeCommandPalette();
      else openCommandPalette();
    }
  });

  initialize();
})();
