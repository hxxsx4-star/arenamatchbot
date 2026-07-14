const $ = (id) => document.getElementById(id);

let bidMode = "fixed";

function captainRow(i) {
  const d = document.createElement("div");
  d.className = "rowline captain-cols cap-row";
  d.innerHTML = `
    <div class="idx">팀장${i + 1}</div>
    <input type="text" class="c-name" placeholder="이름 *">
    <input type="text" class="c-pos" placeholder="포지션">
    <input type="text" class="c-intro" placeholder="한줄소개">
    <input type="number" class="c-pts" placeholder="0" min="0">`;
  return d;
}

function memberRow(i) {
  const d = document.createElement("div");
  d.className = "rowline member-cols mem-row";
  d.innerHTML = `
    <div class="idx">${i + 1}</div>
    <input type="text" class="m-name" placeholder="이름 *">
    <input type="text" class="m-pos" placeholder="포지션">
    <input type="text" class="m-intro" placeholder="한줄소개">`;
  return d;
}

function rebuildCaptains() {
  const n = Math.max(2, parseInt($("team_count").value) || 0);
  const box = $("captains");
  const existing = [...box.querySelectorAll(".cap-row")].map((r) => ({
    name: r.querySelector(".c-name").value,
    pos: r.querySelector(".c-pos").value,
    intro: r.querySelector(".c-intro").value,
    pts: r.querySelector(".c-pts").value,
  }));
  box.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const row = captainRow(i);
    if (existing[i]) {
      row.querySelector(".c-name").value = existing[i].name;
      row.querySelector(".c-pos").value = existing[i].pos;
      row.querySelector(".c-intro").value = existing[i].intro;
      row.querySelector(".c-pts").value = existing[i].pts;
    }
    box.appendChild(row);
  }
  $("captainCount").textContent = `(${n}명)`;
}

function rebuildMembers() {
  const teams = Math.max(2, parseInt($("team_count").value) || 0);
  const size = Math.max(2, parseInt($("team_size").value) || 0);
  const target = (size - 1) * teams;
  const box = $("members");
  const rows = box.querySelectorAll(".mem-row");
  const cur = rows.length;
  if (target > cur) {
    for (let i = cur; i < target; i++) box.appendChild(memberRow(i));
  } else if (target < cur) {
    for (let i = cur - 1; i >= target; i--) rows[i].remove();
  }
  renumberMembers();
}

function renumberMembers() {
  const rows = $("members").querySelectorAll(".mem-row");
  rows.forEach((r, i) => (r.querySelector(".idx").textContent = i + 1));
  $("memberCount").textContent = `(${rows.length}명)`;
  updateSummary();
}

function updateSummary() {
  const teams = parseInt($("team_count").value) || 0;
  const size = parseInt($("team_size").value) || 0;
  const members = $("members").querySelectorAll(".mem-row").length;
  $("sumline1").innerHTML = `<b>${teams}개 팀</b> × <b>${size}명</b>(팀장 포함) = 총 <b>${teams * size}명</b>`;
  $("sumline2").textContent = `팀장 ${teams}명 + 경매 대상 팀원 ${members}명`;
}

// 입찰 방식 토글
$("bidmode").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  bidMode = b.dataset.mode;
  [...$("bidmode").children].forEach((x) => x.classList.toggle("active", x === b));
  $("fixed_wrap").style.display = bidMode === "fixed" ? "" : "none";
  $("ratio_wrap").style.display = bidMode === "ratio" ? "" : "none";
});

$("team_count").addEventListener("input", () => { rebuildCaptains(); rebuildMembers(); });
$("team_size").addEventListener("input", rebuildMembers);
$("addMember").addEventListener("click", () => {
  $("members").appendChild(memberRow(0));
  renumberMembers();
});

$("createBtn").addEventListener("click", submitAuction);

async function submitAuction() {
  const captains = [...document.querySelectorAll(".cap-row")].map((r) => ({
    name: r.querySelector(".c-name").value.trim(),
    position: r.querySelector(".c-pos").value.trim(),
    intro: r.querySelector(".c-intro").value.trim(),
    points: parseInt(r.querySelector(".c-pts").value) || 0,
  }));
  const members = [...document.querySelectorAll(".mem-row")].map((r) => ({
    name: r.querySelector(".m-name").value.trim(),
    position: r.querySelector(".m-pos").value.trim(),
    intro: r.querySelector(".m-intro").value.trim(),
  })).filter((m) => m.name);

  const filledCaptains = captains.filter((c) => c.name);
  const teamCount = parseInt($("team_count").value) || 0;
  if (!$("title").value.trim()) return alert("경매 타이틀을 입력하세요.");
  if (filledCaptains.length !== teamCount) return alert(`팀장 ${teamCount}명의 이름을 모두 입력하세요. (현재 ${filledCaptains.length}명)`);
  if (members.length === 0) return alert("경매 대상 팀원을 최소 1명 입력하세요.");

  const totalPoints = parseInt($("total_points").value) || 0;
  // 팀장 포인트가 0이면 총 포인트로 채움
  filledCaptains.forEach((c) => { if (!c.points) c.points = totalPoints; });

  const payload = {
    title: $("title").value.trim(),
    team_count: teamCount,
    team_size: parseInt($("team_size").value) || 0,
    total_points: totalPoints,
    show_order: $("show_order").checked,
    bid_mode: bidMode,
    fixed_unit: parseInt($("fixed_unit").value) || 1,
    ratio_percent: parseFloat($("ratio_percent").value) || 10,
    bid_time: parseInt($("bid_time").value) || 15,
    extend_time: parseInt($("extend_time").value) || 5,
    captains: filledCaptains,
    members,
  };

  $("createBtn").disabled = true;
  $("createBtn").textContent = "생성 중...";
  try {
    const res = await fetch("/api/auctions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "생성 실패");
    }
    const data = await res.json();
    showLinks(data);
  } catch (e) {
    alert("오류: " + e.message);
    $("createBtn").disabled = false;
    $("createBtn").textContent = "경매 생성하기";
  }
}

function linkRow(who, link) {
  const d = document.createElement("div");
  d.className = "linkbox";
  d.innerHTML = `<div class="who">${who}</div><div class="lk">${link}</div>
    <button class="btn small ghost">복사</button>`;
  const copy = () => navigator.clipboard.writeText(link).then(() => {
    d.querySelector("button").textContent = "복사됨!";
    setTimeout(() => (d.querySelector("button").textContent = "복사"), 1200);
  });
  d.querySelector("button").addEventListener("click", copy);
  d.querySelector(".lk").addEventListener("click", copy);
  return d;
}

function showLinks(data) {
  const box = $("links");
  box.innerHTML = "";
  box.appendChild(linkRow("주최자", data.host_link));
  box.appendChild(linkRow("옵저버", data.observer_link));
  data.captain_links.forEach((c) => box.appendChild(linkRow(`팀장 · ${c.name}`, c.link)));
  $("hostGo").href = data.host_link;
  $("result").style.display = "";
  $("result").scrollIntoView({ behavior: "smooth" });
}

// 초기화
rebuildCaptains();
rebuildMembers();
updateSummary();
