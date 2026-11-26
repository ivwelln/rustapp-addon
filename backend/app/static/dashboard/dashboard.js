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
const verdicts = {
  confirmed_cheating: "Чит подтверждён", false_positive: "Ложное срабатывание",
  inconclusive: "Недостаточно доказательств",
};
const evidenceTime = new Intl.DateTimeFormat("ru-RU", {dateStyle: "medium", timeStyle: "medium"});
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
  const avatarGroup = element("div", "", "player-avatar-group");
  const avatar = element("span", Array.from(record.name?.trim() || "?")[0].toLocaleUpperCase("ru"), "avatar");
  avatar.setAttribute("aria-hidden", "true");
  if (record.avatar_url) {
    const image = element("img", "");
    image.alt = "";
    image.width = image.height = 32;
    image.referrerPolicy = "no-referrer";
    image.loading = "lazy";
    image.addEventListener("error", () => image.remove(), {once: true});
    image.src = record.avatar_url;
    avatar.append(image);
  }
  const reportCount = record.reports == null ? "—" : number.format(record.reports);
  const reports = element("span", reportCount, "report-count");
  reports.dataset.positive = record.reports > 0;
  reports.title = record.reports == null ? "Репорты ещё не собраны" : `Получено репортов: ${reportCount}`;
  reports.setAttribute("role", "img");
  reports.setAttribute("aria-label", reports.title);
  avatarGroup.append(avatar, reports);

  const info = element("div", "", "player-info");
  const nameLine = element("div", "", "player-name-line");
  const name = record.name || "Имя неизвестно";
  const profile = element("a", "", "rustapp-player-link");
  profile.href = `https://rustapp.io/${encodeURIComponent(snapshot.project)}/players?player=${encodeURIComponent(record.steam_id)}`;
  profile.target = "_blank";
  profile.rel = "noopener noreferrer";
  profile.title = "Открыть в RustApp в новой вкладке";
  profile.setAttribute("aria-label", `${profile.title} (${name})`);
  const logo = element("img", "");
  logo.alt = "";
  logo.width = logo.height = 16;
  logo.src = "/dashboard-assets/rustapp.ico";
  profile.append(logo);
  nameLine.append(element("span", name, "player-name"), profile);
  const state = element("span", record.status === "afk" ? "AFK" : "Онлайн", "player-state");
  state.dataset.afk = record.status === "afk";
  info.append(nameLine, state);
  identityGroup.append(avatarGroup, info);
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
  const explanation = element("td", "");
  const open = element("button", `${record.pattern} · №${record.id}`, "decision-link");
  open.type = "button";
  open.addEventListener("click", () => openDecision(record.id));
  explanation.append(open);
  if (record.reason) explanation.append(element("span", record.reason, "detail"));
  const status = element("td", "");
  status.append(badge(record.state));
  if (record.ban_id) status.append(element("span", `Бан №${record.ban_id}`, "detail"));
  if (record.verdict) status.append(element("span", verdicts[record.verdict], "detail"));
  row.append(element("td", record.steam_id, "steam"), explanation, status,
    valueCell(record.at ? dateTime.format(record.at) : null));
  return row;
}

function evidenceDate(value) {
  return Number.isFinite(value) ? evidenceTime.format(value) : "—";
}

function factsList(entries) {
  const list = element("dl", "", "evidence-facts");
  for (const [label, value] of entries) {
    if (value != null) list.append(element("dt", label), element("dd", value));
  }
  return list;
}

