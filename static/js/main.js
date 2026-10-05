let stagedFiles = [];
let activeTasksMap = new Map();
let currentLogTaskId = null;
let eventSource = null;

document.addEventListener('DOMContentLoaded', () => {
    checkEnvironment();
    setupDropzone();
    initSSE();
});

// Check Server Environment Status
async function checkEnvironment() {
    try {
        const res = await fetch('/api/env');
        const data = await res.json();

        updateBadge('badgeJava', data.java);
        updateBadge('badgeFfmpeg', data.ffmpeg);
        updateBadge('badgeFfdec', data.ffdec);
    } catch (e) {
        console.error('Environment check failed:', e);
    }
}

function updateBadge(id, isOk) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('ok', 'missing');
    el.classList.add(isOk ? 'ok' : 'missing');
}

// Setup Drag and Drop
function setupDropzone() {
    const dropzone = document.getElementById('dropzone');
    const fileInput = document.getElementById('fileInput');

    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        dropzone.addEventListener(eventName, preventDefaults, false);
    });

    function preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }

    ['dragenter', 'dragover'].forEach(eventName => {
        dropzone.addEventListener(eventName, () => dropzone.classList.add('dragover'), false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        dropzone.addEventListener(eventName, () => dropzone.classList.remove('dragover'), false);
    });

    dropzone.addEventListener('drop', (e) => {
        const dt = e.dataTransfer;
        const files = dt.files;
        handleSelectedFiles(files);
    });

    fileInput.addEventListener('change', (e) => {
        handleSelectedFiles(e.target.files);
    });
}

function handleSelectedFiles(files) {
    const swfFiles = Array.from(files).filter(f => f.name.toLowerCase().endsWith('.swf'));
    
    if (swfFiles.length === 0) {
        alert('Harap pilih file dengan ekstensi .swf!');
        return;
    }

    stagedFiles = [...stagedFiles, ...swfFiles];
    renderStagedFiles();
}

function renderStagedFiles() {
    const panel = document.getElementById('stagedPanel');
    const countEl = document.getElementById('stagedCount');
    const listEl = document.getElementById('stagedList');

    if (stagedFiles.length === 0) {
        panel.classList.add('hidden');
        return;
    }

    panel.classList.remove('hidden');
    countEl.textContent = stagedFiles.length;

    listEl.innerHTML = stagedFiles.map((file, idx) => `
        <div class="staged-chip">
            <i class="fa-solid fa-file-video"></i>
            <span>${file.name}</span>
            <small>(${formatBytes(file.size)})</small>
            <span style="cursor:pointer; margin-left:6px; color:#f43f5e;" onclick="removeStagedFile(${idx})">&times;</span>
        </div>
    `).join('');
}

function removeStagedFile(index) {
    stagedFiles.splice(index, 1);
    renderStagedFiles();
}

function clearStagedFiles() {
    stagedFiles = [];
    document.getElementById('fileInput').value = '';
    renderStagedFiles();
}

// Upload Files & Create Tasks
async function uploadStagedFiles() {
    if (stagedFiles.length === 0) return;

    const startBtn = document.getElementById('startBatchBtn');
    startBtn.disabled = true;
    startBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Mengunggah...';

    const formData = new FormData();
    stagedFiles.forEach(file => {
        formData.append('files[]', file);
    });

    try {
        const res = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });

        const data = await res.json();
        if (data.success) {
            clearStagedFiles();
            fetchTasks();
        } else {
            alert('Upload gagal: ' + (data.error || 'Terjadi kesalahan'));
        }
    } catch (e) {
        alert('Gagal mengirim file ke server.');
        console.error(e);
    } finally {
        startBtn.disabled = false;
        startBtn.innerHTML = '<i class="fa-solid fa-play"></i> Mulai Konversi';
    }
}

// SSE Realtime Updates
function initSSE() {
    if (eventSource) eventSource.close();

    eventSource = new EventSource('/api/stream');

    eventSource.onmessage = (event) => {
        try {
            const taskList = JSON.parse(event.data);
            updateTasksState(taskList);
        } catch (e) {
            console.error('SSE Error:', e);
        }
    };
}

async function fetchTasks() {
    try {
        const res = await fetch('/api/tasks');
        const taskList = await res.json();
        updateTasksState(taskList);
    } catch (e) {
        console.error('Fetch tasks error:', e);
    }
}

function updateTasksState(taskList) {
    activeTasksMap.clear();
    taskList.forEach(t => activeTasksMap.set(t.id, t));

    renderTasksGrid(taskList);
    updateGlobalProgress(taskList);

    // If modal open, refresh modal log
    if (currentLogTaskId && activeTasksMap.has(currentLogTaskId)) {
        renderModalLogs(activeTasksMap.get(currentLogTaskId));
    }
}

