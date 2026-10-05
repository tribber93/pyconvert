let stagedFiles = [];
let activeTasksMap = new Map();
let currentLogTaskId = null;
let eventSource = null;

// State untuk modal pemilih file dari folder zenius/
let zeniusTree = null;          // struktur folder hasil /api/zenius-scan
let zeniusCurrentPath = '';     // folder yang sedang dibuka ('' = root zenius)
let zeniusSelected = new Set(); // path file .swf yang dicentang (relatif ke zenius/)

document.addEventListener('DOMContentLoaded', () => {
    checkEnvironment();
    setupDropzone();
    setupZeniusPicker();
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

    dropzone.addEventListener('drop', async (e) => {
        const dt = e.dataTransfer;

        // Ambil entry secara sinkron (setelah await, objek item tidak valid lagi)
        const entries = dt.items
            ? Array.from(dt.items)
                .filter(it => it.kind === 'file')
                .map(it => it.webkitGetAsEntry ? it.webkitGetAsEntry() : null)
                .filter(Boolean)
            : [];

        if (entries.length > 0) {
            // Tarik-lepas folder: telusuri isinya supaya struktur folder ikut terbawa
            const collected = [];
            for (const entry of entries) {
                await walkEntry(entry, '', collected);
            }
            handleStagedItems(collected);
        } else {
            // Fallback browser lama: hanya file, tanpa struktur folder
            handleSelectedFiles(dt.files);
        }
    });

    fileInput.addEventListener('change', (e) => {
        handleSelectedFiles(e.target.files);
    });

    // Upload folder (webkitdirectory)
    const folderInput = document.getElementById('folderInput');
    if (folderInput) {
        folderInput.addEventListener('change', (e) => {
            handleSelectedFiles(e.target.files);
        });
    }
}

// Ambil jalur relatif sebuah File dari atribut webkitRelativePath (kosong jika bukan dari folder)
function getRelPath(file) {
    const rel = file.webkitRelativePath || '';
    return rel ? rel.replace(/\\/g, '/') : file.name;
}

// Telusuri DataTransferItem entry secara rekursif; hasil: [{file, relPath}]
function walkEntry(entry, prefix, out) {
    return new Promise((resolve) => {
        if (entry.isFile) {
            entry.file(file => {
                out.push({ file, relPath: prefix ? `${prefix}/${file.name}` : file.name });
                resolve();
            }, () => resolve());
        } else if (entry.isDirectory) {
            const reader = entry.createReader();
            const dirPrefix = prefix ? `${prefix}/${entry.name}` : entry.name;
            const readBatch = () => {
                reader.readEntries(async (batch) => {
                    if (batch.length === 0) { resolve(); return; }
                    for (const child of batch) {
                        await walkEntry(child, dirPrefix, out);
                    }
                    readBatch(); // readEntries memanggil balik per batch, bukan sekaligus
                }, () => resolve());
            };
            readBatch();
        } else {
            resolve();
        }
    });
}

function handleSelectedFiles(files) {
    const swfFiles = Array.from(files).filter(f => f.name.toLowerCase().endsWith('.swf'));
    handleStagedItems(swfFiles.map(file => ({ file, relPath: getRelPath(file) })));
}

// Staging bersama untuk pemilihan file/folder (via input) maupun tarik-lepas
function handleStagedItems(items) {
    const swfItems = items.filter(it => it.file.name.toLowerCase().endsWith('.swf'));

    if (swfItems.length === 0) {
        alert('Harap pilih file dengan ekstensi .swf!');
        return;
    }

    // Hindari duplikat: folder yang sama bisa dipilih dua kali
    const existing = new Set(stagedFiles.map(s => `${s.relPath}|${s.file.size}`));
    swfItems.forEach(({ file, relPath }) => {
        const key = `${relPath}|${file.size}`;
        if (!existing.has(key)) {
            existing.add(key);
            stagedFiles.push({ file, relPath });
        }
    });

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

    listEl.innerHTML = stagedFiles.map((item, idx) => `
        <div class="staged-chip" title="${escapeHtml(item.relPath)}">
            <i class="fa-solid fa-file-video"></i>
            <span>${escapeHtml(item.relPath)}</span>
            <small>(${formatBytes(item.file.size)})</small>
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
    const folderInput = document.getElementById('folderInput');
    if (folderInput) folderInput.value = '';
    renderStagedFiles();
}

// Upload Files & Create Tasks
async function uploadStagedFiles() {
    if (stagedFiles.length === 0) return;

    const startBtn = document.getElementById('startBatchBtn');
    startBtn.disabled = true;
    startBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Mengunggah...';

    const formData = new FormData();
    stagedFiles.forEach(item => {
        formData.append('files[]', item.file, item.file.name);
        formData.append('paths[]', item.relPath);
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

    // Koneksi SSE terputus. Bisa karena session habis, bisa juga karena
    // gangguan jaringan biasa — jadi dipastikan dulu lewat /api/tasks supaya
    // tidak salah melempar pengguna ke halaman login.
    eventSource.onerror = async () => {
        if (!eventSource || eventSource.readyState !== EventSource.CLOSED) return;
        try {
            const res = await fetch('/api/tasks');
            if (res.status === 401) {
                window.location.href = '/login';
            } else {
                setTimeout(initSSE, 2000); // jaringan pulih: sambung ulang
            }
        } catch (e) {
            setTimeout(initSSE, 3000);
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
                        ${isActive(task.status) ? `
                            <button class="btn btn-sm btn-outline" style="border-color:#f43f5e; color:#f43f5e;" onclick="cancelTask('${task.id}')">
                                <i class="fa-solid fa-circle-stop"></i> Batalkan
                            </button>
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

    const completed = taskList.filter(t => isTerminal(t.status)).length;
    const total = taskList.length;
    const isRunning = taskList.some(t => !isTerminal(t.status));

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
        case 'cancelled': return 'Dibatalkan';
        case 'cancelling': return 'Menghentikan...';
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

// Batalkan task yang sedang berjalan / mengantre
async function cancelTask(taskId) {
    if (!confirm('Hentikan proses konversi file ini?')) return;
    try {
        await fetch(`/api/cancel/${taskId}`, { method: 'POST' });
        fetchTasks();
    } catch (e) {
        console.error('Cancel error:', e);
    }
}

// Retry Task
async function retryTask(taskId) {
    try {
        const res = await fetch(`/api/retry/${taskId}`, { method: 'POST' });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            alert(data.error || 'Gagal mengulang proses.');
        }
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

// ===========================================================================
// Zenius Folder Picker
// Pemilih file .swf langsung dari folder zenius/ di halaman utama, sehingga
// pengguna tidak perlu membuka File Manager terpisah lalu menekan Konversi.
// ===========================================================================

function setupZeniusPicker() {
    const modal = document.getElementById('zeniusModal');
    if (!modal) return;
    // Tutup modal saat area gelap di luar kartu diklik
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeZeniusPicker();
    });
    // Tutup dengan tombol Escape
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !modal.classList.contains('hidden')) closeZeniusPicker();
    });
}

