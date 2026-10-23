/* Each browser keeps its own unguessable seat credential. All cards/actions
 * are authorized and validated by the server; the browser never shuffles. */
"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = value => Number(value).toLocaleString("zh-CN");
const signed = value => `${value > 0 ? "+" : ""}${num(value)}`;
const hide = (id, hidden) => $(id).classList.toggle("hidden", hidden);
const SUITS = {s:"♠",h:"♥",d:"♦",c:"♣"};
const POSITION_NAMES = {"D":"庄家 / BTN","SB":"小盲","BB":"大盲","D / SB":"庄家兼小盲","UTG":"枪口位","UTG+1":"枪口后第一位","UTG+2":"枪口后第二位","MP":"中位","MP+1":"中位后第一位","LJ":"Lojack","HJ":"Hijack","CO":"Cutoff"};
// Retain the existing key so the brand rename preserves saved seat credentials.
const SEAT_STORAGE_KEY="tongzhuo.rooms";
let snapshot = null, busy = false, polling = false, pollTimer, toastTimer;
let sizingTurn = "", selectedRaise = null, selectedPreset = "", allInCall = false;
let serverOffset = 0, savedSettings = "", connectionOK = true;
let timerAnchor=null, countdownNoticeKey="", timeoutSyncKey="";
let sessionReport = null, reportBusy = false, chartSelection = null;
let chatMessages = [], chatCursor = 0, chatTarget = 0, lastReadChat = null;
let chatReady = false, chatLoading = false, chatSending = false, chatEpoch = 0, pendingChat = null;
const hiddenCurves = new Set();
let code = location.pathname.match(/^\/rooms\/([A-F0-9]{10})$/)?.[1] || null;
let invite = new URLSearchParams(location.hash.slice(1)).get("invite") || "";
let storage = {};
try {
  const saved = JSON.parse(localStorage.getItem(SEAT_STORAGE_KEY) || "{}");
  if (saved && typeof saved === "object" && !Array.isArray(saved)) storage = saved;
} catch (_) { /* Storage may be unavailable. */ }
let token = code ? storage[code]?.token || "" : "";

function saveStorage(removedCode=null, removedToken=null) {
  try {
    // Another open room may have saved a seat since this page loaded.
    let saved={};
    try {saved=JSON.parse(localStorage.getItem(SEAT_STORAGE_KEY) || "{}");}
    catch (_) { /* Replace malformed data while preserving any valid current seat. */ }
    const latest=saved && typeof saved==="object" && !Array.isArray(saved) ? saved : {};
    if(removedCode) {
      if(latest[removedCode]?.token===removedToken)delete latest[removedCode];
    } else if(code && storage[code]) {
      const seat=storage[code], previous=latest[code];
      latest[code]={...seat};
      if(previous?.token===seat.token && (previous.lastReadChat!=null || seat.lastReadChat!=null)) {
        latest[code].lastReadChat=Math.max(previous.lastReadChat || 0,seat.lastReadChat || 0);
      }
    }
    storage=latest;
    localStorage.setItem(SEAT_STORAGE_KEY, JSON.stringify(storage));
  }
  catch (_) { toast("浏览器无法保存座位，关闭页面后可能无法恢复。请允许本地存储。", 10000); }
}
function toast(message, duration = 4500) {
  $("toast").textContent = message;
  hide("toast", false);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => hide("toast", true), duration);
}
function connection(ok, text) {
  connectionOK = ok;
  $("connection").classList.toggle("offline", !ok);
  $("connection").lastElementChild.textContent = text || (ok ? "已连接 · 私人房间" : "连接中断 · 正在重连");
  updateEnabled();
}
async function request(path, data, auth = true) {
  const headers = {"Content-Type":"application/json"};
  if (auth && token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, {
    method: data === undefined ? "GET" : "POST", headers,
    body: data === undefined ? undefined : JSON.stringify(data),
    cache:"no-store", signal:AbortSignal.timeout(12000),
  });
  const result = await response.json().catch(() => ({error:"服务暂时不可用，请稍后重试。"}));
  if (!response.ok) {
    const error = new Error(result.error || "请求失败。");
    error.status = response.status;
    throw error;
  }
  return result;
}
function card(code, back = false) {
  if (!code) return `<span class="playing-card ${back ? "back" : "placeholder"}"></span>`;
  const suit = code.slice(-1), rank = code.slice(0,-1).replace("T","10");
  return `<span class="playing-card ${suit === "h" || suit === "d" ? "red" : ""}" aria-label="${esc(rank + SUITS[suit])}">${esc(rank)}<small>${SUITS[suit] || ""}</small></span>`;
}
function invitationURL() { return `${location.origin}/rooms/${code}#invite=${snapshot.invite}`; }
function myPlayer() { return snapshot?.players.find(p => p.id === snapshot.me); }

