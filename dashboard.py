#!/usr/bin/env python3
from flask import Flask, render_template_string, jsonify
from trading_bot import get_signal, get_all_signals

app = Flask(__name__)

TEMPLATE = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Crypto Bot Dashboard</title>
    <style>
      body { font-family: sans-serif; background:#111; color:#eee; text-align:center; padding-top:30px; }

      .box, .chart-container, .explain-box {
        width:90%;
        max-width:1100px;
        margin:0 auto 30px auto;
        background:#222;
        padding:20px;
        border-radius:12px;
        box-shadow:0 0 10px #000;
      }

      table {
        width:100%;
        border-collapse:collapse;
        font-size:14px;
        margin-top:10px;
      }
      table th {
        border-bottom:1px solid #444;
        padding:6px;
        color:#ccc;
      }
      table td {
        padding:4px;
        border-bottom:1px solid #333;
      }

      tr.clickable-row { cursor:pointer; }

      .BUY  { color:#00ff7f; }
      .SELL { color:#ff4d4d; }
      .WAIT { color:#ffaa33; }

      .explain-box ul {
        text-align:left;
        margin:10px auto 0 auto;
        max-width:900px;
        line-height:1.5;
        color:#ddd;
      }
      .explain-box li span.key { color:#ffa500; font-weight:bold; }

      .range-controls {
        margin-bottom:8px;
      }
      .range-btn {
        background:#333;
        border:1px solid #555;
        border-radius:6px;
        padding:4px 10px;
        margin:0 3px;
        color:#eee;
        cursor:pointer;
        font-size:12px;
      }
      .range-btn.active {
        background:#555;
        border-color:#ffa500;
        color:#ffa500;
      }
    </style>

    <!-- Chart.js -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <!-- Zoom plugin Chart.js-hez -->
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.umd.min.js"></script>

    <script>
      let selectedChart = null;     // nagy history chart
      let allSignalsData = [];      // /api/all_signals utolsó eredménye
      let currentSymbol = null;     // aktuálisan kiválasztott coin
      let currentHistory = null;    // teljes history az adott coinra
      let currentRange = '24h';     // 1h / 6h / 12h / 24h / all

      function makeRsiLines(labels) {
        const buy = labels.map(() => 30);  // BUY zóna határ
        const mid = labels.map(() => 50);  // WAIT vonal
        const sell = labels.map(() => 70); // SELL zóna határ
        return { buy, mid, sell };
      }

      // RSI+EMA kombó jelzések marker pontjai (ár skálán)
      function makeCombinedMarkers(labels, prices, ema9, ema21, rsi) {
        const buyMarkers = [];
        const sellMarkers = [];

        for (let i = 0; i < labels.length; i++) {
          const rv = rsi[i];
          const e9 = ema9[i];
          const e21 = ema21[i];
          const p = prices[i];

          if (rv == null || e9 == null || e21 == null || p == null) {
            buyMarkers.push(null);
            sellMarkers.push(null);
            continue;
          }

          if (rv < 30 && e9 > e21) {
            buyMarkers.push(p);   // BUY pont az ár vonalon
            sellMarkers.push(null);
          } else if (rv > 70 && e9 < e21) {
            sellMarkers.push(p);  // SELL pont az ár vonalon
            buyMarkers.push(null);
          } else {
            buyMarkers.push(null);
            sellMarkers.push(null);
          }
        }
        return { buyMarkers, sellMarkers };
      }

      function updateRangeButtons() {
        const btns = document.querySelectorAll('.range-btn');
        btns.forEach(b => {
          if (b.dataset.range === currentRange) {
            b.classList.add('active');
          } else {
            b.classList.remove('active');
          }
        });
      }

      function setRange(range) {
        currentRange = range;
        updateRangeButtons();
        updateChartFromHistory();
        if (selectedChart) {
          selectedChart.resetZoom();
        }
      }

      function computeSliceIndex(totalLen) {
        // 5 perces gyertyák → 12 db / óra
        const perHour = 12;
        let need;
        if (currentRange === '1h') need = 1 * perHour;
        else if (currentRange === '6h') need = 6 * perHour;
        else if (currentRange === '12h') need = 12 * perHour;
        else if (currentRange === '24h') need = 24 * perHour;
        else need = totalLen; // all
        if (need >= totalLen) return 0;
        return totalLen - need;
      }

      function updateChartFromHistory() {
        if (!currentHistory) return;

        const fullLabels = currentHistory.times;
        const fullPrices = currentHistory.prices;
        const fullEma9 = currentHistory.ema9;
        const fullEma21 = currentHistory.ema21;
        const fullRsi = currentHistory.rsi;

        const len = fullLabels.length;
        const start = computeSliceIndex(len);

        const labels = fullLabels.slice(start);
        const prices = fullPrices.slice(start);
        const ema9 = fullEma9.slice(start);
        const ema21 = fullEma21.slice(start);
        const rsi = fullRsi.slice(start);

        const rsiLines = makeRsiLines(labels);
        const markersFull = makeCombinedMarkers(fullLabels, fullPrices, fullEma9, fullEma21, fullRsi);
        const buyMarkers = markersFull.buyMarkers.slice(start);
        const sellMarkers = markersFull.sellMarkers.slice(start);

        const ctx = document.getElementById('selectedChart').getContext('2d');

        if (!selectedChart) {
          selectedChart = new Chart(ctx, {
            type: 'line',
            data: {
              labels: labels,
              datasets: [
                { label: 'Ár', data: prices, borderWidth: 1, pointRadius: 0, yAxisID: 'y' },
                { label: 'EMA9', data: ema9, borderWidth: 1, pointRadius: 0, yAxisID: 'y' },
                { label: 'EMA21', data: ema21, borderWidth: 1, pointRadius: 0, yAxisID: 'y' },
                { label: 'RSI', data: rsi, borderWidth: 1, pointRadius: 0, yAxisID: 'y1' },
                { label: 'RSI BUY (30)', data: rsiLines.buy, borderWidth: 1, pointRadius: 0, yAxisID: 'y1', borderDash: [4,4] },
                { label: 'RSI WAIT (50)', data: rsiLines.mid, borderWidth: 1, pointRadius: 0, yAxisID: 'y1', borderDash: [4,4] },
                { label: 'RSI SELL (70)', data: rsiLines.sell, borderWidth: 1, pointRadius: 0, yAxisID: 'y1', borderDash: [4,4] },
                { label: 'RSI+EMA BUY', data: buyMarkers, borderWidth: 0, pointRadius: 4, pointStyle: 'triangle', yAxisID: 'y' },
                { label: 'RSI+EMA SELL', data: sellMarkers, borderWidth: 0, pointRadius: 4, pointStyle: 'triangle', rotation: 180, yAxisID: 'y' }
              ]
            },
            options: {
              responsive: true,
              animation: false,
              plugins: {
                legend: { labels: { color: '#ccc' } },
                zoom: {
                  zoom: {
                    wheel: { enabled: true },
                    pinch: { enabled: true },
                    mode: 'x'
                  },
                  pan: {
                    enabled: true,
                    mode: 'x'
                  }
                }
              },
              scales: {
                x: { ticks: { color: '#ccc', maxTicksLimit: 12 } },
                y: {
                  ticks: { color: '#ccc' },
                  position: 'left'
                },
                y1: {
                  ticks: { color: '#ffa500', max: 100, min: 0 },
                  position: 'right',
                  grid: { drawOnChartArea: false }
                }
              }
            }
          });
        } else {
          selectedChart.data.labels = labels;
          selectedChart.data.datasets[0].data = prices;
          selectedChart.data.datasets[1].data = ema9;
          selectedChart.data.datasets[2].data = ema21;
          selectedChart.data.datasets[3].data = rsi;

          selectedChart.data.datasets[4].data = rsiLines.buy;
          selectedChart.data.datasets[5].data = rsiLines.mid;
          selectedChart.data.datasets[6].data = rsiLines.sell;

          selectedChart.data.datasets[7].data = buyMarkers;
          selectedChart.data.datasets[8].data = sellMarkers;

          selectedChart.update();
        }

        const title = document.getElementById('selected-title');
        const rangeLabel = (currentRange === 'all') ? 'összes adat' : currentRange;
        if (currentSymbol) {
          title.textContent = currentSymbol + ' – history (' + rangeLabel + ', RSI+EMA jelzések pontokkal)';
        }
      }

      function selectSymbol(symbol) {
        if (!allSignalsData || allSignalsData.length === 0) return;
        const row = allSignalsData.find(r => r.symbol === symbol);
        if (!row || !row.history) return;

        currentSymbol = symbol;
        currentHistory = row.history;
        if (selectedChart) {
          selectedChart.resetZoom();
        }
        updateChartFromHistory();
      }

      async function refreshAllSignals() {
        try {
          const resp = await fetch('/api/all_signals');
          const data = await resp.json();

          if (data.error) {
            console.error('all_signals API hiba:', data.error);
            return;
          }

          if (!Array.isArray(data)) {
            console.error('Nem lista jött /api/all_signals-tól:', data);
            return;
          }

          allSignalsData = data;

          // ----- TÁBLÁZAT -----
          const tbody = document.getElementById('signals-table-body');
          tbody.innerHTML = '';

          data.forEach(row => {
            const tr = document.createElement('tr');
            tr.className = 'clickable-row';

            tr.innerHTML = `
              <td>${row.symbol}</td>
              <td>${row.price.toFixed(2)}</td>
              <td>${row.rsi.toFixed(2)}</td>
              <td>${row.ema9.toFixed(2)}</td>
              <td>${row.ema21.toFixed(2)}</td>
              <td class="${row.signal_rsi}">${row.signal_rsi}</td>
              <td class="${row.signal}">${row.signal}</td>
            `;
            tr.addEventListener('click', () => selectSymbol(row.symbol));
            tbody.appendChild(tr);
          });

          // ha már van kiválasztott coin, frissítsük a history-ját
          if (currentSymbol) {
            const row = data.find(r => r.symbol === currentSymbol);
            if (row && row.history) {
              currentHistory = row.history;
              updateChartFromHistory();
            }
          } else if (data.length > 0) {
            // első betöltéskor válasszuk az első coint
            currentSymbol = data[0].symbol;
            currentHistory = data[0].history;
            updateChartFromHistory();
          }

        } catch (err) {
          console.error('refreshAllSignals hiba:', err);
        }
      }

      window.addEventListener('load', function() {
        updateRangeButtons();
        refreshAllSignals();
        setInterval(() => {
          refreshAllSignals();
        }, 5000);
      });
    </script>
  </head>

  <body>
    <div class="explain-box">
      <h2>Magyarázat – mit látsz a grafikonon?</h2>
      <ul>
        <li><span class="key">RSI (sárga vonal, jobb tengely):</span> a túlvett / túladott állapotot mutatja.
          <br>• 30 alatt: piac túladott → erős <b>vételi</b> zóna.<br>
          • 30–70 között: normál tartomány → inkább <b>várakozás</b>.<br>
          • 70 felett: piac túlvett → erős <b>eladási</b> zóna.</li>
        <li><span class="key">Pontozott RSI-vonalak:</span> 30 (BUY), 50 (semleges), 70 (SELL) szintek – vizuális segítség.</li>
        <li><span class="key">EMA9 / EMA21 (piros / narancs):</span> rövid és hosszú mozgóátlag.
          <br>• Ha EMA9 az EMA21 <b>fölé kerül</b> → emelkedő trend (bullish).<br>
          • Ha EMA9 az EMA21 <b>alá esik</b> → csökkenő trend (bearish).</li>
        <li><span class="key">RSI jelzés:</span> csak az RSI 30 / 70 szintek alapján dönt (egyszerűbb, de zajosabb).</li>
        <li><span class="key">RSI+EMA jelzés:</span> akkor ad BUY/SELL-t, amikor az RSI <b>ÉS</b> az EMA-k is megerősítik egymást – óvatosabb, de megbízhatóbb jelzés.
          A grafikonon ezek a jelzések <b>háromszög alakú pontokként</b> jelennek meg az ár vonalán.</li>
        <li><span class="key">Zoom / nagyítás:</span> egérgörgővel vagy pinch-zoommal vízszintesen nagyíthatsz, és húzással pannolhatsz.
          Az <b>Alaphelyzet</b> gomb visszaállítja az eredeti nézetet.</li>
      </ul>
    </div>

    <div class="chart-container">
      <h2 id="selected-title">History</h2>
      <div class="range-controls">
        <button class="range-btn" data-range="1h" onclick="setRange('1h')">1h</button>
        <button class="range-btn" data-range="6h" onclick="setRange('6h')">6h</button>
        <button class="range-btn" data-range="12h" onclick="setRange('12h')">12h</button>
        <button class="range-btn active" data-range="24h" onclick="setRange('24h')">24h</button>
        <button class="range-btn" data-range="all" onclick="setRange('all')">Összes</button>
        <button class="range-btn" onclick="if (selectedChart) selectedChart.resetZoom();">Alaphelyzet (zoom)</button>
      </div>
      <canvas id="selectedChart" height="150"></canvas>
    </div>

    <div class="box">
      <h2>Coin overview</h2>
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Ár (USDC)</th>
            <th>RSI</th>
            <th>EMA9</th>
            <th>EMA21</th>
            <th>RSI jelzés</th>
            <th>RSI+EMA jelzés</th>
          </tr>
        </thead>
        <tbody id="signals-table-body"></tbody>
      </table>
    </div>

  </body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(TEMPLATE)

@app.route("/api/signal")
def api_signal():
    # backend oldalon meghagyjuk, ha később kell
    try:
        s = get_signal("BTCUSDC")
        return jsonify(s)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/all_signals")
def api_all_signals():
    try:
        s = get_all_signals()
        return jsonify(s)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=6000, debug=False)