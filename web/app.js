const $ = (id) => document.getElementById(id);
const store = {
  token: localStorage.getItem("bb_access_token") || "",
  refresh: localStorage.getItem("bb_refresh_token") || "",
  device: localStorage.getItem("bb_device_id") || "",
  session: localStorage.getItem("bb_session_id") || "",
  cursor: Number(localStorage.getItem("bb_cursor") || 0),
  source: null,
};

if (!store.device) {
  store.device = (crypto.randomUUID ? crypto.randomUUID() : `dev-${Date.now()}`);
  localStorage.setItem("bb_device_id", store.device);
}

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------
async function refreshToken() {
  if (!store.refresh) return false;
  const res = await fetch("/api/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: store.refresh }),
  });
  if (!res.ok) return false;
  saveTokens(await res.json());
  return true;
}

function saveTokens(data) {
  store.token = data.access_token;
  store.refresh = data.refresh_token;
  localStorage.setItem("bb_access_token", store.token);
  localStorage.setItem("bb_refresh_token", store.refresh);
}

async function api(path, options = {}, retry = true) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if (store.token) headers["Authorization"] = `Bearer ${store.token}`;
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401 && retry && (await refreshToken())) {
    return api(path, options, false);
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

async function register(verbose = false) {
  const res = await fetch("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device_id: store.device, display_name: $("display-name").value || "手机用户" }),
  });
  if (!res.ok) throw new Error("注册失败");
  saveTokens(await res.json());
  if (verbose) setSettingsStatus("已重新注册本地设备");
}

async function ensureAuth() {
  if (store.token) {
    try {
      await api("/api/auth/me");
      return;
    } catch (_) {
      if (await refreshToken()) return;
    }
  }
  await register();
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------
function clearMessages() {
  const box = $("messages");
  box.innerHTML = "";
}

function nearBottom() {
  const box = $("messages");
  return box.scrollHeight - box.scrollTop - box.clientHeight < 120;
}

function scrollIfNeeded(force = false) {
  if (force || nearBottom()) {
    requestAnimationFrame(() => {
      $("messages").scrollTop = $("messages").scrollHeight;
    });
  }
}

function addBubble(role, text) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  $("messages").appendChild(wrap);
  scrollIfNeeded(role === "user");
  return wrap;
}

function setChip(state) {
  if (state) $("state-chip").textContent = state;
}

function decisionLabel(recommendation) {
  const map = {
    buy_now: ["建议立即购买", "label-buy"],
    watch: ["建议继续观察", "label-watch"],
    no_buy: ["超出预算，暂不购买", "label-stop"],
    no_go: ["数据异常，暂不出手", "label-stop"],
  };
  return map[recommendation] || [recommendation || "-", "label-watch"];
}

function addDecisionCard(decision, options = {}) {
  if (!decision) return;
  const [label, cls] = decisionLabel(decision.recommendation);
  const card = document.createElement("div");
  card.className = "card";

  const title = document.createElement("span");
  title.className = `decision-label ${cls}`;
  title.textContent = label;
  card.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `当前最低实付 ¥${Number(decision.current_effective_price || 0).toFixed(2)} · 预测中位 ¥${Number(decision.predicted_median || 0).toFixed(2)}`;
  card.appendChild(meta);

  const metrics = document.createElement("div");
  metrics.className = "metrics";
  [
    ["决策评分", Number(decision.decision_score || 0).toFixed(2)],
    ["置信度", Number(decision.confidence || 0).toFixed(2)],
  ].forEach(([name, value]) => {
    const item = document.createElement("div");
    item.className = "metric";
    const span = document.createElement("span");
    span.textContent = name;
    const strong = document.createElement("strong");
    strong.textContent = value;
    item.append(span, strong);
    metrics.appendChild(item);
  });
  card.appendChild(metrics);

  if (decision.reasons?.length) {
    const list = document.createElement("ul");
    decision.reasons.slice(0, 4).forEach((reason) => {
      const li = document.createElement("li");
      li.textContent = reason;
      list.appendChild(li);
    });
    card.appendChild(list);
  }

  if (options.actions) {
    const row = document.createElement("div");
    row.className = "dialog-row";
    const buy = document.createElement("button");
    buy.className = "primary";
    buy.textContent = "进入下单";
    buy.onclick = () => send("确认购买");
    const watch = document.createElement("button");
    watch.className = "secondary";
    watch.textContent = "继续观察";
    watch.onclick = () => send("继续观察");
    row.append(buy, watch);
    card.appendChild(row);
  }
  $("messages").appendChild(card);
  scrollIfNeeded();
}