function renderSeats() {
  const me = myPlayer(), anchor = me?.seat ?? 0;
  const capacity = snapshot.settings.max_players;
  $("table").classList.toggle("large-table", capacity >= 7);
  $("table").classList.toggle("full-table", capacity >= 10);
  const lastActions = new Map(snapshot.actions.map(a => [a.seat, a.label]));
  $("seats").innerHTML = Array.from({length:capacity}, (_, seat) => {
    const p = snapshot.players.find(p => p.seat === seat);
    const angle = 2 * Math.PI * ((seat - anchor + capacity) % capacity) / capacity;
    const [x,y] = [50 - 38 * Math.sin(angle), 50 + 39 * Math.cos(angle)];
    if (!p) return `<div class="seat empty" style="left:${x}%;top:${y}%"><div class="seat-info"><div class="seat-name">空位 ${seat+1}</div><div class="seat-stack">＋</div><div class="seat-status">待入席</div></div></div>`;
    const mine = p.id === snapshot.me, turn = snapshot.phase === "playing" && snapshot.actor_seat === seat;
    const delta = p.delta === null ? "" : ` · ${signed(p.delta)}`;
    const status = p.status === "等待入局" ? "下手入局" : p.status;
    return `<div class="seat${mine ? " mine" : ""}${turn ? " turn" : ""}${p.status === "已弃牌" ? " folded" : ""}" style="left:${x}%;top:${y}%">
      ${p.bet ? `<span class="seat-bet">● ${num(p.bet)}</span>` : ""}
      ${snapshot.dealer === seat && snapshot.hand_number ? '<span class="dealer" title="庄家">D</span>' : ""}
      <div class="seat-info">${p.position ? `<div class="seat-position" title="${esc(POSITION_NAMES[p.position] || p.position)}">${esc(p.position)}</div>` : ""}<div class="seat-name"><i class="seat-online${p.online ? "" : " away"}"></i>${esc(p.name)}${mine ? ' <span class="you">你</span>' : ""}${p.id === snapshot.owner ? " ♛" : ""}</div>${p.is_agent ? '<div class="seat-agent">AGENT</div>' : ""}<div class="seat-stack">${num(p.stack)}</div><div class="seat-status">${esc(snapshot.phase === "lobby" ? "已入座" : lastActions.get(seat) || status)}${esc(delta)}</div>${turn && snapshot.turn_timer?.enabled ? '<div id="seatClock" class="seat-clock"><span id="seatTimerValue"></span><div class="countdown-track"><i></i></div></div>' : ""}</div>
      <div class="seat-cards">${snapshot.phase === "lobby" || p.status === "等待入局" ? "" : p.cards.length ? p.cards.map(c => card(c)).join("") : card(null,true)+card(null,true)}</div>
    </div>`;
  }).join("");
}
function renderLog() {
  const players = new Map(snapshot.players.map(p => [p.seat,p.name]));
  let previousStreet = -1;
  const labels = ["翻牌前","翻牌","转牌","河牌"];
  const html = snapshot.actions.map(a => {
    const street = a.street !== previousStreet ? `<div class="log-street">${labels[a.street]}</div>` : "";
    previousStreet = a.street;
    return `${street}<div class="log-row"><b>${esc(players.get(a.seat) || `座位 ${a.seat+1}`)}</b><span class="${a.kind === "raise" ? "log-raise" : ""}">${esc(a.label)}${a.agent ? " · Agent" : a.automatic ? " · 超时" : ""}</span></div>`;
  }).join("");
  $("actionLog").innerHTML = html || (snapshot.phase === "lobby" ? "分享邀请链接，或添加 Agent。由房主开局。" : "已发底牌，等待行动。");
  $("actionLog").scrollTop = $("actionLog").scrollHeight;
  $("history").innerHTML = snapshot.history.slice().reverse().map(h => `<details><summary>第 ${h.number} 手<span>查看结果</span></summary><p>${esc(h.board.map(c=>c.slice(0,-1).replace("T","10")+SUITS[c.slice(-1)]).join(" ")) || "未发公共牌"}</p>${h.results.map(p=>`<div class="result-row"><span>${esc(p.name)}</span><b class="${p.delta > 0 ? "positive" : p.delta < 0 ? "negative" : ""}">${signed(p.delta)}</b></div>`).join("")}</details>`).join("") || "完整打完一手后，结果会留在这里。";
}
function renderActions() {
  const me = myPlayer(), isHost = snapshot.me === snapshot.owner, active = snapshot.phase === "playing";
  hide("betActions", !snapshot.legal);
  $("actionPanel").classList.toggle("your-turn", Boolean(snapshot.legal));
  hide("startBtn", active || !isHost || snapshot.session_ended);
  $("startBtn").textContent = snapshot.hand_number ? "开始下一手 →" : "开始第一手 →";
  if (snapshot.legal) {
    const legal = snapshot.legal;
    $("callLabel").textContent = legal.call ? `${legal.call >= me.stack ? "全下跟注" : "跟注"} ${num(legal.call)}` : "过牌";
    const signature = `${snapshot.hand_number}:${snapshot.actions.length}:${snapshot.me}`;
    if (sizingTurn !== signature) {
      sizingTurn = signature;
      selectedRaise = legal.can_raise ? legal.min_raise : null;
      selectedPreset = "";
      allInCall = false;
      $("raiseAmount").value = selectedRaise ?? "";
    }
    $("halfPotAmount").textContent = legal.can_raise ? num(legal.half_pot_to) : "—";
    $("potAmount").textContent = legal.can_raise ? num(legal.pot_to) : "—";
    $("halfPotBtn").setAttribute("aria-label",legal.can_raise ? `半底池，加注至 ${num(legal.half_pot_to)}，直接下注` : "半底池，当前不可加注");
    $("potBtn").setAttribute("aria-label",legal.can_raise ? `满底池，加注至 ${num(legal.pot_to)}，直接下注` : "满底池，当前不可加注");
    $("allinKind").textContent = legal.all_in?.kind === "call" ? "跟注" : "加注至";
    $("allinAmount").textContent = legal.all_in ? num(legal.all_in.kind === "call" ? legal.call : legal.all_in.amount) : "—";
    renderSizing();
  }
  let message = "";
  if (snapshot.session_ended) message = "本场已结束，最终账单已保存。点击「赛后总结」查看或导出，下一场请创建新房间。";
  else if (snapshot.paused) message = "牌局已暂停，等待房主继续。";
  else if (snapshot.phase === "lobby") message = `已入座 ${snapshot.players.length} / ${snapshot.settings.max_players} 人。${isHost ? "至少两位玩家有筹码即可开局。" : "等待房主开局。"}`;
  else if (snapshot.phase === "finished") message = `${me.delta === null ? "本手已结束。" : `本手 ${signed(me.delta)} 筹码。`}${isHost ? "可开始下一手。" : "等待房主开始下一手。"} 可在此时补码或离桌。`;
  else if (me.status === "等待入局") message = "本手已经开始，你将在下一手加入。";
  else if (me.status === "已弃牌") message = "你已弃牌，等待本手结束。";
  else if (me.status === "全下") message = "你已全下，等待牌局结算。";
  else if (snapshot.legal) message = "轮到你了。";
  else message = "等待其他玩家行动。";
  $("tableMessage").textContent = message;
  hide("hostPanel", !isHost);
  hide("pauseBtn", !active);
  $("pauseBtn").textContent = snapshot.paused ? "继续牌局" : "暂停牌局";
  $("rebuyBtn").textContent = `补码 +${num(snapshot.settings.starting_stack)}`;
  $("settingsForm").querySelectorAll("input,select,button").forEach(el => { el.disabled = active || snapshot.session_ended; });
  updateEnabled();
}

