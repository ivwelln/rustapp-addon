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
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const rowMotion = {duration: 360, easing: "cubic-bezier(.22, 1, .36, 1)"};
const rowContents = new WeakMap();
let snapshot = null;
let snapshotReceivedAt = 0;
let activeTab = "players";
let presenceFilter = "all";
let lastTableKey = null;
let lastView = null;

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
  row.dataset.priority = String(record.priority);
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
  const checkAge = element("span", "", "check-age");
  checkAge.setAttribute("role", "img");
  const checkClock = element("span", "", "check-clock");
  checkClock.setAttribute("aria-hidden", "true");
  checkAge.append(checkClock, element("span", "", "check-seconds"));
  const meta = element("div", "", "player-meta");
  meta.append(state, checkAge);
  info.append(nameLine, meta);
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
  const kills = record.kills == null ? "—" : number.format(record.kills);
  const deaths = record.deaths == null ? "—" : number.format(record.deaths);
  const combat = element("td", "", "combat-cell number");
  combat.append(element("span", kills), element("span", "/", "combat-separator"),
    element("span", deaths));
  combat.title = record.priority ? "Вне белого списка: больше 5 убийств и меньше 2 смертей"
    : "Убийства и смерти за 24 часа по собранным данным";
  row.append(identity, element("td", record.steam_id, "steam"), whitelist,
    valueCell(record.provider || null), country, combat);
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

function updateCheckTimers() {
  if (!snapshot) return;
  const now = Date.parse(snapshot.updated_at) + performance.now() - snapshotReceivedAt;
  for (const badge of get("rows").querySelectorAll(".check-age")) {
    const checked = Number(badge.dataset.checkedAt);
    const seconds = checked > 0 ? Math.max(0, Math.floor((now - checked) / 1000)) : null;
    const label = seconds == null ? "—" : String(seconds);
    const value = badge.querySelector(".check-seconds");
    if (value.textContent === label) continue;
    value.textContent = label;
    badge.title = seconds == null ? "Автоматической проверки ещё не было"
      : `С последней автоматической проверки: ${seconds} с`;
    badge.setAttribute("aria-label", badge.title);
  }
}

function syncActivityRows(items, animate) {
  const body = get("rows");
  const current = new Map(Array.from(body.rows, (row) => [row.dataset.key, row]));
  const focusedKey = document.activeElement.closest("#rows tr")?.dataset.key;
  const positions = new Map();
  if (animate) {
    for (const [key, row] of current) positions.set(key, row.getBoundingClientRect().top);
  }
  const rows = items.map(({key, record, divider}) => {
    const signature = JSON.stringify({...record, checked_at: null});
    let row = current.get(key);
    current.delete(key);
    if (!row || rowContents.get(row) !== signature) {
      row?.remove();
      if (divider) {
        row = element("tr", "", "group-divider");
        const title = element("td", "Белый список");
        title.colSpan = 6;
        row.append(title);
      } else {
        row = activeTab === "players" ? rosterRow(record) : decisionRow(record);
      }
      row.dataset.key = key;
      rowContents.set(row, signature);
    }
    if (activeTab === "players" && !divider) {
      row.querySelector(".check-age").dataset.checkedAt = record.checked_at ?? "";
    }
    return row;
  });
  for (const row of current.values()) row.remove();
  let cursor = body.firstElementChild;
  for (const row of rows) {
    if (row === cursor) cursor = cursor.nextElementSibling;
    else if (row.parentElement === body && body.moveBefore) body.moveBefore(row, cursor);
    else body.insertBefore(row, cursor);
  }
  if (focusedKey && !body.contains(document.activeElement)) {
    rows.find((row) => row.dataset.key === focusedKey)?.querySelector("a, button")?.focus({preventScroll: true});
  }
  if (!animate) return;
  const viewport = body.closest(".table-scroll").getBoundingClientRect();
  for (const row of rows) {
    if (row.classList.contains("group-divider")) continue;
    // Cancel only the movement this table started, before measuring the final position.
    for (const animation of row.getAnimations()) animation.cancel();
    const rect = row.getBoundingClientRect();
    if (rect.bottom < viewport.top || rect.top > viewport.bottom) continue;
    const start = positions.get(row.dataset.key);
    if (start == null) {
      row.animate([{opacity: .35, transform: "translateY(8px)"}, {opacity: 1, transform: "none"}], rowMotion);
    } else if (Math.abs(start - rect.top) > 1) {
      row.animate([{transform: `translateY(${start - rect.top}px)`}, {transform: "none"}], rowMotion);
    }
  }
}

function renderActivity() {
  const players = activeTab === "players";
  get("activity-title").textContent = players ? "Игроки на серверах" : "Решения модерации";
  get("filters").hidden = !players;
  get("search").placeholder = players ? "Имя или SteamID" : "Поиск по SteamID";
  get("search").setAttribute("aria-label", get("search").placeholder);
  get("columns").closest("table").classList.toggle("roster", players);
  const headers = players
    ? ["Имя", "SteamID", "Белый список", "Провайдер", "Страна", "K/D"]
    : ["SteamID", "Правило и основание", "Статус", "Срабатывание"];
  const query = get("search").value.trim().toLocaleLowerCase("ru");
  const records = snapshot?.[activeTab].filter((record) => (
    (!players || presenceFilter === "all" || record.status === presenceFilter)
    && (record.steam_id.includes(query) || (players && record.name?.toLocaleLowerCase("ru").includes(query)))
  )) || [];
  const viewKey = JSON.stringify([snapshot?.project, activeTab, presenceFilter, query]);
  const currentKey = JSON.stringify([viewKey, records]);
  if (currentKey !== lastTableKey) {
    if (lastView?.tab !== activeTab) {
      const heading = element("tr", "");
      for (const title of headers) {
        const cell = element("th", title);
        cell.scope = "col";
        heading.append(cell);
      }
      get("columns").replaceChildren(heading);
    }
    const items = [];
    let showDivider = players && records.some((record) => !record.trusted);
    for (const record of records) {
      if (showDivider && record.trusted) {
        items.push({key: "whitelist", divider: true});
        showDivider = false;
      }
      items.push({key: players ? `player:${record.steam_id}` : `decision:${record.id}`, record});
    }
    const animate = !reducedMotion.matches && (lastView?.key === viewKey || !get("rows").hasChildNodes());
    syncActivityRows(items, animate);
    lastTableKey = currentKey;
    lastView = {key: viewKey, tab: activeTab};
  }
  if (players) updateCheckTimers();
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
  snapshotReceivedAt = performance.now();
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
window.setInterval(updateCheckTimers, 1000);
window.addEventListener("resize", renderChart);
renderActivity();