function addOffersCard(offers, state) {
  if (!offers?.length) return;
  const card = document.createElement("div");
  card.className = "card";
  const title = document.createElement("h4");
  title.textContent = "可选方案（按实付价升序）";
  card.appendChild(title);

  const selectable = ["Watching", "SelectingOffer", "Deciding"].includes(state);
  offers.forEach((offer, index) => {
    const row = document.createElement("div");
    row.className = `offer${index === 0 ? " best" : ""}`;
    const info = document.createElement("div");
    info.className = "info";
    const platform = document.createElement("div");
    platform.textContent = `${offer.platform}${offer.in_stock === false ? "（缺货）" : ""} · ${offer.item_name}`;
    const detail = document.createElement("div");
    detail.textContent = `标价 ¥${Number(offer.price).toFixed(2)} · 优惠 ¥${Number(offer.discount).toFixed(2)}${offer.coupon_title ? ` · ${offer.coupon_title}` : ""}`;
    info.append(platform, detail);
    const price = document.createElement("div");
    price.className = "price";
    price.textContent = `¥${Number(offer.paid_price).toFixed(2)}`;
    row.append(info, price);
    if (selectable) {
      const button = document.createElement("button");
      button.textContent = "选择";
      button.onclick = () => send(String(index + 1));
      row.appendChild(button);
    }
    card.appendChild(row);
  });
  $("messages").appendChild(card);
  scrollIfNeeded();
}

function addDeepLink(order) {
  if (!order?.deep_link) return;
  const card = document.createElement("div");
  card.className = "card";
  const title = document.createElement("h4");
  title.textContent = `待支付订单 · ${order.platform}`;
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `实付 ¥${Number(order.paid_price || 0).toFixed(2)}，优惠 ¥${Number(order.discount || 0).toFixed(2)}`;
  const link = document.createElement("a");
  link.className = "link-btn";
  link.href = order.deep_link;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "打开平台下单页";
  card.append(title, meta, link);
  $("messages").appendChild(card);
  scrollIfNeeded();
}

function addNotification(title, body) {
  const box = document.createElement("div");
  box.className = "notification";
  box.textContent = `${title || "提醒"}：${body || ""}`;
  $("messages").appendChild(box);
  scrollIfNeeded();
  if ("Notification" in window && Notification.permission === "granted") {
    try {
      new Notification(title || "Better Buyer", { body: body || "" });
    } catch (_) {}
  }
}

function renderPayload(payload) {
  setChip(payload.state);
  if (payload.reply) addBubble("assistant", payload.reply);
  if (payload.decision) addDecisionCard(payload.decision, { actions: false });
  if (payload.offers?.length) addOffersCard(payload.offers, payload.state);
  if (payload.order) addDeepLink(payload.order);
  if (payload.receipt) {
    addNotification(
      "购买完成",
      `实付 ¥${Number(payload.receipt.paid_price || 0).toFixed(2)}，已创建 ${payload.receipt.notifications?.length || 0} 条提醒`
    );
  }
}

function renderHistory(messages) {
  clearMessages();
  messages.forEach((message) => addBubble(message.role === "user" ? "user" : "assistant", message.content));
  scrollIfNeeded(true);
}

// ---------------------------------------------------------------------------
// 会话与发送
// ---------------------------------------------------------------------------
async function ensureSession() {
  if (store.session) {
    try {
      const data = await api(`/api/chat/sessions/${store.session}/messages`);
      renderHistory(data.messages || []);
      return;
    } catch (_) {
      store.session = "";
      localStorage.removeItem("bb_session_id");
    }
  }
  const data = await api("/api/chat/sessions", { method: "POST" });
  store.session = data.session_id;
  localStorage.setItem("bb_session_id", store.session);
  clearMessages();
  addBubble("assistant", data.greeting);
  setChip(data.state);
}

