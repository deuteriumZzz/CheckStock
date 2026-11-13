"use strict";

const $ = (id) => document.getElementById(id);
const state = { stock: null, warehouses: [], busy: false, kind: "receipt", prev: null, collapsed: new Set() };

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ic = (n) => `<svg class="i" aria-hidden="true"><use href="#i-${n}"/></svg>`;
const when = (iso) => new Date(iso).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });

// тип события: [название, иконка, тон, знак у количества]
const EVENT = {
  RECEIPT: ["Приход", "plus", "in", "+"],
  RESERVE: ["Резерв FBS", "lock", "hold", ""],
  CANCEL_RESERVE: ["Отмена резерва", "x", "mute", ""],
  SHIP_FBS: ["Отправка клиенту (FBS)", "user", "out", "−"],
  SHIP_TRANSFER: ["Перемещение: отправка", "truck", "out", "−"],
  RECEIVE_TRANSFER: ["Перемещение: приёмка", "check", "in", "+"],
  SHIP_FBO: ["Отправка FBO", "store", "out", "−"],
  RECEIVE_FBO: ["FBO: принято маркетплейсом", "check", "mute", ""],
  ADJUST_IN: ["Корректировка остатка", "pencil", "in", "+"],
  ADJUST_OUT: ["Корректировка остатка", "pencil", "out", "−"],
};
const FIXED_DEST = { SHIP_FBS: "Клиент", SHIP_FBO: "Склад маркетплейса", RECEIVE_FBO: "Склад маркетплейса" };

// одни и те же иконки и цвета везде: приход — зелёный плюс, резерв — жёлтый замок, уезжает — рыжая стрелка
const ACTIONS = {
  receipt: { icon: "plus", tone: "in", title: "Приход", short: "Приход", sub: "Товар приехал на склад", submit: "Оформить приход", ok: "Приход оформлен" },
  reserve: { icon: "lock", tone: "hold", title: "Резерв FBS", short: "Резерв", sub: "Закрепить товар под заказ покупателя", submit: "Зарезервировать", ok: "Резерв создан", needs: true },
  transfer: { icon: "truck", tone: "out", title: "Перемещение", short: "Перемещение", sub: "Отправить на другой ваш склад", submit: "Отправить перемещение", ok: "Перемещение отправлено, товар в пути", needs: true },
  fbo: { icon: "store", tone: "out", title: "Отправка FBO", short: "FBO", sub: "Отправить на склад маркетплейса", submit: "Отправить на маркетплейс", ok: "Отправка FBO оформлена, товар в пути", needs: true },
  adjust: { icon: "pencil", tone: "mute", title: "Исправить остаток", short: "Исправить", sub: "Ввести количество по факту пересчёта", submit: "Сохранить остаток", ok: "Остаток исправлен" },
};

