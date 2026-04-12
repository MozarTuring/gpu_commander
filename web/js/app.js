const API = '';
let machines = [];
let pollTimer = null;
let _authToken = localStorage.getItem('gpu_cmd_token') || null;
let _currentUser = null;
let _serverVersion = null;  // set on first poll, triggers reload if it changes

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
        if (btn.dataset.tab === 'llm') loadLLMTab();
        if (btn.dataset.tab === 'tasks') loadTasks();
        if (btn.dataset.tab === 'settings') loadSettings();
    });
});

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------
async function api(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (_authToken) headers['Authorization'] = `Bearer ${_authToken}`;
    const resp = await fetch(`${API}${path}`, { headers: { ...headers, ...opts.headers }, ...opts });
    if (resp.status === 401) {
        showLoginOverlay();
        throw new Error('Not authenticated');
    }
    if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`${resp.status}: ${text}`);
    }
    return resp.json();
}

// ---------------------------------------------------------------------------
// GPU Bar color helper
// ---------------------------------------------------------------------------
function barClass(pct) {
    if (pct >= 90) return 'crit';
    if (pct >= 70) return 'warn';
    return '';
}

function tempColor(c) {
    if (c >= 85) return 'var(--red)';
    if (c >= 70) return 'var(--yellow)';
    return 'var(--text-dim)';
}

// ---------------------------------------------------------------------------
// Render machine cards
// ---------------------------------------------------------------------------
function renderMachines(data) {
    machines = data;
    const grid = document.getElementById('machinesGrid');
    grid.innerHTML = '';

    populateMachineSelects(data);

    data.forEach(m => {
        const card = document.createElement('div');
        const online = m.online;
        card.className = `machine-card ${online ? 'is-online' : 'is-offline'}`;

        const statusClass = online ? 'online' : 'offline';
        const statusText = online ? 'Online' : 'Offline';

        let gpuHtml = '';
        if (m.gpu_cache && m.gpu_cache.gpus) {
            gpuHtml = '<div class="gpu-list">' + m.gpu_cache.gpus.map(g => {
                const memPct = g.memory_total_mib > 0
                    ? Math.round(g.memory_used_mib / g.memory_total_mib * 100)
                    : 0;
                const gpuUtilClass = barClass(g.utilization_gpu_pct);
                const memUtilClass = barClass(memPct);

                let procHtml = '';
                if (g.processes && g.processes.length > 0) {
                    procHtml = `<div class="gpu-processes"><table>
                        <tr><th>PID</th><th>Process</th><th>Memory</th></tr>
                        ${g.processes.map(p => `<tr><td>${p.pid}</td><td>${p.name}</td><td>${p.used_memory_mib} MiB</td></tr>`).join('')}
                    </table></div>`;
                }

                return `<div class="gpu-item">
                    <div class="gpu-item-header">
                        <span class="gpu-item-name">GPU ${g.index}: ${g.name}</span>
                        <span class="gpu-item-temp" style="color:${tempColor(g.temperature_c)}">${g.temperature_c}°C${g.power_draw_w != null ? ` · ${g.power_draw_w.toFixed(0)}W` : ''}</span>
                    </div>
                    <div class="bar-row">
                        <span class="bar-label">GPU</span>
                        <div class="bar-track"><div class="bar-fill gpu-util ${gpuUtilClass}" style="width:${g.utilization_gpu_pct}%"></div></div>
                        <span class="bar-value">${g.utilization_gpu_pct}%</span>
                    </div>
                    <div class="bar-row">
                        <span class="bar-label">VRAM</span>
                        <div class="bar-track"><div class="bar-fill mem-util ${memUtilClass}" style="width:${memPct}%"></div></div>
                        <span class="bar-value">${g.memory_used_mib}/${g.memory_total_mib}</span>
                    </div>
                    ${procHtml}
                </div>`;
            }).join('') + '</div>';
        } else if (online) {
            gpuHtml = '<div class="empty-state">Fetching GPU data...</div>';
        } else {
            gpuHtml = '<div class="empty-state">Machine offline</div>';
        }

        let meta = '';
        if (m.gpu_cache) {
            meta = `<span style="font-size:11px; color:var(--text-dim)">Driver ${m.gpu_cache.driver_version} · CUDA ${m.gpu_cache.cuda_version}</span>`;
        }

        card.innerHTML = `
            <div class="machine-header">
                <div>
                    <div class="machine-name">${displayName(m.name)}</div>
                    <div class="machine-desc">${m.description || m.host}</div>
                </div>
                <span class="status-badge ${statusClass}"><span class="status-dot"></span>${statusText}</span>
            </div>
            ${gpuHtml}
            ${meta ? `<div style="margin-top:10px">${meta}</div>` : ''}
        `;
        grid.appendChild(card);
    });

    document.getElementById('lastUpdated').textContent = `Updated ${new Date().toLocaleTimeString()}`;

    const activeTab = document.querySelector('.tab.active')?.dataset.tab;
    if (activeTab === 'llm') loadLLMTab();
    if (activeTab === 'tasks') loadTasks();
}