function renderAgents() {
  const isHost=snapshot.me===snapshot.owner, agents=snapshot.players.filter(p=>p.is_agent);
  hide("agentPanel", !isHost && agents.length===0);
  hide("agentForm", !isHost);
  $("agentList").innerHTML=agents.map(p=>`<div class="agent-row"><div><b>${esc(p.name)}</b><small>座位 ${p.seat+1} · ${num(p.stack)} 筹码</small></div>${isHost ? `<div class="agent-controls"><button type="button" class="text-button" data-agent-command="rebuy_agent" data-agent-id="${esc(p.id)}" aria-label="给 ${esc(p.name)} 补码">补码</button><button type="button" class="text-button" data-agent-command="remove_agent" data-agent-id="${esc(p.id)}" aria-label="移除 ${esc(p.name)}">移除</button></div>` : '<span class="agent-badge">AGENT</span>'}</div>`).join("") || '<p class="micro">暂无 Agent，可添加至空座位。</p>';
  const available=snapshot.settings.max_players-snapshot.players.length, select=$("agentCount");
  if(select.options.length!==available) {
    const selected=Number(select.value)||1;
    select.innerHTML=Array.from({length:available},(_,i)=>`<option value="${i+1}">${i+1} 位</option>`).join("");
    select.value=Math.min(selected,available);
  }
  $("agentHint").textContent=snapshot.session_ended ? "本场已结束，Agent 账目已保留。" : snapshot.phase==="playing" ? "本手进行中；两手之间可添加、移除或补码。" : !isHost ? "由房主在两手之间管理 Agent。" : available ? `还有 ${available} 个空位。Agent 使用本桌买入，补码由房主操作。` : "座位已满，可移除 Agent 或调整人数。";
}

function renderSizing(forceAmount = false) {
  const legal = snapshot?.legal;
  if (!legal) return;
  hide("raiseForm", !legal.can_raise && !allInCall);
  hide("raiseSizing", !legal.can_raise);
  hide("amountField", !legal.can_raise);
  $("raiseAmount").required = legal.can_raise;
  if (legal.can_raise) {
    const input = $("raiseAmount"), slider = $("raiseSlider");
    input.min = slider.min = legal.min_raise;
    input.max = slider.max = legal.max_raise;
    selectedRaise = Math.max(legal.min_raise, Math.min(legal.max_raise, selectedRaise));
    slider.value = selectedRaise;
    slider.setAttribute("aria-valuetext", `本轮加注至 ${num(selectedRaise)}`);
    if (forceAmount || document.activeElement !== input) input.value = selectedRaise;
    $("selectedRaise").textContent = num(selectedRaise);
    $("raisePayment").textContent = `本次投入 ${num(selectedRaise - myPlayer().bet)} · 已下注 ${num(myPlayer().bet)}`;
    $("raiseMinimum").textContent = `最小 ${num(legal.min_raise)}`;
    $("raiseMaximum").textContent = `全下 ${num(legal.max_raise)}`;
    $("raiseLabel").textContent = selectedRaise === legal.max_raise ? `确认全下至 ${num(selectedRaise)}` : `加注至 ${num(selectedRaise)}`;
    $("raiseBtn").classList.toggle("all-in-confirm", selectedRaise === legal.max_raise);
    $("raiseHelp").textContent = "半池／满池点击即下注。自定义金额与 All-in 需确认。加注至为本轮总投入；底池比例按跟注后的底池计算。";
  } else {
    $("raiseLabel").textContent = `确认全下跟注 ${num(legal.call)}`;
    $("raiseBtn").classList.toggle("all-in-confirm", allInCall);
    $("raiseHelp").textContent = legal.all_in ? "筹码不足完整跟注时，可全下跟注。" : "当前未开放加注权，可跟注或弃牌。";
  }
  $("allinBtn").setAttribute("aria-pressed", String(selectedPreset === "allin"));
}

function selectPreset(preset) {
  const legal = snapshot?.legal;
  if (!legal || busy || !connectionOK || countdownState().expired) return;
  if (preset === "half" || preset === "pot") {
    if (!legal.can_raise) return;
    return send("act",{kind:"raise",amount:preset === "half" ? legal.half_pot_to : legal.pot_to});
  }
  if (preset === "allin" && legal.all_in) {
    allInCall = legal.all_in.kind === "call";
    selectedRaise = allInCall ? null : legal.all_in.amount;
  } else return;
  selectedPreset = preset;
  renderSizing(true);
  updateEnabled();
}

function confirmRaise() {
  const legal = snapshot?.legal;
  if (!legal || busy || !connectionOK || $("raiseBtn").disabled) return;
  if (allInCall && legal.all_in?.kind === "call") return send("act",{kind:"call"});
  if (!legal.can_raise || !$("raiseForm").reportValidity()) return;
  return send("act",{kind:"raise",amount:Number($("raiseAmount").value)});
}