function openZeniusPicker() {
    const modal = document.getElementById('zeniusModal');
    if (!modal) return;
    modal.classList.remove('hidden');
    loadZeniusTree();
}

function closeZeniusPicker() {
    const modal = document.getElementById('zeniusModal');
    if (modal) modal.classList.add('hidden');
}

// Muat ulang daftar file, tetap membuka folder yang sedang aktif
function refreshZeniusPicker() {
    loadZeniusTree(zeniusCurrentPath);
}

async function loadZeniusTree(keepPath = '') {
    const listEl = document.getElementById('zeniusList');
    listEl.innerHTML = '<div class="zenius-loading"><i class="fa-solid fa-spinner fa-spin"></i> Memuat daftar file...</div>';
    try {
        const res = await fetch('/api/zenius-scan');
        if (res.status === 401) { window.location.href = '/login'; return; }
        const data = await res.json();
        zeniusTree = data;
        // Kalau folder yang tersimpan sudah tidak ada lagi, kembali ke root
        zeniusCurrentPath = findZeniusNode(keepPath) ? keepPath : '';
        renderZeniusList();
    } catch (e) {
        listEl.innerHTML = '<div class="zenius-empty"><i class="fa-solid fa-triangle-exclamation"></i> Gagal memuat daftar folder zenius.</div>';
        console.error('Zenius scan error:', e);
    }
}