// Ключ повтора: одна открытая форма операции = один ключ, повторная отправка не создаст дубль.
const newKey = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`);

async function api(path, method = "GET", body, extra = {}) {
  const res = await fetch("/api" + path, {
    method,
    headers: body ? { "Content-Type": "application/json", ...extra } : extra,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.error?.message ?? `Ошибка ${res.status}`);
  return data;
}

function toast(text, isErr = false) {
  const el = $("toast");
  el.textContent = text;
  el.className = "show" + (isErr ? " err" : "");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.className = ""), isErr ? 7000 : 3000);
}

/* Блокируем только активные кнопки и возвращаем ровно их. */
function lock(on) {
  document.querySelectorAll("button").forEach((b) => {
    if (on) { if (!b.disabled) { b.disabled = true; b.dataset.lock = "1"; } }
    else if (b.dataset.lock) { b.disabled = false; delete b.dataset.lock; }
  });
}

/* Одна команда за раз; успех показываем только после ответа сервера. errEl — где показать ошибку. */
async function act(fn, okMessage, errEl) {
  if (state.busy) return false;
  state.busy = true;
  lock(true);
  document.body.classList.add("busy");
  if (errEl) errEl.hidden = true;
  try {
    await fn();
    await refresh();
    toast(okMessage);
    return true;
  } catch (e) {
    if (errEl) { errEl.textContent = e.message; errEl.hidden = false; }
    toast(e.message, true);
    return false;
  } finally {
    state.busy = false;
    lock(false);
    document.body.classList.remove("busy");
  }
}

/* Подсвечиваем числа, изменившиеся с прошлой отрисовки: видно, что сделала операция. */
function flashChanges() {
  const next = new Map();
  document.querySelectorAll("#view-stock [data-k]").forEach((el) => {
    next.set(el.dataset.k, el.dataset.v);
    if (state.prev?.has(el.dataset.k) && state.prev.get(el.dataset.k) !== el.dataset.v) el.classList.add("flash");
  });
  state.prev = next;
}

/* Число в сводке плавно «докручивается» до нового значения. */
function countUp(el) {
  const to = Number(el.dataset.v), from = Number(state.prev?.get(el.dataset.k) ?? 0);
  if (from === to) return;
  const t0 = performance.now();
  const tick = (t) => {
    const p = Math.min(1, (t - t0) / 500);
    el.textContent = Math.round(from + (to - from) * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(tick);
  };
  el.textContent = from;
  requestAnimationFrame(tick);
}

/* --- остатки --- */

function renderStats({ products }) {
  let on = 0, res = 0, av = 0, tr = 0;
  products.forEach((p) => {
    p.cells.forEach((c) => { on += c.on_hand; res += c.reserved; av += c.available; });
    tr += p.in_transit.TRANSFER + p.in_transit.FBO + p.in_transit.FBS;
  });
  const card = (icon, tone, label, v) => `<div class="stat"><span class="ico ${tone}">${ic(icon)}</span>
    <div><b data-k="s:${label}" data-v="${v}">${v}</b><span>${label}</span></div></div>`;
  $("stats").innerHTML = card("box", "in", "На складах", on) + card("lock", "hold", "В резерве", res)
    + card("check", "in", "Доступно", av) + card("truck", "out", "В пути", tr);
  $("stats").querySelectorAll("b").forEach(countUp);
}

// почему значок недоступен (пусто — доступен)
function whyNot(kind, w, c) {
  if (w.archived && kind !== "transfer") return "склад в архиве";
  if (ACTIONS[kind].needs && c.available <= 0) return "нет доступного товара";
  if (kind === "transfer" && !state.warehouses.some((x) => !x.archived && x.id !== w.id)) return "нет другого склада";
  return "";
}

function renderList({ warehouses, products }) {
  const num = (v, label, key, cls = "") =>
    `<span class="n ${cls}${v === 0 ? " zero" : ""}" data-l="${label}" data-k="${key}" data-v="${v}">${v}</span>`;
  $("plist").innerHTML = products.map((p) => {
    const sum = (f) => p.cells.reduce((a, c) => a + c[f], 0);
    const t = p.in_transit, tr = t.TRANSFER + t.FBO + t.FBS;
    const open = !state.collapsed.has(p.id);
    const rows = warehouses.map((w, i) => {
      const c = p.cells[i];
      const btns = Object.entries(ACTIONS).map(([k, a]) => {
        const why = whyNot(k, w, c);
        return `<button type="button" class="ib ${a.tone}${why ? " off" : ""}" data-do="${k}" data-p="${p.id}" data-w="${w.id}"
          ${why ? `data-why="${esc(why)}"` : ""} title="${a.title}${why ? ` — ${esc(why)}` : ""}"
          aria-label="${a.title}, склад ${esc(w.name)}">${ic(a.icon)}</button>`;
      }).join("");
      const k = (s) => `${p.id}:${w.id}:${s}`;
      return `<div class="wr gr${w.archived ? " archived" : ""}">
        <span class="wn">${ic("warehouse")}<span>${esc(w.name)}${w.archived ? '<span class="tag">архив</span>' : ""}</span></span>
        ${num(c.on_hand, "на складе", k("on"))}${num(c.reserved, "в резерве", k("res"))}${num(c.available, "доступно", k("av"), "av")}<span class="n"></span>
        <span class="wa">${btns}</span></div>`;
    }).join("");
    return `<article class="prod${open ? "" : " closed"}" data-id="${p.id}">
      <button type="button" class="prod-head gr" data-toggle="${p.id}" aria-expanded="${open}">
        <span class="pn">${ic("chev")}<span class="pt"><b>${esc(p.sku)}</b><small>${esc(p.name)}</small></span></span>
        ${num(p.total_on_hand, "на складах", `${p.id}:total`)}${num(sum("reserved"), "в резерве", `${p.id}:res`)}
        ${num(sum("available"), "доступно", `${p.id}:av`, "av")}${num(tr, "в пути", `${p.id}:tr`, "tr")}<span class="wa"></span>
      </button>
      <div class="prod-body"><div>${rows}</div></div></article>`;
  }).join("") || '<div class="empty-state"><p>Нет товаров</p></div>';
}

function applySearch() {
  const q = $("stock-search").value.trim().toLowerCase();
  document.querySelectorAll("#plist .prod").forEach((a) => { a.hidden = q !== "" && !a.querySelector(".pt").textContent.toLowerCase().includes(q); });
}

/* --- операции, ждущие действия --- */

const isPending = (o) => !(o.type === "FBS" && o.status === "IN_TRANSIT");
const plural = (n) => { const m = n % 100, d = n % 10; return m >= 11 && m <= 14 ? "операций" : d === 1 ? "операция" : d >= 2 && d <= 4 ? "операции" : "операций"; };

function opMeta(o) {
  if (o.type === "FBS") return o.status === "ACTIVE" ? ["Резерв FBS", "lock", "hold"] : ["Отправлено клиенту", "user", "out"];
  if (o.type === "TRANSFER") return ["Перемещение", "truck", "out"];
  return ["Отправка FBO", "store", "out"];
}

/* Цепочка статусов: пройденные шаги с галочкой, текущий пульсирует. */
function stepper(o) {
  let labels, cur;
  if (o.type === "FBS") { labels = ["Зарезервировано", "Отправлено клиенту"]; cur = o.status === "ACTIVE" ? 0 : 2; }
  else { labels = ["Отправлено", "В пути", o.type === "FBO" ? "Принято маркетплейсом" : "Принято на складе"]; cur = 1; }
  return `<ol class="steps">${labels.map((l, i) => `<li class="${i < cur ? "done" : i === cur ? "cur" : ""}">
    <span class="dot">${i < cur ? ic("check") : ""}</span>${l}</li>`).join("")}</ol>`;
}

function opActions(o) {
  const b = (a, label, cls, icon) => `<button class="btn ${cls}" data-act="${a}" data-id="${o.id}">${ic(icon)}${label}</button>`;
  if (o.type === "FBS" && o.status === "ACTIVE") return b("fbs-ship", "Отправить клиенту", "primary", "user") + b("fbs-cancel", "Отменить", "danger", "x");
  if (o.type === "TRANSFER") return b("transfer-receive", "Принять на складе", "primary", "check");
  if (o.type === "FBO") return b("fbo-receive", "Принято маркетплейсом", "primary", "check");
  return "";
}

function opRow(o) {
  const [label, icon, tone] = opMeta(o);
  const from = esc(o.source_warehouse ?? "—");
  const route = o.type === "FBS" ? (o.status === "ACTIVE" ? `на складе «${from}»` : `${from} → клиент`)
    : o.type === "TRANSFER" ? `${from} → ${esc(o.dest_warehouse ?? "—")}` : `${from} → маркетплейс`;
  return `<li class="row"><span class="ico ${tone}">${ic(icon)}</span>
    <div class="main"><b>${label} <small>#${o.id}</small></b>
      <span>${esc(o.sku)} · ${esc(o.product_name)} — <b>${o.quantity} шт</b></span>
      <span class="route">${route}</span>${stepper(o)}</div>
    <div class="acts">${opActions(o)}</div></li>`;
}

function renderOps(ops) {
  const pending = ops.filter(isPending), done = ops.filter((o) => !isPending(o));
  $("ops-list").innerHTML = pending.map(opRow).join("") ||
    `<li class="empty-state">${ic("check")}<p>Всё сделано: сейчас ничего не ждёт вашего действия.</p>
     <button class="btn primary" data-view-go="stock">К остаткам</button></li>`;
  $("ops-done").innerHTML = done.map(opRow).join("") || '<li class="empty-state"><p>Пока ничего не отправлено клиентам.</p></li>';

  const n = pending.length, badge = $("ops-badge");
  if (n > 0 && badge.textContent !== String(n)) {
    badge.classList.remove("pop");
    void badge.offsetWidth; // перезапуск анимации
    badge.classList.add("pop");
  }
  badge.hidden = n === 0;
  badge.textContent = n;
  $("ops-notice").hidden = n === 0;
  if (n) {
    const count = (t) => pending.filter((o) => o.type === t).length;
    const parts = [["FBS", "резервов FBS"], ["TRANSFER", "перемещений в пути"], ["FBO", "отправок FBO в пути"]]
      .filter(([t]) => count(t)).map(([t, name]) => `${name} — ${count(t)}`);
    $("ops-notice-text").textContent = `Ждут вашего действия: ${n} ${plural(n)} (${parts.join(", ")})`;
  }
}

function renderHistory(rows) {
  $("history").innerHTML = rows.map((h) => {
    const [label, icon, tone, sign] = EVENT[h.type] ?? [h.type, "clock", "mute", ""];
    const from = h.from_warehouse, to = h.to_warehouse ?? FIXED_DEST[h.type];
    const route = from && to ? `${esc(from)} → ${esc(to)}` : to ? `→ ${esc(to)}` : from ? `${esc(from)} →` : "";
    return `<li class="row ev"><span class="ico ${tone}">${ic(icon)}</span>
      <div class="main"><b>${label}</b><span>${esc(h.sku)} · ${esc(h.product_name)}</span>
        ${route ? `<span class="route">${route}</span>` : ""}${h.note ? `<small class="note">Причина: ${esc(h.note)}</small>` : ""}</div>
      <div class="qty ${tone}">${sign}${h.quantity}<small>${when(h.created_at)}</small></div></li>`;
  }).join("") || '<li class="empty-state"><p>История пуста.</p></li>';
}

function renderWarehouses(list) {
  $("wh-list").innerHTML = list.map((w) => `<li>
    <span class="ico mute">${ic("warehouse")}</span>
    <span class="name">${esc(w.name)}${w.archived ? '<span class="tag">архив</span>' : ""}
      <small>физически ${w.on_hand}, в резерве ${w.reserved}</small></span>
    <span class="acts"><button class="btn sm" data-act="wh-rename" data-id="${w.id}" data-name="${esc(w.name)}">${ic("pencil")}Переименовать</button>
    ${w.archived ? "" : `<button class="btn sm danger" data-act="wh-archive" data-id="${w.id}" data-name="${esc(w.name)}">${ic("trash")}Удалить</button>`}</span></li>`).join("");
  const keep = $("history-filter").value;
  $("history-filter").innerHTML = '<option value="">Все склады</option>' +
    list.map((w) => `<option value="${w.id}">${esc(w.name)}${w.archived ? " (архив)" : ""}</option>`).join("");
  $("history-filter").value = keep;
  const active = list.filter((w) => !w.archived).length;
  $("wh-count").hidden = active === 0;
  $("wh-count").textContent = active;
}

async function refresh() {
  const wid = $("history-filter").value;
  const [stock, ops, hist, warehouses] = await Promise.all([
    api("/stock"), api("/operations/open"),
    api("/history?limit=50" + (wid ? `&warehouse_id=${wid}` : "")), api("/warehouses"),
  ]);
  state.stock = stock;
  state.warehouses = warehouses;
  renderStats(stock);
  renderList(stock);
  flashChanges();
  applySearch();
  renderOps(ops);
  renderHistory(hist);
  renderWarehouses(warehouses);
}

/* --- меню «+ Операция» и панель справа --- */

const menu = $("new-menu");
menu.innerHTML = Object.entries(ACTIONS).map(([k, a]) => `<button type="button" class="mi" role="menuitem" data-new="${k}">
  <span class="ico ${a.tone}">${ic(a.icon)}</span><span><b>${a.title}</b><span>${a.sub}</span></span></button>`).join("");
$("icon-legend").innerHTML = '<span class="lg-title">Значки в строке склада:</span>' + Object.values(ACTIONS)
  .map((a) => `<span class="lg ${a.tone}">${ic(a.icon)}${a.short}</span>`).join("");
$("ad-kinds").innerHTML = Object.entries(ACTIONS).map(([k, a]) =>
  `<button type="button" class="kt ${a.tone}" role="tab" data-kind="${k}">${ic(a.icon)}<span>${a.short}</span></button>`).join("");

function setMenu(open) {
  menu.hidden = !open;
  $("new-btn").setAttribute("aria-expanded", String(open));
}

const cur = () => {
  const { products, warehouses } = state.stock;
  const product = products.find((p) => String(p.id) === $("ad-product").value);
  const col = warehouses.findIndex((w) => String(w.id) === $("ad-wh").value);
  return { product, wh: warehouses[col], c: product?.cells[col] };
};

function fillPickers(productId, whId) {
  const { products, warehouses } = state.stock;
  $("ad-product").innerHTML = products.map((p) => `<option value="${p.id}">${esc(p.sku)} · ${esc(p.name)}</option>`).join("");
  const opts = warehouses.filter((w) => !w.archived || state.kind === "transfer" || w.id === whId);
  $("ad-wh").innerHTML = opts.map((w) => `<option value="${w.id}">${esc(w.name)}${w.archived ? " (архив)" : ""}</option>`).join("");
  if (productId) $("ad-product").value = productId;
  if (whId) $("ad-wh").value = whId;
}

function syncDialog(resetQty) {
  const { wh, c } = cur();
  if (!c) return;
  const a = ACTIONS[state.kind];
  if (state.kind === "transfer") {
    const keep = $("ad-dest").value;
    $("ad-dest").innerHTML = state.stock.warehouses.filter((w) => !w.archived && w.id !== wh.id)
      .map((w) => `<option value="${w.id}">${esc(w.name)}</option>`).join("");
    if ([...$("ad-dest").options].some((o) => o.value === keep)) $("ad-dest").value = keep;
  }
  $("ad-avail").textContent = `На «${wh.name}»: на складе ${c.on_hand}, в резерве ${c.reserved}, доступно ${c.available}`;
  $("ad-max").hidden = !a.needs;
  $("ad-max").textContent = `Всё: ${c.available}`;
  if (resetQty) {
    $("ad-qty").value = state.kind === "adjust" ? c.on_hand : "";
    state.idem = newKey(); // новая форма или новые товар/склад/тип — новый ключ
  }
}

function setKind(kind) {
  state.kind = kind;
  const a = ACTIONS[kind], adjust = kind === "adjust";
  fillPickers(Number($("ad-product").value), Number($("ad-wh").value));
  document.querySelectorAll("#ad-kinds .kt").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.kind === kind)));
  $("ad-chosen").innerHTML = `<span class="ico ${a.tone}">${ic(a.icon)}</span><span><b>${a.title}</b><span>${a.sub}</span></span>`;
  $("ad-wh-text").textContent = kind === "transfer" ? "Откуда" : kind === "fbo" ? "С какого склада" : "Склад";
  $("ad-dest-row").hidden = kind !== "transfer";
  $("ad-reason-row").hidden = !adjust;
  $("ad-reason").value = "";
  $("ad-qty-label").textContent = adjust ? "Сколько должно быть на складе по факту" : "Количество";
  $("ad-submit").textContent = a.submit;
  $("ad-error").hidden = true;
  syncDialog(true);
}

function openDrawer({ kind = "receipt", productId, whId }) {
  const { products, warehouses } = state.stock;
  productId ??= products[0]?.id;
  whId ??= (warehouses.find((w) => !w.archived) ?? warehouses[0])?.id;
  state.kind = kind;
  fillPickers(productId, whId);
  setKind(kind);
  if (!$("ad-dialog").open) $("ad-dialog").showModal();
  $("ad-qty").focus();
}

$("ad-kinds").addEventListener("click", (e) => { const b = e.target.closest("[data-kind]"); if (b) setKind(b.dataset.kind); });
["ad-product", "ad-wh"].forEach((id) => $(id).addEventListener("change", () => syncDialog(true)));
$("ad-close").addEventListener("click", () => $("ad-dialog").close());
$("ad-cancel").addEventListener("click", () => $("ad-dialog").close());
$("ad-dialog").addEventListener("click", (e) => { if (e.target === $("ad-dialog")) $("ad-dialog").close(); });
$("ad-max").addEventListener("click", () => { $("ad-qty").value = cur().c.available; $("ad-qty").focus(); });
const bump = (d) => {
  const min = state.kind === "adjust" ? 0 : 1;
  $("ad-qty").value = Math.max(min, (Number($("ad-qty").value) || 0) + d);
  $("ad-qty").focus();
};
$("ad-minus").addEventListener("click", () => bump(-1));
$("ad-plus").addEventListener("click", () => bump(1));

$("ad-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const a = ACTIONS[state.kind], adjust = state.kind === "adjust";
  const { product, wh } = cur();
  const err = $("ad-error");
  const quantity = Number($("ad-qty").value);
  const fail = (m) => { err.textContent = m; err.hidden = false; };
  if ($("ad-qty").value === "" || !Number.isInteger(quantity) || quantity < (adjust ? 0 : 1)) {
    return fail(adjust ? "Введите количество по факту: целое число, не меньше нуля" : "Введите количество: целое число больше нуля");
  }
  if (adjust && !$("ad-reason").value.trim()) return fail("Укажите причину корректировки");
  const base = { warehouse_id: wh.id, product_id: product.id, quantity };
  const calls = {
    receipt: ["/receipts", base],
    reserve: ["/fbs/reservations", base],
    transfer: ["/transfers", { source_id: wh.id, dest_id: Number($("ad-dest").value), product_id: product.id, quantity }],
    fbo: ["/fbo", base],
    adjust: ["/adjustments", { warehouse_id: wh.id, product_id: product.id, new_on_hand: quantity, reason: $("ad-reason").value }],
  };
  const [path, body] = calls[state.kind];
  act(() => api(path, "POST", body, { "Idempotency-Key": state.idem }), a.ok, err)
    .then((ok) => { if (ok) $("ad-dialog").close(); });
});

/* --- свой диалог подтверждения вместо confirm()/prompt() --- */

const cf = $("cf-dialog");
/* Возвращает Promise: true / введённый текст при подтверждении, null при отмене. */
function ask({ title, text, ok, icon = "check", tone = "in", danger = false, input = null }) {
  $("cf-title").textContent = title;
  $("cf-text").textContent = text;
  $("cf-ico").className = `ico ${tone}`;
  $("cf-ico").innerHTML = ic(icon);
  $("cf-input").hidden = input === null;
  if (input !== null) $("cf-input").value = input;
  $("cf-ok").textContent = ok;
  $("cf-ok").className = "btn " + (danger ? "danger-solid" : "primary");
  cf.returnValue = "";
  return new Promise((resolve) => {
    cf.addEventListener("close", () => resolve(cf.returnValue === "ok" ? (input !== null ? $("cf-input").value : true) : null), { once: true });
    cf.showModal();
    (input !== null ? $("cf-input") : $("cf-cancel")).focus();
  });
}
$("cf-cancel").addEventListener("click", () => cf.close());
cf.addEventListener("click", (e) => { if (e.target === cf) cf.close(); });

/* --- разделы и остальные события --- */

const VIEWS = {
  stock: ["Остатки", "Где сейчас лежит товар и сколько его доступно"],
  ops: ["Ждут действия", "Операции, которые ждут вашего нажатия"],
  history: ["История", "Все движения товара: что, сколько, откуда и куда"],
  warehouses: ["Склады", "Ваши склады. «Удалить» переносит склад в архив: остатки и история сохраняются"],
};

function showView(name) {
  if (!VIEWS[name]) name = "stock";
  Object.keys(VIEWS).forEach((v) => { $("view-" + v).hidden = v !== name; });
  const shown = $("view-" + name);
  shown.classList.remove("enter");
  void shown.offsetWidth; // перезапуск анимации появления
  shown.classList.add("enter");
  setTimeout(() => shown.classList.remove("enter"), 700); // чтобы обновления данных не проигрывали анимацию заново
  document.querySelectorAll(".navtab").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === name)));
  $("page-title").textContent = VIEWS[name][0];
  $("page-sub").textContent = VIEWS[name][1];
  if (location.hash !== "#" + name) window.history.replaceState(null, "", "#" + name);
  window.scrollTo({ top: 0 });
}

document.querySelector(".nav").addEventListener("click", (e) => {
  const b = e.target.closest("[data-view]");
  if (b) showView(b.dataset.view);
});
window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
$("stock-search").addEventListener("input", applySearch);
$("refresh").addEventListener("click", () => act(async () => {}, "Данные обновлены"));
$("history-filter").addEventListener("change", () => act(async () => {}, "История обновлена"));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") setMenu(false); });

$("wh-form").addEventListener("submit", (e) => {
  e.preventDefault();
  act(() => api("/warehouses", "POST", { name: $("wh-name").value }), "Склад добавлен", $("wh-error"))
    .then((done) => { if (done) $("wh-name").value = ""; });
});

document.addEventListener("click", (e) => {
  if (e.target.closest("#new-btn")) { setMenu(menu.hidden); return; }
  if (!e.target.closest("#new-menu")) setMenu(false);

  const go = e.target.closest("[data-view-go]");
  if (go) { showView(go.dataset.viewGo); return; }
  const nw = e.target.closest("[data-new]");
  if (nw) { setMenu(false); if (!state.busy && state.stock) openDrawer({ kind: nw.dataset.new }); return; }

  const ib = e.target.closest(".ib");
  if (ib) {
    if (ib.dataset.why) toast(`${ACTIONS[ib.dataset.do].title}: ${ib.dataset.why}`, true);
    else if (!state.busy && state.stock) openDrawer({ kind: ib.dataset.do, productId: Number(ib.dataset.p), whId: Number(ib.dataset.w) });
    return;
  }
  const tg = e.target.closest("[data-toggle]");
  if (tg) {
    const art = tg.closest(".prod"), id = Number(tg.dataset.toggle);
    const closed = art.classList.toggle("closed");
    tg.setAttribute("aria-expanded", String(!closed));
    closed ? state.collapsed.add(id) : state.collapsed.delete(id);
    return;
  }

  const b = e.target.closest("button[data-act]");
  if (b) {
    const { act: action, id, name } = b.dataset, whErr = $("wh-error");
    if (action === "fbs-ship") {
      ask({ title: "Отправить клиенту?", text: `Заказ #${id}: товар будет списан со склада и поедет к клиенту. Отменить это уже нельзя.`,
        ok: "Отправить", icon: "user", tone: "out" })
        .then((yes) => yes && act(() => api(`/fbs/${id}/ship`, "POST"), "Заказ отправлен клиенту"));
    } else if (action === "fbs-cancel") {
      act(() => api(`/fbs/${id}/cancel`, "POST"), "Резерв отменён");
    } else if (action === "transfer-receive") {
      act(() => api(`/transfers/${id}/receive`, "POST"), "Перемещение принято, товар на складе назначения");
    } else if (action === "fbo-receive") {
      act(() => api(`/fbo/${id}/receive`, "POST"), "Отправка FBO принята");
    } else if (action === "wh-rename") {
      ask({ title: "Переименовать склад", text: "Введите новое название.", ok: "Сохранить", icon: "pencil", tone: "mute", input: name })
        .then((next) => next !== null && act(() => api(`/warehouses/${id}`, "PATCH", { name: next }), "Склад переименован", whErr));
    } else if (action === "wh-archive") {
      ask({ title: `Удалить склад «${name}»?`, text: "Склад уйдёт в архив: остатки и история сохранятся, но новые операции на него оформлять будет нельзя.",
        ok: "Удалить склад", icon: "trash", tone: "out", danger: true })
        .then((yes) => yes && act(() => api(`/warehouses/${id}/archive`, "POST"), "Склад перенесён в архив", whErr));
    }
  }
});

showView(location.hash.slice(1));
refresh().catch((e) => toast(e.message, true));