// ---------------------------------------------------------------------------
// Populate machine dropdowns
// ---------------------------------------------------------------------------
function populateMachineSelects(data) {
    const selectors = ['cmdMachine', 'taskMachine', 'taskFilterMachine'];
    selectors.forEach(id => {
        const el = document.getElementById(id);
        const current = el.value;
        const isFilter = id === 'taskFilterMachine';

        el.innerHTML = isFilter ? '<option value="">All machines</option>' : '';
        data.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m.name;
            opt.textContent = `${displayName(m.name)}${m.online ? '' : ' (offline)'}`;
            el.appendChild(opt);
        });
        if (current) el.value = current;
    });
}

// ---------------------------------------------------------------------------
// Command execution
// ---------------------------------------------------------------------------
document.getElementById('cmdRun').addEventListener('click', runCommand);
document.getElementById('cmdInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') runCommand();
});

async function runCommand() {
    const machine = document.getElementById('cmdMachine').value;
    const command = document.getElementById('cmdInput').value.trim();
    const output = document.getElementById('cmdOutput');
    const btn = document.getElementById('cmdRun');

    if (!machine || !command) return;

    btn.disabled = true;
    output.innerHTML = '<span class="spinner"></span> Running...';

    try {
        const result = await api(`/api/machines/${machine}/execute`, {
            method: 'POST',
            body: JSON.stringify({ command, timeout: 300 }),
        });

        if (result.exit_code === 0) {
            output.textContent = result.stdout || '(no output)';
        } else {
            output.innerHTML = `<span class="error">Exit code: ${result.exit_code}\n${escapeHtml(result.stderr)}</span>\n${escapeHtml(result.stdout)}`;
        }
    } catch (err) {
        output.innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
    } finally {
        btn.disabled = false;
    }
}

// ---------------------------------------------------------------------------
// Task queue
// ---------------------------------------------------------------------------
document.getElementById('taskSubmit').addEventListener('click', submitTask);
document.getElementById('taskInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') submitTask();
});

async function submitTask() {
    const machine = document.getElementById('taskMachine').value;
    const command = document.getElementById('taskInput').value.trim();
    if (!machine || !command) return;

    const btn = document.getElementById('taskSubmit');
    btn.disabled = true;

    try {
        await api(`/api/machines/${machine}/tasks/submit`, {
            method: 'POST',
            body: JSON.stringify({ command }),
        });
        document.getElementById('taskInput').value = '';
        loadTasks();
    } catch (err) {
        alert('Failed to submit task: ' + err.message);
    } finally {
        btn.disabled = false;
    }
}

async function loadTasks() {
    const filterMachine = document.getElementById('taskFilterMachine').value;
    const container = document.getElementById('taskTableContainer');

    const machinesToQuery = filterMachine
        ? [machines.find(m => m.name === filterMachine)].filter(Boolean)
        : machines.filter(m => m.online);

    if (machinesToQuery.length === 0) {
        container.innerHTML = '<div class="empty-state">No online machines</div>';
        return;
    }

    try {
        const results = await Promise.allSettled(
            machinesToQuery.map(async m => {
                const tasks = await api(`/api/machines/${m.name}/tasks`);
                return tasks
                    .filter(t => _currentUser?.role === 'admin' || t.submitted_by === _currentUser?.username)
                    .map(t => ({ ...t, machine: m.name }));
            })
        );

        const allTasks = results
            .filter(r => r.status === 'fulfilled')
            .flatMap(r => r.value)
            .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));

        if (allTasks.length === 0) {
            container.innerHTML = '<div class="empty-state">No tasks yet</div>';
            return;
        }

        container.innerHTML = `<table class="task-table">
            <thead><tr>
                <th>ID</th><th>Machine</th><th>Command</th><th>Status</th><th>Created</th><th></th>
            </tr></thead>
            <tbody>
                ${allTasks.map(t => `<tr>
                    <td style="font-family:var(--mono); font-size:12px">${t.id}</td>
                    <td>${displayName(t.machine)}</td>
                    <td style="font-family:var(--mono); font-size:12px; max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap">${escapeHtml(t.command)}</td>
                    <td><span class="task-status ${t.status}">${t.status}</span></td>
                    <td style="font-size:12px; color:var(--text-dim)">${t.created_at ? new Date(t.created_at * 1000).toLocaleString() : '—'}</td>
                    <td>${(t.status === 'queued' || t.status === 'running') ? `<button class="cancel-btn" onclick="cancelTask('${t.machine}','${t.id}')">Cancel</button>` : ''}</td>
                </tr>`).join('')}
            </tbody>
        </table>`;
    } catch (err) {
        container.innerHTML = `<div class="empty-state">Error loading tasks: ${escapeHtml(err.message)}</div>`;
    }
}

async function cancelTask(machine, taskId) {
    if (!confirm(`Cancel task ${taskId}?`)) return;
    try {
        await api(`/api/machines/${machine}/tasks/${taskId}`, { method: 'DELETE' });
        loadTasks();
    } catch (err) {
        alert('Failed to cancel: ' + err.message);
    }
}

document.getElementById('taskFilterMachine').addEventListener('change', loadTasks);

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------
async function refresh() {
    try {
        const data = await api('/api/machines');
        renderMachines(data);
    } catch (err) {
        console.error('Failed to refresh:', err);
    }
}