function updateEnabled() {
  if (!snapshot) return;
  const active = snapshot.phase === "playing", blocked = busy || !connectionOK;
  const betBlocked=blocked || countdownState().expired;
  $("startBtn").disabled = blocked || snapshot.session_ended || snapshot.players.filter(p=>p.stack>0).length < 2;
  $("foldBtn").disabled = betBlocked || !snapshot.legal?.fold;
  $("callBtn").disabled = betBlocked || !snapshot.legal;
  const legal = snapshot.legal;
  $("halfPotBtn").disabled = $("potBtn").disabled = betBlocked || !legal?.can_raise;
  $("allinBtn").disabled = betBlocked || !legal?.all_in;
  $("raiseSlider").disabled = betBlocked || !legal?.can_raise;
  $("raiseBtn").disabled = betBlocked || !(allInCall && legal?.all_in?.kind === "call" || legal?.can_raise && $("raiseAmount").checkValidity());
  $("rebuyBtn").disabled = blocked || active || snapshot.session_ended;
  $("leaveBtn").disabled = blocked || active;
  $("renameBtn").disabled = blocked || snapshot.session_ended;
  $("endSessionBtn").disabled = $("confirmEnd").disabled = blocked || active || snapshot.session_ended;
  $("pauseBtn").disabled = blocked;
  const agentBlocked=blocked || active || snapshot.session_ended || snapshot.me!==snapshot.owner;
  $("agentCount").disabled=agentBlocked || snapshot.players.length>=snapshot.settings.max_players;
  $("addAgentBtn").disabled=$("agentCount").disabled;
  $("agentList").querySelectorAll("button").forEach(button=>{button.disabled=agentBlocked;});
  updateChatControls();
}
function render(next) {
  // Ignore a slower GET response that predates an accepted local command.
  if (snapshot && next.version < snapshot.version) return;
  const changed = !snapshot || next.version !== snapshot.version;
  const justEnded = next.session_ended && !snapshot?.session_ended;
  if (!snapshot) {
    countdownNoticeKey="";timeoutSyncKey="";
    chatEpoch++; chatMessages=[]; chatCursor=0; chatTarget=0;
    chatReady=false; chatLoading=false; chatSending=false; pendingChat=null;
    lastReadChat=storage[code]?.lastReadChat ?? null;
    $("chatMessages").innerHTML='<p class="chat-empty">暂无消息</p>';
  }
  snapshot = next;
  timerAnchor=next.turn_timer?.enabled ? {at:performance.now(),remaining:next.turn_timer.remaining || 0,paused:next.turn_timer.paused} : null;
  serverOffset = next.server_time * 1000 - Date.now();
  connection(true);
  hide("welcome",true); hide("room",false);
  const me = myPlayer();
  if (storage[code] && storage[code].name!==me.name) { storage[code].name = me.name; saveStorage(); }
  $("roomCode").textContent = `ROOM ${code}`;
  $("blindInfo").textContent = `无限注德州 · 盲注 ${num(next.settings.sb)} / ${num(next.settings.bb)}`;
  $("handInfo").textContent = `${next.hand_number ? `第 ${next.hand_number} 手 · ${next.street}` : `${next.settings.max_players} 人私人桌`}`;
  $("playerCount").textContent = `${next.players.length} / ${next.settings.max_players}`;
  $("potLabel").innerHTML = next.phase === "playing" ? `底池 <b>${num(next.pot)}</b>` : next.phase === "finished" ? "本 手 结 算" : "等待玩家入席";
  $("myName").textContent = me.name;
  $("myStack").textContent = num(me.stack);
  $("myBuyin").textContent = num(me.buyin);
  $("myNet").textContent = signed(me.stack-me.buyin);
  $("myNet").className = me.stack > me.buyin ? "positive" : me.stack < me.buyin ? "negative" : "";
  renderSeats();
  if (changed) {
    $("board").innerHTML = Array.from({length:5},(_,i)=>card(next.board[i])).join("");
    renderLog();
  }
  const settingsKey = JSON.stringify(next.settings);
  if (savedSettings !== settingsKey) {
    for (const [key,value] of Object.entries(next.settings)) $("settingsForm").elements[key].value = value;
    savedSettings = settingsKey;
  }
  if(changed)renderAgents();
  renderActions(); updateClock();
  $("summaryBtn").textContent = next.session_ended ? "赛后总结" : "整场统计";
  if (justEnded && !$("summaryDialog").open && !$("chatDialog").open) $("summaryDialog").showModal();
  if ($("summaryDialog").open && (changed || !sessionReport)) loadSummary();
  chatTarget=Math.max(chatTarget,next.chat_version || 0);
  if (!chatReady || chatTarget>chatCursor) loadChat();
}
function updateClock() {
  if (!snapshot) return;
  const actor = snapshot.players.find(p=>p.seat===snapshot.actor_seat);
  let text = snapshot.session_ended ? "本场已结束 · 总结已保存" : snapshot.paused ? "牌局暂停" : snapshot.phase === "playing" ? `${actor?.name || "玩家"} 行动中` : snapshot.phase === "finished" ? "等待下一手" : "等待房主开局";
  $("turnNote").textContent = text;
  updateCountdown();
  if ($("summaryDialog").open && sessionReport) updateSessionDuration();
  hide("chatTurnNotice",!snapshot.legal);
  if(snapshot.legal) $("chatTurnNotice").textContent=`轮到你行动了${snapshot.turn_timer?.enabled ? ` · 剩 ${countdownState().seconds} 秒` : ""} · 返回牌桌 →`;
}

function countdownState() {
  const timer=snapshot?.turn_timer;
  const enabled=Boolean(snapshot?.phase==="playing" && timer?.enabled && timerAnchor);
  const remaining=enabled ? Math.max(0,timerAnchor.remaining-(timerAnchor.paused ? 0 : (performance.now()-timerAnchor.at)/1000)) : 0;
  const seconds=Math.ceil(remaining), paused=Boolean(enabled && timerAnchor.paused);
  return {enabled,remaining,seconds,paused,expired:enabled && !paused && remaining<=0,
    ratio:enabled ? Math.max(0,Math.min(100,remaining/timer.duration*100)) : 0,
    label:`${String(Math.floor(seconds/60)).padStart(2,"0")}:${String(seconds%60).padStart(2,"0")}`};
}
function updateCountdown() {
  const active=snapshot.phase==="playing", timer=countdownState();
  $("timerRuleLabel").textContent=snapshot.settings.turn_seconds ? `行动限时 ${snapshot.settings.turn_seconds} 秒` : "行动不限时";
  hide("turnTimer",!active);
  const actor=snapshot.players.find(p=>p.seat===snapshot.actor_seat);
  const mine=actor?.id===snapshot.me;
  $("turnTimerActor").textContent=snapshot.paused ? `牌局暂停 · ${actor?.name || "玩家"}` : mine ? "轮到你行动" : `${actor?.name || "玩家"} 行动中`;
  $("turnTimerValue").textContent=timer.enabled ? timer.label : "不限时";
  $("turnTimerHint").textContent=!connectionOK ? "连接中断，以重连后的牌局为准" : snapshot.paused ? timer.enabled ? "剩余时间已保留，继续后接着倒数" : "牌局已暂停，继续后恢复行动" : timer.expired ? "时间到，正在同步服务器结果…" : timer.enabled ? `每次 ${snapshot.settings.turn_seconds} 秒 · 超时自动过牌或弃牌` : "房主可在两手之间开启行动计时";
  hide("turnTimerTrack",!timer.enabled);
  $("turnTimerTrack").setAttribute("aria-valuemax",String(snapshot.settings.turn_seconds));
  $("turnTimerTrack").setAttribute("aria-valuenow",String(timer.seconds));
  $("turnTimerTrack").setAttribute("aria-valuetext",timer.paused ? `已暂停，剩余 ${timer.seconds} 秒` : `剩余 ${timer.seconds} 秒`);
  hide("myTurnTimer",!active || !mine || !timer.enabled);
  $("myTimerLabel").textContent=timer.paused ? "已暂停 · 保留剩余时间" : timer.expired ? "时间已到 · 等待同步" : "你的行动倒计时";
  $("myTimerValue").textContent=timer.label;
  const seat=$("seatClock");
  if(seat)$("seatTimerValue").textContent=timer.paused ? `暂停 ${timer.label}` : `⏱ ${timer.label}`;
  for(const element of [$("turnTimer"),$("myTurnTimer"),seat].filter(Boolean)) {
    element.style.setProperty("--time-left",`${timer.ratio}%`);
    element.classList.toggle("paused",snapshot.paused);
    element.classList.toggle("warning",timer.enabled && !timer.paused && timer.seconds<=10);
    element.classList.toggle("urgent",timer.enabled && !timer.paused && timer.seconds<=5);
  }
  const key=`${snapshot.hand_number}:${snapshot.actions.length}:${snapshot.actor_seat}`;
  if(!active || !timer.enabled || timer.paused || !mine || timer.seconds===0 || timer.seconds>10) {
    if($("timerAnnouncement").textContent)$("timerAnnouncement").textContent="";
  }
  if(timer.enabled && !timer.paused && mine && timer.seconds>0 && timer.seconds<=10 && countdownNoticeKey!==key) {
    countdownNoticeKey=key;
    $("timerAnnouncement").textContent=`你的行动时间还剩 ${timer.seconds} 秒，超时自动过牌或弃牌。`;
  }
  updateEnabled();
  if(timer.expired && connectionOK && timeoutSyncKey!==key) {timeoutSyncKey=key;poll();}
}

