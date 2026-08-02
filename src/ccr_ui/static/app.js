'use strict';

/* CCR 配置面板前端逻辑。
 * 关键约束：所有资源/请求用相对路径（./api/...），适配 code-server /proxy/<port>/ 代理。
 */

const state = {
  token: null,
  providers: [],
  models: [],
  preferredProvider: '',
  currentModel: '',
  editingId: null,
};

const $ = (sel) => document.querySelector(sel);

class AuthError extends Error {}

/* ---------------- 访问口令 ---------------- */

function getTokenFromUrl() {
  const params = new URLSearchParams(location.search);
  const t = params.get('t');
  if (t) {
    params.delete('t');
    const qs = params.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
  }
  return t;
}

function initToken() {
  const fromUrl = getTokenFromUrl();
  const stored = sessionStorage.getItem('ccr_ui_token');
  state.token = fromUrl || stored;
  if (fromUrl && fromUrl !== stored) {
    sessionStorage.setItem('ccr_ui_token', fromUrl);
  }
}

/* ---------------- API 封装 ---------------- */

async function api(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  let resp;
  try {
    resp = await fetch('./api' + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new Error('无法连接本服务：' + e.message);
  }
  let payload = null;
  try { payload = await resp.json(); } catch (e) { /* 忽略非 JSON 响应 */ }
  if (!resp.ok || !payload || payload.ok !== true) {
    const msg = (payload && payload.error) || ('HTTP ' + resp.status);
    if (resp.status === 401) throw new AuthError(msg);
    throw new Error(msg);
  }
  return payload.value;
}

/* ---------------- 渲染 ---------------- */

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function renderModels() {
  const sel = $('#model-select');
  sel.innerHTML = '';
  for (const p of state.providers) {
    if (!p.models || !p.models.length) continue;
    const group = document.createElement('optgroup');
    group.label = p.name + (p.name === state.preferredProvider ? '（默认）' : '');
    for (const m of p.models) {
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      if (m === state.currentModel) opt.selected = true;
      group.appendChild(opt);
    }
    sel.appendChild(group);
  }
  if (!state.currentModel && sel.options.length) sel.value = sel.options[0].value;
  $('#cur-provider').textContent = state.preferredProvider || '（未设置）';
  $('#current-model-line').textContent = state.currentModel
    ? '当前模型：' + state.currentModel
    : '当前模型：（未设置）';
}

function renderProviders() {
  const tbody = $('#provider-tbody');
  tbody.innerHTML = '';
  for (const p of state.providers) {
    const isDefault = p.name === state.preferredProvider;
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td><b>' + esc(p.name) + '</b>' + (isDefault ? ' <span class="badge green">默认</span>' : '') + '</td>' +
      '<td class="small">' + esc(p.type) + '</td>' +
      '<td class="mono small">' + esc(p.baseurl) + '</td>' +
      '<td class="small">' + esc(p.models.join(', ')) + '</td>' +
      '<td class="mono small">' + esc(p.key_masked || (p.has_key ? '' : '（未设置）')) + '</td>' +
      '<td class="ops">' +
      '<button class="btn small" data-act="default" data-id="' + esc(p.id) + '"' + (isDefault ? ' disabled' : '') + '>设为默认</button>' +
      '<button class="btn small" data-act="edit" data-id="' + esc(p.id) + '">编辑</button>' +
      '<button class="btn small danger" data-act="del" data-id="' + esc(p.id) + '">删除</button>' +
      '</td>';
    tbody.appendChild(tr);
  }
}

/* ---------------- toast ---------------- */

let toastTimer = null;
function toast(msg, kind) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast ' + (kind || 'info');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), kind === 'err' ? 6000 : 3500);
}

/* ---------------- 数据加载 / 登录 ---------------- */

async function bootstrap() {
  try {
    const data = await api('GET', '/bootstrap');
    state.providers = data.providers || [];
    state.models = data.models || [];
    state.preferredProvider = data.preferredProvider || '';
    state.currentModel = data.currentModel || '';
    renderModels();
    renderProviders();
    $('#conn-status').textContent = '已连接';
    $('#conn-status').classList.add('green');
  } catch (e) {
    if (e instanceof AuthError) { showLogin(); return; }
    $('#conn-status').textContent = '连接失败';
    toast('加载失败：' + e.message, 'err');
  }
}