async function checkVersion() {
    try {
        const data = await fetch('/api/version').then(r => r.json());
        if (_serverVersion === null) {
            _serverVersion = data.version;
        } else if (data.version !== _serverVersion) {
            window.location.reload();
        }
    } catch (_) {}
}

function startPolling(interval = 10000) {
    refresh();
    checkVersion();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => {
        refresh();
        checkVersion();
        const activeTab = document.querySelector('.tab.active')?.dataset.tab;
        if (activeTab === 'tasks') loadTasks();
        if (activeTab === 'llm') loadLLMTab();
    }, interval);
}

// ---------------------------------------------------------------------------
// Util
// ---------------------------------------------------------------------------
function displayName(name) {
    return (name || '').replace(/^custodian2/, '');
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str || '';
    return div.innerHTML;
}

// ---------------------------------------------------------------------------
// LLM Services
// ---------------------------------------------------------------------------
let _llmModels = (() => { try { const m = JSON.parse(localStorage.getItem('gpu_cmd_llm_models') || '[]'); return m.every(x => x.type) ? m : []; } catch(_) { return []; } })();
let _llmRunning = {};     // { machineName: [...containers] }

async function loadLLMTab() {
    const grid = document.getElementById('llmGrid');
    const llmMachines = machines.filter(m => m.vllm_service_dir);

    if (llmMachines.length === 0) {
        grid.innerHTML = '<div class="empty-state">No machines with LLM services configured</div>';
        return;
    }

    // Show loading state only if no cached models available
    if (_llmModels.length === 0 && !grid.querySelector('.panel')) {
        grid.innerHTML = '<div class="empty-state">Loading LLM services...</div>';
    }

    // Fetch models from first online vllm machine (same repo on all machines)
    const sourceMachine = llmMachines.find(m => m.online);
    if (sourceMachine && _llmModels.length === 0) {
        _llmModels = await api(`/api/machines/${sourceMachine.name}/llm/models`).catch(() => []);
        if (_llmModels.length > 0) localStorage.setItem('gpu_cmd_llm_models', JSON.stringify(_llmModels));
    }

    // Fetch running containers + idle status in parallel
    const [runningResults, idleStatus] = await Promise.all([
        Promise.all(
            llmMachines.map(m => m.online
                ? api(`/api/machines/${m.name}/llm/running`).then(r => [m.name, r]).catch(() => [m.name, []])
                : Promise.resolve([m.name, []])
            )
        ),
        api('/api/llm/idle-status').catch(() => ({ containers: {}, timeout_hours: 2 })),
    ]);
    runningResults.forEach(([name, containers]) => { _llmRunning[name] = containers; });
    window._idleStatus = idleStatus;

    grid.innerHTML = '';

    // Deploy panel
    const deployPanel = document.createElement('div');
    deployPanel.className = 'cmd-panel';
    deployPanel.style.marginBottom = '20px';
    deployPanel.innerHTML = renderDeployPanel(llmMachines);
    grid.appendChild(deployPanel);

    const accessHintHtml = `
        <div style="margin-bottom:12px; padding:12px 14px; background:var(--bg); border-radius:6px; border-left:3px solid var(--accent); font-family:var(--mono); font-size:11px; color:var(--text-mid); line-height:2">
            <span style="color:var(--accent); font-family:var(--sans); font-size:11px; text-transform:uppercase; letter-spacing:.08em; font-weight:600">How to access a running service</span><br>
            ssh -f -N -L <span style="color:var(--accent)">&lt;local_port&gt;</span>:localhost:<span style="color:var(--accent)">&lt;remote_port&gt;</span> ${_currentUser?.stellar_account || '[stellar_account]'}@<span style="color:var(--accent)">&lt;machine_name&gt;</span>.stellar.research.liu.se<br>
            curl http://localhost:<span style="color:var(--accent)">&lt;local_port&gt;</span>/v1/models
        </div>`;

    // Per-machine running services
    const runningPanel = document.createElement('div');
    runningPanel.className = 'panel';
    runningPanel.innerHTML = `
        <div class="panel-header">
            <span class="panel-label">Running Services</span>
        </div>
        ${llmMachines.map(m => renderRunningSection(m)).join('')}
    `;
    grid.appendChild(runningPanel);

    // My Deploy Tasks panel
    const tasksPanel = document.createElement('div');
    tasksPanel.className = 'panel';
    tasksPanel.style.marginBottom = '16px';
    tasksPanel.id = 'llm-tasks-panel';
    tasksPanel.innerHTML = `
        <div class="panel-header"><span class="panel-label">Deploying Tasks</span></div>
        ${accessHintHtml}
        <div id="llm-tasks-body"><div class="empty-state">No deploying tasks</div></div>
    `;
    grid.insertBefore(tasksPanel, deployPanel);
    loadDeployTasks();

    // Restore previously selected model (survives poll refreshes)
    const modelSel = document.getElementById('llm-model-select');
    if (modelSel) {
        if (_selectedModel && modelSel.querySelector(`option[value="${CSS.escape(_selectedModel)}"]`)) {
            modelSel.value = _selectedModel;
        }
        modelSel.addEventListener('change', () => {
            _selectedModel = modelSel.value;
            updateMachineTable(llmMachines);
        });
        updateMachineTable(llmMachines);
    }
}