function updateChatControls() {
  const count=Array.from($("chatInput").value.trim()).length;
  $("chatCharCount").textContent=`${count} / 300`;
  $("chatCharCount").classList.toggle("negative",count>300);
  $("chatSendBtn").disabled=!snapshot || !connectionOK || chatSending || !count || count>300;
  $("chatSendBtn").textContent=chatSending ? "发送中…" : "发送";
}
function updateChatBadge() {
  const unread=lastReadChat == null ? 0 : chatMessages.filter(m=>m.id>lastReadChat && m.player_id!==snapshot?.me).length;
  for (const id of ["chatBtn","actionChatBtn"]) {
    const badge=$(id).querySelector(".chat-unread");
    badge.textContent=unread>99 ? "99+" : unread;
    badge.classList.toggle("hidden",unread===0);
    $(id).setAttribute("aria-label",unread ? `房间聊天，${unread} 条未读消息` : "房间聊天");
  }
}
function markChatRead() {
  if (lastReadChat==null || chatCursor>lastReadChat) {
    lastReadChat=chatCursor;
    if(storage[code]) {storage[code].lastReadChat=lastReadChat;saveStorage();}
  }
  updateChatBadge();
}
function chatAtBottom() {
  const log=$("chatMessages");
  return log.scrollHeight-log.scrollTop-log.clientHeight<45;
}
function scrollChatToBottom() {
  const log=$("chatMessages");
  log.scrollTop=log.scrollHeight;
  hide("chatLatestBtn",true); markChatRead();
}
function renderChat(forceBottom=false) {
  const log=$("chatMessages"), atBottom=chatAtBottom(), top=log.getBoundingClientRect().top;
  const anchor=Array.from(log.querySelectorAll(".chat-message")).find(el=>el.getBoundingClientRect().bottom>top);
  const anchorOffset=anchor ? anchor.getBoundingClientRect().top-top : 0;
  const allowed=new Set(chatMessages.map(m=>String(m.id)));
  log.querySelectorAll(".chat-message").forEach(el=>{if(!allowed.has(el.dataset.messageId))el.remove();});
  if(chatMessages.length)log.querySelector(".chat-empty")?.remove();
  let added=false;
  for (const m of chatMessages) {
    if(log.querySelector(`[data-message-id="${m.id}"]`))continue;
    added=true;
    const mine=m.player_id===snapshot.me;
    const date=new Date(m.created_at*1000);
    const time=date.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
    const html=`<article class="chat-message${mine ? " mine" : ""}" data-message-id="${m.id}"><div class="chat-meta"><b>${esc(m.name)}${mine ? " · 你" : ""}</b><time datetime="${date.toISOString()}">${esc(time)}</time></div><div class="chat-bubble">${esc(m.text)}</div></article>`;
    const next=Array.from(log.querySelectorAll(".chat-message")).find(el=>Number(el.dataset.messageId)>m.id);
    if(next)next.insertAdjacentHTML("beforebegin",html);else log.insertAdjacentHTML("beforeend",html);
  }
  if($("chatDialog").open && (forceBottom || atBottom))scrollChatToBottom();
  else if(anchor?.isConnected)log.scrollTop+=anchor.getBoundingClientRect().top-top-anchorOffset;
  if(added && $("chatDialog").open && !forceBottom && !atBottom)hide("chatLatestBtn",false);
  updateChatBadge();
}
async function loadChat() {
  if(chatLoading || !snapshot)return;
  const epoch=chatEpoch;
  chatLoading=true;
  let loaded=false;
  try {
    const result=await request(`/api/rooms/${code}/chat?after=${chatCursor}`);
    if(epoch!==chatEpoch || !snapshot)return;
    const messages=new Map(chatMessages.map(m=>[m.id,m]));
    for(const message of result.messages)messages.set(message.id,message);
    chatMessages=Array.from(messages.values()).filter(m=>m.id>=result.oldest_id).sort((a,b)=>a.id-b.id).slice(-200);
    chatCursor=Math.max(chatCursor,result.version);
    if(lastReadChat==null)lastReadChat=chatCursor;
    chatReady=true;loaded=true;
    renderChat();
    if(!chatSending && !pendingChat)$("chatStatus").textContent="";
  } catch(error) {
    if(epoch===chatEpoch)$("chatStatus").textContent=`聊天同步失败，正在等待重连。${error.message}`;
  } finally {
    if(epoch===chatEpoch) {
      chatLoading=false;
      if(loaded && chatTarget>chatCursor)loadChat();
    }
  }
}
function openChat() {
  $("chatDialog").showModal();
  scrollChatToBottom();loadChat();
  // Mobile keyboards open when the player chooses the input, keeping history visible.
  if(window.matchMedia("(min-width:761px)").matches)$("chatInput").focus({preventScroll:true});
}
function chatMessageId() {
  const bytes=new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes,b=>b.toString(16).padStart(2,"0")).join("");
}
async function sendChat() {
  if($("chatSendBtn").disabled || !snapshot)return;
  const input=$("chatInput"), text=input.value.trim(), epoch=chatEpoch;
  if(!pendingChat || pendingChat.text!==text)pendingChat={text,client_id:chatMessageId()};
  const payload=pendingChat;
  chatSending=true;updateChatControls();$("chatStatus").textContent="";
  try {
    const result=await request(`/api/rooms/${code}/chat`,payload);
    if(epoch!==chatEpoch || !snapshot)return;
    pendingChat=null;
    if(input.value.trim()===text)input.value="";
    if(!chatMessages.some(m=>m.id===result.message.id))chatMessages.push(result.message);
    chatMessages=chatMessages.sort((a,b)=>a.id-b.id).slice(-200);
    chatTarget=Math.max(chatTarget,result.version);
    renderChat(true);loadChat();
  } catch(error) {
    if(epoch===chatEpoch)$("chatStatus").textContent=`未确认发送：${error.message} 内容已保留，可再点发送重试。`;
  } finally {
    if(epoch===chatEpoch) {chatSending=false;updateChatControls();}
  }
}

