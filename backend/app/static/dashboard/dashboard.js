"use strict";

const get = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("ru-RU");
const clock = new Intl.DateTimeFormat("ru-RU", {hour: "2-digit", minute: "2-digit", second: "2-digit"});
const dateTime = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
});
const regions = new Intl.DisplayNames(["ru"], {type: "region", fallback: "none"});
const labels = {
  pending: ["Ожидает отправки", "warm"], sending: ["На сверке", "cool"],
  confirmed: ["Бан подтверждён", "good"], superseded: ["Уже заблокирован", ""],
  failed: ["Ошибка", "error"], cancelled: ["Отменено", ""], recorded: ["Зафиксировано", ""],
};
let snapshot = null;
let activeTab = "players";
let presenceFilter = "all";
let busy = false;
let lastTableKey = null;

function element(tag, content, className) {
  const node = document.createElement(tag);
  node.textContent = content;
  if (className) node.className = className;
  return node;
}

function badge(status) {
  const [label, tone] = labels[status] || [status, ""];
  const node = element("span", label, "status");
  node.dataset.tone = tone;
  return node;
}

function valueCell(value) {
  return element("td", value ?? "—", value == null ? "placeholder" : "");
}

function rosterRow(record) {
  const row = element("tr", "");
  row.dataset.trusted = String(record.trusted);
  const identity = element("td", "");
  const identityGroup = element("div", "", "player-cell");
  const avatar = element("span", Array.from(record.name || "?")[0].toLocaleUpperCase("ru"), "avatar");
  avatar.setAttribute("aria-hidden", "true");
  const info = element("div", "");
  info.append(element("span", record.name || "Имя неизвестно", "player-name"));
  const state = element("span", "", "player-state");
  const dot = element("span", "", "player-dot");
  dot.dataset.afk = record.status === "afk";
  dot.setAttribute("aria-hidden", "true");
  state.append(dot, document.createTextNode(record.status === "afk" ? "AFK" : "Онлайн"));
  info.append(state);
  identityGroup.append(avatar, info);
  identity.append(identityGroup);

  const whitelist = element("td", "");
  const icon = element("span", record.trusted ? "✓" : "—", record.trusted ? "trust" : "untrusted");
  icon.title = record.trusted ? "В белом списке" : "Вне белого списка";
  icon.setAttribute("role", "img");
  icon.setAttribute("aria-label", icon.title);
  whitelist.append(icon);

  const code = (record.country_code || "").toUpperCase();
  const validCode = /^[A-Z]{2}$/.test(code);
  const country = element("td", "", "country");
  if (validCode && code !== "ZZ") {
    const flag = String.fromCodePoint(...Array.from(code, (letter) => letter.charCodeAt(0) + 127397));
    const flagNode = element("span", flag, "country-flag");
    flagNode.setAttribute("aria-hidden", "true");
    country.append(flagNode);
  }
  const countryLabel = (validCode ? regions.of(code) : null) || record.country_name;
  if (!countryLabel) country.classList.add("placeholder");
  country.append(document.createTextNode(countryLabel || "—"));
  const kills = valueCell(record.kills == null ? null : number.format(record.kills));
  kills.classList.add("number");
  if (record.kills == null) kills.title = "Килллог за последние 24 часа ещё не собран";
  row.append(identity, element("td", record.steam_id, "steam"), whitelist,
    valueCell(record.provider || null), country, kills);
  return row;
}

function decisionRow(record) {
  const row = element("tr", "");
  const explanation = element("td", record.pattern);
  if (record.reason) explanation.append(element("span", record.reason, "detail"));
  const status = element("td", "");
  status.append(badge(record.state));
  if (record.ban_id) status.append(element("span", `Бан №${record.ban_id}`, "detail"));
  row.append(element("td", record.steam_id, "steam"), explanation, status,
    valueCell(record.at ? dateTime.format(record.at) : null));
  return row;
}

