/* Shared settlement presentation rules; no access to hidden cards or game logic. */
(() => {
  "use strict";
  function describe(result, me) {
    const winners=result.players.filter(p=>p.won), mine=result.players.find(p=>p.id===me);
    const sharedPot=result.split && result.pots.length===1;
    const headline=sharedPot ? "SPLIT POT" : winners.length>1 ? "SIDE POTS" :
      winners[0]?.id===me ? (mine.delta>0 ? "YOU WIN" : "POT WON") : "WINNER";
    const title=winners.map(p=>p.name).join("、") + (sharedPot ? " · 平分底池" : winners.length>1 ? " · 分获底池" : " · 赢得底池");
    return {headline,title,net:mine?.delta ?? null,won:Boolean(mine?.won)};
  }
  function chipEvents(previous, next) {
    if(!previous || previous.code!==next.code || previous.hand_number!==next.hand_number)return [];
    const settled=previous.phase==="playing" && next.phase==="finished";
    const awards=new Map(settled ? (next.result?.players || []).map(p=>[p.id,p.awarded]) : []);
    return next.players.flatMap(p=>{
      const old=previous.players.find(q=>q.id===p.id);
      if(!old)return [];
      const award=awards.get(p.id) || 0;
      const topup=previous.settings.starting_stack===next.settings.starting_stack ? Math.max(0,p.buyin-old.buyin) : 0;
      const amount=award+topup;
      return amount>0 ? [{id:p.id,amount,from:Math.max(0,p.stack-amount),to:p.stack,
        source:award>0 ? "pot" : "topup",label:award && topup ? "获池及补码" : award ? "获池" : "补码"}] : [];
    });
  }
  const api={describe,chipEvents};
  if(typeof module!=="undefined" && module.exports)module.exports=api;
  else globalThis.PokerResults=api;
})();
