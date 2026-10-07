'use strict';
(() => {
 const root=document.querySelector('#exit-simulator');if(!root)return;
 const status=document.querySelector('#simulator-status'),rows=document.querySelector('#simulator-rows');
 const params=new URLSearchParams();for(const k of ['symbol','from','to']){const v=new URLSearchParams(location.search).get(k);if(v)params.set(k,v);}
 const pid=root.dataset.positionId;if(pid)params.set('position_id',pid);
 const select=document.querySelector('#simulator-scenario'),bound=document.querySelector('#simulator-bound'),marker=document.querySelector('#simulator-marker');
 const fmt=v=>v===null||v===undefined?'—':(typeof v==='number'||(typeof v==='string'&&/^[+-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?$/i.test(v)))?Number(v).toLocaleString('en-US',{maximumFractionDigits:6}):String(v);
 let trades=[],busy=false;
 function showSelected(){
  if(!select)return;const scenario=trades[0]?.scenarios.find(s=>s.scenario_name===select.value),result=scenario?.[bound.value];
  document.querySelector('#simulator-selected').textContent=result?`${scenario.scenario_name} · ${bound.selectedOptions[0].text}: ${fmt(result.return_pct)}% · ${result.trigger_reason} · ${result.trigger_time_utc} (${result.time_precision}) · fill ${fmt(result.fill_price)} · fee ${fmt(result.fee)} · slippage ${fmt(result.slippage)} · ambiguous ${scenario.intrabar_ambiguity}`:'Selected scenario unavailable / pending';
  document.dispatchEvent(new CustomEvent('paper-simulated-exit',{detail:marker.checked&&result&&scenario.scenario_name!=='BASELINE'?{time:result.trigger_time_utc,price:result.trigger_price,label:'WHAT-IF EXIT'}:null}));
 }
 async function get(url){const r=await fetch(url+'?'+params);if(!r.ok)throw Error(`Simulator unavailable (${r.status})`);return r.json();}
 async function refresh(){
  if(busy||document.hidden)return;busy=true;
  try{
   const [s,data]=await Promise.all([get(root.dataset.summaryUrl),pid?get(root.dataset.tradesUrl):Promise.resolve(null)]);
   rows.replaceChildren();(s.scenarios||[]).forEach(item=>{
    const row=document.createElement('tr');if(item.scenario_name==='BASELINE')row.className='simulator-baseline';
    const labels=['Scenario','Trades / usable','Win rate %','Avg return %','Total PnL','Δ PnL vs actual','Ambiguous'];
    [item.scenario_name,`${item.trade_count} / ${item.usable_trade_count}`,item.win_rate,item.average_return,item.total_pnl,item.total_difference_vs_actual,item.ambiguous_trade_count].forEach((v,i)=>{const td=document.createElement('td');td.textContent=fmt(v);td.dataset.label=labels[i];row.append(td);});rows.append(row);
   });
   document.querySelector('#simulator-sample').textContent=`SAMPLE SIZE: ${fmt(s.sample_size)} · NOT STATISTICALLY VALIDATED`;
   status.textContent=`${s.aggregation_status} · conservative bounds · ${Object.entries(s.simulation_status_counts||{}).map(([k,v])=>`${k}: ${v}`).join(' · ')}`;
   if(data){
    trades=data.trades;const t=trades[0];
    const old=select.value;select.replaceChildren();data.scenario_catalog.forEach(c=>{const o=document.createElement('option');o.value=o.textContent=c.scenario_name;select.append(o);});select.value=old||'BASELINE';
    const actual=document.querySelector('#simulator-actual');actual.replaceChildren();
    if(t){for(const [label,key] of [['Actual return %','actual_return_pct'],['Actual PnL','actual_realized_pnl'],['MFE %','mfe_pct'],['MAE % (interior candles)','mae_pct'],['MAE including actual bid %','mae_including_exit_pct'],['Profit giveback pp','profit_giveback_pct'],['Final minus MFE pp','mfe_to_final_return_pct'],['MFE capture ratio','mfe_capture_ratio'],['Capture interpretation','mfe_capture_status'],['Entry ask drift %','entry_quote_drift_pct'],['Entry ask drift / ATR','entry_quote_drift_atr']]){const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=fmt(t[key]);div.append(dt,dd);actual.append(div);}}
    status.textContent+=` · trade ${t?.simulation_status||'UNAVAILABLE'}`;
    document.querySelector('#simulator-audit').textContent=JSON.stringify(t?.scenarios||[],null,2);showSelected();
   }else document.querySelector('#simulator-audit').textContent=JSON.stringify(s.scenarios||[],null,2);
  }catch(e){status.textContent=e.message;}
  finally{busy=false;}
 }
 select?.addEventListener('change',showSelected);bound?.addEventListener('change',showSelected);marker?.addEventListener('change',showSelected);
 refresh();setInterval(refresh,10000);
})();
