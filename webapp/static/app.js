(() => {
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const MODEL_LABELS = {'claude-opus-5-5': 'Claude Opus 5.5 (recommended)', 'claude-sonnet-5-5': 'Claude Sonnet 5.5 (lower cost)',
  'claude-haiku-5-5': 'Claude Haiku 5.5 (cheapest, least thorough)', 'claude-fable-5-1': 'Claude Fable 5.1 (most capable, highest cost)'};
let S = null;            // /api/session
let currentRun = null;   // run id shown in steps 4-5
let poll = null;

async function api(path, opts = {}) {
  const r = await fetch(path, {headers: opts.body && !(opts.body instanceof FormData) ? {'Content-Type': 'application/json'} : {}, ...opts});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}
function banner(msg, kind = 'error') {
  const b = $('#banner'); if (!msg) { b.hidden = true; return; }
  b.textContent = msg; b.className = 'banner' + (kind === 'info' ? ' info' : ''); b.hidden = false;
}
function setStep(id, {locked, done, state}) {
  const el = document.getElementById(id);
  if (locked !== undefined) el.classList.toggle('locked', locked);
  if (done !== undefined) el.classList.toggle('done', done);
  if (state !== undefined) el.querySelector('.state').textContent = state;
}

// ---------------------------------------------------------------- Step 1 - Salesforce
const apiSel = $('#sf-api');
apiSel.innerHTML = '<option value="latest">Latest supported by the org</option>' +
  Array.from({length: 10}, (_, i) => (68 - i).toFixed(1)).map((v) => `<option value="${v}">${v}</option>`).join('');
$('#sf-env').addEventListener('change', (e) => { $('#sf-domain-wrap').hidden = e.target.value !== 'custom'; });
$('#sf-login').addEventListener('submit', (e) => {
  if ($('#sf-env').value === 'custom' && !$('#sf-domain').value.trim()) { e.preventDefault(); banner('Enter your My Domain.'); }
  try { localStorage.setItem('rc-login', JSON.stringify({env: $('#sf-env').value, domain: $('#sf-domain').value, api: apiSel.value})); } catch (_) {}
});
try { const last = JSON.parse(localStorage.getItem('rc-login') || 'null');
  if (last) { $('#sf-env').value = last.env; $('#sf-domain').value = last.domain || ''; apiSel.value = last.api || 'latest';
    $('#sf-domain-wrap').hidden = last.env !== 'custom'; } } catch (_) {}
$('#sf-logout').addEventListener('click', async () => { await api('/api/sf/logout', {method: 'POST'}); await refresh(); });

// ---------------------------------------------------------------- Step 2 - Claude (account or API key)
function showTab(name) {
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('on', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach((p) => { p.hidden = p.dataset.panel !== name; });
}
document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => showTab(t.dataset.tab)));
async function connectClaude(e, path, body, input, label) {
  e.preventDefault();
  const btn = e.submitter, text = btn.textContent; btn.disabled = true; btn.textContent = label; banner();
  try { await api(path, {method: 'POST', body: JSON.stringify(body)}); input.value = ''; await refresh(); }
  catch (err) { banner(err.message); } finally { btn.disabled = false; btn.textContent = text; }
}
$('#cl-account').addEventListener('submit', (e) => connectClaude(e, '/api/claude/account', {token: $('#cl-token').value.trim()},
  $('#cl-token'), 'Verifying with Claude Code...'));
$('#cl-form').addEventListener('submit', (e) => connectClaude(e, '/api/claude/key', {api_key: $('#cl-key').value.trim()},
  $('#cl-key'), 'Verifying...'));
$('#cl-clear').addEventListener('click', async () => { await api('/api/claude/key', {method: 'DELETE'}); await refresh(); });

// ---------------------------------------------------------------- Step 3 - release notes
const drop = $('#drop');
['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', (e) => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));
$('#rn-file').addEventListener('change', (e) => e.target.files[0] && upload(e.target.files[0]));

