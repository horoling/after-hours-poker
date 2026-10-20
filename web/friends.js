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
let snapshot = null, busy = false, polling = false, pollTimer, toastTimer;
let sizingTurn = "", selectedRaise = null, selectedPreset = "", allInCall = false;
let serverOffset = 0, savedSettings = "", connectionOK = true;
let code = location.pathname.match(/^\/rooms\/([A-F0-9]{10})$/)?.[1] || null;
let invite = new URLSearchParams(location.hash.slice(1)).get("invite") || "";
let storage = {};
try {
  const saved = JSON.parse(localStorage.getItem("tongzhuo.rooms") || "{}");
  if (saved && typeof saved === "object" && !Array.isArray(saved)) storage = saved;
} catch (_) { /* Storage may be unavailable. */ }
let token = code ? storage[code]?.token || "" : "";

function saveStorage() {
  try { localStorage.setItem("tongzhuo.rooms", JSON.stringify(storage)); }
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
    if (!p) return `<div class="seat empty" style="left:${x}%;top:${y}%"><div class="seat-info"><div class="seat-name">空位 ${seat+1}</div><div class="seat-stack">＋</div><div class="seat-status">等待朋友</div></div></div>`;
    const mine = p.id === snapshot.me, turn = snapshot.phase === "playing" && snapshot.actor_seat === seat;
    const delta = p.delta === null ? "" : ` · ${signed(p.delta)}`;
    const status = p.status === "等待入局" ? "下手入局" : p.status;
    return `<div class="seat${mine ? " mine" : ""}${turn ? " turn" : ""}${p.status === "已弃牌" ? " folded" : ""}" style="left:${x}%;top:${y}%">
      ${p.bet ? `<span class="seat-bet">● ${num(p.bet)}</span>` : ""}
      ${snapshot.dealer === seat && snapshot.hand_number ? '<span class="dealer" title="庄家">D</span>' : ""}
      <div class="seat-info">${p.position ? `<div class="seat-position" title="${esc(POSITION_NAMES[p.position] || p.position)}">${esc(p.position)}</div>` : ""}<div class="seat-name"><i class="seat-online${p.online ? "" : " away"}"></i>${esc(p.name)}${mine ? ' <span class="you">你</span>' : ""}${p.id === snapshot.owner ? " ♛" : ""}</div><div class="seat-stack">${num(p.stack)}</div><div class="seat-status">${esc(snapshot.phase === "lobby" ? "已入座" : lastActions.get(seat) || status)}${esc(delta)}</div></div>
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
    return `${street}<div class="log-row"><b>${esc(players.get(a.seat) || `座位 ${a.seat+1}`)}</b><span>${esc(a.label)}${a.automatic ? " · 超时" : ""}</span></div>`;
  }).join("");
  $("actionLog").innerHTML = html || (snapshot.phase === "lobby" ? "把链接发给朋友，入座后由房主开局。" : "底牌已发出，等待第一位玩家行动。");
  $("actionLog").scrollTop = $("actionLog").scrollHeight;
  $("history").innerHTML = snapshot.history.slice().reverse().map(h => `<details><summary>第 ${h.number} 手<span>查看结果</span></summary><p>${esc(h.board.map(c=>c.slice(0,-1).replace("T","10")+SUITS[c.slice(-1)]).join(" ")) || "未发公共牌"}</p>${h.results.map(p=>`<div class="result-row"><span>${esc(p.name)}</span><b class="${p.delta > 0 ? "positive" : p.delta < 0 ? "negative" : ""}">${signed(p.delta)}</b></div>`).join("")}</details>`).join("") || "完整打完一手后，结果会留在这里。";
}
function renderActions() {
  const me = myPlayer(), isHost = snapshot.me === snapshot.owner, active = snapshot.phase === "playing";
  hide("betActions", !snapshot.legal);
  $("actionPanel").classList.toggle("your-turn", Boolean(snapshot.legal));
  hide("startBtn", active || !isHost);
  $("startBtn").textContent = snapshot.hand_number ? "开始下一手 →" : "朋友入座了，开始第一手 →";
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
    $("halfPotAmount").textContent = legal.can_raise ? `至 ${num(legal.half_pot_to)}` : "当前不可加注";
    $("potAmount").textContent = legal.can_raise ? `至 ${num(legal.pot_to)}` : "当前不可加注";
    $("allinAmount").textContent = legal.all_in ? legal.all_in.kind === "call" ? `跟注 ${num(legal.call)}` : `至 ${num(legal.all_in.amount)}` : "当前不可全下";
    renderSizing();
  }
  let message = "";
  if (snapshot.paused) message = "牌局已暂停，等待房主继续。";
  else if (snapshot.phase === "lobby") message = `已入座 ${snapshot.players.length} / ${snapshot.settings.max_players} 人。${isHost ? "至少两人有筹码即可开局，也可以等朋友到齐。" : "等待房主开局。"}`;
  else if (snapshot.phase === "finished") message = `${me.delta === null ? "本手已结束。" : `本手 ${signed(me.delta)} 筹码。`}${isHost ? "准备好后开始下一手。" : "等待房主开始下一手。"} 可在此时补码或离桌。`;
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
  $("settingsForm").querySelectorAll("input,select,button").forEach(el => { el.disabled = active; });
  updateEnabled();
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
    $("raiseMinimum").textContent = `最小 ${num(legal.min_raise)}`;
    $("raiseMaximum").textContent = `全下 ${num(legal.max_raise)}`;
    $("raiseLabel").textContent = selectedRaise === legal.max_raise ? `确认全下至 ${num(selectedRaise)}` : `加注至 ${num(selectedRaise)}`;
    $("raiseBtn").classList.toggle("all-in-confirm", selectedRaise === legal.max_raise);
    $("raiseHelp").textContent = "金额包含本轮已投入筹码。按钮和滑杆只选金额，确认后下注；½ 底池按跟注后的底池计算，筹码取整并遵守最小加注。";
  } else {
    $("raiseLabel").textContent = `确认全下跟注 ${num(legal.call)}`;
    $("raiseBtn").classList.toggle("all-in-confirm", allInCall);
    $("raiseHelp").textContent = legal.all_in ? "筹码不足完整跟注时，可全下跟注。" : "当前未开放加注权，可跟注或弃牌。";
  }
  for (const [id, preset] of [["halfPotBtn","half"],["potBtn","pot"],["allinBtn","allin"]]) {
    $(id).setAttribute("aria-pressed", String(selectedPreset === preset));
  }
}

function selectPreset(preset) {
  const legal = snapshot?.legal;
  if (!legal || busy || !connectionOK) return;
  if (preset === "allin" && legal.all_in) {
    allInCall = legal.all_in.kind === "call";
    selectedRaise = allInCall ? null : legal.all_in.amount;
  } else if (legal.can_raise && (preset === "half" || preset === "pot")) {
    allInCall = false;
    selectedRaise = preset === "half" ? legal.half_pot_to : legal.pot_to;
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
  $("startBtn").disabled = blocked || snapshot.players.filter(p=>p.stack>0).length < 2;
  $("foldBtn").disabled = blocked || !snapshot.legal?.fold;
  $("callBtn").disabled = blocked || !snapshot.legal;
  const legal = snapshot.legal;
  $("halfPotBtn").disabled = $("potBtn").disabled = blocked || !legal?.can_raise;
  $("allinBtn").disabled = blocked || !legal?.all_in;
  $("raiseSlider").disabled = blocked || !legal?.can_raise;
  $("raiseBtn").disabled = blocked || !(allInCall && legal?.all_in?.kind === "call" || legal?.can_raise && $("raiseAmount").checkValidity());
  $("rebuyBtn").disabled = $("leaveBtn").disabled = blocked || active;
  $("pauseBtn").disabled = blocked;
}
function render(next) {
  // Ignore a slower GET response that predates an accepted local command.
  if (snapshot && next.version < snapshot.version) return;
  const changed = !snapshot || next.version !== snapshot.version;
  snapshot = next;
  serverOffset = next.server_time * 1000 - Date.now();
  connection(true);
  hide("welcome",true); hide("room",false);
  const me = myPlayer();
  if (storage[code]) { storage[code].name = me.name; saveStorage(); }
  $("roomCode").textContent = `ROOM ${code}`;
  $("blindInfo").textContent = `无限注德州 · 盲注 ${num(next.settings.sb)} / ${num(next.settings.bb)}`;
  $("handInfo").textContent = `${next.hand_number ? `第 ${next.hand_number} 手 · ${next.street}` : `${next.settings.max_players} 人私人桌`}`;
  $("playerCount").textContent = `${next.players.length} / ${next.settings.max_players}`;
  $("potLabel").innerHTML = next.phase === "playing" ? `底池 <b>${num(next.pot)}</b>` : next.phase === "finished" ? "本 手 结 算" : "等待朋友入座";
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
  renderActions(); updateClock();
}
function updateClock() {
  if (!snapshot) return;
  const actor = snapshot.players.find(p=>p.seat===snapshot.actor_seat);
  let text = snapshot.paused ? "牌局暂停" : snapshot.phase === "playing" ? `${actor?.name || "玩家"} 行动中` : snapshot.phase === "finished" ? "下一手，继续同桌" : "朋友入座后由房主开局";
  if (snapshot.phase === "playing" && !snapshot.paused && snapshot.deadline) text += ` · ${Math.max(0,Math.ceil(snapshot.deadline-(Date.now()+serverOffset)/1000))}s`;
  $("turnNote").textContent = text;
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
      delete storage[code]; saveStorage(); setupWelcome();
    }
  } finally { polling = false; }
}
function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(poll,1100);
}
async function send(command, data={}) {
  if (busy) return;
  busy = true; updateEnabled();
  try {
    const result = await request(`/api/rooms/${code}/${command}`, {...data,version:snapshot.version});
    if (result.left) {
      delete storage[code]; saveStorage();
      location.href = "/friends";
    } else render(result);
    return result;
  } catch (error) { toast(error.message); }
  finally { busy = false; updateEnabled(); poll(); }
}
function setupWelcome() {
  if (code) {
    hide("sizeField",true);
    $("entryEyebrow").textContent = "YOU'RE INVITED";
    $("entryTitle").textContent = "朋友给你留了位。";
    $("entryDescription").textContent = invite ? "输入昵称，加入这张私人牌桌。" : "需要完整邀请链接才能加入。请向房主索取链接。";
    $("entrySubmit").textContent = "加入朋友的牌桌 ↗";
    $("entrySubmit").disabled = !invite;
  }
  $("savedRooms").innerHTML = Object.entries(storage).filter(([key])=>key!==code).map(([key,value])=>`<a href="/rooms/${encodeURIComponent(key)}">回到 ${esc(value.name)} 的座位 · ${esc(key)} →</a>`).join("");
}
$("entryForm").addEventListener("submit",async event=>{
  event.preventDefault(); if (busy) return;
  busy=true; $("entrySubmit").disabled=true;
  try {
    const name=$("nickname").value.trim();
    const result=await request(code ? `/api/rooms/${code}/join` : "/api/rooms", code ? {name,invite} : {name,max_players:Number($("tableSize").value)},false);
    code=result.room.code; token=result.token; invite=result.room.invite;
    storage[code]={token,name}; saveStorage();
    history.replaceState({},"",`/rooms/${code}#invite=${invite}`);
    render(result.room); startPolling();
  } catch (error) { toast(error.message); }
  finally { busy=false; $("entrySubmit").disabled=false; updateEnabled(); }
});
$("inviteBtn").addEventListener("click",async()=>{
  try { await navigator.clipboard.writeText(invitationURL()); toast("邀请链接已复制，发给朋友即可入座。"); }
  catch (_) { $("inviteLink").value=invitationURL(); $("inviteDialog").showModal(); $("inviteLink").select(); }
});
$("closeInvite").addEventListener("click",()=>$("inviteDialog").close());
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