const REPORT_COLORS = ["#215644", "#b5463b", "#386da6", "#9a6b20", "#8b55a0", "#087e88", "#ad5e83", "#657b24", "#b66127", "#5057b0", "#687b8a"];
const reportDate = timestamp => timestamp == null ? "未开局" : new Date(timestamp * 1000).toLocaleString("zh-CN", {hour12:false});
function updateSessionDuration() {
  const start = sessionReport.started_at;
  const seconds = start == null ? 0 : Math.max(0, Math.floor((sessionReport.ended_at ?? (Date.now()+serverOffset)/1000) - start));
  $("sessionDuration").textContent = `${Math.floor(seconds/3600)}:${String(Math.floor(seconds%3600/60)).padStart(2,"0")}:${String(seconds%60).padStart(2,"0")}`;
}
async function loadSummary() {
  if (reportBusy || !snapshot) return;
  reportBusy = true;
  let loaded = false;
  try {
    const result = await request(`/api/rooms/${code}/summary`);
    if (!sessionReport || result.version >= sessionReport.version) {
      sessionReport = result;
      renderSummary();
      loaded = true;
    }
  } catch (error) { $("summaryStatus").textContent = `读取失败：${error.message} 关闭后重新打开可重试。`; }
  finally {
    reportBusy = false;
    if (loaded && $("summaryDialog").open && snapshot.version > sessionReport.version) loadSummary();
  }
}
function renderSummary() {
  const r = sessionReport, ended = r.ended_at != null;
  hide("summaryContent", false);
  $("summaryTitle").textContent = ended ? "夜局 · 赛后总结" : "夜局 · 整场统计";
  $("summaryStatus").textContent = `${ended ? "最终账单 · 已固定" : "进行中 · 已结算快照"}${r.active ? " · 本手尚未计入" : ""}${r.legacy ? `。旧房间历史不完整：手数、获胜、底池及曲线变化从第 ${r.first_hand} 手起统计；盈亏沿用已有买入账目，时长从统计启用时起算。` : ""}`;
  $("sessionHands").textContent = `${num(r.completed_hands)} 手`;
  $("sessionMaxPot").textContent = r.largest_pot ? num(r.largest_pot.pot) : "—";
  $("sessionBuyin").textContent = num(r.total_buyin);
  $("sessionTimes").textContent = `${r.legacy ? "统计起算" : "开始"}：${reportDate(r.started_at)}${ended ? `　结束：${reportDate(r.ended_at)}` : "　从第一手起计时，含暂停"}`;
  $("rankingTitle").textContent = ended ? "全桌最终盈亏榜" : "全桌当前盈亏榜";
  $("rankingRows").innerHTML = r.players.map((p,i)=>`<tr><th scope="row"><span class="rank-number">${i+1}</span>${esc(p.name)}${p.id === snapshot.me ? ' <small>你</small>' : ""}<small>#${p.entry} · ${p.is_agent ? "Agent · " : ""}${esc(p.status)}</small></th><td class="${p.net>0 ? "positive" : p.net<0 ? "negative" : ""}">${signed(p.net)}</td><td>${num(p.buyin)}</td><td>${num(p.left_at != null ? p.cashout : p.stack)}<small>${p.left_at != null ? "离桌带出" : "桌上筹码"}</small></td><td>${num(p.hands_played)}</td><td>${num(p.hands_won)}</td></tr>`).join("");
  $("accountingCheck").textContent = `买入 ${num(r.total_buyin)} · 桌上及离桌带出 ${num(r.total_chips)} · 全桌净盈亏 ${signed(r.net_total)}${r.net_total === 0 ? " · 账目平衡" : r.legacy ? " · 旧记录可能缺少此前离桌玩家" : " · 请核对账目"}`;
  const largest = r.largest_pot;
  $("largestPotDetail").innerHTML = largest ? `<div class="largest-pot-title">第 ${largest.number} 手 <b>${num(largest.pot)} 筹码</b></div><div class="report-board">${largest.board.map(c=>card(c)).join("") || '<span class="muted">未发公共牌</span>'}</div><p>${largest.results.map(p=>`${esc(p.name)} <b class="${p.delta>0 ? "positive" : p.delta<0 ? "negative" : ""}">${signed(p.delta)}</b>`).join("　")}</p>` : '<p class="muted">完成第一手后，这里会记录最大底池。</p>';
  hide("endSessionBtn", snapshot.me !== snapshot.owner || ended);
  $("endSessionHint").textContent = ended ? "账目已固定，可随时导出。开始下一场请新建房间。" : snapshot.me !== snapshot.owner ? "由房主在两手之间结束本场，生成最终账单。" : r.active ? "本手结束后才能结束本场。" : "结束本场后，最终账单固定，不能继续开牌或补码。";
  updateSessionDuration(); renderProfitChart(); updateEnabled();
}
function renderProfitChart() {
  const r = sessionReport;
  const first = r.first_hand - 1, last = r.hands.at(-1)?.number ?? first;
  const slider = $("chartHand");
  slider.min = first; slider.max = last; slider.disabled = last === first;
  if (chartSelection != null) chartSelection = Math.max(first, Math.min(last, chartSelection));
  const selected = chartSelection ?? last;
  slider.value = selected;
  $("chartHandLabel").textContent = selected === first ? "统计起点" : `第 ${selected} 手后`;
  slider.setAttribute("aria-valuetext", $("chartHandLabel").textContent);
  const series = r.players.map(p=>{
    const points = [[Math.max(first,p.joined_after_hand),p.baseline_net]];
    for (const h of r.hands) {
      const result = h.results.find(result=>result.id===p.id);
      if (result) points.push([h.number,result.net]);
    }
    if (points.at(-1)[0] < last) points.push([last,points.at(-1)[1]]);
    const value = selected < points[0][0] ? null : points.filter(([x])=>x<=selected).at(-1)?.[1] ?? p.baseline_net;
    return {player:p, points, value, color:REPORT_COLORS[(p.entry-1)%REPORT_COLORS.length]};
  });
  const visible = series.filter(s=>!hiddenCurves.has(s.player.id));
  let low = 0, high = 0;
  for (const s of visible) for (const [,value] of s.points) {low=Math.min(low,value);high=Math.max(high,value);}
  const padding = Math.max(5,(high-low)*.12);
  const min = low-padding, max = high+padding;
  const x = n=>64+(n-first)/Math.max(1,last-first)*606;
  const y = n=>20+(max-n)/(max-min)*200;
  let svg = '<svg viewBox="0 0 700 260" role="img" aria-label="各玩家已结算的累计净盈亏，横轴为手数，纵轴为筹码"><title>已结算累计盈亏曲线，补码不计盈利</title>';
  const axisNumber = n=>Math.abs(n)>=1e8 ? `${Number((n/1e8).toFixed(1))}亿` : Math.abs(n)>=1e4 ? `${Number((n/1e4).toFixed(1))}万` : num(n);
  for (const amount of [high,0,low].filter((v,i,a)=>a.indexOf(v)===i)) svg += `<line x1="64" x2="670" y1="${y(amount)}" y2="${y(amount)}" stroke="#dce2d5" ${amount===0 ? 'stroke-dasharray="5 4"' : ""}/><text x="54" y="${y(amount)+4}" text-anchor="end">${axisNumber(amount)}</text>`;
  svg += `<text x="64" y="249">${first ? `第 ${first} 手后` : "开局"}</text><text x="670" y="249" text-anchor="end">${last>first ? `第 ${last} 手` : "等待结算"}</text>`;
  for (const s of visible) {
    svg += `<polyline points="${s.points.map(([a,b])=>`${x(a)},${y(b)}`).join(" ")}" fill="none" stroke="${s.color}" stroke-width="2.5" stroke-linejoin="round"><title>${esc(s.player.name)} #${s.player.entry}</title></polyline>`;
    if (s.value != null) svg += `<circle cx="${x(selected)}" cy="${y(s.value)}" r="4" fill="${s.color}"/>`;
  }
  svg += `<line x1="${x(selected)}" x2="${x(selected)}" y1="20" y2="220" stroke="#7a837b" stroke-dasharray="3 5"/></svg>`;
  $("profitChart").innerHTML = svg;
  $("chartLegend").innerHTML = series.map(s=>`<button type="button" data-curve="${s.player.id}" aria-pressed="${!hiddenCurves.has(s.player.id)}" style="--curve-color:${s.color}"><i></i><span>${esc(s.player.name)} <small>#${s.player.entry}</small></span><b>${s.value == null ? "未入座" : signed(s.value)}</b></button>`).join("");
}
async function exportBill(kind) {
  const button = $(kind === "summary" ? "exportSummary" : "exportHands");
  button.disabled = true;
  try {
    const response = await fetch(`/api/rooms/${code}/export/${kind}`, {headers:{Authorization:`Bearer ${token}`},cache:"no-store",signal:AbortSignal.timeout(20000)});
    if (!response.ok) throw new Error((await response.json()).error || "导出失败");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href=url; link.download=`After-Hours-${code}-${kind === "summary" ? "总账" : "逐手账单"}.csv`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),60000);
    toast("账单已生成，手机可在下载列表中查看或分享。");
  } catch (error) { toast(error.message); }
  finally { button.disabled=false; }
}
async function poll() {
  if (!code || !token || polling || busy) return;
  polling = true;
  try { render(await request(`/api/rooms/${code}`)); }
  catch (error) {
    connection(false);
    if (error.status === 401 || error.status === 404) {
      clearInterval(pollTimer);
      toast(error.message,10000);
      hide("room",true); hide("welcome",false);
      token = ""; snapshot = null;
      chatEpoch++;chatLoading=false;chatSending=false;pendingChat=null;
      if($("chatDialog").open)$("chatDialog").close();
      $("chatInput").value="";updateChatControls();
      saveStorage(code,storage[code]?.token); setupWelcome();
    }
  } finally { polling = false; }
}
function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(poll,1100);
}
async function send(command, data={}) {
  if (busy) return;
  if(command==="act" && countdownState().expired) {toast("行动时间已到，正在同步牌局。");poll();return;}
  busy = true; updateEnabled();
  try {
    const result = await request(`/api/rooms/${code}/${command}`, {...data,version:snapshot.version});
    if (result.left) {
      saveStorage(code,token);
      location.href = "/friends";
    } else render(result);
    return result;
  } catch (error) { toast(error.message); }
  finally { busy = false; updateEnabled(); poll(); }
}
function setupWelcome() {
  if (code) {
    hide("sizeField",true);
    hide("timerField",true);
    $("entryEyebrow").textContent = "INVITATION";
    $("entryTitle").textContent = "加入牌桌";
    $("entryDescription").textContent = invite ? "输入昵称后入席。" : "请使用房主提供的完整邀请链接。";
    $("entrySubmit").textContent = "加入牌桌 ↗";
    $("entrySubmit").disabled = !invite;
  }
  $("savedRooms").innerHTML = Object.entries(storage).filter(([key])=>key!==code).map(([key,value])=>`<a href="/rooms/${encodeURIComponent(key)}">回到 ${esc(value.name)} 的座位 · ${esc(key)} →</a>`).join("");
}
$("entryForm").addEventListener("submit",async event=>{
  event.preventDefault(); if (busy) return;
  busy=true; $("entrySubmit").disabled=true;
  try {
    const name=$("nickname").value.trim();
    const result=await request(code ? `/api/rooms/${code}/join` : "/api/rooms", code ? {name,invite} : {name,max_players:Number($("tableSize").value),turn_seconds:Number($("entryTurnSeconds").value)},false);
    code=result.room.code; token=result.token; invite=result.room.invite;
    storage[code]={token,name}; saveStorage();
    history.replaceState({},"",`/rooms/${code}#invite=${invite}`);
    render(result.room); startPolling();
  } catch (error) { toast(error.message); }
  finally { busy=false; $("entrySubmit").disabled=false; updateEnabled(); }
});
$("inviteBtn").addEventListener("click",async()=>{
  try { await navigator.clipboard.writeText(invitationURL()); toast("邀请链接已复制。"); }
  catch (_) { $("inviteLink").value=invitationURL(); $("inviteDialog").showModal(); $("inviteLink").select(); }
});
$("closeInvite").addEventListener("click",()=>$("inviteDialog").close());
$("chatBtn").addEventListener("click",openChat);
$("actionChatBtn").addEventListener("click",openChat);
$("closeChat").addEventListener("click",()=>$("chatDialog").close());
$("chatTurnNotice").addEventListener("click",()=>$("chatDialog").close());
$("chatLatestBtn").addEventListener("click",scrollChatToBottom);
$("chatMessages").addEventListener("scroll",()=>{if($("chatDialog").open && chatAtBottom()){hide("chatLatestBtn",true);markChatRead();}});
$("chatInput").addEventListener("input",updateChatControls);
$("chatInput").addEventListener("keydown",event=>{
  if(event.key==="Enter" && !event.shiftKey && !event.isComposing && event.keyCode!==229) {
    event.preventDefault();$("chatForm").requestSubmit();
  }
});
$("chatForm").addEventListener("submit",event=>{event.preventDefault();sendChat();});
$("summaryBtn").addEventListener("click",()=>{$("summaryDialog").showModal();loadSummary();});
$("closeSummary").addEventListener("click",()=>$("summaryDialog").close());
$("chartHand").addEventListener("input",()=>{chartSelection=Number($("chartHand").value);renderProfitChart();});
$("chartLegend").addEventListener("click",event=>{
  const button=event.target.closest("[data-curve]");
  if (!button) return;
  const id=button.dataset.curve;
  if(hiddenCurves.has(id))hiddenCurves.delete(id);else hiddenCurves.add(id);
  renderProfitChart();
  $("chartLegend").querySelector(`[data-curve="${id}"]`)?.focus({preventScroll:true});
});
$("exportSummary").addEventListener("click",()=>exportBill("summary"));
$("exportHands").addEventListener("click",()=>exportBill("hands"));
$("endSessionBtn").addEventListener("click",()=>$("endDialog").showModal());
$("cancelEnd").addEventListener("click",()=>$("endDialog").close());
$("confirmEnd").addEventListener("click",async()=>{if(await send("end")){$("endDialog").close();loadSummary();}});
$("startBtn").addEventListener("click",()=>send("start"));
$("foldBtn").addEventListener("click",()=>send("act",{kind:"fold"}));
$("callBtn").addEventListener("click",()=>send("act",{kind:"call"}));
$("halfPotBtn").addEventListener("click",()=>selectPreset("half"));
$("potBtn").addEventListener("click",()=>selectPreset("pot"));
$("allinBtn").addEventListener("click",()=>selectPreset("allin"));
$("raiseForm").addEventListener("submit",event=>{event.preventDefault();confirmRaise();});
$("raiseSlider").addEventListener("input",()=>{
  selectedRaise = Number($("raiseSlider").value);
  allInCall = false; selectedPreset = "";
  renderSizing(true); updateEnabled();
});
$("raiseAmount").addEventListener("input",()=>{
  if ($("raiseAmount").checkValidity()) {
    selectedRaise = Number($("raiseAmount").value);
    allInCall = false; selectedPreset = "";
    renderSizing();
  }
  updateEnabled();
});
document.addEventListener("keydown",event=>{
  if (event.repeat || event.isComposing || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey || document.hidden || !snapshot?.legal || busy || !connectionOK || document.querySelector("dialog[open]")) return;
  const target = event.target;
  if (target instanceof Element && target.closest("input,select,textarea,a,summary,[contenteditable]")) return;
  const actions = {
    f:()=>{if (!$("foldBtn").disabled)$("foldBtn").click();},
    c:()=>{if (!$("callBtn").disabled)$("callBtn").click();},
    "1":()=>selectPreset("half"), "2":()=>selectPreset("pot"),
    a:()=>selectPreset("allin"), r:confirmRaise,
  };
  const action = actions[event.key.toLowerCase()];
  if (action) {event.preventDefault();action();}
});
$("pauseBtn").addEventListener("click",()=>send("pause"));
$("agentForm").addEventListener("submit",event=>{event.preventDefault();send("add_agent",{count:Number($("agentCount").value)});});
$("agentList").addEventListener("click",event=>{
  const button=event.target.closest("[data-agent-command]");
  if(button && !button.disabled)send(button.dataset.agentCommand,{agent_id:button.dataset.agentId});
});
$("rebuyBtn").addEventListener("click",()=>send("rebuy"));
$("leaveBtn").addEventListener("click",()=>send("leave"));
$("renameBtn").addEventListener("click",()=>{$("newName").value=myPlayer().name;$("nameDialog").showModal();});
$("cancelRename").addEventListener("click",()=>$("nameDialog").close());
$("renameForm").addEventListener("submit",async event=>{event.preventDefault();if(await send("rename",{name:$("newName").value.trim()}))$("nameDialog").close();});
$("settingsForm").addEventListener("submit",async event=>{
  event.preventDefault();
  const settings=Object.fromEntries([...new FormData(event.currentTarget)].map(([key,value])=>[key,Number(value)]));
  if(await send("settings",settings))toast("牌桌规则已保存。");
});
document.addEventListener("visibilitychange",()=>{if(!document.hidden)poll();});
window.addEventListener("online",poll);
window.addEventListener("offline",()=>connection(false));
setInterval(updateClock,250);
setupWelcome();
if(code&&token){connection(false,"正在恢复你的座位…");poll();startPolling();}
