// Frontend for the multi-agent exam scheduler. Plain JS, no build step.
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const SESSION = (() => {
  try {
    let id = sessionStorage.getItem("examSession");
    if (!id) { id = Math.random().toString(36).slice(2); sessionStorage.setItem("examSession", id); }
    return id;
  } catch { return "default"; }
})();

const state = {
  scenarios: [],
  snap: null,
  prevCommitted: {},
  moved: new Set(),
  view: "room",
  evKind: "room",
  playing: null,
  msgFilter: "",
};

// ------------------------------------------------------------------ API
async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session: SESSION, ...body }),
  });
  let data;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) throw new Error(data?.detail || res.statusText || "Server error");
  if (!data) throw new Error("Invalid response from server");
  return data;
}

function applySnapshot(snap, { fresh = false } = {}) {
  const prev = fresh ? {} : (state.snap?.coordinator.committed || {});
  state.moved = new Set();
  for (const [c, a] of Object.entries(snap.coordinator.committed)) {
    const p = prev[c];
    // highlight exams that moved, or that were newly placed into an existing timetable
    const changed = p ? p.room !== a.room || JSON.stringify(p.interval) !== JSON.stringify(a.interval) || p.invigilators.join() !== a.invigilators.join()
                      : Object.keys(prev).length > 0;
    if (!fresh && changed) {
      state.moved.add(c);
    }
  }
  state.snap = snap;
  render();
}

// ------------------------------------------------------------------ setup
async function init() {
  initTheme();
  state.scenarios = await api("/api/scenarios");
  const sel = $("#scenario");
  sel.innerHTML = state.scenarios.map((s) => `<option value="${esc(s.name)}">${esc(s.title)}</option>`).join("");
  sel.addEventListener("change", onScenarioChange);
  // deep links: ?scenario=contention&run=1
  const q = new URLSearchParams(location.search);
  if (state.scenarios.some((s) => s.name === q.get("scenario"))) sel.value = q.get("scenario");
  onScenarioChange();

  initBuilder();
  $("#loadBtn").onclick = load;
  $("#stepBtn").onclick = () => guard(async () => applySnapshot(await api("/api/sim/step", { ticks: 1 })));
  $("#runBtn").onclick = () => guard(async () => { stopPlay(); applySnapshot(await api("/api/sim/run", { ticks: 100 })); });
  $("#playBtn").onclick = togglePlay;
  $("#evBtn").onclick = injectEvent;
  $("#msgFilter").onchange = (e) => { state.msgFilter = e.target.value; renderMessages(); };

  document.querySelectorAll("#evKind button").forEach((b) => b.onclick = () => {
    state.evKind = b.dataset.kind;
    document.querySelectorAll("#evKind button").forEach((x) => x.classList.toggle("on", x === b));
    $("#evReason").value = state.evKind === "room" ? "AC maintenance" : "sick leave";
    renderEventForm();
  });
  document.querySelectorAll("#ttView button").forEach((b) => b.onclick = () => {
    state.view = b.dataset.view;
    document.querySelectorAll("#ttView button").forEach((x) => x.classList.toggle("on", x === b));
    renderTimetable();
  });
  document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== `tab-${b.dataset.tab}`));
  });
  await load();
  if (q.get("run")) applySnapshot(await api("/api/sim/run", { ticks: 100 }));
}

const DEFAULT_PARAMS = {
  stress: { num_exams: 75, num_rooms: 6, num_faculty: 12, num_days: 5, seed: 7, flakiness: 0.03 },
  random: { num_exams: 20, num_rooms: 5, num_faculty: 10, num_days: 5, seed: 42, flakiness: 0 },
};

function onScenarioChange() {
  const name = $("#scenario").value;
  const s = state.scenarios.find((x) => x.name === name);
  $("#scenarioDesc").textContent = s?.description || "";
  $("#params").classList.toggle("hidden", !s?.configurable);
  for (const [k, v] of Object.entries(DEFAULT_PARAMS[name] || {})) $(`#p_${k}`).value = v;
}

