const R = window.ROOM;
const $ = (id) => document.getElementById(id);
const fmt = (n) => (n || 0).toLocaleString();

let ws = null;
let state = null;
let reconnectTimer = null;

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}${R.prefix||""}/ws/${R.rid}${R.search}`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "state") { state = msg.state; render(); }
    else if (msg.type === "notice") { showNotice(msg.msg, !msg.ok); }
  };
  ws.onclose = () => {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, 1500);
  };
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function showNotice(text, isErr = true) {
  const n = $("notice");
  n.textContent = text;
  n.style.color = isErr ? "var(--danger)" : "var(--ok)";
  clearTimeout(n._t);
  n._t = setTimeout(() => (n.textContent = ""), 2500);
}

// ---- 역할 배지 ----
const roleName = { host: "주최자", captain: "팀장 · " + R.cname, observer: "옵저버" };
$("roleBadge").textContent = roleName[R.role] || "관전";
$("roleBadge").classList.add(R.role);

// ---- 렌더 ----
function render() {
  if (!state) return;
  const phaseKo = { lobby: "대기 중", bidding: "입찰 중", sold: "낙찰 결과", finished: "경매 종료" };
  const badge = $("phaseBadge");
  badge.textContent = phaseKo[state.phase] || state.phase;
  badge.className = "badge " + state.phase;

  $("subinfo").textContent =
    `팀 ${state.team_count} · 팀당 ${state.team_size}명 · 총 ${fmt(state.total_points)}P · ` +
    (state.bid_mode === "fixed" ? `고정 +${fmt(state.fixed_unit)}` : `비율 +${state.ratio_percent}%`);
  $("progress").textContent = `${state.progress.done} / ${state.progress.total}`;

  renderStage();
  renderControls();
  renderTeams();
  renderUpcoming();
}

function renderStage() {
  const body = $("stageBody");
  if (state.phase === "lobby") {
    body.innerHTML = `<div class="lobby-msg">${
      R.role === "host" ? "아래 '경매 시작'을 누르면 팀원이 무작위 순서로 경매됩니다." : "주최자가 경매를 시작하기를 기다리는 중…"
    }</div>`;
  } else if (state.phase === "finished") {
    body.innerHTML = `<div class="result-grid"><div class="lobby-msg">🏆 경매가 종료되었습니다! 아래 팀 현황이 최종 결과입니다.<br>결과는 디스코드 내전 로그에도 기록됩니다.</div></div>`;
  } else if (state.current) {
    const c = state.current;
    const t = state.timer_remaining;
    const warn = t <= (state.extend_time || 5) ? "warn" : "";
    const hbwho = state.high_cid ? capName(state.high_cid) : "—";
    const soldOverlay = state.phase === "sold"
      ? `<div class="hbwho" style="color:var(--ok)">${state.high_cid ? "낙찰: " + hbwho : "유찰"}</div>` : "";
    body.innerHTML = `
      <div class="now">
        <div class="who">
          <div class="nm">${esc(c.name)}</div>
          ${c.position ? `<div class="pos">${esc(c.position)}</div>` : ""}
          ${c.intro ? `<div class="intro">${esc(c.intro)}</div>` : ""}
        </div>
        <div class="bidinfo">
          <div class="hb">${state.high_cid ? fmt(state.high_bid) + "P" : "입찰 없음"}</div>
          <div class="hbwho">${state.high_cid ? "최고 입찰: " + hbwho : "시작가 " + fmt(state.min_next_bid) + "P"}</div>
          ${soldOverlay}
        </div>
        <div class="timer ${warn}">${state.phase === "sold" ? "✔" : t}</div>
      </div>`;
  }
}

function renderControls() {
  const isHost = R.role === "host";
  const isCap = R.role === "captain";
  $("hostCtrl").style.display = isHost ? "flex" : "none";
  $("bidCtrl").style.display = isCap ? "block" : "none";

  if (isHost) {
    $("btnStart").style.display = state.phase === "lobby" ? "" : "none";
    $("btnReshuffle").style.display = state.phase === "lobby" ? "" : "none";
    $("btnSkip").style.display = state.phase === "bidding" ? "" : "none";
  }

  if (isCap) {
    const me = state.captains.find((c) => c.cid === R.viewer_cid || c.cid === R.cid);
    if (me) {
      $("myPoints").textContent = fmt(me.points) + "P";
      $("mySlots").textContent = me.slots_left;
    }
    const canBid = state.phase === "bidding";
    $("btnBid").disabled = !canBid;
    $("bidAmount").disabled = !canBid;
    if (canBid) {
      const min = state.min_next_bid;
      const cur = parseInt($("bidAmount").value) || 0;
      if (cur < min) $("bidAmount").value = min;
      renderQuick(min);
    } else {
      $("bidQuick").innerHTML = "";
    }
  }
}

function renderQuick(min) {
  const unit = state.bid_mode === "fixed" ? state.fixed_unit
    : Math.max(1, Math.round(state.high_bid * state.ratio_percent / 100));
  const box = $("bidQuick");
  box.innerHTML = "";
  [0, unit, unit * 2, unit * 5].forEach((add, i) => {
    const val = min + add;
    const b = document.createElement("button");
    b.textContent = i === 0 ? `최소 ${fmt(val)}` : `+${fmt(add)} → ${fmt(val)}`;
    b.onclick = () => { $("bidAmount").value = val; };
    box.appendChild(b);
  });
}

function renderTeams() {
  const box = $("teams");
  box.innerHTML = "";
  state.captains.forEach((c) => {
    const d = document.createElement("div");
    d.className = "team" + (c.cid === state.high_cid ? " leading" : "");
    const roster = c.roster.map((mid) => {
      const m = state.members.find((x) => x.mid === mid);
      return m ? `<li>${esc(m.name)}<span class="pr">${fmt(m.won_price)}P</span></li>` : "";
    }).join("");
    d.innerHTML = `
      <div class="th">
        <span class="cap">🧑‍✈️ ${esc(c.name)}</span>
        <span class="pts">${fmt(c.points)}P</span>
      </div>
      <div class="slots">남은 슬롯 ${c.slots_left} · 영입 ${c.roster.length}명</div>
      <ul>${roster}</ul>`;
    box.appendChild(d);
  });
}

function renderUpcoming() {
  const card = $("upcomingCard");
  if (!state.show_order || !state.upcoming || !state.upcoming.length || state.phase === "finished") {
    card.style.display = "none"; return;
  }
  card.style.display = "";
  $("upcoming").innerHTML = state.upcoming.map((m) =>
    `<div class="u">${esc(m.name)}${m.position ? ` <span class="p">${esc(m.position)}</span>` : ""}</div>`
  ).join("");
}

function capName(cid) {
  const c = state.captains.find((x) => x.cid === cid);
  return c ? c.name : "?";
}
function esc(s) { return (s || "").replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m])); }

// ---- 이벤트 ----
$("btnStart") && ($("btnStart").onclick = () => send({ action: "start" }));
$("btnReshuffle") && ($("btnReshuffle").onclick = () => send({ action: "reshuffle" }));
$("btnSkip") && ($("btnSkip").onclick = () => { if (confirm("현재 팀원을 유찰 처리할까요?")) send({ action: "skip" }); });
$("btnBid") && ($("btnBid").onclick = () => {
  const amt = parseInt($("bidAmount").value) || 0;
  send({ action: "bid", amount: amt });
});
$("btnPlus") && ($("btnPlus").onclick = () => {
  const unit = state && state.bid_mode === "fixed" ? state.fixed_unit : 10;
  $("bidAmount").value = (parseInt($("bidAmount").value) || 0) + unit;
});
$("btnMinus") && ($("btnMinus").onclick = () => {
  const unit = state && state.bid_mode === "fixed" ? state.fixed_unit : 10;
  $("bidAmount").value = Math.max(0, (parseInt($("bidAmount").value) || 0) - unit);
});

connect();