function upload(file) {
  banner();
  const fd = new FormData(); fd.append('file', file);
  const xhr = new XMLHttpRequest();
  $('#rn-progress').hidden = false; $('#rn-bar').style.width = '0%'; $('#rn-msg').textContent = 'Uploading...';
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) {
    const p = Math.round(100 * e.loaded / e.total); $('#rn-bar').style.width = p + '%';
    $('#rn-msg').textContent = p < 100 ? `Uploading ${p}%` : 'Reading the release notes (about 15 s)...'; } };
  xhr.onload = async () => {
    $('#rn-progress').hidden = true; $('#rn-file').value = '';
    let data = {}; try { data = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status >= 400) banner(data.error || 'Upload failed'); else await refresh();
  };
  xhr.onerror = () => { $('#rn-progress').hidden = true; banner('Upload failed'); };
  xhr.open('POST', '/api/release-notes'); xhr.send(fd);
}

// ---------------------------------------------------------------- Step 4 - analysis
$('#run-noai').addEventListener('change', (e) => { $('#run-model').disabled = $('#run-budget').disabled = e.target.checked; });
$('#run-form').addEventListener('submit', async (e) => {
  e.preventDefault(); banner();
  const body = {model: $('#run-model').value, include_source: $('#run-source').checked, no_ai: $('#run-noai').checked,
    parallel: +$('#run-parallel').value || 4, budget_usd: S.claude.mode === 'account' ? null : (+$('#run-budget').value || null)};
  $('#run-start').disabled = true;
  try { const r = await api('/api/runs', {method: 'POST', body: JSON.stringify(body)}); showRun(r.id); loadRuns(); }
  catch (err) { banner(err.message); } finally { $('#run-start').disabled = false; }
});
$('#run-cancel').addEventListener('click', async () => { if (currentRun && confirm('Cancel this analysis?')) await api(`/api/runs/${currentRun}/cancel`, {method: 'POST'}); });
$('#run-new').addEventListener('click', () => { currentRun = null; history.replaceState(null, '', '/'); render(); });

const ICONS = {pending: '○', running: '', done: '✓', failed: '✕', warning: '!', skipped: '–'};
function showRun(id) { currentRun = id; history.replaceState(null, '', `/?run=${id}`); clearTimeout(poll); tick(); }
async function tick() {
  if (!currentRun) return;
  let st; try { st = await api(`/api/runs/${currentRun}`); } catch (err) { banner(err.message); currentRun = null; render(); return; }
  renderRun(st);
  if (['running', 'queued'].includes(st.status)) poll = setTimeout(tick, 1500); else loadRuns();
}
function elapsed(st) {
  if (!st.started) return '';
  const s = Math.round(((st.finished || Date.now() / 1000) - st.started)); return `${Math.floor(s / 60)} min ${s % 60} s`;
}
function fmtTokens(n) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : `${n}`;
}
function tokenText(t) {
  if (!t) return '';
  const total = t.input + t.output + t.cache_write + t.cache_read;
  if (!total) return '';
  const tip = `Input ${t.input.toLocaleString()} · Cache write ${t.cache_write.toLocaleString()} · ` +
    `Cache read ${t.cache_read.toLocaleString()} · Output ${t.output.toLocaleString()}`;
  return ` · <span class="tokens" title="${tip}">${fmtTokens(total)} tokens ` +
    `(${fmtTokens(t.input + t.cache_write)} new input · ${fmtTokens(t.cache_read)} cached · ${fmtTokens(t.output)} output)</span>`;
}
function renderRun(st) {
  $('#run-form').hidden = true; $('#run-live').hidden = false;
  const active = ['running', 'queued'].includes(st.status);
  $('#run-cancel').hidden = !active; $('#run-new').hidden = active;
  setStep('step-run', {locked: false, done: st.status === 'done', state:
    {running: 'Running...', queued: 'Starting...', done: 'Finished', failed: 'Failed', cancelled: 'Cancelled', interrupted: 'Interrupted'}[st.status] || st.status});
  $('#run-meta').innerHTML = [`<span><b>${esc(st.release)}</b> · ${esc(st.source)}</span>`,
    `<span>${esc(st.org.name || st.org.alias)} (${esc(st.org.username)})</span>`,
    st.options.no_ai ? '<span>rule-based only</span>' : `<span>${esc(MODEL_LABELS[st.options.model] || st.options.model)}</span>`,
    `<span>${elapsed(st)}</span>`,
    st.options.no_ai ? '' : st.options.engine === 'account'
      ? `<span>Claude account · API-equivalent usage $${(st.cost_usd || 0).toFixed(2)} (not billed)${tokenText(st.tokens)}</span>`
      : `<span>est. cost $${(st.cost_usd || 0).toFixed(2)}${st.options.budget_usd ? ` of $${st.options.budget_usd} cap` : ''}${tokenText(st.tokens)}</span>`].join('');
  $('#stages').innerHTML = st.stages.map((s) => `<li class="s-${s.status}"><span class="ic">${ICONS[s.status] ?? ''}</span>
    <span>${esc(s.label)}</span><span class="d">${esc(s.detail)}</span></li>`).join('');
  const p = st.progress || {};
  $('#ai-progress').hidden = !p.total;
  if (p.total) { $('#ai-bar').style.width = `${Math.round(100 * p.done / p.total)}%`;
    $('#ai-msg').textContent = `${p.done} / ${p.total} AI batches · ${p.features} features`; }
  const log = $('#log'); const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
  log.textContent = (st.log || []).map((l) => `${l.t}  ${l.msg}`).join('\n');
  if (atEnd) log.scrollTop = log.scrollHeight;
  if (st.error) banner(`Analysis failed: ${st.error}`);
  renderDocs(st);
}