// Cari node folder berdasarkan path relatif (mis. "kursus/bab1")
function findZeniusNode(path, node = zeniusTree) {
    if (!node) return null;
    if (!path) return node;
    const segs = path.split('/').filter(Boolean);
    let current = node;
    for (const seg of segs) {
        const next = (current.dirs || []).find(d => d.name === seg);
        if (!next) return null;
        current = next;
    }
    return current;
}

// Kumpulkan semua path file .swf di dalam sebuah node (termasuk subfolder)
function collectZeniusFiles(node, out = []) {
    if (!node) return out;
    (node.files || []).forEach(f => out.push(f));
    (node.dirs || []).forEach(d => collectZeniusFiles(d, out));
    return out;
}

function renderZeniusList() {
    const listEl = document.getElementById('zeniusList');
    const node = findZeniusNode(zeniusCurrentPath);
    if (!node) {
        listEl.innerHTML = '<div class="zenius-empty"><i class="fa-regular fa-folder-open"></i> Folder tidak ditemukan.</div>';
        return;
    }

    renderZeniusBreadcrumb(zeniusCurrentPath);

    let html = '';

    // Baris "kembali ke folder atas" bila tidak berada di root
    if (zeniusCurrentPath) {
        const parent = zeniusCurrentPath.split('/').filter(Boolean).slice(0, -1).join('/');
        html += `
            <div class="zenius-row dir" data-nav="${escapeHtml(parent)}">
                <i class="fa-solid fa-arrow-left zenius-dir-icon"></i>
                <span class="zenius-name">.. (Kembali ke folder atas)</span>
            </div>`;
    }

    // Folder lebih dulu, lalu file
    (node.dirs || []).forEach(dir => {
        const fileCount = collectZeniusFiles(dir).length;
        html += `
            <div class="zenius-row dir" data-nav="${escapeHtml(dir.path || dir.name)}">
                <i class="fa-solid fa-folder zenius-dir-icon"></i>
                <span class="zenius-name">${escapeHtml(dir.name)}</span>
                <span class="zenius-size">${fileCount} file .swf</span>
                <button class="btn btn-sm btn-outline" data-select-folder="${escapeHtml(dir.path || dir.name)}" title="Pilih semua .swf di folder ini">
                    <i class="fa-solid fa-check-double"></i>
                </button>
            </div>`;
    });

    (node.files || []).forEach(file => {
        const checked = zeniusSelected.has(file.path) ? 'checked' : '';
        html += `
            <div class="zenius-row file" data-select-file="${escapeHtml(file.path)}">
                <input type="checkbox" ${checked}>
                <i class="fa-solid fa-file-video zenius-file-icon"></i>
                <span class="zenius-name">${escapeHtml(file.name)}</span>
                <span class="zenius-size">${formatBytes(file.size)}</span>
            </div>`;
    });

    if (!html) {
        listEl.innerHTML = '<div class="zenius-empty"><i class="fa-regular fa-folder-open"></i> Tidak ada file .swf di folder ini.<br>Tambahkan file ke folder <code>zenius/</code> lalu muat ulang.</div>';
        updateZeniusSelectionInfo();
        return;
    }

    listEl.innerHTML = html;

    // Pasang handler lewat listener + data-* attribute (bukan onclick inline),
    // supaya nama folder/file yang mengandung tanda kutip tetap aman.
    listEl.querySelectorAll('[data-nav]').forEach(el => {
        el.addEventListener('click', (e) => {
            if (e.target.closest('button')) return; // tombol "pilih folder" jangan ikut navigasi
            zeniusCurrentPath = el.getAttribute('data-nav');
            renderZeniusList();
        });
    });

    listEl.querySelectorAll('[data-select-folder]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            selectZeniusFolder(btn.getAttribute('data-select-folder'));
        });
    });

    listEl.querySelectorAll('[data-select-file]').forEach(row => {
        const path = row.getAttribute('data-select-file');
        const checkbox = row.querySelector('input[type="checkbox"]');
        checkbox.addEventListener('change', () => {
            toggleZeniusFile(path, checkbox.checked);
        });
        // Klik area baris (selain checkbox) juga ikut mencentang
        row.addEventListener('click', (e) => {
            if (e.target === checkbox) return;
            checkbox.checked = !checkbox.checked;
            toggleZeniusFile(path, checkbox.checked);
        });
    });

    // Sinkronkan "Pilih Semua", tombol konversi, dan info jumlah terpilih
    updateZeniusSelectionInfo();
}

