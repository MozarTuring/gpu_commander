const API = '';
let machines = [];
let pollTimer = null;

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
    const resp = await fetch(`${API}${path}`, {
        headers: { 'Content-Type': 'application/json', ...opts.headers },
        ...opts,
    });
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
                    <div class="machine-name">${m.name}</div>
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
            opt.textContent = `${m.name}${m.online ? '' : ' (offline)'}`;
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
                return tasks.map(t => ({ ...t, machine: m.name }));
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
                    <td>${t.machine}</td>
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

function startPolling(interval = 10000) {
    refresh();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => {
        refresh();
        const activeTab = document.querySelector('.tab.active')?.dataset.tab;
        if (activeTab === 'tasks') loadTasks();
        if (activeTab === 'llm') loadLLMTab();
    }, interval);
}

// ---------------------------------------------------------------------------
// Util
// ---------------------------------------------------------------------------
function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str || '';
    return div.innerHTML;
}

// ---------------------------------------------------------------------------
// LLM Services
// ---------------------------------------------------------------------------
let _llmModels = [];      // fetched once from first vllm machine
let _llmRunning = {};     // { machineName: [...containers] }

async function loadLLMTab() {
    const grid = document.getElementById('llmGrid');
    const llmMachines = machines.filter(m => m.vllm_service_dir);

    if (llmMachines.length === 0) {
        grid.innerHTML = '<div class="empty-state">No machines with LLM services configured</div>';
        return;
    }

    // Fetch models from first online vllm machine (same repo on all machines)
    const sourceMachine = llmMachines.find(m => m.online);
    if (sourceMachine && _llmModels.length === 0) {
        _llmModels = await api(`/api/machines/${sourceMachine.name}/llm/models`).catch(() => []);
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

    // Per-machine running services
    const runningPanel = document.createElement('div');
    runningPanel.className = 'cmd-panel';
    runningPanel.innerHTML = `
        <h3 style="margin-bottom:14px">Running Services</h3>
        ${llmMachines.map(m => renderRunningSection(m)).join('')}
    `;
    grid.appendChild(runningPanel);

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
    const modelOptions = _llmModels.map(m =>
        `<option value="${escapeHtml(m.name)}">${escapeHtml(m.name)} — ${escapeHtml(m.model)}</option>`
    ).join('');

    return `
        <h3 style="margin-bottom:14px">Deploy Model</h3>
        <div class="cmd-row" style="margin-bottom:12px">
            <select id="llm-model-select" style="flex:2">${modelOptions}</select>
        </div>
        <div id="llm-machine-table" style="margin-bottom:12px"></div>
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

    const memUtil = model.memory_utilization ?? 0.85;
    let bestFree = -1;
    _bestMachine = null;
    _bestGpu = null;

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
            if (fits && free > bestFree) {
                bestFree = free;
                _bestMachine = m.name;
                _bestGpu = gpu.index;
            }
            rows.push({ machine: m.name, gpuIdx: gpu.index, required, free, fits, status: fits ? 'fits' : 'no fit' });
        }
    }

    tableDiv.innerHTML = `<table class="task-table"><thead><tr>
        <th>Machine</th><th>GPU</th><th>Required</th><th>Free</th><th>Status</th>
    </tr></thead><tbody>${rows.map(r => `<tr${r.machine === _bestMachine && r.gpuIdx === _bestGpu ? ' style="background:rgba(52,211,153,0.05)"' : ''}>
        <td>${escapeHtml(r.machine)}</td>
        <td style="font-family:var(--mono)">${r.gpuIdx}</td>
        <td style="font-family:var(--mono)">${r.required != null ? r.required + ' MiB' : '—'}</td>
        <td style="font-family:var(--mono); color:${r.free != null ? (r.fits ? 'var(--green)' : 'var(--red)') : 'inherit'}">${r.free != null ? r.free + ' MiB' : '—'}</td>
        <td><span class="task-status ${r.fits ? 'completed' : r.status === 'offline' ? 'cancelled' : 'failed'}">${r.status}${r.machine === _bestMachine && r.gpuIdx === _bestGpu ? ' ★' : ''}</span></td>
    </tr>`).join('')}</tbody></table>`;

    const deployBtn = document.getElementById('llm-deploy-btn');
    if (_bestMachine) {
        if (deployBtn) { deployBtn.disabled = false; deployBtn.title = `Will deploy on ${_bestMachine} GPU${_bestGpu}`; }
    } else {
        if (deployBtn) {
            deployBtn.disabled = true;
            deployBtn.title = 'No machine has enough free GPU memory. Stop a running model first.';
        }
    }
}

function renderRunningSection(m) {
    const containers = _llmRunning[m.name] || [];
    const idleData = window._idleStatus?.containers ?? {};
    const timeoutHours = window._idleStatus?.timeout_hours ?? 2;
    const rowsHtml = containers.length === 0
        ? `<div style="color:var(--text-dim); font-size:13px; margin-bottom:12px">No running containers</div>`
        : `<table class="task-table" style="margin-bottom:12px"><thead><tr><th>Container</th><th>Status</th><th>Ports</th><th>Idle</th><th></th></tr></thead><tbody>
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
                return `<tr>
                    <td style="font-family:var(--mono); font-size:12px">${escapeHtml(c.name)}</td>
                    <td style="font-size:12px">${escapeHtml(c.status)}</td>
                    <td style="font-family:var(--mono); font-size:12px">${escapeHtml(c.ports)}</td>
                    <td>${idleHtml}</td>
                    <td><button class="cancel-btn" onclick="stopContainer('${escapeHtml(m.name)}','${escapeHtml(c.name)}')">Stop</button></td>
                </tr>`;
            }).join('')}
           </tbody></table>`;
    return `<div style="margin-bottom:12px">
        <div style="font-size:12px; color:var(--text-dim); margin-bottom:6px; text-transform:uppercase; letter-spacing:.05em">${escapeHtml(m.name)}</div>
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
    const btn = document.getElementById('llm-deploy-btn');
    const output = document.getElementById('llm-output');
    if (!model || !_bestMachine) return;

    btn.disabled = true;
    output.style.display = 'block';
    output.innerHTML = '<span class="spinner"></span> Submitting deploy task...';

    try {
        const body = { model };
        if (_bestGpu !== null) body.which_gpu = _bestGpu;
        const task = await api(`/api/machines/${_bestMachine}/llm/deploy`, {
            method: 'POST',
            body: JSON.stringify(body),
        });
        const gpuInfo = body.which_gpu !== undefined ? ` GPU${body.which_gpu}` : '';
        output.textContent = `Task submitted — ID: ${task.id}\nMachine: ${_bestMachine}${gpuInfo}\nModel: ${model}\nStatus: ${task.status}\n\nDeploy is running in background. Check Task Queue tab for progress.`;
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
// Init
// ---------------------------------------------------------------------------
startPolling();
