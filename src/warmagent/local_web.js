(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const stateUrl = "/api/state";
  const csrfHeaders = { "X-Local-CSRF": csrfToken, Accept: "application/json" };
  const optimistic = new Map();
  const ackedParts = new Map();
  let currentState = null;
  let selectedScope = null;
  let pollingPaused = false;
  let scopeSwitching = false;
  let pollRun = null;
  let receiptRun = null;
  let activityBusy = false;
  let conversationRenderSignature = null;
  let messageRenderSignature = null;

  const modeLabels = {
    chat_first: "聊天优先",
    focus_first: "专注优先",
    adaptive: "自适应",
    live: "联网模式",
    offline: "离线演示",
  };
  const statusLabels = {
    received: "已接收",
    processing: "处理中",
    prepared: "待发送",
    sending: "发送中",
    waiting_for_computer: "等待电脑操作",
    queued: "已排队",
    pending: "等待处理",
    running: "处理中",
    completed: "已完成",
    sent: "已送达",
    failed: "失败",
    blocked: "受阻",
    paused: "已暂停",
    skipped: "已跳过",
    unknown: "结果未确认",
    result_unknown: "结果未确认",
    awaiting: "等待中",
  };
  const activityLabels = {
    queued: "等待继续",
    running: "正在推进",
    active: "等待续做",
    paused: "已暂停",
    pausing: "正在暂停",
    resting: "休息中",
    waiting_external: "等待外部结果",
    completed: "已完成",
    failed: "失败",
    blocked: "受阻",
    unknown: "结果未确认",
    cancelled: "已取消",
  };
  const schedulerReasons = {
    cost_quiet_hours: "省费时段，暂停自主活动",
    idle_scan: "空闲检查",
    scheduled_wakeup: "预约唤醒",
    chat_quiet_period: "等待聊天安静",
    step_running: "正在推进当前步骤",
    ready: "已可检查",
    paused: "活动已暂停",
    unknown: "结果待核对",
    blocked: "活动受阻",
    waiting_external: "等待外部事件",
  };

  function showError(message) {
    const banner = $("error-banner");
    banner.textContent = message;
    banner.hidden = !message;
  }

  function parseResponse(response) {
    return response.text().then((body) => {
      let data = null;
      if (body) {
        try {
          data = JSON.parse(body);
        } catch {
          data = null;
        }
      }
      if (!response.ok) {
        const error = new Error("请求失败（HTTP " + response.status + "）");
        error.status = response.status;
        throw error;
      }
      return data;
    });
  }

  async function getState() {
    const response = await fetch(stateUrl, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    return parseResponse(response);
  }

  async function postJson(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { ...csrfHeaders, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return parseResponse(response);
  }

  function localTime(value) {
    if (value === null || value === undefined || value === "") return "未安排";
    const date = typeof value === "number"
      ? new Date(value < 100000000000 ? value * 1000 : value)
      : new Date(value);
    if (Number.isNaN(date.getTime())) return "时间未知";
    return new Intl.DateTimeFormat("zh-CN", {
      month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
    }).format(date);
  }

  function textOr(value, fallback) {
    return typeof value === "string" && value.trim() ? value : fallback;
  }

  function conversationKind(kind) {
    if (kind === "group") return "群聊范围";
    if (kind === "private") return "私聊范围";
    if (kind === "local") return "本机私聊";
    return "会话范围";
  }

  function renderConversations(conversations) {
    const list = $("conversation-list");
    const available = Array.isArray(conversations) ? conversations : [];
    const signature = JSON.stringify({
      selectedScope,
      scopeSwitching,
      conversations: available.map((conversation) => [conversation.scope, conversation.label, conversation.kind]),
    });
    if (signature === conversationRenderSignature) return;
    conversationRenderSignature = signature;
    list.replaceChildren();
    $("conversation-count").textContent = available.length ? String(available.length) : "";
    if (!available.length) {
      const empty = document.createElement("p");
      empty.className = "empty-note";
      empty.textContent = "当前没有可选会话。";
      list.append(empty);
      return;
    }
    for (const conversation of available) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "conversation-button";
      button.dataset.scope = String(conversation.scope || "");
      button.setAttribute("aria-current", String(conversation.scope) === String(selectedScope));
      button.disabled = scopeSwitching || !conversation.scope;

      const icon = document.createElement("span");
      icon.className = "conversation-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = conversation.kind === "group" ? "群" : "聊";
      const label = document.createElement("span");
      label.className = "conversation-label";
      label.textContent = textOr(conversation.label, "未命名会话");
      const kind = document.createElement("span");
      kind.className = "conversation-kind";
      kind.textContent = conversationKind(conversation.kind);
      label.append(kind);
      button.append(icon, label);
      list.append(button);
    }
  }

  function messageStatus(message) {
    const status = String(message.status || "");
    if (status === "unknown" || status === "result_unknown" || status === "delivery_unknown") {
      return { label: "结果未确认", key: "unknown" };
    }
    return { label: statusLabels[status] || status, key: status };
  }

  function messageRole(role) {
    if (role === "user") return { label: "我", className: "user", avatar: "我" };
    if (role === "assistant") return { label: "小温暖", className: "assistant", avatar: "暖" };
    if (role === "activity" || role === "autonomous") return { label: "自主活动", className: "activity", avatar: "✳" };
    if (role === "background" || role === "background_task" || role === "job") {
      return { label: "后台任务", className: "background", avatar: "⌁" };
    }
    const roleKey = String(role || "").toLowerCase();
    if (roleKey.includes("tool") || roleKey.includes("harness") || roleKey.includes("raw")) return null;
    return { label: "运行消息", className: "background", avatar: "·" };
  }

  function messageNode(message, pending = false) {
    const role = messageRole(message.role);
    if (!role) return null;
    const row = document.createElement("article");
    row.className = "message-row " + role.className + (pending ? " pending" : "");
    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = role.avatar;

    const content = document.createElement("div");
    content.className = "message-content";
    const meta = document.createElement("div");
    meta.className = "message-meta";
    const name = document.createElement("span");
    name.textContent = role.label;
    const time = document.createElement("time");
    time.textContent = localTime(message.created_at);
    meta.append(name, time);

    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    bubble.textContent = typeof message.text === "string" ? message.text : "";
    content.append(meta, bubble);

    const statusData = messageStatus(message);
    if (statusData.label) {
      const status = document.createElement("div");
      status.className = "message-meta message-status";
      status.dataset.status = statusData.key;
      status.textContent = statusData.label;
      if (statusData.key === "unknown") {
        status.setAttribute("role", "status");
        status.setAttribute("aria-label", "结果未确认，不会自动重发");
      }
      content.append(status);
    }
    row.append(avatar, content);
    return row;
  }

  function mergeMessages(messages, scope) {
    const serverMessages = Array.isArray(messages) ? messages.slice(-80) : [];
    const clientIds = new Set(serverMessages.map((message) => message.client_id).filter(Boolean));
    for (const [clientId, local] of optimistic) {
      if (clientIds.has(clientId)) optimistic.delete(clientId);
      else if (local.scope !== scope) continue;
    }
    const combined = serverMessages.map((message, order) => ({ message, order, local: false }));
    let order = combined.length;
    for (const [clientId, local] of optimistic) {
      if (local.scope === scope && !clientIds.has(clientId)) {
        combined.push({ message: local, order: order++, local: true });
      }
    }
    combined.sort((left, right) => {
      const a = timestamp(left.message.created_at);
      const b = timestamp(right.message.created_at);
      if (Number.isFinite(a) && Number.isFinite(b) && a !== b) return a - b;
      return left.order - right.order;
    });
    return combined.slice(-80);
  }

  function timestamp(value) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return value < 100000000000 ? value * 1000 : value;
    }
    const parsed = Date.parse(value || "");
    return Number.isFinite(parsed) ? parsed : Number.NaN;
  }

  function renderMessages(messages, scope) {
    const list = $("message-list");
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 100;
    const rows = mergeMessages(messages, scope);
    const rendered = rows
      .filter((entry) => !entry.local && messageRole(entry.message.role))
      .map((entry) => entry.message);
    const signature = JSON.stringify([
      scope,
      rows.map(({ message, local }) => ({
        id: message.id,
        client_id: message.client_id,
        role: message.role,
        text: message.text,
        status: message.status,
        created_at: message.created_at,
        delivery_id: message.delivery_id,
        part_index: message.part_index,
        ack_required: message.ack_required,
        local,
      })),
    ]);
    if (signature === messageRenderSignature) {
      if (!document.hidden) acknowledgeRendered(rendered, scope);
      return;
    }
    messageRenderSignature = signature;
    list.replaceChildren();
    for (const entry of rows) {
      const node = messageNode(entry.message, entry.local && entry.message.status === "sending");
      if (node) {
        list.append(node);
      }
    }
    if (!rows.some((entry) => messageRole(entry.message.role))) {
      const empty = document.createElement("p");
      empty.className = "empty-note";
      empty.textContent = "这里还没有消息，想说什么都可以。";
      list.append(empty);
    } else if (nearBottom) {
      list.scrollTop = list.scrollHeight;
    }
    if (!document.hidden) acknowledgeRendered(rendered, scope);
  }

  function renderActivity(state) {
    const activity = state.activity || {};
    const scheduler = state.scheduler || {};
    const activityStatus = String(activity.status || "");
    $("activity-goal").textContent = textOr(activity.goal, "暂无自主活动");
    $("activity-status").textContent = activityLabels[activityStatus] || (activityStatus || "暂无活动状态");
    $("next-check").textContent = localTime(scheduler.next_check_at);
    const reason = scheduler.reason;
    $("check-reason").textContent = schedulerReasons[reason] || textOr(reason, "暂未安排");

    const pause = $("pause-activity");
    const resume = $("resume-activity");
    pause.disabled = activityBusy || !["running", "active", "resting", "waiting_external"].includes(activityStatus);
    resume.disabled = activityBusy || !["paused", "resting", "unknown", "blocked"].includes(activityStatus);

    const index = state.index;
    $("index-card").hidden = !index;
    if (index) $("index-status").textContent = textOr(index.state, "状态可用");
  }

  function renderState(state) {
    if (!state || typeof state !== "object") throw new Error("服务返回的状态格式不正确");
    currentState = state;
    selectedScope = state.selected_scope || selectedScope;
    const conversations = Array.isArray(state.conversations) ? state.conversations : [];
    const selected = conversations.find((item) => String(item.scope) === String(selectedScope));
    $("current-conversation").textContent = selected ? textOr(selected.label, "当前会话") : "当前会话";
    $("mode-badge").textContent = modeLabels[state.mode] || textOr(state.mode, "模式未知");
    $("connection-status").dataset.state = "online";
    $("connection-label").textContent = "本地服务在线";
    renderConversations(conversations);
    renderActivity(state);
    renderMessages(state.messages, selectedScope);
    showError("");
  }

  async function refreshState() {
    if (pollingPaused) return null;
    if (pollRun) return pollRun;
    pollRun = (async () => {
      try {
        const state = await getState();
        renderState(state);
        return state;
      } catch (error) {
        $("connection-status").dataset.state = "offline";
        $("connection-label").textContent = "连接暂时中断";
        if (!currentState) $("message-list").textContent = "正在等待本地聊天服务…";
        showError("暂时无法读取本地聊天状态。恢复连接后会自动刷新；未确认的消息不会自动重发。");
        return null;
      }
    })().finally(() => { pollRun = null; });
    return pollRun;
  }

  async function switchScope(scope) {
    if (!scope || scope === selectedScope || scopeSwitching) return;
    scopeSwitching = true;
    pollingPaused = true;
    renderConversations(currentState?.conversations);
    try {
      if (pollRun) await pollRun;
      await postJson("/api/select", { scope });
      selectedScope = scope;
      const state = await getState();
      renderState(state);
    } catch (error) {
      renderConversations(currentState?.conversations);
      showError("切换会话没有完成。请稍后刷新状态；已有消息不会自动重发。");
    } finally {
      scopeSwitching = false;
      pollingPaused = false;
      renderConversations(currentState?.conversations);
      refreshState();
    }
  }

  async function sendMessage(text) {
    const scope = selectedScope;
    if (!scope) {
      showError("还没有可用的会话，请等会话列表加载完成。");
      return;
    }
    const clientId = crypto.randomUUID();
    const message = {
      role: "user",
      text,
      status: "sending",
      created_at: new Date().toISOString(),
      client_id: clientId,
      scope,
    };
    optimistic.set(clientId, message);
    renderMessages(currentState?.messages || [], scope);
    showError("");
    try {
      const result = await postJson("/api/messages", { scope, text, client_id: clientId });
      const pending = optimistic.get(clientId);
      if (pending) pending.status = textOr(result?.status, "pending");
      await refreshState();
    } catch (error) {
      const pending = optimistic.get(clientId);
      if (pending) pending.status = "unknown";
      renderMessages(currentState?.messages || [], scope);
      if (error.status === 409) {
        showError("会话已变化，这条消息没有得到确认。请核对当前记录后再决定是否发送；页面不会自动重发。");
      } else {
        showError("消息请求没有得到确认。请先核对会话记录；页面不会自动重发。");
      }
    }
  }

  function unknownDeliveryStatus(status) {
    return ["unknown", "result_unknown", "delivery_unknown"].includes(String(status || ""));
  }

  async function acknowledgeRendered(messages, scope) {
    if (receiptRun || !messages.length) return receiptRun;
    const deliveries = new Map();
    for (const message of messages) {
      if (!message.delivery_id || !Number.isInteger(message.part_index) || message.part_index < 0) continue;
      const rows = deliveries.get(message.delivery_id) || [];
      rows.push(message);
      deliveries.set(message.delivery_id, rows);
    }
    if (!deliveries.size) return null;
    receiptRun = (async () => {
      for (const [deliveryId, rows] of deliveries) {
        rows.sort((a, b) => a.part_index - b.part_index);
        let next = ackedParts.get(deliveryId) || 0;
        for (const message of rows) {
          if (message.part_index < next) continue;
          if (message.part_index !== next || unknownDeliveryStatus(message.status)) break;
          if (message.ack_required === true) {
            if (!["prepared", "sending"].includes(String(message.status || ""))) break;
            try {
              await postJson("/api/receipts", {
                scope,
                delivery_id: deliveryId,
                part_index: message.part_index,
              });
            } catch {
              showError("消息已显示，送达确认暂未完成；后续状态刷新时会重试确认。");
              break;
            }
          }
          next += 1;
          ackedParts.set(deliveryId, next);
        }
      }
    })().finally(() => { receiptRun = null; });
    return receiptRun;
  }

  async function controlActivity(action) {
    if (activityBusy) return;
    activityBusy = true;
    renderActivity(currentState || {});
    try {
      await postJson("/api/activity", { action });
      await refreshState();
    } catch {
      showError("活动控制没有得到确认。请查看运行监视页中的当前状态。");
    } finally {
      activityBusy = false;
      if (currentState) renderActivity(currentState);
    }
  }

  $("conversation-list").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-scope]");
    if (button) switchScope(button.dataset.scope);
  });

  $("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("message-input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendMessage(text);
    input.focus();
  });

  $("message-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });

  $("pause-activity").addEventListener("click", () => controlActivity("pause"));
  $("resume-activity").addEventListener("click", () => controlActivity("resume"));

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshState();
  });

  refreshState();
  window.setInterval(() => {
    if (!document.hidden) refreshState();
  }, 1500);
})();