function renderDecision(record) {
  const body = get("decision-evidence");
  body.replaceChildren(factsList([
    ["SteamID", record.steam_id], ["Сервер", record.server_id],
    ["Вход в игру", evidenceDate(record.joined_at)], ["Состояние", labels[record.state]?.[0]],
    ["Бан RustApp", record.ban_id], ["Проверенный исходный бан", record.reference_ban_id],
  ]));
  if (record.reason) body.append(element("p", record.reason));
  for (const rule of record.rules) {
    const section = element("section", "");
    const settings = rule.settings;
    const evidence = rule.evidence;
    section.append(element("h3", `${rule.code} · версия ${settings.version ?? "—"}`));
    section.append(factsList([
      ["Срабатывание", evidenceDate(rule.at)], ["Окно, мин", settings.minutes],
      ["Число жертв", settings.victims], ["Дистанция от, м", settings.meters],
      ["Попаданий на жертву", settings.hits], ["Минимум убийств", settings.kills],
      ["Порог попаданий в голову, %", settings.percent], ["Возраст профиля, мин", settings.age_minutes],
      ["Допуск даты Steam, с", settings.tolerance_seconds],
      ["Дата регистрации Steam", evidence.signed_at && evidenceDate(evidence.signed_at)],
      ["Профиль создан", evidence.first_seen && evidenceDate(evidence.first_seen)],
      ["Начало полного окна", evidence.window_start && evidenceDate(evidence.window_start)],
      ["Убийства в голову", evidence.total != null ? `${evidence.heads} из ${evidence.total}` : null],
      ["Исходные баны", evidence.prior_bans?.map((id) => `№${id}`).join(", ")],
      ["Закрытый Steam без даты", evidence.private_without_date ? "Да" : null],
    ]));
    for (const ban of evidence.bans) {
      section.append(element("p", `Бан №${ban.id} · ${evidenceDate(ban.at)} · Steam ${evidenceDate(ban.signed_at)}`));
    }
    if (evidence.kills.length) {
      const list = element("ul", "", "evidence-list");
      for (const kill of evidence.kills) {
        const distance = kill.distance == null ? "" : ` · ${number.format(kill.distance)} м`;
        const head = kill.head ? " · В голову" : "";
        const hits = kill.head_hits_only ? ` · Попаданий в голову ${kill.head_hits_only}` : "";
        list.append(element("li", `${evidenceDate(kill.created_at)} · №${kill.id} · ${kill.victim} · ${kill.weapon || "—"}${distance}${head}${hits}`));
      }
      section.append(list);
    }
    body.append(section);
  }
  get("verdict").value = record.verdict || "";
  get("verdict-note").value = record.verdict_note || "";
  get("decision-review").hidden = false;
  decisionMessage(record.verdict_at ? `Сохранено ${evidenceDate(record.verdict_at)}` : "");
}

function decisionMessage(message, error = false) {
  const notice = get("decision-message");
  notice.textContent = message;
  notice.dataset.error = error;
}

async function fetchDecision(id, options) {
  const response = await fetch(`/api/dashboard/decisions/${id}`, options);
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Проверьте данные формы");
  return result;
}

async function openDecision(id) {
  const card = get("decision-card");
  card.dataset.id = id;
  get("decision-title").textContent = `Решение №${id}`;
  get("decision-evidence").replaceChildren();
  get("decision-review").hidden = true;
  decisionMessage("Загружаем доказательства…");
  card.showModal();
  try {
    const record = await fetchDecision(id);
    if (card.open && card.dataset.id === String(id)) renderDecision(record);
  } catch (error) {
    if (card.open && card.dataset.id === String(id)) decisionMessage(error.message, true);
  }
}

get("decision-review").addEventListener("submit", async (event) => {
  event.preventDefault();
  const card = get("decision-card");
  const id = card.dataset.id;
  const save = get("save-verdict");
  save.disabled = true;
  try {
    const record = await fetchDecision(id, {
      method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({verdict: get("verdict").value, note: get("verdict-note").value}),
    });
    if (card.open && card.dataset.id === id) renderDecision(record);
  } catch (error) {
    if (card.open && card.dataset.id === id) decisionMessage(error.message, true);
  } finally {
    save.disabled = false;
  }
});

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
  const currentKey = JSON.stringify([snapshot?.project, activeTab, presenceFilter, query, records]);
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
  const waiting = !snapshot && get("connection").dataset.state !== "error";
  get("roster-loading").hidden = !waiting;
  empty.hidden = waiting || records.length > 0;
  empty.textContent = !snapshot ? "Данные пока недоступны"
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
  const colors = getComputedStyle(canvas);
  ctx.strokeStyle = colors.getPropertyValue("--chart-grid");
  for (let step = 0; step < 4; step += 1) {
    const y = 1 + (height - 2) * step / 3;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }
  const slot = width / values.length;
  ctx.fillStyle = colors.getPropertyValue("--chart-bars");
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
  get("online-detail").textContent = `Онлайн: ${data.players.length - afk}, AFK: ${afk}`;
  get("queue").textContent = number.format(data.queue);
  get("ready").textContent = `Готовы к проверке: ${number.format(data.ready)}`;
  get("trusted").textContent = number.format(data.trusted);
  get("trusted-detail").textContent = `${trusted} сейчас на серверах`;
  get("requests").textContent = number.format(data.requests_hour);
  get("stored-players").textContent = data.storage ? number.format(data.storage.players) : "—";
  get("stored-kills").textContent = data.storage ? number.format(data.storage.kills) : "—";
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
  if (!snapshot) renderActivity();
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