function renderDeployPanel(llmMachines) {
    const modelOptions = _llmModels.map(m => {
        const tag = m.type === 'whisper' ? ` [whisper${m.language ? '·' + m.language : ''}]` : '';
        return `<option value="${escapeHtml(m.name)}">${escapeHtml(m.name)}${tag} — ${escapeHtml(m.model)}</option>`;
    }).join('');

    const machineOptions = `<option value="auto">Auto</option>` +
        llmMachines.map(m => `<option value="${escapeHtml(m.name)}">${escapeHtml(displayName(m.name))}</option>`).join('');

    return `
        <h3 style="margin-bottom:14px">Deploy Model</h3>
        <div class="cmd-row" style="margin-bottom:12px; gap:10px">
            <select id="llm-model-select" style="flex:2">${modelOptions}</select>
            <select id="llm-machine-select" style="flex:1">${machineOptions}</select>
        </div>
        <div id="llm-already-running" style="display:none; margin-bottom:10px; padding:8px 14px; background:var(--green-lo); border:1px solid rgba(16,217,160,.3); border-radius:8px; font-size:13px; color:var(--green)"></div>
        <div id="llm-machine-table" style="margin-bottom:12px; display:none"></div>
        <!-- <div class="cmd-row" style="margin-bottom:8px">
            <label style="font-size:13px; color:var(--fg2); cursor:pointer">
                <input type="checkbox" id="llm-force-build" style="margin-right:6px">
                Rebuild image (FORCE_BUILD)
            </label>
        </div> -->
        <div class="cmd-row">
            <button id="llm-deploy-btn" onclick="deployLLM()">Deploy</button>
        </div>
        <div class="cmd-output" id="llm-output" style="display:none; margin-top:10px"></div>
    `;
}

let _bestMachine = null;
let _bestGpu = null;
let _selectedModel = null;

function updateMachineTable(llmMachines) {
    const modelSel = document.getElementById('llm-model-select');
    const machineSel = document.getElementById('llm-machine-select');
    const tableDiv = document.getElementById('llm-machine-table');
    if (!modelSel || !tableDiv) return;

    const model = _llmModels.find(m => m.name === modelSel.value);
    if (!model) { tableDiv.innerHTML = ''; return; }

    _bestMachine = null;
    _bestGpu = null;

    if (model.type === 'whisper') {
        // Whisper: no memory check, just pick first online machine
        for (const m of llmMachines) {
            if (m.online) { _bestMachine = m.name; break; }
        }
        tableDiv.style.display = 'none';
    } else {
        const memUtil = model.memory_utilization ?? 0.85;
        let bestFree = -1;

        // Expand each machine into one row per GPU
        const rows = [];
        for (const m of llmMachines) {
            const gpus = m.gpu_cache?.gpus ?? [];
            if (!m.online || gpus.length === 0) {
                rows.push({ machine: m.name, gpuIdx: '—', required: null, free: null, fits: false, status: 'offline' });
                continue;
            }
            for (const gpu of gpus) {
                const required = Math.round(memUtil * gpu.memory_total_mib);
                const free = gpu.memory_free_mib;
                const fits = free >= required;
                if (free > bestFree) {
                    bestFree = free;
                    _bestMachine = m.name;
                    _bestGpu = gpu.index;
                }
                rows.push({ machine: m.name, gpuIdx: gpu.index, required, free, fits, status: fits ? 'fits' : 'no fit' });
            }
        }

        const bestFits = rows.find(r => r.machine === _bestMachine && r.gpuIdx === _bestGpu)?.fits ?? false;

        tableDiv.style.display = 'block';
        tableDiv.innerHTML = `<table class="task-table"><thead><tr>
            <th>Machine</th><th>GPU</th><th>Required</th><th>Free</th><th>Status</th>
        </tr></thead><tbody>${rows.map(r => `<tr${r.machine === _bestMachine && r.gpuIdx === _bestGpu ? ' style="background:rgba(52,211,153,0.05)"' : ''}>
            <td>${displayName(r.machine)}</td>
            <td style="font-family:var(--mono)">${r.gpuIdx}</td>
            <td style="font-family:var(--mono)">${r.required != null ? r.required + ' MiB' : '—'}</td>
            <td style="font-family:var(--mono); color:${r.free != null ? (r.fits ? 'var(--green)' : 'var(--red)') : 'inherit'}">${r.free != null ? r.free + ' MiB' : '—'}</td>
            <td><span class="task-status ${r.fits ? 'completed' : r.status === 'offline' ? 'cancelled' : 'failed'}">${r.status}${r.machine === _bestMachine && r.gpuIdx === _bestGpu ? ' ★' : ''}</span></td>
        </tr>`).join('')}</tbody></table>`;

        const deployBtn = document.getElementById('llm-deploy-btn');
        if (deployBtn && _bestMachine) {
            deployBtn.disabled = false;
            deployBtn.title = bestFits
                ? `Will deploy on ${displayName(_bestMachine)} GPU${_bestGpu}`
                : `Will queue on ${displayName(_bestMachine)} GPU${_bestGpu} — waiting for free memory`;
        }
    }

    // Check if this model is already running somewhere
    const containerName = model.container_name || model.name;
    let alreadyRunning = null;
    for (const [machineName, containers] of Object.entries(_llmRunning)) {
        const match = containers.find(c => c.name === containerName);
        if (match) { alreadyRunning = { machine: machineName, container: match }; break; }
    }

    const alreadyEl = document.getElementById('llm-already-running');
    if (alreadyEl) {
        if (alreadyRunning) {
            const owner = alreadyRunning.container.owner;
            alreadyEl.style.display = 'block';
            alreadyEl.innerHTML = `Already running on <strong>${escapeHtml(displayName(alreadyRunning.machine))}</strong>`
                + (owner ? ` · deployed by <strong>${escapeHtml(owner)}</strong>` : '');
        } else {
            alreadyEl.style.display = 'none';
        }
    }

}