function renderZeniusBreadcrumb(path) {
    const el = document.getElementById('zeniusBreadcrumb');
    const parts = path.split('/').filter(Boolean);
    let html = `<a data-crumb="">zenius</a>`;
    let acc = '';
    parts.forEach((part, i) => {
        acc = acc ? `${acc}/${part}` : part;
        html += `<span class="divider">/</span><a data-crumb="${escapeHtml(acc)}">${escapeHtml(part)}</a>`;
    });
    el.innerHTML = html;
    el.querySelectorAll('[data-crumb]').forEach(a => {
        a.addEventListener('click', () => {
            zeniusCurrentPath = a.getAttribute('data-crumb');
            renderZeniusList();
        });
    });
}

function toggleZeniusFile(path, isChecked) {
    if (isChecked) zeniusSelected.add(path);
    else zeniusSelected.delete(path);
    updateZeniusSelectionInfo();
}

// Pilih / batalkan semua file .swf di dalam sebuah folder
function selectZeniusFolder(folderPath) {
    const node = findZeniusNode(folderPath);
    const files = collectZeniusFiles(node);
    if (files.length === 0) return;
    const allSelected = files.every(f => zeniusSelected.has(f.path));
    files.forEach(f => {
        if (allSelected) zeniusSelected.delete(f.path);
        else zeniusSelected.add(f.path);
    });
    renderZeniusList();
    updateZeniusSelectionInfo();
}

function toggleZeniusSelectAll(master) {
    const node = findZeniusNode(zeniusCurrentPath);
    (node.files || []).forEach(f => {
        if (master.checked) zeniusSelected.add(f.path);
        else zeniusSelected.delete(f.path);
    });
    renderZeniusList();
    updateZeniusSelectionInfo();
}

function updateZeniusSelectionInfo() {
    const count = zeniusSelected.size;
    document.getElementById('zeniusSelectionInfo').textContent = `${count} file dipilih`;
    document.getElementById('zeniusConvertBtn').disabled = count === 0;
    const master = document.getElementById('zeniusSelectAll');
    if (master) {
        const node = findZeniusNode(zeniusCurrentPath);
        const files = (node && node.files) || [];
        master.checked = files.length > 0 && files.every(f => zeniusSelected.has(f.path));
    }
}

// Kirim file terpilih ke antrean konversi (endpoint /api/convert-local)
async function submitZeniusConversion() {
    const items = Array.from(zeniusSelected);
    if (items.length === 0) return;

    const btn = document.getElementById('zeniusConvertBtn');
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Memulai...';

    try {
        const res = await fetch('/api/convert-local', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ items })
        });
        const data = await res.json();
        if (data.success) {
            zeniusSelected.clear();
            closeZeniusPicker();
            fetchTasks();
            document.querySelector('.tasks-section')?.scrollIntoView({ behavior: 'smooth' });
        } else {
            alert('Gagal memulai konversi: ' + (data.error || 'Terjadi kesalahan'));
        }
    } catch (e) {
        alert('Gagal menghubungi server.');
        console.error('Zenius convert error:', e);
    } finally {
        btn.disabled = zeniusSelected.size === 0;
        btn.innerHTML = original;
        updateZeniusSelectionInfo();
    }
}

// Helpers
function isTerminal(status) {
    return status === 'completed' || status === 'error' || status === 'cancelled';
}

// Task yang masih berjalan atau menunggu (bisa dibatalkan)
function isActive(status) {
    return !isTerminal(status);
}

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
