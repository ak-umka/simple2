"use strict";

// ---------------------------------------------------------------- утилиты

async function api(method, url, body) {
  const resp = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    let msg = data.detail;
    if (Array.isArray(msg)) msg = msg.map((e) => e.msg).join("; ");
    throw new Error(msg || `Ошибка ${resp.status}`);
  }
  return data;
}

let toastTimer;
function toast(text, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), isError ? 6000 : 2500);
}

async function guarded(fn, btn) {
  if (btn) btn.disabled = true;
  try {
    return await fn();
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// Поиск офферов: инпут + выпадающий список. onPick(offer) вызывается при выборе.
function setupOfferPicker(picker, onPick) {
  const input = picker.querySelector(".offer-search");
  const list = picker.querySelector(".offer-results");

  const search = debounce(async () => {
    const q = input.value.trim();
    onPick(null);
    if (!q) return list.classList.add("hidden");
    try {
      const offers = await api("GET", `/api/offers?q=${encodeURIComponent(q)}`);
      list.innerHTML = "";
      if (!offers.length) list.innerHTML = '<li class="muted">Ничего не найдено</li>';
      for (const o of offers) {
        const li = document.createElement("li");
        li.textContent = `${o.id} — ${o.name}`;
        li.title = li.textContent;
        li.addEventListener("mousedown", (ev) => {
          ev.preventDefault();
          input.value = li.textContent;
          list.classList.add("hidden");
          onPick(o);
        });
        list.appendChild(li);
      }
      list.classList.remove("hidden");
    } catch (e) {
      toast(e.message, true);
    }
  }, 250);

  input.addEventListener("input", search);
  input.addEventListener("blur", () => list.classList.add("hidden"));
  return { reset: () => { input.value = ""; onPick(null); } };
}

// ---------------------------------------------------------------- вкладки

document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${btn.dataset.tab}`));
    if (btn.dataset.tab === "editor") loadCampaignList();
  })
);

// ---------------------------------------------------------------- создаватор

const form = document.getElementById("create-form");
const createPicker = setupOfferPicker(form.querySelector(".offer-picker"), (o) => {
  form.elements.offer_id.value = o ? o.id : "";
});

function fillSelect(select, items, emptyLabel) {
  select.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = items.length ? "— не выбрано —" : emptyLabel;
  select.appendChild(empty);
  for (const it of items) {
    const opt = document.createElement("option");
    opt.value = it.id;
    opt.textContent = it.name || `#${it.id}`;
    select.appendChild(opt);
  }
  if (items.length === 1) select.value = items[0].id;
}

async function loadMeta() {
  await guarded(async () => {
    const meta = await api("GET", "/api/meta");
    document.getElementById("prefix").textContent = meta.prefix;
    fillSelect(form.elements.domain_id, meta.domains, "нет доменов в Keitaro");
    fillSelect(form.elements.group_id, meta.groups, "нет групп в Keitaro");
    fillSelect(form.elements.traffic_source_id, meta.traffic_sources, "нет источников в Keitaro");
  });
}

form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const fd = new FormData(form);
  if (!fd.get("offer_id")) return toast("Выберите оффер из списка", true);
  const num = (v) => (v ? Number(v) : null);
  const body = {
    name: fd.get("name").trim(),
    geo: fd.get("geo").trim().toUpperCase(),
    offer_id: Number(fd.get("offer_id")),
    domain_id: num(fd.get("domain_id")),
    group_id: num(fd.get("group_id")),
    traffic_source_id: num(fd.get("traffic_source_id")),
  };
  guarded(async () => {
    const c = await api("POST", "/api/campaigns", body);
    const res = document.getElementById("create-result");
    res.innerHTML = "";
    const card = document.createElement("div");
    card.className = "card result-ok";
    card.innerHTML = `<strong></strong><div class="muted"></div>`;
    card.querySelector("strong").textContent = `Кампания «${c.name}» создана (ID ${c.keitaro_id})`;
    card.querySelector("div").textContent = c.streams.map((s) => s.name).join(" · ");
    const open = document.createElement("button");
    open.textContent = "Открыть в редакторе";
    open.style.marginTop = "10px";
    open.addEventListener("click", () => openInEditor(c.keitaro_id));
    card.appendChild(open);
    res.appendChild(card);
    form.reset();
    createPicker.reset();
    loadMeta();
    toast("Кампания создана");
  }, form.querySelector("button[type=submit]"));
});

// ---------------------------------------------------------------- редактор

const campaignSelect = document.getElementById("campaign-select");
const view = document.getElementById("campaign-view");
let currentCampaign = null;

async function loadCampaignList(selectId) {
  await guarded(async () => {
    const list = await api("GET", "/api/campaigns");
    const keep = selectId || campaignSelect.value;
    campaignSelect.innerHTML = '<option value="">— выберите кампанию —</option>';
    for (const c of list) {
      const opt = document.createElement("option");
      opt.value = c.keitaro_id;
      opt.textContent = `${c.name} (#${c.keitaro_id})`;
      campaignSelect.appendChild(opt);
    }
    if (keep) campaignSelect.value = keep;
  });
}

async function openInEditor(keitaroId) {
  document.querySelector('.tab[data-tab="editor"]').click();
  await loadCampaignList(String(keitaroId));
  await loadCampaign(keitaroId);
}

async function loadCampaign(keitaroId) {
  if (!keitaroId) return (view.innerHTML = "");
  await guarded(async () => renderCampaign(await api("GET", `/api/campaigns/${keitaroId}`)));
}

campaignSelect.addEventListener("change", () => loadCampaign(campaignSelect.value));

