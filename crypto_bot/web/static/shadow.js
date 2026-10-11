'use strict';
(() => {
 const form=document.querySelector('#shadow-form');if(!form)return;
 const exits=JSON.parse(form.dataset.exits),cards=document.querySelector('#shadow-policies'),status=document.querySelector('#shadow-status');
 let next=0;
 function add(selected){
  if(cards.children.length>=10){status.textContent='Maximum 10 shadow policies';return;}
  const card=document.createElement('fieldset'),legend=document.createElement('legend'),select=document.createElement('select'),params=document.createElement('textarea'),remove=document.createElement('button');
  card.dataset.id='policy_'+(++next);legend.textContent=card.dataset.id;select.className='shadow-exit-policy';params.className='shadow-exit-params';params.setAttribute('aria-label','Exit parameters JSON');
  Object.keys(exits).forEach(id=>{const option=document.createElement('option');option.value=id;option.textContent=id;select.append(option);});
  select.value=selected||Object.keys(exits)[0];params.value=JSON.stringify(exits[select.value],null,2);
  select.addEventListener('change',()=>{params.value=JSON.stringify(exits[select.value],null,2);});
  remove.type='button';remove.textContent='Remove';remove.addEventListener('click',()=>card.remove());
  card.append(legend,select,params,remove);cards.append(card);
 }
 // Requested starting comparison, still editable through the full registry.
 ['baseline_v1','fixed_tp','r_tp','trailing_pct'].filter(id=>id in exits).forEach(add);
 document.querySelector('#shadow-add').addEventListener('click',()=>add());
 function config(){const [run,variant]=document.querySelector('#shadow-anchor').value.split(':');return {name:document.querySelector('#shadow-name').value,anchor_run_id:run,anchor_variant:Number(variant),policies:[...cards.children].map(card=>({id:card.dataset.id,exit_policy:card.querySelector('select').value,exit_parameters:JSON.parse(card.querySelector('textarea').value)}))};}
 async function post(path){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':form.dataset.csrf},body:JSON.stringify(config())});const data=await r.json();if(!r.ok)throw Error(data.errors?data.errors.map(e=>e.field+': '+e.message).join('\n'):'Shadow request failed');return data;}
 document.querySelector('#shadow-validate').addEventListener('click',async()=>{try{await post('/api/replay/shadow/config/validate');status.textContent='VALID — no job started. Click START SHADOW RESEARCH explicitly.';}catch(e){status.textContent=e.message;}});
 form.addEventListener('submit',async e=>{e.preventDefault();const button=form.querySelector('button[type="submit"]');button.disabled=true;try{const data=await post('/api/replay/shadow/runs');location.href=data.url;}catch(error){status.textContent=error.message;button.disabled=false;}});
})();