async function load() {
  stopPlay();
  const scenario = $("#scenario").value;
  const params = {};
  if (DEFAULT_PARAMS[scenario]) for (const k of Object.keys(DEFAULT_PARAMS[scenario])) params[k] = $(`#p_${k}`).value;
  await guard(async () => applySnapshot(await api("/api/sim", { scenario, params }), { fresh: true }));
}

async function guard(fn) {
  try { await fn(); } catch (e) { $("#evMsg").textContent = "⚠ " + e.message; stopPlay(); }
}

function togglePlay() {
  if (state.playing) return stopPlay();
  $("#playBtn").textContent = "Pause ❚❚";
  state.playing = setInterval(async () => {
    if (state.snap?.summary.done) return stopPlay();
    await guard(async () => applySnapshot(await api("/api/sim/step", { ticks: 1 })));
  }, 900);
}
function stopPlay() {
  clearInterval(state.playing);
  state.playing = null;
  $("#playBtn").textContent = "Auto ▶";
}

async function injectEvent() {
  const [day, start, end] = ["#evDay", "#evStart", "#evEnd"].map((s) => Number($(s).value));
  if (end <= start) { $("#evMsg").textContent = "⚠ end must be after start"; return; }
  await guard(async () => {
    applySnapshot(await api("/api/sim/event", {
      kind: state.evKind, resource: $("#evResource").value, day, start, end, reason: $("#evReason").value || "unavailable",
    }));
    const ev = state.snap.events.at(-1);
    $("#evMsg").textContent = ev ? `Broadcast sent. Affected: ${ev.affected.join(", ") || "none"}` : "";
  });
}

// ------------------------------------------------------------------ render
function render() {
  renderStats();
  renderTimetable();
  renderAgents();
  renderTrace();
  renderPeas();
  renderDecisions();
  renderMessages();
  renderEventForm();
  renderBuilderForm();
  const up = state.snap.upcoming_events;
  $("#upcoming").textContent = up.length ? `Scheduled disruptions: ${up.map((e) => `t${e.tick} ${e.description}`).join("; ")}.` : "";
}

function renderStats() {
  const s = state.snap.summary;
  const tiles = [
    ["Tick", s.tick + (s.done ? " ✓" : "")],
    ["Scheduled", `${s.scheduled}/${s.exams}`, s.scheduled === s.exams ? "good" : ""],
    ["Hard conflicts", s.hard_conflicts, s.hard_conflicts ? "bad" : ""],
    ["In negotiation", s.in_progress, s.in_progress ? "warn" : ""],
    ["Messages", s.messages],
    ["Search nodes", s.search_nodes],
    ["Backtracks", s.backtracks, s.backtracks ? "warn" : ""],
    ["Priority bumps", s.bumps],
    ["Invig. swaps", s.invigilator_swaps],
    ["Local replans", s.localized_replans],
    ["Booking refusals", s.booking_rejections],
  ];
  $("#stats").innerHTML = tiles.map(([k, v, cls]) => `<div class="stat ${cls || ""}"><b>${esc(v)}</b><span>${esc(k)}</span></div>`).join("");
  const bad = state.snap.violations.length;
  const badge = $("#validity");
  badge.className = "badge " + (bad ? "bad" : "ok");
  badge.textContent = bad ? `✗ ${bad} hard-constraint violation(s)` : "✓ All hard constraints satisfied";
  badge.title = state.snap.violations.join("\n");
}

function prioClass(p) { return p >= 3 ? "p3" : p === 2 ? "p2" : "p1"; }