function renderTasksGrid(taskList) {
    const grid = document.getElementById('tasksGrid');
    const emptyState = document.getElementById('emptyState');
    const totalBadge = document.getElementById('totalTasksBadge');
    const downloadAllBtn = document.getElementById('downloadAllBtn');

    totalBadge.textContent = `${taskList.length} File`;

    if (taskList.length === 0) {
        emptyState.classList.remove('hidden');
        grid.innerHTML = '';
        downloadAllBtn.disabled = true;
        return;
    }

    emptyState.classList.add('hidden');

    const hasCompleted = taskList.some(t => t.status === 'completed');
    downloadAllBtn.disabled = !hasCompleted;

    grid.innerHTML = taskList.map(task => {
        const statusText = getStatusLabel(task.status);
        const progress = task.progress || 0;

        return `
            <div class="task-card" id="task-${task.id}">
                <div class="task-card-header">
                    <div class="task-file-info">
                        <div class="file-icon">
                            <i class="fa-solid fa-file-film"></i>
                        </div>
                        <div>
                            <div class="task-name">${escapeHtml(task.filename)}</div>
                            <div class="task-size">${formatBytes(task.file_size)}</div>
                        </div>
                    </div>
                    <span class="task-status-pill status-${task.status}">${statusText}</span>
                </div>

                <div class="task-body">
                    <div class="task-progress-label">
                        <span>Status: <strong>${statusText}</strong></span>
                        <span>${progress}%</span>
                    </div>
                    <div class="progress-bar-bg">
                        <div class="progress-bar-fill" style="width: ${progress}%;"></div>
                    </div>
                </div>

                <div class="task-footer">
                    <div>
                        ${task.error_message ? `<span style="color:#f43f5e;"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(task.error_message)}</span>` : ''}
                    </div>
                    <div class="task-actions">
                        <button class="btn btn-sm btn-outline" onclick="openLogModal('${task.id}')" title="Lihat Terminal Log">
                            <i class="fa-solid fa-terminal"></i> Log
                        </button>
                        ${task.status === 'completed' ? `
                            <button class="btn btn-sm btn-outline" onclick="openVideoModal('${task.id}', '${escapeHtml(task.filename_stem)}')">
                                <i class="fa-solid fa-play"></i> Preview
                            </button>
                            <a href="/api/download/${task.id}" class="btn btn-sm btn-success" download>
                                <i class="fa-solid fa-download"></i> Unduh MP4
                            </a>
                        ` : ''}
                        ${task.status === 'error' ? `
                            <button class="btn btn-sm btn-outline" style="border-color:#f43f5e; color:#f43f5e;" onclick="retryTask('${task.id}')">
                                <i class="fa-solid fa-rotate-right"></i> Coba Lagi
                            </button>
                        ` : ''}
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function updateGlobalProgress(taskList) {
    const card = document.getElementById('globalProgressCard');
    if (taskList.length === 0) {
        card.classList.add('hidden');
        return;
    }

    const completed = taskList.filter(t => t.status === 'completed' || t.status === 'error').length;
    const total = taskList.length;
    const isRunning = taskList.some(t => t.status !== 'completed' && t.status !== 'error');

    if (isRunning) {
        card.classList.remove('hidden');
        const percent = Math.round((completed / total) * 100);
        document.getElementById('completedCount').textContent = completed;
        document.getElementById('totalBatchCount').textContent = total;
        document.getElementById('globalPercentText').textContent = `${percent}%`;
        document.getElementById('globalProgressBar').style.width = `${percent}%`;
    } else {
        card.classList.add('hidden');
    }
}

function getStatusLabel(status) {
    switch (status) {
        case 'pending': return 'Menunggu Antrean';
        case 'exporting_frames': return 'Tahap 1: Ekstrak Frame AVI';
        case 'exporting_sound': return 'Tahap 2: Ekstrak Audio';
        case 'encoding_mp4': return 'Tahap 3: Encoding MP4 (FFmpeg)';
        case 'completed': return 'Selesai';
        case 'error': return 'Gagal';
        default: return status;
    }
}

// Log Modal Functions
function openLogModal(taskId) {
    currentLogTaskId = taskId;
    const task = activeTasksMap.get(taskId);
    if (!task) return;

    document.getElementById('logModalTitle').textContent = task.filename;
    renderModalLogs(task);
    document.getElementById('logModal').classList.remove('hidden');
}

function renderModalLogs(task) {
    const container = document.getElementById('terminalLogs');
    container.textContent = (task.logs || []).join('\n');
    const windowEl = document.getElementById('terminalWindow');
    windowEl.scrollTop = windowEl.scrollHeight;
}

function closeLogModal() {
    currentLogTaskId = null;
    document.getElementById('logModal').classList.add('hidden');
}

function copyLogs() {
    const logs = document.getElementById('terminalLogs').textContent;
    navigator.clipboard.writeText(logs).then(() => {
        alert('Log berhasil disalin ke clipboard!');
    });
}

// Video Preview Modal Functions
function openVideoModal(taskId, filenameStem) {
    const player = document.getElementById('player');
    const source = document.getElementById('videoSource');
    const title = document.getElementById('videoModalTitle');
    const downloadBtn = document.getElementById('videoDownloadBtn');

    title.textContent = filenameStem + '.mp4';
    source.src = `/api/preview/${taskId}`;
    downloadBtn.href = `/api/download/${taskId}`;
    
    player.load();
    document.getElementById('videoModal').classList.remove('hidden');
}

function closeVideoModal() {
    const player = document.getElementById('player');
    player.pause();
    document.getElementById('videoModal').classList.add('hidden');
}

// Retry Task
async function retryTask(taskId) {
    try {
        await fetch(`/api/retry/${taskId}`, { method: 'POST' });
        fetchTasks();
    } catch (e) {
        console.error('Retry error:', e);
    }
}

// Clear Finished Tasks
async function clearFinishedTasks() {
    try {
        await fetch('/api/clear', { method: 'POST' });
        fetchTasks();
    } catch (e) {
        console.error('Clear tasks error:', e);
    }
}

// Download Zip
function downloadAllZip() {
    window.location.href = '/api/download-all';
}

// Helpers
function formatBytes(bytes, decimals = 2) {
    if (!bytes || bytes === 0) return '0 Bytes';
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
}

function escapeHtml(text) {
    if (!text) return '';
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}