function renderRunningSection(m) {
    const containers = _llmRunning[m.name] || [];
    const idleData = window._idleStatus?.containers ?? {};
    const timeoutHours = window._idleStatus?.timeout_hours ?? 2;
    const rowsHtml = containers.length === 0
        ? `<div style="color:var(--text-dim); font-size:13px; margin-bottom:12px">No running containers</div>`
        : `<table class="task-table" style="margin-bottom:12px"><thead><tr><th>Container</th><th>Deployed by</th><th>Status</th><th>Ports</th><th>Idle</th><th></th></tr></thead><tbody>
            ${containers.map(c => {
                const key = `${m.name}:${c.name}`;
                const idle = idleData[key];
                let idleHtml = '<span style="color:var(--text-dim)">—</span>';
                if (idle) {
                    const idleMin = idle.idle_minutes;
                    const stopInMin = Math.round(idle.will_stop_in_seconds / 60);
                    const pct = Math.min(100, (idleMin / (timeoutHours * 60)) * 100);
                    const color = pct > 80 ? 'var(--red)' : pct > 50 ? 'var(--yellow)' : 'var(--text-dim)';
                    idleHtml = `<span style="color:${color}; font-size:12px">${idleMin}m idle · stops in ${stopInMin}m</span>`;
                }
                const isOwner = c.owner === _currentUser?.username;
                const stopBtn = isOwner || _currentUser?.role === 'admin'
                    ? `<button class="cancel-btn" onclick="stopContainer('${escapeHtml(m.name)}','${escapeHtml(c.name)}')">Stop</button>`
                    : '';
                return `<tr>
                    <td style="font-family:var(--mono); font-size:12px">${escapeHtml(c.name)}</td>
                    <td style="font-size:12px; color:var(--text-dim)">${escapeHtml(c.owner || '—')}</td>
                    <td style="font-size:12px">${escapeHtml(c.status)}</td>
                    <td style="font-family:var(--mono); font-size:12px">${escapeHtml(c.ports)}</td>
                    <td>${idleHtml}</td>
                    <td>${stopBtn}</td>
                </tr>`;
            }).join('')}
           </tbody></table>`;
    return `<div style="margin-bottom:12px">
        <div style="font-size:12px; color:var(--text-dim); margin-bottom:6px; text-transform:uppercase; letter-spacing:.05em">${escapeHtml(displayName(m.name))}</div>
        ${rowsHtml}
    </div>`;
}

async function stopContainer(machineName, container) {
    if (!confirm(`Stop and remove container "${container}"?`)) return;
    try {
        await api(`/api/machines/${machineName}/llm/stop`, {
            method: 'POST',
            body: JSON.stringify({ container }),
        });
        _llmRunning[machineName] = (_llmRunning[machineName] || []).filter(c => c.name !== container);
        loadLLMTab();
    } catch (err) {
        alert(`Failed to stop ${container}: ${err.message}`);
    }
}

async function deployLLM() {
    const model = document.getElementById('llm-model-select')?.value;
    const machineChoice = document.getElementById('llm-machine-select')?.value || 'auto';
    const targetMachine = machineChoice === 'auto' ? _bestMachine : machineChoice;
    const btn = document.getElementById('llm-deploy-btn');
    const output = document.getElementById('llm-output');
    if (!model || !targetMachine) return;

    btn.disabled = true;
    output.style.display = 'block';
    output.innerHTML = '<span class="spinner"></span> Submitting deploy task...';

    try {
        const forceBuild = document.getElementById('llm-force-build')?.checked || false;
        const body = { model, force_build: forceBuild };
        const task = await api(`/api/machines/${targetMachine}/llm/deploy`, {
            method: 'POST',
            body: JSON.stringify(body),
        });
        const gpuInfo = '';
        const queueMsg = task.memory_insufficient
            ? `\n\n⏳ GPU memory is full. Task will deploy automatically once memory frees up (checks every 60s). You can cancel it below in "My Deploy Tasks".`
            : `\n\nDeploy started. Track progress below in "My Deploy Tasks".`;
        output.textContent = `Task submitted — ID: ${task.id}\nMachine: ${displayName(targetMachine)}${gpuInfo}\nModel: ${model}${queueMsg}`;
        loadDeployTasks();
    } catch (err) {
        const msg = err.message.includes('409') || err.message.includes('Insufficient')
            ? `Deployment blocked: ${err.message.replace(/^\d+:\s*/, '')}`
            : err.message;
        output.innerHTML = `<span class="error">${escapeHtml(msg)}</span>`;
    } finally {
        // Re-evaluate Deploy button state based on memory
        const llmMachines = machines.filter(m => m.vllm_service_dir);
        updateMachineTable(llmMachines);
    }
}