function renderTimetable() {
  const snap = state.snap;
  const { days, day_start, day_end } = snap.period;
  const span = day_end - day_start;
  const examsBy = Object.fromEntries(snap.exams.map((e) => [e.course, e]));
  const rows = state.view === "room" ? snap.rooms : snap.faculty;
  const committed = Object.values(snap.coordinator.committed);

  const pct = (h) => ((h - day_start) / span) * 100;
  const hourLabels = [];
  for (let h = day_start; h <= day_end; h += 2) hourLabels.push(`<span>${h}</span>`);

  let html = `<div class="tt" style="grid-template-columns: 150px repeat(${days.length}, minmax(130px, 1fr))">`;
  html += `<div class="corner">${state.view === "room" ? "Room (cap.)" : "Invigilator"}</div>`;
  html += days.map((d) => `<div class="dayh">${esc(d)}<div class="hours">${hourLabels.join("")}</div></div>`).join("");

  for (const r of rows) {
    const sub = state.view === "room" ? `cap ${r.capacity}` : `${r.department} · ${r.bookings.length} duties`;
    html += `<div class="rowh" title="${esc(r.name)}">${esc(r.id)}<small>${esc(r.name)} · ${esc(sub)}</small></div>`;
    for (let d = 0; d < days.length; d++) {
      html += `<div class="cell" style="background-size:${100 / span}% 100%">`;
      for (const b of r.blocked.filter((b) => b.day === d)) {
        html += `<div class="blk off" style="left:${pct(b.start)}%;width:${pct(b.end) - pct(b.start)}%" data-tip="${esc(`Unavailable ${b.start}:00–${b.end}:00 — ${b.reason}`)}"></div>`;
      }
      const mine = committed.filter((a) => a.interval.day === d && (state.view === "room" ? a.room === r.id : a.invigilators.includes(r.id)));
      for (const a of mine) {
        const e = examsBy[a.course];
        const tip = `${a.course} — ${e.title}\n${e.students} students · priority ${e.priority}\n${days[d]} ${a.interval.start}:00–${a.interval.end}:00 in ${a.room}\nInvigilators: ${a.invigilators.join(", ")}\nGroups: ${e.groups.join(", ")}`;
        html += `<div class="blk exam ${prioClass(e.priority)} ${state.moved.has(a.course) ? "moved" : ""}" style="left:${pct(a.interval.start)}%;width:${pct(a.interval.end) - pct(a.interval.start)}%" data-tip="${esc(tip)}">
          ${esc(a.course)}<small>${state.view === "room" ? e.students + " st" : a.room}</small></div>`;
      }
      html += `</div>`;
    }
  }
  html += `</div>`;
  $("#timetable").innerHTML = html;

  const unresolved = Object.entries(snap.coordinator.unresolved);
  $("#unplaced").innerHTML = unresolved.length
    ? `<div class="unplaced"><h3>Could not be placed (${unresolved.length})</h3><ul>${unresolved.map(([c, r]) => `<li><b>${esc(c)}</b> (${examsBy[c].students} st, ${examsBy[c].duration}h, prio ${examsBy[c].priority}): ${esc(r)}</li>`).join("")}</ul></div>`
    : "";
}