document.getElementById("fetch-btn").addEventListener("click", (ev) => {
  const id = campaignSelect.value;
  if (!id) return toast("Сначала выберите кампанию", true);
  const dirty = currentCampaign && currentCampaign.streams.some((s) => s.dirty);
  if (dirty && !window.confirm("Неопубликованные изменения будут сброшены. Продолжить?")) return;
  guarded(async () => {
    renderCampaign(await api("POST", `/api/campaigns/${id}/fetch`));
    toast("Потоки обновлены из Keitaro");
  }, ev.currentTarget);
});

function renderCampaign(c) {
  currentCampaign = c;
  view.innerHTML = "";
  if (!c.streams.length) {
    view.innerHTML = '<div class="card muted">В кампании нет потоков.</div>';
    return;
  }
  for (const s of c.streams) view.appendChild(renderStream(s));
}

function replaceStream(s) {
  const idx = currentCampaign.streams.findIndex((x) => x.id === s.id);
  currentCampaign.streams[idx] = s;
  const old = view.querySelector(`[data-stream-id="${s.id}"]`);
  old.replaceWith(renderStream(s));
}

async function streamAction(method, url, body, btn, streamId) {
  if (btn) btn.disabled = true;
  try {
    replaceStream(await api(method, url, body));
    return true;
  } catch (e) {
    toast(e.message, true);
    // сервер отклонил изменение — возвращаем поток к последнему известному состоянию
    const s = streamId && currentCampaign.streams.find((x) => x.id === streamId);
    if (s) replaceStream(s);
    return false;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderStream(s) {
  const el = document.getElementById("stream-tpl").content.firstElementChild.cloneNode(true);
  el.dataset.streamId = s.id;
  el.classList.toggle("dirty", s.dirty);
  el.querySelector(".stream-name").textContent = s.name || `Поток #${s.keitaro_id}`;
  el.querySelector(".stream-meta").textContent = ` · #${s.keitaro_id} · ${s.schema || "—"}`;

  for (const sel of [".badge.draft", ".cancel-btn", ".push-btn"]) el.querySelector(sel).classList.toggle("hidden", !s.dirty);
  el.querySelector(".push-btn").addEventListener("click", (ev) => streamAction("POST", `/api/streams/${s.id}/push`, null, ev.currentTarget).then((ok) => ok && toast("Опубликовано в Keitaro")));
  el.querySelector(".cancel-btn").addEventListener("click", (ev) => streamAction("POST", `/api/streams/${s.id}/cancel`, null, ev.currentTarget));

  if (!s.accepts_offers) {
    el.querySelector(".no-offers").classList.remove("hidden");
    return el;
  }

  const table = el.querySelector(".offers");
  const tbody = table.querySelector("tbody");
  table.classList.toggle("hidden", !s.offers.length);
  let total = 0;
  for (const o of s.offers) {
    if (o.status === "active") total += o.share;
    tbody.appendChild(renderOfferRow(o, s.id));
  }
  const totalCell = table.querySelector(".total");
  totalCell.textContent = `${total}%`;
  totalCell.classList.toggle("bad", s.offers.some((o) => o.status === "active") && total !== 100);

  const add = el.querySelector(".add-offer");
  add.classList.remove("hidden");
  const addBtn = add.querySelector(".add-btn");
  let picked = null;
  setupOfferPicker(add.querySelector(".offer-picker"), (o) => {
    picked = o;
    addBtn.disabled = !o;
  });
  addBtn.addEventListener("click", () => {
    if (picked) streamAction("POST", `/api/streams/${s.id}/offers`, { offer_id: picked.id }, addBtn);
  });
  return el;
}

function renderOfferRow(o, streamId) {
  const tr = document.createElement("tr");
  tr.className = o.status === "removed" ? "removed" : o.is_new ? "new" : "";
  tr.innerHTML = `<td></td><td class="name"></td><td class="num"></td><td></td><td class="num"></td>`;
  const [idCell, nameCell, shareCell, pinCell, actCell] = tr.children;
  idCell.textContent = o.offer_id;
  nameCell.textContent = o.offer_name || "—";
  nameCell.title = o.offer_name;
  shareCell.textContent = `${o.share}%`;

  if (o.status === "active") {
    const wrap = document.createElement("div");
    wrap.className = "pin";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = o.pinned;
    box.title = "Закрепить долю";
    const val = document.createElement("input");
    val.type = "number";
    val.min = 0;
    val.max = 100;
    val.value = o.share;
    val.title = "Закреплённая доля, %";
    val.classList.toggle("hidden", !o.pinned);
    box.addEventListener("change", () => {
      if (box.checked) streamAction("POST", `/api/stream-offers/${o.id}/pin`, { share: o.share }, null, streamId);
      else streamAction("POST", `/api/stream-offers/${o.id}/unpin`, null, null, streamId);
    });
    val.addEventListener("change", () =>
      streamAction("POST", `/api/stream-offers/${o.id}/pin`, { share: Number(val.value) }, null, streamId)
    );
    wrap.append(box, val);
    pinCell.appendChild(wrap);

    const rm = document.createElement("button");
    rm.className = "link danger";
    rm.textContent = "Remove";
    rm.addEventListener("click", () => streamAction("POST", `/api/stream-offers/${o.id}/remove`, null, rm));
    actCell.appendChild(rm);
  } else {
    const back = document.createElement("button");
    back.className = "link";
    back.textContent = "Bring back";
    back.addEventListener("click", () => streamAction("POST", `/api/stream-offers/${o.id}/bring-back`, null, back));
    actCell.appendChild(back);
  }
  return tr;
}

loadMeta();
