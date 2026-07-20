// 경기 추가 모달 - 동적 팀/선수 입력
let teamSeq = 0;
const POS = ["", "탑", "정글", "미드", "원딜", "서포터"];

function posOptions() {
  return POS.map(p => `<option value="${p}">${p || "-"}</option>`).join("");
}

function playerRow() {
  return `<div class="player-line pl">
    <input class="p_sum" placeholder="라이엇 ID#태그">
    <input class="p_champ" placeholder="챔피언">
    <select class="p_pos">${posOptions()}</select>
    <input class="p_k" type="number" min="0" placeholder="K" style="width:52px">
    <input class="p_d" type="number" min="0" placeholder="D" style="width:52px">
    <input class="p_a" type="number" min="0" placeholder="A" style="width:52px">
    <span class="remove" onclick="this.closest('.pl').remove()">×</span>
  </div>`;
}

function teamBlock(idx) {
  const id = "team" + (teamSeq++);
  const d = document.createElement("div");
  d.className = "card team-block";
  d.id = id;
  d.style.margin = "10px 0";
  d.innerHTML = `
    <div class="row" style="align-items:center">
      <input class="t_name" value="${idx}팀" style="max-width:120px">
      <label style="display:flex;align-items:center;gap:6px;margin:0;font-weight:700;flex:none">
        <input type="checkbox" class="t_win" style="width:auto"> 승리</label>
      <span class="remove right" onclick="document.getElementById('${id}').remove()">팀 삭제</span>
    </div>
    <div class="players"></div>
    <button class="btn btn-ghost btn-sm" style="margin-top:8px" onclick="this.previousElementSibling.insertAdjacentHTML('beforeend', playerRow())">+ 선수</button>`;
  document.getElementById("teams").appendChild(d);
  const box = d.querySelector(".players");
  box.insertAdjacentHTML("beforeend", playerRow());
  box.insertAdjacentHTML("beforeend", playerRow());
}

function addTeam() {
  const n = document.querySelectorAll(".team-block").length + 1;
  teamBlock(n);
}

function initTeams() {
  document.getElementById("teams").innerHTML = "";
  teamBlock(1);
  teamBlock(2);
  const dt = document.getElementById("mt_date");
  if (dt && !dt.value) dt.value = new Date().toISOString().slice(0, 10);
}

function openMatch() { document.getElementById("matchModal").classList.add("show"); }
function closeMatch() { document.getElementById("matchModal").classList.remove("show"); }

async function saveMatch() {
  const teams = [];
  document.querySelectorAll(".team-block").forEach(tb => {
    const players = [];
    tb.querySelectorAll(".pl").forEach(pl => {
      const sum = pl.querySelector(".p_sum").value.trim();
      if (!sum) return;
      players.push({
        summoner: sum,
        champion: pl.querySelector(".p_champ").value.trim(),
        position: pl.querySelector(".p_pos").value,
        kills: pl.querySelector(".p_k").value || 0,
        deaths: pl.querySelector(".p_d").value || 0,
        assists: pl.querySelector(".p_a").value || 0,
      });
    });
    if (players.length)
      teams.push({ name: tb.querySelector(".t_name").value.trim() || "팀",
        win: tb.querySelector(".t_win").checked, players });
  });
  if (!teams.length) { alert("최소 한 팀의 선수를 입력하세요."); return; }
  const body = {
    date: document.getElementById("mt_date").value,
    mode: document.getElementById("mt_mode").value,
    title: document.getElementById("mt_title").value.trim() || "단판 매치",
    mvp: document.getElementById("mt_mvp").value.trim(),
    teams,
  };
  const r = await fetch("/api/matches", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (r.ok) location.reload();
  else alert("저장 실패: " + (await r.json()).detail);
}

async function delMatch(id) {
  if (!confirm("이 경기를 삭제할까요?")) return;
  await fetch("/api/matches/" + id, { method: "DELETE" });
  location.reload();
}