function renderAgents() {
  const snap = state.snap;
  const c = snap.coordinator;
  const dayName = (d) => snap.period.days[d];
  $("#coordCard").innerHTML = `<div class="card">
    <header><b>coordinator</b><span class="pill ${c.pending.length ? "requested" : "scheduled"}">${c.pending.length ? "planning" : "idle"}</span></header>
    <div class="kvline"><span>Master timetable</span><b>${Object.keys(c.committed).length} exams</b></div>
    <div class="kvline"><span>Pending queue</span><b>${esc(c.pending.join(", ") || "empty")}</b></div>
    <div class="kvline"><span>Unresolved</span><b>${Object.keys(c.unresolved).length}</b></div>
    <div class="kvline"><span>Planning rounds</span><b>${c.stats.planning_rounds}</b></div>
    <div class="kvline"><span>Disruptions handled</span><b>${c.stats.disruptions}</b></div>
  </div>`;

  const hours = snap.period.days.length * (snap.period.day_end - snap.period.day_start);
  const resCard = (r, extra) => {
    const used = r.bookings.reduce((s, b) => s + b.end - b.start, 0);
    return `<div class="card"><header><b>${esc(r.id)}</b><span class="muted">${esc(extra)}</span></header>
      <div class="kvline"><span>${esc(r.name)}</span><span>${r.bookings.length} booked</span></div>
      <div class="kvline muted"><span>accepted ${r.stats.accepted} · rejected ${r.stats.rejected} · queries ${r.stats.queries}</span></div>
      ${r.blocked.length ? `<ul>${r.blocked.map((b) => `<li>✗ ${esc(dayName(b.day))} ${b.start}–${b.end}: ${esc(b.reason)}</li>`).join("")}</ul>` : ""}
      <div class="bar" title="utilisation"><i style="width:${Math.min(100, (used / hours) * 100)}%"></i></div></div>`;
  };
  $("#roomCards").innerHTML = snap.rooms.map((r) => resCard(r, `cap ${r.capacity}`)).join("");
  $("#facCards").innerHTML = snap.faculty.map((f) => resCard(f, f.department)).join("");

  const order = { hard_conflict: 0, disrupted: 1, rejected: 1, requested: 2, waiting: 3, scheduled: 4 };
  const exams = [...snap.exams].sort((a, b) => order[a.status] - order[b.status] || a.course.localeCompare(b.course));
  $("#examCards").innerHTML = exams.map((e) => `<div class="card">
    <header><b>${esc(e.course)}</b><span class="pill ${e.status}">${esc(e.status.replace("_", " "))}</span></header>
    <div class="muted">${esc(e.title)}</div>
    <div class="kvline"><span>${e.students} students · ${e.duration}h · prio ${e.priority}</span><span>${esc(e.groups.join(", "))}</span></div>
    <div class="kvline"><span>Window</span><span>${esc(e.allowed_days.map(dayName).join(", "))}${e.window_index ? ` (alt #${e.window_index})` : ""}</span></div>
    ${e.conflicts_with.length ? `<div class="kvline"><span>Shares students with</span><span>${esc(e.conflicts_with.join(", "))}</span></div>` : ""}
    ${e.assignment ? `<div class="kvline"><span>Booked</span><b>${esc(e.assignment.room)} ${esc(dayName(e.assignment.interval.day))} ${e.assignment.interval.start}:00</b></div>` : ""}
    ${e.history.length ? `<ul>${e.history.map((h) => `<li>${esc(h)}</li>`).join("")}</ul>` : ""}
  </div>`).join("");
}

function renderTrace() {
  const t = state.snap.coordinator.last_trace;
  $("#trace").innerHTML = t.length
    ? t.map((x) => `<li class="${x.op}"><span class="op">${esc(x.op)}</span>${esc(x.course || "")}${x.room ? ` → ${esc(x.room)} ${esc(x.slot)}` : ""}${x.detail ? ` <span class="muted">— ${esc(x.detail)}</span>` : ""}</li>`).join("")
    : `<li class="muted">No planning round yet. Press Step.</li>`;
}

function renderPeas() {
  const labels = { exam: "Exam Request Agent", room: "Resource Agent: Room", faculty: "Resource Agent: Invigilator", coordinator: "Scheduler / Coordinator" };
  $("#peas").innerHTML = `<div class="peas-grid">${Object.entries(state.snap.peas).map(([k, p]) => `
    <div><h3>${esc(labels[k])}</h3><table class="kv">${Object.entries(p).map(([a, b]) => `<tr><th>${esc(a)}</th><td>${esc(b)}</td></tr>`).join("")}</table></div>`).join("")}</div>`;
}

function renderDecisions() {
  const d = [...state.snap.coordinator.decisions].reverse();
  $("#decisions").innerHTML = d.length
    ? d.map((x) => `<li class="${x.kind}"><span class="t">t${x.tick}</span>${esc(x.text)}</li>`).join("")
    : `<li class="muted">Nothing yet.</li>`;
}

const FILTERS = {
  negotiation: ["EXAM_REQUEST", "COUNTER_OFFER", "CONFIRM", "REJECT", "HARD_CONFLICT", "RESCHEDULE_NOTICE"],
  booking: ["BOOK_REQUEST", "BOOK_ACCEPT", "BOOK_REJECT", "CANCEL_BOOKING", "CANCEL_ACK"],
  query: ["QUERY_AVAILABILITY", "AVAILABILITY"],
};
function renderMessages() {
  const f = state.msgFilter;
  const all = state.snap.messages;
  const msgs = all.filter((m) => !f || (FILTERS[f] ? FILTERS[f].includes(m.type) : m.type === f)).reverse().slice(0, 300);
  $("#msgCount").textContent = `(${state.snap.summary.messages} total)`;
  $("#messages").innerHTML = msgs.map((m) => `<li><span class="t">t${m.tick}</span><span class="mt ${m.type}">${esc(m.type)}</span>
    <span class="route">${esc(m.sender)} → ${esc(m.recipient === "*" ? "all" : m.recipient)}</span><br>${esc(m.summary)}</li>`).join("");
}

function renderEventForm() {
  const snap = state.snap;
  const pool = state.evKind === "room" ? snap.rooms : snap.faculty;
  const keep = (sel, html) => { const v = $(sel).value; $(sel).innerHTML = html; if ([...$(sel).options].some((o) => o.value === v)) $(sel).value = v; };
  keep("#evResource", pool.map((r) => `<option value="${esc(r.id)}">${esc(r.id)} · ${esc(r.name)} (${r.bookings.length} booked)</option>`).join(""));
  keep("#evDay", snap.period.days.map((d, i) => `<option value="${i}">${esc(d)}</option>`).join(""));
  const hrs = [];
  for (let h = snap.period.day_start; h <= snap.period.day_end; h++) hrs.push(h);
  keep("#evStart", hrs.slice(0, -1).map((h) => `<option value="${h}">${h}:00</option>`).join(""));
  keep("#evEnd", hrs.slice(1).map((h) => `<option value="${h}">${h}:00</option>`).join(""));
  if (!$("#evEnd").dataset.touched) { $("#evEnd").value = String(snap.period.day_end); $("#evEnd").dataset.touched = "1"; }
}

// ------------------------------------------------------------------ builder panel
function initBuilder() {
  document.querySelectorAll("#builderTabs button").forEach((b) => b.onclick = () => {
    document.querySelectorAll("#builderTabs button").forEach((x) => x.classList.toggle("on", x === b));
    document.querySelectorAll(".builder-form").forEach((f) => f.classList.toggle("hidden", f.id !== `bf-${b.dataset.builder}`));
    $("#builderMsg").textContent = "";
  });
  $("#ar_btn").onclick = addRoom;
  $("#af_btn").onclick = addFaculty;
  $("#ae_btn").onclick = addExam;
}

function builderMsg(text, ok) {
  const el = $("#builderMsg");
  el.textContent = (ok ? "✓ " : "⚠ ") + text;
  el.className = "small " + (ok ? "success" : "error");
}

async function addRoom() {
  const id = $("#ar_id").value.trim();
  if (!id) { builderMsg("Room ID is required", false); return; }
  try {
    const snap = await api("/api/sim/room", {
      id, name: $("#ar_name").value, capacity: Number($("#ar_cap").value), building: $("#ar_bldg").value,
    });
    applySnapshot(snap);
    builderMsg(`Room ${id} added — now available for scheduling`, true);
    $("#ar_id").value = ""; $("#ar_name").value = "";
  } catch (e) { builderMsg(e.message, false); }
}

async function addFaculty() {
  const id = $("#af_id").value.trim();
  const name = $("#af_name").value.trim();
  if (!id) { builderMsg("Faculty ID is required", false); return; }
  if (!name) { builderMsg("Faculty name is required", false); return; }
  try {
    const snap = await api("/api/sim/faculty", {
      id, name, department: $("#af_dept").value,
    });
    applySnapshot(snap);
    builderMsg(`${name} (${id}) added as invigilator`, true);
    $("#af_id").value = ""; $("#af_name").value = "";
  } catch (e) { builderMsg(e.message, false); }
}

async function addExam() {
  const course = $("#ae_course").value.trim();
  if (!course) { builderMsg("Course code is required", false); return; }
  const groupsRaw = $("#ae_groups").value.trim();
  const groups = groupsRaw ? groupsRaw.split(",").map((g) => g.trim()).filter(Boolean) : ["GEN"];
  // Gather checked days
  const checkedDays = [...document.querySelectorAll("#ae_days input:checked")].map((cb) => Number(cb.value));
  const windows = checkedDays.length ? [checkedDays, list_all_days()] : [];
  const prefDay = $("#ae_pday").value;
  const prefHour = $("#ae_phour").value;
  try {
    const snap = await api("/api/sim/exam", {
      course,
      title: $("#ae_title").value || course,
      students: Number($("#ae_students").value),
      groups,
      duration: Number($("#ae_duration").value),
      priority: Number($("#ae_prio").value),
      windows,
      preferred_day: prefDay !== "" ? Number(prefDay) : null,
      preferred_hour: prefHour !== "" ? Number(prefHour) : null,
      preferred_room: $("#ae_room").value || null,
    });
    applySnapshot(snap);
    builderMsg(`Exam ${course} added — step the simulation to schedule it`, true);
    $("#ae_course").value = ""; $("#ae_title").value = "";
  } catch (e) { builderMsg(e.message, false); }
}

function list_all_days() {
  if (!state.snap) return [];
  return state.snap.period.days.map((_, i) => i);
}

function renderBuilderForm() {
  const snap = state.snap;
  if (!snap) return;
  const { days, day_start, day_end } = snap.period;
  // Day checkboxes
  $("#ae_days").innerHTML = days.map((d, i) => `<label><input type="checkbox" value="${i}"><span>${esc(d)}</span></label>`).join("");
  // Preferred day dropdown
  const keepVal = (sel, html) => { const v = $(sel).value; $(sel).innerHTML = html; if ([...$(sel).options].some((o) => o.value === v)) $(sel).value = v; };
  keepVal("#ae_pday", `<option value="">None</option>` + days.map((d, i) => `<option value="${i}">${esc(d)}</option>`).join(""));
  // Preferred hour dropdown
  const hrs = [];
  for (let h = day_start; h < day_end; h++) hrs.push(h);
  keepVal("#ae_phour", `<option value="">None</option>` + hrs.map((h) => `<option value="${h}">${h}:00</option>`).join(""));
  // Preferred room dropdown
  keepVal("#ae_room", `<option value="">Any room</option>` + snap.rooms.map((r) => `<option value="${esc(r.id)}">${esc(r.id)} (cap ${r.capacity})</option>`).join(""));
}

// ------------------------------------------------------------------ misc
function initTheme() {
  let t = null;
  try { t = localStorage.getItem("theme"); } catch { /* storage unavailable */ }
  if (!t) t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.dataset.theme = t;
  $("#themeBtn").onclick = () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch { /* ignore */ }
  };
}

const tip = $("#tooltip");
document.addEventListener("mousemove", (e) => {
  const el = e.target.closest("[data-tip]");
  if (!el) { tip.classList.add("hidden"); return; }
  tip.textContent = el.dataset.tip;
  tip.style.whiteSpace = "pre-line";
  tip.classList.remove("hidden");
  const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8);
  const y = Math.min(e.clientY + 14, innerHeight - tip.offsetHeight - 8);
  tip.style.left = x + "px";
  tip.style.top = y + "px";
});

init().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<p style="color:red;padding:12px">Failed to start: ${esc(e.message)}. Is the server running?</p>`); });

