'use strict';
const search = document.querySelector('#search');
if (search) search.addEventListener('input', () => {
  document.querySelectorAll('#markets tbody tr').forEach(row => {
    row.hidden = !row.cells[0].textContent.toLowerCase().includes(search.value.toLowerCase());
  });
});
document.querySelector('#refresh')?.addEventListener('click', () => location.reload());
document.querySelectorAll('.sort').forEach(button => {
  let descending = true;
  button.addEventListener('click', () => {
    const index = Number(button.dataset.column);
    const body = document.querySelector('#markets tbody');
    const rows = Array.from(body.rows).filter(row => row.cells.length > 1);
    const value = row => {
      if (row.cells[index].querySelector('select')) return row.cells[index].querySelector('select').value;
      const text = row.cells[index].textContent.trim();
      if (text === '—') return null;
      const num = Number(text.replaceAll(',', ''));
      return text !== '' && Number.isFinite(num) ? num : text;
    };
    rows.sort((a,b) => {
      const av = value(a), bv = value(b);
      if (av === null) return bv === null ? 0 : 1;
      if (bv === null) return -1;
      const order = typeof av === 'number' && typeof bv === 'number' ? av-bv : String(av).localeCompare(String(bv));
      return descending ? -order : order;
    });
    rows.forEach(row => body.appendChild(row));
    descending = !descending;
  });
});
const canvas = document.querySelector('#history-chart');
if (canvas) {
  fetch(canvas.dataset.url).then(response => { if (!response.ok) throw Error('History unavailable'); return response.json(); }).then(data => {
    const status = document.querySelector('#chart-status');
    if (!data.length) { status.textContent = 'Nincs history.'; return; }
    status.textContent = `${data.length} megfigyelés · Score: türkiz · Ár: arany. A hiányos időszakok üresen maradnak.`;
    function draw() {
      const width = canvas.clientWidth, height = canvas.clientHeight, ratio = window.devicePixelRatio || 1;
      canvas.width = width*ratio; canvas.height = height*ratio;
      const ctx = canvas.getContext('2d'); ctx.scale(ratio,ratio);
      const left = 50, right = width-95, top = 25, bottom = height-65;
      const times = data.map(p => Date.parse(p.timestamp)), prices = data.map(p => p.features.price);
      const tmin = times[0], tmax = Math.max(tmin+1,times.at(-1));
      let pmin = Math.min(...prices), pmax = Math.max(...prices);
      const pad = (pmax-pmin)*0.08 || pmax*0.001 || 1; pmin -= pad; pmax += pad;
      const x = t => left + (t-tmin)/(tmax-tmin)*(right-left);
      const y = (v,min,max) => bottom-(v-min)/(max-min)*(bottom-top);
      ctx.font='11px system-ui';
      for(let i=0;i<=4;i++) {
        const yy=y(i*25,0,100); ctx.strokeStyle='#35465d';ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();
        ctx.fillStyle='#71d4dc';ctx.fillText(String(i*25),5,yy+4);
        ctx.fillStyle='#efbd72';ctx.fillText((pmin+(pmax-pmin)*i/4).toPrecision(5),right+8,yy+4);
      }
      const intervals = times.slice(1).map((t,i)=>t-times[i]).filter(v=>v>0).sort((a,b)=>a-b);
      const gap = (intervals[Math.floor(intervals.length/2)] || Infinity)*3;
      function line(values,min,max,color) {
        ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();
        values.forEach((v,i)=>{if(i===0 || times[i]-times[i-1]>gap)ctx.moveTo(x(times[i]),y(v,min,max));else ctx.lineTo(x(times[i]),y(v,min,max));});ctx.stroke();
        ctx.fillStyle=color;values.forEach((v,i)=>{ctx.beginPath();ctx.arc(x(times[i]),y(v,min,max),2,0,Math.PI*2);ctx.fill();});
      }
      line(data.map(p=>p.total_score),0,100,'#71d4dc');line(prices,pmin,pmax,'#efbd72');
      ctx.fillStyle='#bac7d8';
      [0,Math.floor((data.length-1)/2),data.length-1].forEach(i=>{
        const label=new Date(times[i]).toISOString().slice(5,16).replace('T',' ');
        ctx.fillText(label,Math.max(0,Math.min(width-110,x(times[i])-35)),height-25);
      });
    }
    draw();window.addEventListener('resize',draw);
  }).catch(()=>{document.querySelector('#chart-status').textContent='A history nem tölthető be. Az alábbi táblázat továbbra is használható.';});
}

// Native Details links remain usable without JavaScript and from the keyboard.
document.querySelectorAll('[data-position-url]').forEach(row => {
  row.addEventListener('click', event => {
    if (event.target.closest('a,button,input,select,textarea,form,label') ||
        event.ctrlKey || event.metaKey || event.shiftKey || event.altKey ||
        window.getSelection()?.toString()) return;
    location.assign(row.dataset.positionUrl);
  });
});
