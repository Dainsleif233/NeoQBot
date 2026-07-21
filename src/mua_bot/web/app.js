(function () {
  "use strict";

  var state = { csrf: "", username: "", config: null, qqUrl: "", activeView: "dashboard" };
  var $ = function (id) { return document.getElementById(id); };

  function showToast(message, isError) {
    var toast = $("toast");
    toast.textContent = message;
    toast.className = "toast show" + (isError ? " error" : "");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(function () { toast.className = "toast"; }, 3600);
  }

  function errorText(detail) {
    if (typeof detail === "string") return detail;
    try { return JSON.stringify(detail); } catch (_) { return "请求失败"; }
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

  var titles = {
    dashboard: ["Overview", "运行概览"],
    integrations: ["Connections", "平台登录"],
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
    (diagnostics.errors || []).forEach(function (text) { items.push(["error", text]); });
    (diagnostics.warnings || []).forEach(function (text) { items.push(["warning", text]); });
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

  async function loadDashboard() {
    var data = await api("/api/gui/dashboard");
    $("count-joins").textContent = data.counts.join_requests || 0;
    $("count-messages").textContent = data.counts.group_messages || 0;
    $("count-runs").textContent = data.counts.moderation_runs || 0;
    $("count-notices").textContent = data.counts.announcements || 0;
    $("managed-groups").textContent = (data.managed_groups || []).length + " 个群";
    $("dry-run-badge").textContent = data.dry_run ? "安全演练中" : "真实动作已启用";
    $("dry-run-badge").className = "pill " + (data.dry_run ? "warning" : "success");
    renderDiagnostics(data.diagnostics);
  }

  document.querySelectorAll("[data-job]").forEach(function (button) {
    button.addEventListener("click", async function () {
      button.disabled = true;
      $("job-output").textContent = "任务执行中…";
      try {
        var result = await api("/api/gui/jobs/" + button.dataset.job, { method: "POST" });
        $("job-output").textContent = JSON.stringify(result.result, null, 2);
        showToast("任务执行完成");
        await loadDashboard();
      } catch (error) {
        $("job-output").textContent = error.message;
        showToast(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  });

  function statusText(status) {
    if (status && status.ok) return "已连接 · " + JSON.stringify(status);
    return "未连接 · " + (status && status.error ? status.error : JSON.stringify(status || {}));
  }

  async function checkQQ() {
    $("qq-status").textContent = "检测中…";
    var data = await api("/api/gui/integrations/qq");
    $("qq-status").textContent = statusText(data.status);
    state.qqUrl = data.webui_public_url || (window.location.protocol + "//" + window.location.hostname + ":" + data.webui_public_port);
  }

  async function checkFeishu() {
    $("feishu-status").textContent = "检测中…";
    var data = await api("/api/gui/integrations/feishu");
    $("feishu-status").textContent = statusText(data.status);
  }

  async function loadIntegrationStatus() {
    await Promise.allSettled([checkQQ(), checkFeishu()]);
  }

  $("qq-check").addEventListener("click", function () { checkQQ().catch(function (error) { showToast(error.message, true); }); });
  $("qq-open").addEventListener("click", async function () {
    try {
      if (!state.qqUrl) await checkQQ();
      $("qq-frame").src = state.qqUrl;
      $("qq-frame-panel").classList.remove("hidden");
      $("qq-frame-panel").scrollIntoView({ behavior: "smooth" });
    } catch (error) { showToast(error.message, true); }
  });
  $("qq-external").addEventListener("click", function () { if (state.qqUrl) window.open(state.qqUrl, "_blank", "noopener"); });
  $("feishu-check").addEventListener("click", function () { checkFeishu().catch(function (error) { showToast(error.message, true); }); });

  async function feishuAction(action, button) {
    button.disabled = true;
    $("feishu-auth-link").classList.add("hidden");
    $("feishu-output").textContent = action === "login" ? "等待飞书 CLI 返回授权信息…" : "正在退出飞书…";
    try {
      var data = await api("/api/gui/integrations/feishu/" + action, { method: "POST" });
      $("feishu-output").textContent = typeof data.result === "string" ? data.result : JSON.stringify(data.result, null, 2);
      var serialized = typeof data.result === "string" ? data.result : JSON.stringify(data.result);
      var match = serialized.match(/https?:\/\/[^\s"'<>]+/);
      if (match) {
        $("feishu-auth-link").href = match[0];
        $("feishu-auth-link").classList.remove("hidden");
      }
      showToast("飞书操作已完成");
      await checkFeishu();
    } catch (error) {
      $("feishu-output").textContent = error.message;
      showToast(error.message, true);
    } finally { button.disabled = false; }
  }
  $("feishu-login").addEventListener("click", function () { feishuAction("login", this); });
  $("feishu-logout").addEventListener("click", function () { feishuAction("logout", this); });

  document.querySelectorAll(".settings-tab").forEach(function (button) {
    button.addEventListener("click", function () {
      document.querySelectorAll(".settings-tab").forEach(function (node) { node.classList.remove("active"); });
      document.querySelectorAll(".settings-section").forEach(function (node) { node.classList.remove("active"); });
      button.classList.add("active");
      document.querySelector('[data-settings-section="' + button.dataset.section + '"]').classList.add("active");
    });
  });

  function listValue(value) { return (value || []).join("\n"); }
  function readList(id) { return $(id).value.split(/[\n,]/).map(function (item) { return item.trim(); }).filter(Boolean); }
  function numberValue(id) { return Number($(id).value); }
  function pretty(value) { return JSON.stringify(value || {}, null, 2); }
  function parseJson(id, label) {
    try { return JSON.parse($(id).value || "{}"); }
    catch (_) { throw new Error(label + "不是有效 JSON"); }
  }

  function populateSettings(c) {
    $("cfg-environment").value = c.app.environment;
    $("cfg-log-level").value = c.app.log_level;
    $("cfg-dry-run").checked = c.app.dry_run;
    $("cfg-admin-token").value = "";
    $("cfg-secure-cookie").checked = c.gui.secure_cookie;
    $("cfg-qq-url").value = c.qq.onebot_base_url;
    $("cfg-qq-token").value = "";
    $("cfg-qq-secret").value = "";
    $("cfg-groups").value = listValue(c.qq.managed_group_ids);
    $("cfg-admins").value = listValue(c.qq.administrator_qq_ids);
    $("cfg-qq-webui-port").value = c.qq.webui_public_port;
    $("cfg-qq-webui-url").value = c.qq.webui_public_url || "";
    $("cfg-notice-actions").value = listValue(c.qq.announcement_actions);
    $("cfg-llm-driver").value = c.llm.driver;
    $("cfg-llm-model").value = c.llm.model;
    $("cfg-llm-url").value = c.llm.base_url;
    $("cfg-llm-key").value = "";
    $("cfg-llm-timeout").value = c.llm.timeout_seconds;
    $("cfg-llm-retries").value = c.llm.max_retries;
    $("cfg-llm-json").checked = c.llm.json_response_format;
    $("cfg-auto-approve").checked = c.join_approval.auto_approve;
    $("cfg-auto-reject").checked = c.join_approval.auto_reject;
    $("cfg-confidence").value = c.join_approval.minimum_confidence;
    $("cfg-join-policy").value = c.join_approval.policy;
    $("cfg-mod-interval").value = c.moderation.interval_minutes;
    $("cfg-mod-window").value = c.moderation.window_minutes;
    $("cfg-risk").value = c.moderation.risk_threshold;
    $("cfg-max-messages").value = c.moderation.max_messages_per_run;
    $("cfg-notice-interval").value = c.announcements.sync_interval_minutes;
    $("cfg-notice-startup").checked = c.announcements.sync_on_startup;
    $("cfg-required-keywords").value = listValue(c.join_approval.required_keywords);
    $("cfg-forbidden-keywords").value = listValue(c.join_approval.forbidden_keywords);
    $("cfg-mod-policy").value = c.moderation.policy;
    $("cfg-rule-keywords").value = pretty(c.moderation.rule_keywords);
    $("cfg-feishu-enabled").checked = c.feishu.enabled;
    $("cfg-feishu-bin").value = c.feishu.executable;
    $("cfg-feishu-timeout").value = c.feishu.timeout_seconds;
    $("cfg-feishu-results").value = c.feishu.max_search_results;
    $("cfg-feishu-stdin").checked = c.feishu.archive_payload_stdin;
    $("cfg-search-prefixes").value = listValue(c.feishu.search_prefixes);
    $("cfg-command-templates").value = pretty(c.feishu.command_templates);
    $("cfg-feishu-env").value = pretty(c.feishu.extra_environment);
    $("cfg-retention-enabled").checked = c.retention.enabled;
    $("cfg-message-days").value = c.retention.message_days;
    $("cfg-join-days").value = c.retention.join_request_days;
    $("cfg-mod-days").value = c.retention.moderation_run_days;
    $("cfg-audit-days").value = c.retention.audit_days;
  }

  async function loadSettings() {
    var data = await api("/api/gui/settings");
    state.config = data.config;
    populateSettings(state.config);
  }

  $("settings-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    var button = event.submitter;
    button.disabled = true;
    $("settings-state").textContent = "正在校验并热加载…";
    try {
      var c = JSON.parse(JSON.stringify(state.config));
      c.app.environment = $("cfg-environment").value.trim();
      c.app.log_level = $("cfg-log-level").value;
      c.app.dry_run = $("cfg-dry-run").checked;
      c.app.admin_api_token = $("cfg-admin-token").value;
      c.gui.secure_cookie = $("cfg-secure-cookie").checked;
      c.qq.onebot_base_url = $("cfg-qq-url").value.trim();
      c.qq.access_token = $("cfg-qq-token").value;
      c.qq.webhook_secret = $("cfg-qq-secret").value;
      c.qq.managed_group_ids = readList("cfg-groups");
      c.qq.administrator_qq_ids = readList("cfg-admins");
      c.qq.webui_public_port = numberValue("cfg-qq-webui-port");
      c.qq.webui_public_url = $("cfg-qq-webui-url").value.trim();
      c.qq.announcement_actions = readList("cfg-notice-actions");
      c.llm.driver = $("cfg-llm-driver").value;
      c.llm.model = $("cfg-llm-model").value.trim();
      c.llm.base_url = $("cfg-llm-url").value.trim();
      c.llm.api_key = $("cfg-llm-key").value;
      c.llm.timeout_seconds = numberValue("cfg-llm-timeout");
      c.llm.max_retries = numberValue("cfg-llm-retries");
      c.llm.json_response_format = $("cfg-llm-json").checked;
      c.join_approval.auto_approve = $("cfg-auto-approve").checked;
      c.join_approval.auto_reject = $("cfg-auto-reject").checked;
      c.join_approval.minimum_confidence = numberValue("cfg-confidence");
      c.join_approval.policy = $("cfg-join-policy").value;
      c.moderation.interval_minutes = numberValue("cfg-mod-interval");
      c.moderation.window_minutes = numberValue("cfg-mod-window");
      c.moderation.risk_threshold = numberValue("cfg-risk");
      c.moderation.max_messages_per_run = numberValue("cfg-max-messages");
      c.announcements.sync_interval_minutes = numberValue("cfg-notice-interval");
      c.announcements.sync_on_startup = $("cfg-notice-startup").checked;
      c.join_approval.required_keywords = readList("cfg-required-keywords");
      c.join_approval.forbidden_keywords = readList("cfg-forbidden-keywords");
      c.moderation.policy = $("cfg-mod-policy").value;
      c.moderation.rule_keywords = parseJson("cfg-rule-keywords", "离线关键词规则");
      c.feishu.enabled = $("cfg-feishu-enabled").checked;
      c.feishu.driver = c.feishu.enabled ? "cli" : "disabled";
      c.feishu.executable = $("cfg-feishu-bin").value.trim();
      c.feishu.timeout_seconds = numberValue("cfg-feishu-timeout");
      c.feishu.max_search_results = numberValue("cfg-feishu-results");
      c.feishu.archive_payload_stdin = $("cfg-feishu-stdin").checked;
      c.feishu.search_prefixes = readList("cfg-search-prefixes");
      c.feishu.command_templates = parseJson("cfg-command-templates", "飞书命令模板");
      c.feishu.extra_environment = parseJson("cfg-feishu-env", "飞书环境变量");
      c.retention.enabled = $("cfg-retention-enabled").checked;
      c.retention.message_days = numberValue("cfg-message-days");
      c.retention.join_request_days = numberValue("cfg-join-days");
      c.retention.moderation_run_days = numberValue("cfg-mod-days");
      c.retention.audit_days = numberValue("cfg-audit-days");
      var result = await api("/api/gui/settings", { method: "PUT", body: { config: c } });
      $("settings-state").textContent = result.restart_required.length ? "已保存；这些字段需重启：" + result.restart_required.join(", ") : "已保存并热加载";
      showToast("配置已保存");
      await loadSettings();
    } catch (error) {
      $("settings-state").textContent = "保存失败";
      showToast(error.message, true);
    } finally { button.disabled = false; }
  });

  function recordTitle(kind, item) {
    if (kind === "joins") return "群 " + item.group_id + " · 申请人 " + item.user_id;
    if (kind === "moderation") return "群 " + item.group_id + " · 风险 " + item.max_risk;
    if (kind === "announcements") return "群 " + item.group_id + " · " + (item.title || item.announcement_id);
    return item.action + " · " + item.status;
  }

  function recordTime(kind, item) {
    return item.received_at || item.created_at || item.last_seen_at || item.window_end || "";
  }

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
      timeNode.textContent = recordTime(kind, item);
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
