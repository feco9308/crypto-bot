'use strict';
(() => {
  const form = document.querySelector('#replay-create');
  if (!form) return;
  const container = document.querySelector('#replay-variants');
  const template = document.querySelector('#replay-variant-template');
  const status = document.querySelector('#replay-form-status');
  const textarea = document.querySelector('#replay-config-json');
  const configStatus = document.querySelector('#replay-config-status');
  const errorList = document.querySelector('#replay-config-errors');
  const mode = document.querySelector('#replay-mode');
  let configBusy = false;

  function exitFields(node) {
    const select = node.querySelector('[data-key=exit_policy]');
    const box = node.querySelector('.replay-exit-params');
    box.replaceChildren();
    const params = JSON.parse(select.selectedOptions[0].dataset.params);
    for (const [key, value] of Object.entries(params)) {
      const label = document.createElement('label');
      const input = document.createElement('input');
      label.textContent = key.replaceAll('_', ' ') + ' ';
      Object.assign(input, {type: 'number', step: 'any', min: '0', value});
      input.dataset.group = 'exit_parameters';
      input.dataset.key = key;
      label.append(input);
      box.append(label);
    }
  }

  function updateMode() {
    document.querySelector('#replay-add-variant').hidden = mode.value === 'SINGLE RUN';
    Array.from(container.children).forEach((node, i) => {
      node.hidden = i > 0 && mode.value === 'SINGLE RUN';
    });
  }

  function addVariant(saved) {
    if (container.children.length >= 10) return;
    const node = template.content.firstElementChild.cloneNode(true);
    container.append(node);
    node.querySelector('[data-key=name]').value = saved?.name || `Variant ${container.children.length}`;
    const policy = node.querySelector('[data-key=exit_policy]');
    if (saved) policy.value = saved.exit_policy;
    exitFields(node);
    policy.addEventListener('change', () => exitFields(node));
    node.querySelector('.replay-remove').addEventListener('click', () => {
      if (container.children.length > 1) node.remove();
      updateMode();
    });
    if (saved) {
      node.querySelector('[data-key=strategy]').value = saved.strategy;
      node.querySelectorAll('[data-group]').forEach(input => {
        const value = saved[input.dataset.group]?.[input.dataset.key];
        if (value !== undefined) {
          if (input.type === 'checkbox') input.checked = value;
          else input.value = value;
        }
      });
    }
    updateMode();
    return node;
  }

  function value(input) {
    return input.type === 'checkbox' ? input.checked
      : input.dataset.type === 'integer' ? Number(input.value) : input.value;
  }

  function config() {
    const nodes = Array.from(container.children);
    const variants = (mode.value === 'SINGLE RUN' ? nodes.slice(0, 1) : nodes).map(node => {
      const v = {
        name: node.querySelector('[data-key=name]').value,
        strategy: node.querySelector('[data-key=strategy]').value,
        exit_policy: node.querySelector('[data-key=exit_policy]').value,
        strategy_parameters: {}, exit_parameters: {}, risk_parameters: {pyramiding: false}
      };
      node.querySelectorAll('[data-group]').forEach(input => {
        v[input.dataset.group][input.dataset.key] = value(input);
      });
      return v;
    });
    const symbols = document.querySelector('#replay-candidates').value.trim();
    return {
      name: document.querySelector('#replay-name').value,
      start: document.querySelector('#replay-start').value + 'Z',
      end: document.querySelector('#replay-end').value + 'Z',
      universe_size: Number(document.querySelector('#replay-universe').value),
      minimum_quote_volume: document.querySelector('#replay-min-volume').value,
      spread_approximation_pct: document.querySelector('#replay-spread').value,
      dataset_role: document.querySelector('#replay-role').value,
      stale_position_policy: document.querySelector('#replay-stale-policy').value,
      fallback_5m: document.querySelector('#replay-fallback').checked,
      conservative: document.querySelector('#replay-conservative').checked,
      candidate_symbols: symbols ? symbols.split(',').map(s => s.trim().toUpperCase()) : null,
      variants
    };
  }

  async function post(url, body) {
    const response = await fetch(url, {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.dataset.csrf},
      body: JSON.stringify(body)
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error(data?.errors?.map(e => `${e.field}: ${e.message}`).join('; ')
        || `Replay action failed (${response.status}). Reload if the session expired.`);
      error.fields = data?.errors;
      throw error;
    }
    return data;
  }

  function applyConfig(c) {
    document.querySelector('#replay-name').value = c.name;
    document.querySelector('#replay-start').value = c.start.slice(0, 16);
    document.querySelector('#replay-end').value = c.end.slice(0, 16);
    document.querySelector('#replay-period').value = 'custom';
    document.querySelector('#replay-universe').value = c.universe_size;
    document.querySelector('#replay-role').value = c.dataset_role;
    document.querySelector('#replay-stale-policy').value = c.stale_position_policy || 'STRICT_FRESH_MARKS';
    document.querySelector('#replay-fallback').checked = c.fallback_5m;
    document.querySelector('#replay-conservative').checked = c.conservative;
    document.querySelector('#replay-min-volume').value = c.minimum_quote_volume;
    document.querySelector('#replay-spread').value = c.spread_approximation_pct;
    document.querySelector('#replay-candidates').value = (c.candidate_symbols || []).join(',');
    mode.value = c.variants.length > 1 ? 'COMPARE RUN' : 'SINGLE RUN';
    container.replaceChildren();
    c.variants.forEach(addVariant);
    updateMode();
  }

  function showConfigError(error) {
    const errors = error.fields || [{field: '$', message: error.message}];
    errorList.replaceChildren();
    errors.forEach(e => {
      const item = document.createElement('li');
      item.textContent = `${e.field}: ${e.message}`;
      errorList.append(item);
    });
    errorList.hidden = false;
    configStatus.textContent = 'Validation failed. The form was not changed and no run was started.';
  }

  async function configAction(action) {
    if (configBusy) return;
    configBusy = true;
    const buttons = Array.from(document.querySelectorAll('.replay-config-actions button'));
    const start = form.querySelector('button[type=submit]');
    [...buttons, start].forEach(b => { b.disabled = true; });
    errorList.hidden = true;
    configStatus.textContent = 'Validating replay configuration…';
    try {
      let raw;
      if (action === 'validate') {
        try { raw = JSON.parse(textarea.value); }
        catch (e) { throw new Error('Malformed JSON: ' + e.message); }
      } else raw = config();
      const result = await post(form.dataset.validateUrl, raw);
      const json = JSON.stringify(result.config, null, 2);
      textarea.value = json;
      if (action === 'validate') {
        applyConfig(result.config);
        document.querySelector('#replay-preset').value = '';
        configStatus.textContent = 'Valid config imported into the form. No run started. Click START REPLAY when ready.';
      } else if (action === 'copy') {
        try {
          if (!navigator.clipboard?.writeText) throw new Error('Clipboard API unavailable');
          await navigator.clipboard.writeText(json);
        } catch (_) {
          textarea.focus();
          textarea.select();
          if (!document.execCommand('copy')) throw new Error('Clipboard unavailable. Select the JSON and copy it manually.');
        }
        configStatus.textContent = 'Config JSON copied. No run started.';
      } else if (action === 'download') {
        const url = URL.createObjectURL(new Blob([json + '\n'], {type: 'application/json'}));
        const link = document.createElement('a');
        link.href = url;
        link.download = 'replay-config.json';
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        configStatus.textContent = 'Config JSON downloaded. No run started.';
      } else configStatus.textContent = 'Config JSON exported below. No run started.';
    } catch (error) { showConfigError(error); }
    finally {
      configBusy = false;
      [...buttons, start].forEach(b => { b.disabled = false; });
    }
  }

  ['validate', 'export', 'copy', 'download'].forEach(action => {
    document.querySelector('#replay-config-' + action).addEventListener('click', () => configAction(action));
  });
  form.addEventListener('submit', async e => {
    e.preventDefault();
    status.textContent = 'Queuing replay…';
    try {
      const result = await post(form.dataset.url, config());
      location.assign(result.url);
    } catch (error) { status.textContent = error.message; }
  });
  document.querySelector('#replay-add-variant').addEventListener('click', () => addVariant());
  mode.addEventListener('change', updateMode);
  document.querySelector('#replay-period').addEventListener('change', e => {
    const y = e.target.value;
    if (y === 'custom') return;
    document.querySelector('#replay-start').value = `${y}-01-01T00:00`;
    document.querySelector('#replay-end').value = y === '2026'
      ? new Date().toISOString().slice(0, 10) + 'T00:00' : `${Number(y) + 1}-01-01T00:00`;
  });
  document.querySelector('#replay-save-preset').addEventListener('click', async () => {
    try {
      await post('/api/replay/presets', {name: document.querySelector('#replay-preset-name').value, config: config()});
      status.textContent = 'Replay preset saved. Refresh to load it.';
    } catch (e) { status.textContent = e.message; }
  });
  const presets = JSON.parse(document.querySelector('#replay-preset-data').dataset.presets);
  const presetSelect = document.querySelector('#replay-preset');
  const builtins = [['baseline', 'Production Baseline', 'baseline_v1'], ['tp', 'Current + TP1.5', 'fixed_tp'],
    ['rtp', 'Current + TP0.5R', 'r_tp'], ['trail', 'Current + Trail1.5/0.5', 'trailing_pct']];
  builtins.forEach(([id, name]) => {
    const option = document.createElement('option');
    option.value = 'builtin:' + id;
    option.textContent = name;
    presetSelect.append(option);
  });
  function loadPreset(id) {
    if (id.startsWith('builtin:')) {
      container.replaceChildren();
      const node = addVariant();
      const preset = builtins.find(x => 'builtin:' + x[0] === id);
      node.querySelector('[data-key=name]').value = preset[1];
      node.querySelector('[data-key=exit_policy]').value = preset[2];
      exitFields(node);
      return;
    }
    const c = presets.find(p => String(p.id) === id)?.config;
    if (c) applyConfig(c);
  }
  presetSelect.addEventListener('change', e => loadPreset(e.target.value));
  addVariant({name: 'Production Baseline', strategy: 'watchlist_reference_v1', exit_policy: 'baseline_v1'});
  const selected = new URLSearchParams(location.search).get('preset');
  if (selected) { presetSelect.value = selected; loadPreset(selected); }
})();