// ---------------------------------------------------------------------------
// Deploy tasks
// ---------------------------------------------------------------------------
async function loadDeployTasks() {
    const body = document.getElementById('llm-tasks-body');
    if (!body) return;
    try {
        const allTasks = await api('/api/llm/my-tasks');
        // Dedup: keep only the latest entry per model
        const latest = new Map();
        allTasks.forEach(t => latest.set(t.model, t));
        // Filter out healthy/stopped containers — only show deploying/failed/running
        const tasks = [...latest.values()].filter(t => {
            if (t.container_status === 'stopped') return false;
            if (t.container_status && t.container_status.includes('healthy') && !t.container_status.includes('starting')) return false;
            return true;
        });
        if (tasks.length === 0) {
            body.innerHTML = '<div class="empty-state">No deploying tasks</div>';
            return;
        }
        body.innerHTML = `<table class="task-table">
            <thead><tr><th>Model</th><th>Machine</th><th>Status</th><th>Ports</th><th>By</th><th>Submitted</th><th></th></tr></thead>
            <tbody>${tasks.map(t => {
                const age = Math.round((Date.now()/1000 - t.submitted_at) / 60);
                const canCancel = t.task_status === 'queued' || t.task_status === 'running';
                const statusCell = t.container_status
                    ? t.container_status === 'not found'
                        ? `<span style="font-family:var(--mono); font-size:11px; color:var(--red)">deploy failed</span>`
                        : `<span style="font-family:var(--mono); font-size:11px; color:var(--green)">${escapeHtml(t.container_status)}</span>`
                    : `<span class="task-status ${t.task_status}">${t.task_status}</span>`;
                const portsCell = t.container_ports
                    ? `<span style="font-family:var(--mono); font-size:11px; color:var(--text-mid)">${escapeHtml(t.container_ports)}</span>`
                    : '—';
                return `<tr>
                    <td style="font-family:var(--mono); font-size:12px">${escapeHtml(t.model)}</td>
                    <td style="font-size:12px">${escapeHtml(displayName(t.machine))}</td>
                    <td>${statusCell}</td>
                    <td>${portsCell}</td>
                    <td style="font-size:12px; color:var(--text-dim)">${escapeHtml(t.username || '')}</td>
                    <td style="font-size:12px; color:var(--text-dim)">${age}m ago</td>
                    <td>${canCancel ? `<button class="cancel-btn" onclick="cancelDeployTask('${escapeHtml(t.machine)}','${escapeHtml(t.task_id)}')">Cancel</button>` : ''}</td>
                </tr>`;
            }).join('')}</tbody>
        </table>`;
    } catch (e) {
        body.innerHTML = `<div class="empty-state">Failed to load tasks</div>`;
    }
}

async function cancelDeployTask(machine, taskId) {
    if (!confirm(`Cancel deploy task ${taskId}?`)) return;
    try {
        await api(`/api/machines/${machine}/tasks/${taskId}`, { method: 'DELETE' });
        loadDeployTasks();
    } catch (e) {
        alert('Failed to cancel: ' + e.message);
    }
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
async function loadSettings() {
    const status = document.getElementById('hf-token-status');
    try {
        const s = await api('/api/settings');
        status.innerHTML = s.hf_token_set
            ? `Token set: <span style="font-family:var(--mono); color:var(--green)">${escapeHtml(s.hf_token_masked)}</span>`
            : '<span style="color:var(--yellow)">No token set — gated models will fail to download.</span>';
    } catch (e) {
        status.textContent = 'Failed to load settings.';
    }
    await loadUserManagement();

    // Pre-fill profile fields
    const stellarInput = document.getElementById('settings-stellar');
    if (stellarInput && _currentUser?.stellar_account) stellarInput.value = _currentUser.stellar_account;
}

async function saveHFToken() {
    const input = document.getElementById('hf-token-input');
    const btn = document.getElementById('hf-token-save');
    const token = input.value.trim();
    if (!token) return;
    btn.disabled = true;
    try {
        await api('/api/settings/hf-token', { method: 'POST', body: JSON.stringify({ token }) });
        input.value = '';
        await loadSettings();
    } catch (e) {
        alert('Failed to save token: ' + e.message);
    } finally {
        btn.disabled = false;
    }
}

async function clearHFToken() {
    if (!confirm('Clear the HuggingFace token?')) return;
    await api('/api/settings/hf-token', { method: 'DELETE' });
    await loadSettings();
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
function showLoginOverlay() {
    document.getElementById('login-overlay').style.display = 'flex';
    document.getElementById('userMenu').style.display = 'none';
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function hideLoginOverlay() {
    document.getElementById('login-overlay').style.display = 'none';
}

function updateUserDisplay() {
    if (!_currentUser) return;
    const menu = document.getElementById('userMenu');
    const badge = document.getElementById('userBadge');
    badge.textContent = `${_currentUser.username} · ${_currentUser.role}`;
    menu.style.display = 'flex';

    const adminOnly = ['overview', 'execute', 'tasks'];
    adminOnly.forEach(tab => {
        const btn = document.querySelector(`.tab[data-tab="${tab}"]`);
        if (btn) btn.style.display = _currentUser.role === 'admin' ? '' : 'none';
    });

    // If current tab is now hidden, switch to llm
    const activeTab = document.querySelector('.tab.active');
    if (activeTab && adminOnly.includes(activeTab.dataset.tab) && _currentUser.role !== 'admin') {
        document.querySelector('.tab[data-tab="llm"]').click();
    }
}

async function login() {
    const username = document.getElementById('login-username').value.trim();
    const password = document.getElementById('login-password').value;
    const errorEl = document.getElementById('login-error');
    const btn = document.getElementById('login-btn');
    errorEl.textContent = '';
    if (!username || !password) { errorEl.textContent = 'Enter username and password.'; return; }
    btn.disabled = true;
    try {
        const resp = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            errorEl.textContent = data.detail || 'Login failed';
            return;
        }
        const data = await resp.json();
        _authToken = data.token;
        // Fetch full profile (includes stellar_account, setup_required)
        const meResp = await fetch('/api/auth/me', { headers: { 'Authorization': `Bearer ${_authToken}` } });
        _currentUser = meResp.ok ? await meResp.json() : { username: data.username, role: data.role };
        localStorage.setItem('gpu_cmd_token', _authToken);
        hideLoginOverlay();
        updateUserDisplay();
        document.getElementById('login-password').value = '';
        if (_currentUser.setup_required) { showSetupModal(); return; }
        document.querySelector('.tab[data-tab="llm"]')?.click();
        startPolling();
    } catch (e) {
        errorEl.textContent = 'Connection error';
    } finally {
        btn.disabled = false;
    }
}

document.getElementById('login-password').addEventListener('keydown', e => {
    if (e.key === 'Enter') login();
});
document.getElementById('login-username').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('login-password').focus();
});

