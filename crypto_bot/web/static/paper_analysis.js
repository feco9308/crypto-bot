'use strict';
(() => {
 const root=document.querySelector('#analysis-data');if(!root)return;
 const params=new URLSearchParams(location.search);params.delete('limit');params.delete('offset');
 ['json','csv','meta'].forEach(format=>{const a=document.querySelector(`#export-${format}`);a.href+=`?${params}`;});
 const fmt=v=>v===null||v===undefined?'—':typeof v==='number'?v.toLocaleString('en-US',{maximumFractionDigits:6}):String(v);
 function cell(row,value,label){const td=document.createElement('td');td.textContent=fmt(value);if(label)td.dataset.label=label;row.append(td);return td;}
 function link(td,t){td.replaceChildren();const a=document.createElement('a');a.href=root.dataset.tradePrefix+t.position_id;a.textContent=`${t.symbol} · #${t.position_id}`;td.append(a);}
 let busy=false;
 async function get(url){const response=await fetch(url);if(!response.ok)throw Error(`Analysis unavailable (${response.status})`);return response.json();}
 async function refresh(){
  if(busy||document.hidden)return;busy=true;
  try{
   const [s,data]=await Promise.all([get(root.dataset.summaryUrl+'?'+params),get(root.dataset.tradesUrl+'?'+params+'&limit=50')]);
   document.querySelectorAll('[data-summary]').forEach(node=>{node.textContent=fmt(s[node.dataset.summary]);});
   document.querySelector('#sample-message').textContent=s.sample_message;
   document.querySelector('#portfolio-as-of').textContent=s.portfolio_as_of_utc?`Stored valuation: ${s.portfolio_as_of_utc}`:'';
   document.querySelector('#analysis-progress').textContent=`Historical analysis: ${Object.entries(s.analysis_status_counts||{}).map(([k,v])=>`${k} ${v}`).join(' · ')} · snapshot ${s.generated_at_utc}`;
   const buckets=document.querySelector('#score-buckets');buckets.replaceChildren();
   (s.score_buckets||[]).forEach(b=>{const row=document.createElement('tr');[b.bucket,b.trade_count,b.closed_trades,b.win_rate,b.average_return,b.median_return,b.average_mfe,b.average_mae,b.profit_factor,b.sample_small?'Insufficient sample size':'Descriptive'].forEach(v=>cell(row,v));buckets.append(row);});
   const rows=document.querySelector('#analysis-trades'),timing=document.querySelector('#entry-timing');rows.replaceChildren();timing.replaceChildren();
   data.trades.forEach(t=>{
    const row=document.createElement('tr');
    [['symbol','Symbol'],['entry_time','Entry UTC'],['exit_time','Exit UTC'],['realized_pnl','PnL'],['return_pct','Return %'],['mfe_pct','MFE %'],['mae_pct','MAE %'],['market_score','Market Score'],['delta_4h','Δ4h'],['exit_reason','Exit reason'],['analysis_data_status','Analysis']].forEach(([key,label])=>{const td=cell(row,t[key],label);if(key==='symbol')link(td,t);});rows.append(row);
    const tr=document.createElement('tr');link(cell(tr,t.symbol),t);['price_change_15m_after_entry_pct','price_change_30m_after_entry_pct','price_change_60m_after_entry_pct','entry_to_next_30m_low_pct','entry_to_next_60m_low_pct'].forEach(k=>cell(tr,t[k]));cell(tr,`${t.excursion_timeframe||'—'} · ${t.analysis_data_status}`);timing.append(tr);
   });
  }catch(error){document.querySelector('#analysis-progress').textContent=error.message+' · stored audit records remain available';}
  finally{busy=false;}
 }
 refresh();setInterval(refresh,10000);
})();