// ---------------------------------------------------------------- Step 5 - documentation
function renderDocs(st) {
  const ready = st && st.status === 'done';
  setStep('step-docs', {locked: !ready, done: false, state: ready ? 'Ready' : 'Available when the analysis finishes'});
  if (!ready) return;
  const c = st.counts || {};
  $('#doc-summary').innerHTML = `<div class="counts">
    <div class="count c-High"><b>${c.High ?? 0}</b><span>High</span></div><div class="count c-Medium"><b>${c.Medium ?? 0}</b><span>Medium</span></div>
    <div class="count c-Low"><b>${c.Low ?? 0}</b><span>Low</span></div><div class="count c-NA"><b>${c['Not Applicable'] ?? 0}</b><span>Not applicable</span></div></div>
    ${st.headline ? `<p class="headline">${esc(st.headline)}</p>` : ''}`;
  const f = (st.files || []);
  const link = (name, label, view) => `<a href="/api/runs/${st.id}/files/${encodeURIComponent(name)}${view ? '?view=1' : ''}" ${view ? 'target="_blank" rel="noopener"' : ''}>${label}</a>`;
  const html = f.find((n) => n.endsWith('.html')), xlsx = f.find((n) => n.endsWith('.xlsx')), md = f.find((n) => n.endsWith('.md'));
  $('#doc-extras').innerHTML = 'Also available: ' + [html && link(html, 'Interactive report (HTML)', true), xlsx && link(xlsx, 'Action list (Excel)'),
    md && link(md, 'Markdown')].filter(Boolean).join('');
}
document.querySelectorAll('[data-doc]').forEach((btn) => btn.addEventListener('click', async () => {
  if (!currentRun) return;
  const label = btn.textContent; btn.disabled = true; btn.textContent = 'Generating...'; banner();
  try { const r = await api(`/api/runs/${currentRun}/documents`, {method: 'POST', body: JSON.stringify({kind: btn.dataset.doc, format: btn.dataset.fmt})});
    const a = document.createElement('a'); a.href = r.url; a.download = r.file; document.body.appendChild(a); a.click(); a.remove();
  } catch (err) { banner(err.message); } finally { btn.disabled = false; btn.textContent = label; }
}));

// ---------------------------------------------------------------- history
async function loadRuns() {
  const runs = await api('/api/runs').catch(() => []);
  $('#runs').innerHTML = runs.length ? runs.map((r) => `<tr><td>${esc(r.created)}</td><td>${esc(r.org?.name || r.org?.alias)}</td>
    <td>${esc(r.release)}</td><td><span class="pill p-${esc(r.status)}">${esc(r.status)}</span></td>
    <td>${r.counts ? `${r.counts.High} / ${r.counts.Medium} / ${r.counts.Low}` : ''}</td>
    <td><a data-open="${esc(r.id)}">Open</a>${['running', 'queued'].includes(r.status) ? '' : `<a data-del="${esc(r.id)}">Delete</a>`}</td></tr>`).join('')
    : '<tr><td colspan="6" class="muted">No analyses yet.</td></tr>';
  document.querySelectorAll('[data-open]').forEach((a) => a.onclick = () => { showRun(a.dataset.open); window.scrollTo({top: $('#step-run').offsetTop - 10, behavior: 'smooth'}); });
  document.querySelectorAll('[data-del]').forEach((a) => a.onclick = async () => {
    if (!confirm('Delete this analysis and its documents? This also removes the downloaded org metadata and source code.')) return;
    await api(`/api/runs/${a.dataset.del}`, {method: 'DELETE'}).catch((e) => banner(e.message));
    if (currentRun === a.dataset.del) { currentRun = null; render(); } loadRuns(); });
}