let sending = false;
async function send(text) {
  const value = (text ?? $("input").value).trim();
  if (!value || sending) return;
  sending = true;
  $("send-btn").disabled = true;
  $("input").value = "";
  addBubble("user", value);
  try {
    if (!store.session) await ensureSession();
    const payload = await api(`/api/chat/sessions/${store.session}/messages`, {
      method: "POST",
      body: JSON.stringify({ text: value }),
    });
    renderPayload(payload);
    if (payload.state === "Exited") {
      store.session = "";
      localStorage.removeItem("bb_session_id");
    }
  } catch (error) {
    addBubble("assistant", `出错了：${error.message}`);
  } finally {
    sending = false;
    $("send-btn").disabled = false;
    $("input").focus();
  }
}

// ---------------------------------------------------------------------------
// SSE 后台提醒
// ---------------------------------------------------------------------------
function disconnectEvents() {
  if (store.source) {
    store.source.close();
    store.source = null;
  }
}

function connectEvents() {
  disconnectEvents();
  if (!store.token) return;
  const url = `/api/events?token=${encodeURIComponent(store.token)}&cursor=${store.cursor}`;
  const source = new EventSource(url);
  store.source = source;

  const handle = (raw) => {
    try {
      const event = JSON.parse(raw);
      store.cursor = Math.max(store.cursor, Number(event.id || 0));
      localStorage.setItem("bb_cursor", String(store.cursor));
      if (event.event === "decision" && event.decision) {
        addNotification(
          "盯盘提醒",
          `${decisionLabel(event.decision.recommendation)[0]} · 当前 ¥${Number(event.decision.current_effective_price || 0).toFixed(2)}`
        );
        addDecisionCard(event.decision, { actions: true });
      } else if (event.event === "notification") {
        addNotification(event.title, event.body);
      }
      scrollIfNeeded();
    } catch (_) {}
  };

  source.addEventListener("decision", (e) => handle(e.data));
  source.addEventListener("notification", (e) => handle(e.data));
  source.addEventListener("check", (e) => handle(e.data));
  source.addEventListener("coupons", (e) => handle(e.data));
}

// ---------------------------------------------------------------------------
// 设置
// ---------------------------------------------------------------------------
function setSettingsStatus(text) {
  $("settings-status").textContent = text;
}

async function requestNotificationPermission() {
  if (!("Notification" in window)) {
    setSettingsStatus("当前浏览器不支持通知");
    return;
  }
  const permission = await Notification.requestPermission();
  setSettingsStatus(permission === "granted" ? "通知已开启" : "通知未授权");
}

async function pairAccount() {
  const code = $("pairing-code").value.trim();
  if (!/^\d{4,6}$/.test(code)) {
    setSettingsStatus("请输入 4-6 位配对码");
    return;
  }
  try {
    const res = await fetch("/api/auth/pairing/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        code,
        device_id: store.device,
        display_name: $("display-name").value || "手机用户",
      }),
    });
    if (!res.ok) throw new Error("配对码错误或已过期");
    saveTokens(await res.json());
    store.session = "";
    localStorage.removeItem("bb_session_id");
    await ensureSession();
    connectEvents();
    setSettingsStatus("配对成功");
    $("settings-dialog").close();
  } catch (error) {
    setSettingsStatus(error.message);
  }
}

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------
async function boot() {
  $("device-id").value = store.device;
  $("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    send();
  });
  $("input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });
  document.querySelectorAll("[data-quick]").forEach((button) => {
    button.addEventListener("click", () => send(button.dataset.quick));
  });
  $("settings-btn").addEventListener("click", () => $("settings-dialog").showModal());
  $("register-btn").addEventListener("click", async () => {
    try {
      await register(true);
      store.session = "";
      localStorage.removeItem("bb_session_id");
      await ensureSession();
      connectEvents();
    } catch (error) {
      setSettingsStatus(error.message);
    }
  });
  $("notify-btn").addEventListener("click", requestNotificationPermission);
  $("pair-btn").addEventListener("click", pairAccount);

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }

  try {
    await ensureAuth();
    await ensureSession();
    connectEvents();
    setChip("已连接");
  } catch (error) {
    setChip("连接失败");
    addBubble("assistant", `无法连接服务端：${error.message}`);
  }
}

document.addEventListener("DOMContentLoaded", boot);
