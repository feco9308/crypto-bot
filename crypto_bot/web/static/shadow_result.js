'use strict';
(() => {
 const root=document.querySelector('#shadow-result');if(!root)return;let result;
 const fmt=v=>v===null||v===undefined?'—':typeof v==='number'?v.toFixed(4):String(v);
 function row(body,values){const r=document.createElement('tr');values.forEach(v=>{const cell=document.createElement('td');cell.textContent=fmt(v);r.append(cell);});body.append(r);}
 function groups(){const body=document.querySelector('#shadow-breakdown');body.replaceChildren();const group=document.querySelector('#shadow-group').value;Object.entries(result.breakdowns[group]||{}).forEach(([key,g])=>Object.entries(g.policy_summaries).forEach(([id,s])=>{const c=s.common_matched_outcomes;row(body,[key,id,g.canonical_entry_count,g.common_comparable_entry_count,c.expectancy_r,c.mean_return_pct,s.incomplete_count,s.stale_count,c.sample_label]);}));}
 document.querySelector('#shadow-group').addEventListener('change',()=>{if(result)groups();});
 async function load(){try{const response=await fetch('/api/replay/shadow/runs/'+root.dataset.id);if(!response.ok)throw Error('Could not load shadow job');const job=await response.json();document.querySelector('#shadow-job-status').textContent=job.status+(job.error?' · '+job.error:'')+' · '+JSON.stringify(job.progress);if(!job.result){if(['QUEUED','RUNNING'].includes(job.status))setTimeout(load,5000);return;}result=job.result;
 document.querySelector('#shadow-anchor-summary').textContent='Anchor '+result.metadata.anchor_run_id+' / variant '+result.metadata.anchor_variant+' · '+result.canonical_entry_count+' canonical FILLED BUYs · '+result.shadow_policies.length+' policies · '+result.incomplete_entry_count+' incomplete entries · '+result.stale_entry_count+' stale entries';
 const body=document.querySelector('#shadow-summary');body.replaceChildren();result.shadow_policies.forEach(p=>{const s=result.policy_summaries[p.id],c=s.common_matched_outcomes;row(body,[p.exit_policy+' '+JSON.stringify(p.exit_parameters),result.canonical_entry_count,s.comparable_closed_count,s.incomplete_count,s.stale_count,result.common_comparable_entry_count,c.expectancy_r,c.mean_return_pct,c.win_rate,c.profit_factor_normalized,c.sample_label]);});
 const paired=document.querySelector('#shadow-pairwise');paired.replaceChildren();result.pairwise.forEach(p=>row(paired,[p.policy_a+' vs '+p.policy_b,p.matched_entries,p.a_wins,p.b_wins,p.ties,p.incomplete,p.mean_return_difference,p.median_return_difference,p.mean_r_difference,p.sample_label]));groups();
 }catch(e){document.querySelector('#shadow-job-status').textContent=e.message;}}
 load();
})();