// ---------------------------------------------------------------- render session
function render() {
  const sf = S.salesforce, cl = S.claude, up = S.upload;
  $('#chips').innerHTML = `<span class="chip ${sf ? 'on' : ''}">Salesforce: ${sf ? esc(sf.org_name || sf.username) : 'not connected'}</span>
    <span class="chip ${cl.configured ? 'on' : ''}">Claude: ${cl.configured ? 'key set' : 'no key'}</span>`;
  // 1
  $('#sf-login').hidden = !!sf; $('#sf-connected').hidden = !sf;
  setStep('step-sf', {done: !!sf, state: sf ? `${sf.org_name || ''}${sf.is_sandbox ? ' · sandbox' : ''}` : 'Required'});
  if (sf) $('#sf-info').innerHTML = [['Org', sf.org_name], ['Edition', `${sf.edition || ''}${sf.is_sandbox ? ' (sandbox)' : ''}`],
    ['User', `${sf.display_name || ''} (${sf.username})`], ['Instance', `${sf.instance_url} ${sf.instance_name ? '· ' + sf.instance_name : ''}`],
    ['API version', sf.api_version], ['Connected', sf.connected_at]].map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('');
  // 2
  const modeLabel = cl.mode === 'account' ? 'Claude account (via Claude Code)' : 'Claude API key';
  setStep('step-claude', {locked: !sf, done: cl.configured,
    state: cl.configured ? `${modeLabel}${cl.source === 'environment' ? ' · from environment' : ''}` : 'Needed for AI analysis'});
  $('#cl-connected').hidden = !cl.configured; $('#cl-choose').hidden = cl.configured;
  $('#cl-clear').hidden = cl.source !== 'session';
  if (cl.configured) $('#cl-info').innerHTML = [['Connected with', modeLabel],
    ['Billing', cl.mode === 'account' ? "Counts against your Claude plan's usage limits" : 'Pay per use (API)'],
    ['Source', cl.source === 'environment' ? 'Environment variable (.env)' : 'Entered on this page (memory only)']]
    .map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('');
  if (!cl.account_available) {
    document.querySelector('[data-tab="account"]').disabled = true;
    document.querySelector('[data-tab="account"]').title = 'Claude Code is not installed in this environment (it is in the Docker image)';
    showTab('api');
  }
  $('#run-budget').closest('label').hidden = cl.mode === 'account';
  // 3
  setStep('step-notes', {locked: !sf, done: !!up, state: up ? `${up.release} · ${up.features} features` : 'Upload PDF or Word'});
  $('#rn-info').hidden = !up;
  if (up) $('#rn-info').innerHTML = [['File', up.filename], ['Release', up.release], ['Features found', up.features],
    ['Pages', up.pages ?? '-']].map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('');
  // 4
  $('#run-model').innerHTML = cl.models.map((m) => `<option value="${m}" ${m === cl.default_model ? 'selected' : ''}>${esc(MODEL_LABELS[m] || m)}</option>`).join('');
  const canRun = !!(sf && up);
  if (!currentRun) {
    $('#run-form').hidden = false; $('#run-live').hidden = true;
    setStep('step-run', {locked: !canRun, done: false, state: canRun ? 'Ready to start' : 'Needs steps 1-3'});
    renderDocs(null);
  } else { setStep('step-run', {locked: false}); }
}
async function refresh() {
  try { S = await api('/api/session'); } catch (err) { banner(err.message); return; }
  render();
}

(async function init() {
  const q = new URLSearchParams(location.search);
  if (q.get('login_error')) banner(`Salesforce login failed: ${q.get('login_error')}`);
  await refresh();
  if (q.get('run')) showRun(q.get('run')); else if (q.get('login_error')) history.replaceState(null, '', '/');
  loadRuns();
})();
})();