function renderActivity() {
  const players = activeTab === "players";
  get("activity-title").textContent = players ? "Игроки на серверах" : "Решения модерации";
  get("filters").hidden = !players;
  get("search").placeholder = players ? "Имя или SteamID" : "Поиск по SteamID";
  get("search").setAttribute("aria-label", get("search").placeholder);
  get("columns").closest("table").classList.toggle("roster", players);
  const headers = players
    ? ["Имя", "SteamID", "Белый список", "Провайдер", "Страна", "Убийства"]
    : ["SteamID", "Правило и основание", "Статус", "Срабатывание"];
  const query = get("search").value.trim().toLocaleLowerCase("ru");
  const records = snapshot?.[activeTab].filter((record) => (
    (!players || presenceFilter === "all" || record.status === presenceFilter)
    && (record.steam_id.includes(query) || (players && record.name?.toLocaleLowerCase("ru").includes(query)))
  )) || [];
  const currentKey = JSON.stringify([activeTab, presenceFilter, query, records]);
  if (currentKey !== lastTableKey) {
    const heading = element("tr", "");
    for (const title of headers) {
      const cell = element("th", title);
      cell.scope = "col";
      heading.append(cell);
    }
    get("columns").replaceChildren(heading);
    const rows = [];
    let showDivider = players && records.some((record) => !record.trusted);
    for (const record of records) {
      if (showDivider && record.trusted) {
        const group = element("tr", "", "group-divider");
        const title = element("td", "Белый список");
        title.colSpan = headers.length;
        group.append(title);
        rows.push(group);
        showDivider = false;
      }
      rows.push(players ? rosterRow(record) : decisionRow(record));
    }
    get("rows").replaceChildren(...rows);
    lastTableKey = currentKey;
  }
  const empty = get("empty");
  empty.hidden = records.length > 0;
  empty.textContent = !snapshot ? "Загружаем данные…"
    : query ? "Записей по этому запросу нет"
      : players ? "Нет игроков с выбранным статусом" : "Решений модерации пока нет";
  get("roster-footer").hidden = !players || !snapshot;
  if (snapshot && players) {
    get("visible-count").textContent = `Показано ${records.length} из ${snapshot.players.length}`;
  }
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
  if (snapshot && data.updated_at < snapshot.updated_at) return;
  snapshot = data;
  const afk = data.players.filter((player) => player.status === "afk").length;
  const trusted = data.players.filter((player) => player.trusted).length;
  get("online-total").textContent = number.format(data.players.length);
  get("online-detail").textContent = `Онлайн ${data.players.length - afk} · AFK ${afk}`;
  get("queue").textContent = number.format(data.queue);
  get("ready").textContent = `Готовы к проверке: ${number.format(data.ready)}`;
  get("trusted").textContent = number.format(data.trusted);
  get("trusted-detail").textContent = `${trusted} сейчас на серверах`;
  get("requests").textContent = number.format(data.requests_hour);
  get("project-meta").textContent = `Проект №${data.project}`;
  get("guard").textContent = `${data.guard.used} / ${data.guard.limit}`;
  const exhausted = data.guard.used >= data.guard.limit;
  get("guard").dataset.limit = exhausted;
  get("guard-status").textContent = exhausted ? "Достигнут аварийный лимит" : "Занято мест";
  get("updated").textContent = `Обновлено в ${clock.format(new Date(data.updated_at))}`;
  renderChart();
  renderActivity();
}

function setConnection(state, label, message = "") {
  get("connection").dataset.state = state;
  get("connection-label").textContent = label;
  get("error").hidden = !message;
  get("error").textContent = message;
}

async function refresh() {
  if (busy) return;
  busy = true;
  get("refresh").disabled = true;
  try {
    const response = await fetch("/api/dashboard", {cache: "no-store", signal: AbortSignal.timeout(5000)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Не удалось получить данные");
    showSnapshot(data);
    if (get("connection").dataset.state !== "ok") setConnection("wait", "Последний снимок");
  } catch (error) {
    setConnection("error", "Нет свежих данных", error.message);
  } finally {
    busy = false;
    get("refresh").disabled = false;
  }
}

const events = new EventSource("/api/dashboard/events");
events.addEventListener("snapshot", (event) => {
  showSnapshot(JSON.parse(event.data));
  setConnection("ok", "Обновляется в реальном времени");
});
events.addEventListener("unavailable", (event) => {
  setConnection("error", "Нет свежих данных", JSON.parse(event.data));
});
events.onerror = () => {
  setConnection("error", "Переподключение", "Связь с панелью прервалась. Показаны последние полученные данные.");
};

for (const button of document.querySelectorAll("[data-tab]")) {
  button.addEventListener("click", () => {
    activeTab = button.dataset.tab;
    get("search").value = "";
    for (const option of document.querySelectorAll("[data-tab]")) {
      option.setAttribute("aria-pressed", String(option === button));
    }
    renderActivity();
  });
}
for (const button of document.querySelectorAll("[data-status]")) {
  button.addEventListener("click", () => {
    presenceFilter = button.dataset.status;
    for (const option of document.querySelectorAll("[data-status]")) {
      option.setAttribute("aria-pressed", String(option === button));
    }
    renderActivity();
  });
}
get("search").addEventListener("input", renderActivity);
get("refresh").addEventListener("click", refresh);
window.addEventListener("resize", renderChart);
renderActivity();
