const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="repair-token"]').content;
const terminal = new Set(['completed', 'failed', 'cancelled']);
let selectedFile = null, currentTask = null, timer = null;
const labels = {pending:'准备中', running:'修复中', completed:'已提交修复', failed:'未完成修复', cancelled:'已停止'};

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {...options.headers, 'X-Repair-Token': token}});
  let data;
  try { data = await response.json(); } catch { throw new Error('服务没有返回有效结果，请检查服务终端。'); }
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : '请求无效，请检查文件与模型连接信息。');
    error.status = response.status;
    throw error;
  }
  return data;
}

function chooseFile(file) {
  if (!file || !file.name.toLowerCase().endsWith('.py') || file.size > 1024 * 1024 || !file.size) {
    $('form-error').textContent = '请选择一个非空的 .py 文件，大小不超过 1 MiB。';
    return;
  }
  selectedFile = file;
  $('file-label').textContent = file.name;
  $('file-detail').textContent = `${(file.size / 1024).toFixed(1)} KiB · 点击可更换文件`;
  $('form-error').textContent = '';
}
$('file').addEventListener('change', (e) => chooseFile(e.target.files[0]));
$('dropzone').addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('file').click(); } });
for (const event of ['dragenter', 'dragover']) $('dropzone').addEventListener(event, (e) => { e.preventDefault(); $('dropzone').classList.add('dragover'); });
for (const event of ['dragleave', 'drop']) $('dropzone').addEventListener(event, (e) => { e.preventDefault(); $('dropzone').classList.remove('dragover'); });
$('dropzone').addEventListener('drop', (e) => {
  if (e.dataTransfer.files.length !== 1) { $('form-error').textContent = '每次只能上传一个文件。'; return; }
  chooseFile(e.dataTransfer.files[0]);
});
// Prevent a dropped file outside the upload area from navigating away.
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => e.preventDefault());

function busy(value) {
  for (const input of $('repair-form').querySelectorAll('input, textarea, select, button')) input.disabled = value;
  $('cancel').hidden = !value;
  $('start').textContent = value ? '正在修复…' : '开始修复 ↗';
}

async function poll() {
  try {
    const task = await api(`/api/tasks/${currentTask}`);
    $('status').textContent = labels[task.status] || task.status;
    $('status').className = `badge ${task.status}`;
    const logs = $('logs'), nearEnd = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 35;
    logs.textContent = task.log || '准备本地工作区…';
    if (nearEnd) logs.scrollTop = logs.scrollHeight;
    const result = task.result;
    $('calls').textContent = result.api_calls ?? '—';
    $('elapsed').textContent = result.elapsed_seconds != null ? `${result.elapsed_seconds} s` : '—';
    $('exit-status').textContent = result.exit_status || '—';
    if (terminal.has(task.status)) {
      busy(false);
      sessionStorage.removeItem('repair-task-id');
      $('result-message').textContent = task.status === 'completed'
        ? '已生成提交补丁。请检查修复内容，并在你的实际使用场景中验证。'
        : task.error || '任务已停止，已生成的结果仍保存在输出目录中。';
      if (result.failure_category) $('result-message').textContent += ` 接口错误类别：${result.failure_category}`;
      try {
        const preview = await api(`/api/tasks/${currentTask}/preview`);
        $('patch').textContent = preview.patch || '本次没有生成有效提交补丁。';
        if (preview.truncated) $('patch').textContent += '\n…预览已截断，请下载完整补丁。';
        $('downloads').hidden = false;
        $('download-file').href = `/api/tasks/${currentTask}/download?kind=file`;
        $('download-patch').href = `/api/tasks/${currentTask}/download?kind=patch`;
      } catch { $('patch').textContent = '本次没有可预览的提交补丁。'; }
      return;
    }
    $('result-message').textContent = '正在读取、修改并检查代码。耗时取决于模型响应速度与任务复杂度。';
    timer = setTimeout(poll, 1200);
  } catch (error) {
    if (error.status === 404) {
      sessionStorage.removeItem('repair-task-id');
      busy(false);
      $('form-error').textContent = error.message;
      return;
    }
    $('form-error').textContent = `${error.message} 可刷新页面重新连接任务。`;
    // A temporary browser/network failure must not let the user unknowingly start a second task.
    timer = setTimeout(poll, 4000);
  }
}

$('repair-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!selectedFile) { $('form-error').textContent = '请先选择一个 Python 文件。'; return; }
  const body = new FormData($('repair-form'));
  body.set('file', selectedFile);
  busy(true);
  $('form-error').textContent = '';
  $('downloads').hidden = true;
  $('patch').textContent = '完成后显示提交补丁。';
  for (const id of ['calls', 'elapsed', 'exit-status']) $(id).textContent = '—';
  try {
    const task = await api('/api/tasks', {method:'POST', body});
    // Keep no API key in browser storage or form state after submission.
    $('api-key').value = '';
    currentTask = task.id;
    sessionStorage.setItem('repair-task-id', currentTask);
    clearTimeout(timer);
    await poll();
  } catch (error) { busy(false); $('form-error').textContent = error.message; }
});
$('cancel').addEventListener('click', async () => {
  $('cancel').disabled = true;
  try { await api(`/api/tasks/${currentTask}/cancel`, {method:'POST'}); }
  catch (error) { $('form-error').textContent = error.message; }
  finally { $('cancel').disabled = false; }
});

(async () => {
  try {
    const defaults = await api('/api/defaults');
    $('model').value = defaults.model;
    $('base-url').value = defaults.base_url;
    $('api').value = defaults.api;
    $('environment').textContent = `Python：${defaults.python}`;
    if (!defaults.supported) { busy(true); $('cancel').hidden = true; $('form-error').textContent = '本地执行需要 Linux/macOS；Windows 请在 WSL 或 Linux 服务器启动服务。'; return; }
    const previous = sessionStorage.getItem('repair-task-id');
    if (previous) {
      try {
        const task = await api(`/api/tasks/${previous}`);
        currentTask = task.id;
        busy(!terminal.has(task.status));
        await poll();
      } catch { sessionStorage.removeItem('repair-task-id'); }
    }
  } catch (error) { $('form-error').textContent = error.message; }
})();
