const test=require('node:test');
const assert=require('node:assert/strict');
const {describe,chipEvents}=require('../web/results.js');
const base={code:'ROOM',hand_number:1,phase:'playing',settings:{starting_stack:100},players:[{id:'me',stack:80,buyin:100},{id:'other',stack:80,buyin:100}]};
const winner={id:'me',name:'甲',won:true,delta:20,awarded:40};
const loser={id:'other',name:'乙',won:false,delta:-20,awarded:0};
const result={split:false,pots:[{}],players:[winner,loser]};
const finished={...base,phase:'finished',result,players:[{id:'me',stack:120,buyin:100},{id:'other',stack:80,buyin:100}]};
test('personal win and another player winning have distinct headings',()=>{
  assert.equal(describe(result,'me').headline,'YOU WIN');
  assert.equal(describe(result,'other').headline,'WINNER');
  assert.match(describe(result,'other').title,/甲/);
  assert.equal(describe(result,'other').net,-20);
});
test('split pot takes precedence over a personal positive net',()=>{
  assert.equal(describe({...result,split:true},'me').headline,'SPLIT POT');
});
test('a side-pot award does not imply positive profit',()=>{
  const partial={...result,pots:[{},{}],players:[winner,{...loser,won:true,awarded:10,delta:-10}]};
  assert.equal(describe(partial,'other').headline,'SIDE POTS');
  assert.equal(describe(partial,'other').net,-10);
  assert.equal(describe(partial,'other').won,true);
});
test('a split side pot does not imply every winner split every pot',()=>{
  const partial={...result,split:true,pots:[{},{}],players:[winner,{...loser,won:true,awarded:10}]};
  assert.equal(describe(partial,'me').headline,'SIDE POTS');
  assert.match(describe(partial,'me').title,/分获底池/);
});
test('settlement animates gross pot award exactly once, not net profit',()=>{
  assert.deepEqual(chipEvents(base,finished),[{id:'me',amount:40,from:80,to:120,source:'pot',label:'获池'}]);
  assert.deepEqual(chipEvents(finished,finished),[]);
});
test('opening an already completed hand does not replay animations',()=>{
  assert.deepEqual(chipEvents(null,finished),[]);
  assert.deepEqual(chipEvents({...base,hand_number:0},finished),[]);
  assert.deepEqual(chipEvents({...base,code:'ANOTHER'},finished),[]);
});
test('top-ups animate real increase without a victory claim',()=>{
  const topped={...finished,players:[{id:'me',stack:220,buyin:200},finished.players[1]]};
  assert.deepEqual(chipEvents(finished,topped),[{id:'me',amount:100,from:120,to:220,source:'topup',label:'补码'}]);
  assert.deepEqual(chipEvents(topped,topped),[]);
});
test('settlement plus top-up in one poll produces one combined credit animation',()=>{
  const topped={...finished,players:[{id:'me',stack:220,buyin:200},finished.players[1]]};
  assert.deepEqual(chipEvents(base,topped),[{id:'me',amount:140,from:80,to:220,source:'pot',label:'获池及补码'}]);
});
test('joining and changing initial buy-in are not mistaken for top-ups',()=>{
  assert.deepEqual(chipEvents(base,{...base,players:[...base.players,{id:'new',stack:100,buyin:100}]}),[]);
  assert.deepEqual(chipEvents(base,{...base,settings:{starting_stack:200},players:base.players.map(p=>({...p,stack:200,buyin:200}))}),[]);
});