async function logout() {
    try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) {}
    _authToken = null;
    _currentUser = null;
    localStorage.removeItem('gpu_cmd_token');
    showLoginOverlay();
}

// ---------------------------------------------------------------------------
// User management (admin only)
// ---------------------------------------------------------------------------
async function loadUserManagement() {
    const panel = document.getElementById('user-mgmt-panel');
    if (_currentUser?.role !== 'admin') { panel.style.display = 'none'; return; }
    panel.style.display = 'block';
    const listEl = document.getElementById('user-list');
    try {
        const users = await api('/api/admin/users');
        listEl.innerHTML = users.map(u => `
            <div class="user-row">
                <div class="user-row-info">
                    <span class="user-row-name">${escapeHtml(u.username)}</span>
                    <span class="role-badge ${u.role}">${u.role}</span>
                </div>
                ${u.username !== _currentUser.username ? `<button class="cancel-btn" onclick="deleteUser('${escapeHtml(u.username)}')">Delete</button>` : ''}
            </div>`).join('');
    } catch (e) {
        listEl.innerHTML = `<div class="empty-state">Failed to load users</div>`;
    }
}

async function createUser() {
    const username = document.getElementById('new-username').value.trim();
    const password = document.getElementById('new-password').value;
    const role = document.getElementById('new-role').value;
    if (!username || !password) { alert('Username and password are required.'); return; }
    try {
        await api('/api/admin/users', { method: 'POST', body: JSON.stringify({ username, password, role }) });
        document.getElementById('new-username').value = '';
        document.getElementById('new-password').value = '';
        await loadUserManagement();
    } catch (e) {
        alert('Failed to create user: ' + e.message);
    }
}

async function deleteUser(username) {
    if (!confirm(`Delete user "${username}"?`)) return;
    try {
        await api(`/api/admin/users/${username}`, { method: 'DELETE' });
        await loadUserManagement();
    } catch (e) {
        alert('Failed to delete user: ' + e.message);
    }
}

async function changeOwnPassword() {
    const pw = document.getElementById('change-pw-input').value;
    if (!pw) return;
    try {
        await api('/api/auth/change-password', { method: 'POST', body: JSON.stringify({ new_password: pw }) });
        document.getElementById('change-pw-input').value = '';
        alert('Password updated. Please log in again.');
        await logout();
    } catch (e) {
        alert('Failed: ' + e.message);
    }
}

// ---------------------------------------------------------------------------
// Profile setup modal
// ---------------------------------------------------------------------------
function showSetupModal() {
    document.getElementById('setup-modal').style.display = 'flex';
    if (_currentUser?.stellar_account)
        document.getElementById('setup-stellar').value = _currentUser.stellar_account;
    // Hide stellar field if already set, show only password change
    const needsStellar = !_currentUser?.stellar_account;
    const needsPassword = _currentUser?.must_change_password;
    document.getElementById('setup-stellar-group').style.display = needsStellar ? '' : 'none';
    document.getElementById('setup-password-group').style.display = needsPassword ? '' : 'none';
    document.getElementById('setup-password-confirm-group').style.display = needsPassword ? '' : 'none';
}

