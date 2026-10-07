'use strict';
(() => {
  const root = document.querySelector('#trade-charts');
  if (!root) return;
  const priceCanvas = document.querySelector('#trade-price-chart');
  const scoreCanvas = document.querySelector('#trade-score-chart');
  const timeframe = document.querySelector('#trade-timeframe');
  const priceStatus = document.querySelector('#price-chart-status');
  let candles = [], scores = [], overlays = {}, extent = null, view = null;
  let generation = 0, busy = false, controller = null, simulatedExit = null;
  const utc = ms => new Date(ms).toISOString().replace('T', ' ').replace('.000Z', ' UTC');
  const shortUtc = ms => new Date(ms).toISOString().slice(5,16).replace('T',' ');
  const finite = v => v !== null && v !== undefined && Number.isFinite(Number(v));
  async function json(url, signal) {
    const response = await fetch(url, {signal});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || `Data unavailable (${response.status})`);
    return data;
  }
  function axes(canvas, min, max) {
    const width = canvas.clientWidth, height = canvas.clientHeight, ratio = window.devicePixelRatio || 1;
    canvas.width = width*ratio; canvas.height = height*ratio;
    const ctx = canvas.getContext('2d'); ctx.scale(ratio,ratio); ctx.font='11px system-ui';
    const left=52, right=width-75, top=32, bottom=height-38;
    const x = time => left + (time-view[0])/(view[1]-view[0])*(right-left);
    const y = value => bottom-(value-min)/(max-min)*(bottom-top);
    for (let i=0;i<=4;i++) {
      const value=min+(max-min)*i/4, yy=y(value);
      ctx.strokeStyle='#35465d'; ctx.beginPath(); ctx.moveTo(left,yy); ctx.lineTo(right,yy); ctx.stroke();
      ctx.fillStyle='#b4c3d8';ctx.fillText(value.toPrecision(4),2,yy+4);
    }
    for (let i=0;i<=2;i++) {
      const t=view[0]+(view[1]-view[0])*i/2;
      ctx.fillText(shortUtc(t),Math.max(0,Math.min(width-110,x(t)-38)),height-12);
    }
    return {ctx,x,y,left,right,top,bottom};
  }
  function markers(a) {
    const {ctx,x,top,bottom}=a;
    [['BUY', overlays.entry_time,'#75dca2'],['SELL',overlays.exit_time,'#ff8686'],['WHAT-IF',simulatedExit?.time,'#bca3ff']].forEach(([label,time,color])=>{
      const t=Date.parse(time); if(!Number.isFinite(t) || t<view[0] || t>view[1]) return;
      ctx.strokeStyle=color; ctx.setLineDash([4,4]); ctx.beginPath();ctx.moveTo(x(t),top);ctx.lineTo(x(t),bottom);ctx.stroke();ctx.setLineDash([]);
      ctx.fillStyle=color;ctx.fillText(label,Math.min(a.right-32,x(t)+3),18);
    });
  }
  function drawPrice() {
    if (!view) return;
    const visible=candles.filter(c=>c.time>=view[0] && c.time<=view[1]);
    const levels=[overlays.entry, overlays.stop, overlays.take_profit].filter(finite).map(Number);
    const values=visible.flatMap(c=>[c.high,c.low]).concat(levels);
    let min=values.length ? Math.min(...values) : 0, max=values.length ? Math.max(...values) : 1;
    const pad=(max-min)*.1 || max*.001 || 1;min-=pad;max+=pad;
    const a=axes(priceCanvas,min,max), {ctx,x,y,left,right}=a;
    const interval={'1m':60000,'5m':300000,'15m':900000,'1h':3600000}[timeframe.value];
    const candleWidth=Math.max(1,Math.min(16,interval/(view[1]-view[0])*(right-left)*.65));
    visible.forEach(c=>{
      ctx.strokeStyle=ctx.fillStyle=c.close>=c.open?'#75dca2':'#ff8686';
      ctx.beginPath();ctx.moveTo(x(c.time),y(c.high));ctx.lineTo(x(c.time),y(c.low));ctx.stroke();
      ctx.fillRect(x(c.time)-candleWidth/2,Math.min(y(c.open),y(c.close)),candleWidth,Math.max(1,Math.abs(y(c.open)-y(c.close))));
    });
    [['Entry',overlays.entry,'#71d4dc'],['Stop',overlays.stop,'#ff8686'],['TP',overlays.take_profit,'#e9d56b']].forEach(([label,value,color])=>{
      if(!finite(value))return;
      ctx.strokeStyle=ctx.fillStyle=color;ctx.setLineDash([6,4]);ctx.beginPath();ctx.moveTo(left,y(value));ctx.lineTo(right,y(value));ctx.stroke();ctx.setLineDash([]);
      ctx.fillText(`${label} ${Number(value).toPrecision(5)}`,Math.max(left,right-140),y(value)-5);
    });
    if(simulatedExit && finite(simulatedExit.price)){ctx.fillStyle='#bca3ff';ctx.beginPath();ctx.arc(x(Date.parse(simulatedExit.time)),y(Number(simulatedExit.price)),5,0,Math.PI*2);ctx.fill();}
    markers(a);
  }
  function drawScores() {
    if (!view) return;
    const a=axes(scoreCanvas,0,100), {ctx,x,y,right,top,bottom}=a;
    const deltas=scores.filter(s=>finite(s.delta_4h)).map(s=>Number(s.delta_4h));
    const bound=Math.max(1,...deltas.map(Math.abs));
    const dy=v=>bottom-(v+bound)/(2*bound)*(bottom-top);
    ctx.fillStyle='#efbd72';
    for(let i=0;i<=4;i++) {const v=-bound+2*bound*i/4;ctx.fillText(v.toFixed(1),right+8,dy(v)+4);}
    // Actual dots only: never join sparse observations with interpolated scores.
    scores.filter(s=>s.time>=view[0]&&s.time<=view[1]).forEach(s=>{
      ctx.fillStyle='#71d4dc';ctx.beginPath();ctx.arc(x(s.time),y(s.score),3,0,Math.PI*2);ctx.fill();
      if(finite(s.delta_4h)){ctx.fillStyle='#efbd72';ctx.beginPath();ctx.arc(x(s.time),dy(Number(s.delta_4h)),3,0,Math.PI*2);ctx.fill();}
    });markers(a);
  }
  function draw(){drawPrice();drawScores();}
  async function loadPrices(refresh=false) {
    const gen=++generation;controller?.abort();controller=new AbortController();busy=true;
    if(!refresh){candles=[];view=null;priceStatus.textContent='Loading public candles…';}
    try {
      // Refresh just the tail. Historical pages remain in this browser's memory.
      let url=new URL(root.dataset.priceUrl,location.origin);url.searchParams.set('interval',timeframe.value);
      if(refresh && candles.length)url.searchParams.set('start',Math.max(extent[0],candles.at(-1).time));
      let pages=0;
      while(url){
        const data=await json(url,controller.signal);if(gen!==generation)return;
        const merged=new Map(candles.map(c=>[c.time,c]));data.candles.forEach(c=>merged.set(c.time,c));
        candles=Array.from(merged.values()).sort((a,b)=>a.time-b.time);overlays=data.overlays;
        const wasFull=!view || (extent && view[0]===extent[0] && view[1]===extent[1]);
        extent=[data.range.start,Math.max(data.range.start+1,data.range.end)];if(wasFull)view=[...extent];
        draw();pages++;
        priceStatus.textContent=`${candles.length} public candles · ${timeframe.value} · UTC · visualization only${data.next_start?' · loading next page…':''}`;
        if(data.next_start){
          // Keep extreme histories bounded in the browser and state the limit visibly.
          if(pages>=50){priceStatus.textContent+=' · partial: choose a larger timeframe (50-page display limit)';break;}
          url=new URL(root.dataset.priceUrl,location.origin);url.searchParams.set('interval',timeframe.value);
          url.searchParams.set('start',data.next_start);url.searchParams.set('end',data.range.end);
        } else url=null;
      }
      if(!candles.length)priceStatus.textContent='No public Binance candles available. Audit records remain usable.';
    }catch(error){if(error.name!=='AbortError')priceStatus.textContent=`${error.message} Last successfully loaded candles, if any, remain visible; no fallback.`;}
    finally{if(gen===generation)busy=false;}
  }
  async function loadScores(){
    try{
      const data=await json(root.dataset.scoreUrl);scores=data.points;
      if(!extent){extent=[data.range.start,Math.max(data.range.start+1,data.range.end)];view=[...extent];}
      overlays.entry_time=data.entry_time;overlays.exit_time=data.exit_time;
      document.querySelector('#score-chart-status').textContent=`${scores.length} actual scanner snapshots · no interpolation`;
      const body=document.querySelector('#score-records');body.replaceChildren();
      scores.forEach(s=>{const row=document.createElement('tr');[s.id,s.timestamp,s.score,s.delta_4h??'—'].forEach(v=>{const cell=document.createElement('td');cell.textContent=v;row.append(cell);});body.append(row);});
      draw();
    }catch(error){document.querySelector('#score-chart-status').textContent=error.message;}
  }
  function zoom(factor){if(!view||!extent)return;const middle=(view[0]+view[1])/2,span=Math.min(extent[1]-extent[0],Math.max(60000,(view[1]-view[0])*factor));view=[middle-span/2,middle+span/2];clamp();draw();}
  function clamp(){if(view[0]<extent[0]){view[1]+=extent[0]-view[0];view[0]=extent[0];}if(view[1]>extent[1]){view[0]-=view[1]-extent[1];view[1]=extent[1];}}
  document.querySelector('#chart-zoom-in').addEventListener('click',()=>zoom(.5));
  document.querySelector('#chart-zoom-out').addEventListener('click',()=>zoom(2));
  document.querySelector('#chart-reset').addEventListener('click',()=>{if(extent){view=[...extent];draw();}});
  timeframe.addEventListener('change',()=>loadPrices());
  [priceCanvas,scoreCanvas].forEach(canvas=>{
    let drag=null;
    canvas.addEventListener('pointerdown',e=>{if(view){drag={x:e.clientX,view:[...view]};canvas.setPointerCapture(e.pointerId);}});
    canvas.addEventListener('pointerup',()=>{drag=null;});canvas.addEventListener('pointercancel',()=>{drag=null;});
    canvas.addEventListener('pointermove',e=>{
      if(!view)return;const rect=canvas.getBoundingClientRect(),w=rect.width-127;
      if(drag){const shift=(drag.x-e.clientX)/w*(drag.view[1]-drag.view[0]);view=drag.view.map(v=>v+shift);clamp();draw();}
      const t=view[0]+(e.clientX-rect.left-52)/w*(view[1]-view[0]);
      const items=canvas===priceCanvas?candles:scores;if(!items.length)return;
      const near=items.reduce((a,b)=>Math.abs(b.time-t)<Math.abs(a.time-t)?b:a);
      const status=document.querySelector(canvas===priceCanvas?'#price-chart-hover':'#score-chart-hover');
      status.textContent=canvas===priceCanvas?`${utc(near.time)} · O ${near.open} H ${near.high} L ${near.low} C ${near.close}`:`Snapshot #${near.id} · ${utc(near.time)} · Score ${near.score} · Δ4h ${near.delta_4h??'not recorded'}`;
    });
  });
  document.addEventListener('paper-simulated-exit',e=>{simulatedExit=e.detail;draw();});
  new ResizeObserver(draw).observe(root);
  loadPrices();loadScores();
  const quotePanel=document.querySelector('#current-quote');let quoteBusy=false;
  async function loadQuote(){
    if(!quotePanel||quoteBusy)return;quoteBusy=true;
    try{
      const data=await json(quotePanel.dataset.url),values={...data,...data.quote};
      quotePanel.querySelectorAll('[data-quote-field]').forEach(node=>{
        const value=values[node.dataset.quoteField];node.title=value??'';
        node.textContent=value===null||value===undefined?'—':finite(value)?Number(value).toLocaleString('en-US',{maximumSignificantDigits:10}):value;
      });
      document.querySelector('#quote-status').textContent=`${data.quote.source} · indicative display, ledger unchanged`;
    }catch(error){
      quotePanel.querySelectorAll('[data-quote-field]').forEach(node=>{node.textContent='— / unavailable';});
      document.querySelector('#quote-status').textContent=error.message;
    }finally{quoteBusy=false;}
  }
  loadQuote();
  if(root.dataset.open==='true')setInterval(()=>{if(!document.hidden){if(!busy)loadPrices(true);loadScores();loadQuote();}},30000);
})();
