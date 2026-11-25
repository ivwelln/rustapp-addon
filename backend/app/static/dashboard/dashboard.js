"use strict";

const get = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("ru-RU");
const clock = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dateTime = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
});
const labels = {
  trusted: ["Белый список", "good"], afk: ["AFK", "warm"], waiting: ["Запланировано", "cool"],
  stopped: ["Опрос завершён", ""], pending: ["Ожидает отправки", "warm"],
  sending: ["На сверке", "cool"], confirmed: ["Бан подтверждён", "good"],
  superseded: ["Уже заблокирован", ""], failed: ["Ошибка", "error"],
  cancelled: ["Отменено", ""], recorded: ["Зафиксировано", ""],
};
let snapshot = null;
let activeTab = "players";
let busy = false;

function element(tag, content, className) {
  const node = document.createElement(tag);
  node.textContent = content;
  if (className) node.className = className;
  return node;
}

function badge(status, fallback) {
  const [label, tone] = labels[status] || [fallback || status, ""];
  const node = element("span", label, "status");
  node.dataset.tone = tone;
  return node;
}

function renderActivity() {
  const players = activeTab === "players";
  const headers = players
    ? ["SteamID", "Статус", "Последняя проверка", "Следующая проверка"]
    : ["SteamID", "Правило и основание", "Статус", "Срабатывание"];
  const heading = element("tr", "");
  for (const title of headers) {
    const cell = element("th", title);
    cell.scope = "col";
    heading.append(cell);
  }
  get("columns").replaceChildren(heading);
  const query = get("search").value.trim();
  if (!snapshot) return;
  const records = snapshot[activeTab].filter((record) => record.steam_id.includes(query));
  const now = Date.now();
  const rows = records.map((record) => {
    const row = element("tr", "");
    row.append(element("td", record.steam_id));
    const status = element("td", "");
    if (players) {
      status.append(badge(record.status));
      const due = record.due_at && record.status === "waiting"
        ? (record.due_at <= now ? "Готов к проверке" : clock.format(record.due_at))
        : "—";
      row.append(status, element("td", record.collected_at ? dateTime.format(record.collected_at) : "—"));
      row.append(element("td", due));
    } else {
      const explanation = element("td", record.pattern);
      if (record.reason) explanation.append(element("span", record.reason, "detail"));
      status.append(badge(record.state));
      if (record.ban_id) status.append(element("span", `Бан №${record.ban_id}`, "detail"));
      row.append(explanation, status, element("td", record.at ? dateTime.format(record.at) : "—"));
    }
    return row;
  });
  get("rows").replaceChildren(...rows);
  const empty = get("empty");
  empty.hidden = records.length > 0;
  empty.textContent = query ? "По этому SteamID записей нет" : players ? "В очереди пока нет игроков" : "Решений модерации пока нет";
}

function renderChart() {
  if (!snapshot) return;
  const canvas = get("chart");
  const { width, height } = canvas.getBoundingClientRect();
  if (!width || !height) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const values = snapshot.request_buckets;
  get("chart-empty").hidden = Math.max(...values) > 0;
  const max = Math.max(10, ...values);
  ctx.strokeStyle = "#292f38";
  for (let step = 0; step < 4; step += 1) {
    const y = 1 + (height - 2) * step / 3;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }
  const slot = width / values.length;
  ctx.fillStyle = "#82a8f2";
  values.forEach((value, index) => {
    const bar = (height - 4) * value / max;
    if (bar) ctx.fillRect(index * slot + 1, height - bar - 1, Math.max(1, slot - 2), bar);
  });
}

function showSnapshot(data) {
  snapshot = data;
  for (const key of ["queue", "afk", "trusted"]) get(key).textContent = number.format(data[key]);
  get("ready").textContent = `Готовы к проверке: ${number.format(data.ready)}`;
  get("requests").textContent = number.format(data.requests_hour);
  get("project-meta").textContent = `Проект №${data.project} · очередь проверок и обращения к RustApp`;
  get("guard").textContent = `${data.guard.used} / ${data.guard.limit}`;
  const exhausted = data.guard.used >= data.guard.limit;
  get("guard").dataset.limit = exhausted;
  get("guard-status").textContent = exhausted ? "Достигнут аварийный лимит" : "Занято мест";
  get("updated").textContent = `Обновлено в ${clock.format(new Date(data.updated_at))}`;
  renderChart();
  renderActivity();
}

async function refresh() {
  if (busy) return;
  busy = true;
  get("refresh").disabled = true;
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store", signal: AbortSignal.timeout(5000) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Не удалось получить данные");
    showSnapshot(data);
    get("connection").dataset.state = "ok";
    get("connection-label").textContent = "Обновляется";
    get("error").hidden = true;
  } catch (error) {
    get("connection").dataset.state = "error";
    get("connection-label").textContent = "Нет свежих данных";
    get("error").hidden = false;
    get("error").textContent = error.message === "PostgreSQL не настроена" || error.message.startsWith("PostgreSQL или")
      ? error.message : "Связь с панелью прервалась. Следующая попытка через 5 секунд.";
    if (!snapshot) get("empty").textContent = "Ждём подключения к базе";
  } finally {
    busy = false;
    get("refresh").disabled = false;
  }
}

for (const button of document.querySelectorAll("[data-tab]")) {
  button.addEventListener("click", () => {
    activeTab = button.dataset.tab;
    for (const option of document.querySelectorAll("[data-tab]")) {
      option.setAttribute("aria-pressed", String(option === button));
    }
    renderActivity();
  });
}
get("search").addEventListener("input", renderActivity);
get("refresh").addEventListener("click", refresh);
window.addEventListener("resize", renderChart);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
renderActivity();
refresh();
window.setInterval(() => { if (!document.hidden) refresh(); }, 5000);