async function saveProfile() {
    const stellar = document.getElementById('setup-stellar').value.trim();
    const newPw = document.getElementById('setup-new-password').value;
    const confirmPw = document.getElementById('setup-confirm-password').value;
    const hfToken = document.getElementById('setup-hf-token')?.value.trim() || '';
    const errEl = document.getElementById('setup-error');
    errEl.textContent = '';

    const needsStellar = !_currentUser?.stellar_account;
    const needsPassword = _currentUser?.must_change_password;

    if (needsStellar && !stellar) { errEl.textContent = 'Stellar account is required.'; return; }
    if (needsPassword) {
        if (!newPw) { errEl.textContent = 'Password is required.'; return; }
        if (newPw !== confirmPw) { errEl.textContent = 'Passwords do not match.'; return; }
        if (newPw.length < 6) { errEl.textContent = 'Password must be at least 6 characters.'; return; }
    }
    try {
        const body = {};
        if (needsStellar && stellar) body.stellar_account = stellar;
        if (needsPassword && newPw) body.new_password = newPw;
        if (hfToken) body.hf_token = hfToken;
        await api('/api/auth/profile', { method: 'POST', body: JSON.stringify(body) });
        if (stellar) _currentUser.stellar_account = stellar;
        _currentUser.setup_required = false;
        _currentUser.must_change_password = false;
        document.getElementById('setup-modal').style.display = 'none';
        startPolling();
    } catch (e) {
        errEl.textContent = 'Failed to save: ' + e.message;
    }
}

// Also allow saving from Settings tab
async function saveProfileFromSettings() {
    const stellar = document.getElementById('settings-stellar').value.trim();
    const hfToken = document.getElementById('settings-hf-token-user').value.trim();
    const body = {};
    if (stellar) body.stellar_account = stellar;
    if (hfToken) body.hf_token = hfToken;
    if (!Object.keys(body).length) return;
    try {
        await api('/api/auth/profile', { method: 'POST', body: JSON.stringify(body) });
        if (stellar) _currentUser.stellar_account = stellar;
        alert('Profile saved.');
    } catch (e) {
        alert('Failed: ' + e.message);
    }
}

// ---------------------------------------------------------------------------
// Access modal
// ---------------------------------------------------------------------------
function showAccessModal() {
    const stellarAccount = _currentUser?.stellar_account || '[stellar_account]';
    const allContainers = [];
    for (const [machineName, containers] of Object.entries(_llmRunning)) {
        const m = machines.find(m => m.name === machineName);
        const hostDesc = m?.description || machineName;
        for (const c of containers) {
            const hostPort = (c.ports.match(/:(\d+)->/) || [])[1];
            if (hostPort) allContainers.push({ machineName, hostDesc, container: c, hostPort });
        }
    }

    if (allContainers.length === 0) {
        document.getElementById('access-modal-body').innerHTML =
            '<div class="empty-state">No running models to access.</div>';
        document.getElementById('access-modal').style.display = 'flex';
        return;
    }

    const rows = allContainers.map(({ machineName, hostDesc, container, hostPort }) => {
        const sshCmd = `ssh -f -N -L ${hostPort}:localhost:${hostPort} ${stellarAccount}@${hostDesc}`;
        const curlCmd = `curl http://localhost:${hostPort}/v1/models`;
        const chatCmd = `curl http://localhost:${hostPort}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${container.name.replace(/-vllm-1$/, '')}","messages":[{"role":"user","content":"Hello!"}]}'`;
        return `<div class="model-access-row">
            <div class="model-access-name">${escapeHtml(container.name.replace(/-vllm-1$/, ''))}
                <span style="font-family:var(--font); font-size:11px; color:var(--text-dim); font-weight:400"> — ${escapeHtml(displayName(machineName))} · port ${hostPort}</span>
            </div>
            <div class="access-step-label" style="margin-top:8px">1. Forward port</div>
            <div class="access-cmd">${escapeHtml(sshCmd)}</div>
            <div class="access-step-label" style="margin-top:8px">2. List models</div>
            <div class="access-cmd">${escapeHtml(curlCmd)}</div>
            <div class="access-step-label" style="margin-top:8px">3. Chat</div>
            <div class="access-cmd" style="white-space:pre">${escapeHtml(chatCmd)}</div>
        </div>`;
    }).join('');

    const noStellar = !_currentUser?.stellar_account;
    const warn = noStellar ? `<div style="margin-bottom:16px; padding:10px 14px; background:var(--red-lo); border:1px solid rgba(255,77,109,.3); border-radius:8px; font-size:13px; color:var(--red)">
        Your stellar account is not set. Go to Settings to add it so commands are autofilled.
    </div>` : '';

    document.getElementById('access-modal-body').innerHTML = warn + rows;
    document.getElementById('access-modal').style.display = 'flex';
}

function closeAccessModal() {
    document.getElementById('access-modal').style.display = 'none';
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
(async () => {
    if (!_authToken) { showLoginOverlay(); return; }
    try {
        const me = await fetch('/api/auth/me', {
            headers: { 'Authorization': `Bearer ${_authToken}` }
        });
        if (!me.ok) { showLoginOverlay(); return; }
        _currentUser = await me.json();
        hideLoginOverlay();
        updateUserDisplay();
        if (_currentUser.setup_required) { showSetupModal(); return; }
        startPolling();
    } catch (_) {
        showLoginOverlay();
    }
})();