function showLogin() {
  $('#login-card').classList.remove('hidden');
  $('#main').classList.add('hidden');
  $('#conn-status').textContent = '未授权';
  $('#conn-status').classList.remove('green');
}

function showMain() {
  $('#login-card').classList.add('hidden');
  $('#main').classList.remove('hidden');
}

/* ---------------- 写操作 ---------------- */

function setBusy(busy) {
  $('#switch-btn').disabled = busy;
  $('#add-btn').disabled = busy;
  document.querySelectorAll('.ops button').forEach((b) => { b.disabled = busy; });
  document.querySelectorAll('#edit-modal button').forEach((b) => { b.disabled = busy; });
}

async function run(opName, promise) {
  setBusy(true);
  toast('正在保存（gateway 可能重载数秒，请稍候）…');
  try {
    await promise;
    toast('✓ ' + opName + ' 成功');
    await bootstrap();
  } catch (e) {
    toast('✗ ' + opName + '失败：' + e.message, 'err');
  } finally {
    setBusy(false);
  }
}

/* ---------------- 事件绑定 ---------------- */

function bindEvents() {
  $('#login-btn').addEventListener('click', async () => {
    const t = $('#login-token').value.trim();
    const errEl = $('#login-error');
    if (!t) {
      errEl.textContent = '请输入访问口令';
      errEl.classList.remove('hidden');
      return;
    }
    state.token = t;
    sessionStorage.setItem('ccr_ui_token', t);
    errEl.classList.add('hidden');
    try {
      await api('GET', '/health');
      showMain();
      bootstrap();
    } catch (e) {
      errEl.textContent = '口令不正确或服务不可用：' + e.message;
      errEl.classList.remove('hidden');
      sessionStorage.removeItem('ccr_ui_token');
    }
  });

  $('#switch-btn').addEventListener('click', () => {
    const model = $('#model-select').value;
    if (!model) return;
    run('模型切换', api('POST', '/model/switch', { model }));
  });

  $('#add-btn').addEventListener('click', () => {
    const body = {
      name: $('#p-name').value.trim(),
      baseurl: $('#p-baseurl').value.trim(),
      type: $('#p-type').value,
      models: $('#p-models').value,
      apikey: $('#p-apikey').value.trim(),
    };
    if (!body.name || !body.baseurl || !body.apikey) {
      toast('请填写名称 / Base URL / API Key', 'err');
      return;
    }
    run('添加 Provider', api('POST', '/providers', body).then(() => {
      $('#p-name').value = '';
      $('#p-baseurl').value = '';
      $('#p-type').value = '';
      $('#p-models').value = '';
      $('#p-apikey').value = '';
    }));
  });

  $('#provider-tbody').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;
    const provider = state.providers.find((p) => p.id === id);
    if (!provider) return;
    if (act === 'default') {
      run('设为默认', api('POST', '/providers/' + encodeURIComponent(id) + '/default'));
    } else if (act === 'edit') {
      openEdit(provider);
    } else if (act === 'del') {
      if (!window.confirm('确定删除 Provider「' + provider.name + '」？\n删除会重载 gateway（数秒），该 Provider 下的模型将不可用。')) return;
      run('删除 Provider', api('DELETE', '/providers/' + encodeURIComponent(id)));
    }
  });

  $('#edit-cancel').addEventListener('click', closeEdit);
  $('#edit-save').addEventListener('click', () => {
    const body = {
      name: $('#e-name').value.trim(),
      baseurl: $('#e-baseurl').value.trim(),
      type: $('#e-type').value,
      models: $('#e-models').value,
    };
    const apikey = $('#e-apikey').value.trim();
    if (apikey) body.apikey = apikey;
    run('保存 Provider', api('PUT', '/providers/' + encodeURIComponent(state.editingId), body).then(closeEdit));
  });
}

function openEdit(p) {
  state.editingId = p.id;
  $('#edit-name-label').textContent = '· ' + p.name;
  $('#e-name').value = p.name;
  $('#e-baseurl').value = p.baseurl;
  $('#e-type').value = p.type;
  $('#e-models').value = p.models.join(', ');
  $('#e-apikey').value = '';
  $('#edit-modal').classList.remove('hidden');
}

function closeEdit() {
  $('#edit-modal').classList.add('hidden');
  state.editingId = null;
}

/* ---------------- 启动 ---------------- */

async function start() {
  initToken();
  bindEvents();
  if (!state.token) {
    showLogin();
    return;
  }
  showMain();
  await bootstrap();
}

start();
