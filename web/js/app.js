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
        card.className = 'machine-card';

        const online = m.online;
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
async function loadLLMTab() {
    const grid = document.getElementById('llmGrid');
    const llmMachines = machines.filter(m => m.vllm_service_dir && m.online);

    if (llmMachines.length === 0) {
        grid.innerHTML = '<div class="empty-state">No online machines with LLM services configured</div>';
        return;
    }

    for (const m of llmMachines) {
        let section = document.getElementById(`llm-section-${m.name}`);
        if (!section) {
            section = document.createElement('div');
            section.id = `llm-section-${m.name}`;
            section.className = 'cmd-panel';
            section.style.marginBottom = '20px';
            grid.appendChild(section);
        }

        const [models, running] = await Promise.all([
            api(`/api/machines/${m.name}/llm/models`).catch(() => []),
            api(`/api/machines/${m.name}/llm/running`).catch(() => []),
        ]);

        const runningHtml = running.length === 0
            ? '<div style="color:var(--text-dim); font-size:13px">No running containers</div>'
            : `<table class="task-table"><thead><tr><th>Container</th><th>Status</th><th>Ports</th></tr></thead><tbody>
                ${running.map(c => `<tr>
                    <td style="font-family:var(--mono); font-size:12px">${escapeHtml(c.name)}</td>
                    <td style="font-size:12px">${escapeHtml(c.status)}</td>
                    <td style="font-family:var(--mono); font-size:12px">${escapeHtml(c.ports)}</td>
                </tr>`).join('')}
               </tbody></table>`;

        const modelOptions = models.map(m =>
            `<option value="${escapeHtml(m.name)}">${escapeHtml(m.name)} — ${escapeHtml(m.model)} (port ${m.port})</option>`
        ).join('');

        section.innerHTML = `
            <h3 style="margin-bottom:14px">${m.name}</h3>
            <div style="margin-bottom:14px">
                <div style="font-size:12px; color:var(--text-dim); margin-bottom:8px; text-transform:uppercase; letter-spacing:.05em">Running</div>
                ${runningHtml}
            </div>
            <div>
                <div style="font-size:12px; color:var(--text-dim); margin-bottom:8px; text-transform:uppercase; letter-spacing:.05em">Deploy</div>
                <div class="cmd-row">
                    <select id="llm-model-${m.name}">${modelOptions}</select>
                    <button id="llm-deploy-${m.name}" onclick="deployLLM('${m.name}')">Deploy</button>
                </div>
                <div class="cmd-output" id="llm-output-${m.name}" style="display:none; margin-top:10px"></div>
            </div>
        `;
    }
}

async function deployLLM(machineName) {
    const select = document.getElementById(`llm-model-${machineName}`);
    const btn = document.getElementById(`llm-deploy-${machineName}`);
    const output = document.getElementById(`llm-output-${machineName}`);
    const model = select.value;
    if (!model) return;

    btn.disabled = true;
    output.style.display = 'block';
    output.innerHTML = '<span class="spinner"></span> Submitting deploy task...';

    try {
        const task = await api(`/api/machines/${machineName}/llm/deploy`, {
            method: 'POST',
            body: JSON.stringify({ model }),
        });
        output.textContent = `Task submitted — ID: ${task.id}\nStatus: ${task.status}\n\nDeploy is running in background. Check Task Queue tab for progress.`;
    } catch (err) {
        output.innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
    } finally {
        btn.disabled = false;
    }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
startPolling();
