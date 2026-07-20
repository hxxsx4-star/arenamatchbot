// 사다리타기 - 캔버스 렌더 + 무작위 가로줄 + 경로 추적
let L = null;

function parseList(id) {
  return document.getElementById(id).value
    .split(/[\n,]/).map(s => s.trim()).filter(Boolean);
}

function buildLadder() {
  const names = parseList("names");
  let results = parseList("results");
  if (names.length < 2) { alert("참가자를 2명 이상 입력하세요."); return; }
  while (results.length < names.length) results.push("꽝");
  results = results.slice(0, names.length);

  const n = names.length;
  const rungs = Math.max(6, n + 4);
  // 가로줄: 각 층마다 인접한 세로줄 사이 무작위 연결 (겹치지 않게)
  const bars = [];
  for (let r = 0; r < rungs; r++) {
    const used = new Set();
    for (let c = 0; c < n - 1; c++) {
      if (used.has(c)) continue;
      if (Math.random() < 0.45) { bars.push([r, c]); used.add(c); used.add(c + 1); }
    }
  }
  // 경로 계산
  const map = [];
  for (let start = 0; start < n; start++) {
    let col = start;
    for (let r = 0; r < rungs; r++) {
      if (bars.some(b => b[0] === r && b[1] === col)) col++;
      else if (bars.some(b => b[0] === r && b[1] === col - 1)) col--;
    }
    map.push(col);
  }
  L = { names, results, rungs, bars, map, n };
  draw(-1);
  renderResults();
}

function draw(highlight) {
  const cv = document.getElementById("ladder");
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height, n = L.n;
  ctx.clearRect(0, 0, W, H);
  const padX = 60, padY = 50;
  const colGap = (W - padX * 2) / (n - 1 || 1);
  const rowGap = (H - padY * 2) / L.rungs;
  const x = c => padX + c * colGap;
  const y = r => padY + r * rowGap;

  ctx.font = "13px sans-serif"; ctx.textAlign = "center";
  // 세로줄
  ctx.strokeStyle = "#d5dae6"; ctx.lineWidth = 3;
  for (let c = 0; c < n; c++) {
    ctx.beginPath(); ctx.moveTo(x(c), padY); ctx.lineTo(x(c), H - padY); ctx.stroke();
    ctx.fillStyle = "#1f2430";
    ctx.fillText(L.names[c], x(c), padY - 16);
    ctx.fillStyle = "#ee1515";
    ctx.fillText(L.results[c], x(c), H - padY + 22);
  }
  // 가로줄
  ctx.strokeStyle = "#c5ccdb";
  L.bars.forEach(([r, c]) => {
    ctx.beginPath(); ctx.moveTo(x(c), y(r)); ctx.lineTo(x(c + 1), y(r)); ctx.stroke();
  });

  // 강조 경로
  if (highlight >= 0) {
    ctx.strokeStyle = "#2f6bff"; ctx.lineWidth = 4;
    let col = highlight;
    ctx.beginPath(); ctx.moveTo(x(col), padY);
    for (let r = 0; r < L.rungs; r++) {
      ctx.lineTo(x(col), y(r));
      if (L.bars.some(b => b[0] === r && b[1] === col)) { col++; ctx.lineTo(x(col), y(r)); }
      else if (L.bars.some(b => b[0] === r && b[1] === col - 1)) { col--; ctx.lineTo(x(col), y(r)); }
    }
    ctx.lineTo(x(col), H - padY); ctx.stroke();
  }
}

function renderResults() {
  const box = document.getElementById("result-list");
  box.className = "";
  box.innerHTML = L.names.map((nm, i) =>
    `<div class="list-row"><b style="flex:1">${nm}</b>
     <button class="btn btn-ghost btn-sm" onclick="reveal(${i})">확인</button>
     <span id="res${i}" class="pill pill-gold" style="display:none">${L.results[L.map[i]]}</span></div>`
  ).join("");
}

function reveal(i) {
  draw(i);
  const el = document.getElementById("res" + i);
  if (el) el.style.display = "inline-block";
}

function revealAll() {
  if (!L) return;
  L.names.forEach((_, i) => { const el = document.getElementById("res" + i); if (el) el.style.display = "inline-block"; });
  draw(-1);
}
