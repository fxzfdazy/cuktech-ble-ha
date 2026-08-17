// ── 充电记录模块（phone.html 与 index.html 共用）──
// 三视图：当前充电卡片 / 历史记录列表 / 单次详情（详情在历史视图内原地切换）
const API = window.location.origin;
let _sessionChart = null;

// 两个 C 口配置：端口号、API key、显示名
const CHARGE_PORTS = [
    { num: 1, key: 'c1', name: 'C1' },
    { num: 2, key: 'c2', name: 'C2' },
];

// 当前充电卡片轮询、时长自更新、迷你图刷新定时器
let _pollTimer = null;
let _durationTimer = null;
let _miniChartTimer = null;
// 各口当前活跃会话缓存，供每秒刷新持续时长与迷你图刷新使用
const _activeSessions = { 1: null, 2: null };
// 各口已渲染卡片的会话 id，id 不变时仅原地更新文本，避免整卡重建
const _renderedSessionId = { 1: undefined, 2: undefined };
// 各口已渲染卡片是否为已截止状态（同一会话在结束瞬间需从活跃样式重建为截止样式）
const _renderedEnded = { 1: undefined, 2: undefined };
// 各口迷你图实例，卡片重建时销毁，防止实例泄漏
const _miniCharts = { 1: null, 2: null };
// 各口迷你图最新点集：tooltip 回调经此读取，避免闭包持有建图时的旧数组
const _miniPoints = { 1: [], 2: [] };
// 迷你图瓦时曲线开关（两口共用，localStorage 持久化）
let _miniShowWh = localStorage.getItem('chMiniWh') === '1';
// 时间与时长显示精度（localStorage 持久化，配置页修改后经 storage 事件实时同步）
let _showSeconds = localStorage.getItem('chShowSeconds') !== '0';
window.addEventListener('storage', (e) => {
    if (e.key !== 'chShowSeconds') return;
    _showSeconds = e.newValue !== '0';
    updateDurations();
});
function toggleMiniWh() {
    _miniShowWh = !_miniShowWh;
    localStorage.setItem('chMiniWh', _miniShowWh ? '1' : '0');
    for (const num of [1, 2]) {
        const btn = document.getElementById('chWhToggle' + num);
        if (btn) btn.classList.toggle('active', _miniShowWh);
        const chart = _miniCharts[num];
        if (!chart) continue;
        if (chart.data.datasets[1]) chart.data.datasets[1].hidden = !_miniShowWh;
        if (chart.options.scales && chart.options.scales.y1) {
            chart.options.scales.y1.display = _miniShowWh;
        }
        chart.update();
    }
}
// 记录开关缓存，用于未充电占位卡片上的提示行
let _enabledPorts = null;
let _historyOpen = false;
let _chInitDone = false;

// 时间戳格式化为本地时间，秒级显示跟随 _showSeconds 开关
function fmtTime(ts) {
    if (!ts) return '--';
    const d = new Date(ts * 1000);
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const isToday = d >= today;
    const h = String(d.getHours()).padStart(2, '0');
    const m = String(d.getMinutes()).padStart(2, '0');
    const s = String(d.getSeconds()).padStart(2, '0');
    const t = _showSeconds ? `${h}:${m}:${s}` : `${h}:${m}`;
    if (isToday) return t;
    const yesterday = new Date(today - 86400000);
    if (d >= yesterday) return `昨天 ${t}`;
    return `${d.getMonth()+1}/${d.getDate()} ${t}`;
}

// 秒数格式化为时长，秒级显示跟随 _showSeconds 开关
function fmtDuration(sec) {
    if (!sec && sec !== 0) return '--';
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    if (d > 0) return `${d}天${h}小时${m}分`;
    if (h > 0) return _showSeconds ? `${h}小时${m}分${s}秒` : `${h}小时${m}分`;
    if (m > 0) return _showSeconds ? `${m}分${s}秒` : `${m}分`;
    return `${s}秒`;
}

// 取端口颜色：优先 phone.js 的 PORT_COLORS，其次 CSS 变量，最后回退默认色
function getPortColor(num) {
    if (typeof PORT_COLORS !== 'undefined') {
        const mapped = { 1: PORT_COLORS.c1, 2: PORT_COLORS.c2 }[num];
        if (mapped) return mapped;
    }
    const cs = getComputedStyle(document.documentElement);
    const fromVar = cs.getPropertyValue(`--port-c${num}`).trim();
    if (fromVar) return fromVar;
    return { 1: '#03a9f4', 2: '#7c4dff' }[num] || '#888';
}

// 瓦时按 3.7V 折算毫安时
function whToMah(wh) {
    return Math.round(wh / 3.7 * 1000);
}

// 瓦时显示精度：小电量两位小数避免慢充时长时间不跳字，大电量一位即可
function fmtWh(wh) {
    return wh >= 10 ? wh.toFixed(1) : wh.toFixed(2);
}

// 坐标轴刻度格式化：消除梯形积分等浮点运算产生的尾数（如 2.5000000000001）
function fmtTick(v) {
    return parseFloat(v.toPrecision(6));
}

// 拉取某口最近 limit 条会话
async function fetchSessions(port, limit) {
    try {
        const res = await fetch(`${API}/api/sessions?port=${port}&limit=${limit || 5}`);
        return await res.json();
    } catch (e) { return { sessions: [], total: 0 }; }
}

// 拉取某次会话的明细点（后端按采样间隔降采样，直接返回即可）
async function fetchSessionPoints(sessionId) {
    try {
        const res = await fetch(`${API}/api/sessions/${sessionId}/points`);
        return await res.json();
    } catch (e) { return { points: [] }; }
}

// 初始化充电记录板块：拉取记录开关、启动轮询与定时器、绑定历史按钮、监听 SSE
function initChargeHistory() {
    if (_chInitDone) return;
    _chInitDone = true;
    loadEnabledPorts();
    pollCurrentCharging();
    _pollTimer = setInterval(pollCurrentCharging, 2000);
    // 持续时长每秒前端自更新，避免每秒发请求
    _durationTimer = setInterval(updateDurations, 1000);
    // 迷你功率曲线每 5 秒刷新活跃口
    _miniChartTimer = setInterval(refreshActiveMiniCharts, 5000);
    const btn = document.getElementById('btnChargeHistory');
    if (btn) btn.addEventListener('click', toggleHistoryView);
    // SSE 会话开始/结束时立即刷新当前卡片（主要靠轮询，SSE 作为即时触发器）
    window.addEventListener('sse-session-end', pollCurrentCharging);
    window.addEventListener('sse-session-start', pollCurrentCharging);
}

// 拉取充电记录开关并缓存，供未充电占位卡片判断是否加提示行
async function loadEnabledPorts() {
    try {
        const res = await fetch(`${API}/api/charge_tracking`);
        const data = await res.json();
        const t = data.config || data;
        _enabledPorts = Array.isArray(t.enabled_ports) ? t.enabled_ports : [];
    } catch (e) {
        _enabledPorts = null;
        return;
    }
    // 开关信息到达后重渲染未充电占位卡片，补上或撤下提示行（截止展示卡不覆盖）
    for (const num of [1, 2]) {
        if (!_activeSessions[num] && _renderedSessionId[num] === null) {
            _renderedSessionId[num] = undefined;
            renderCurrentCharging(num, null);
        }
    }
}

// 轮询两个口的当前充电状态：无活跃会话时保留最近一次会话的截止展示，不清空
async function pollCurrentCharging() {
    await Promise.all(CHARGE_PORTS.map(async p => {
        const data = await fetchSessions(p.key, 1);
        const list = data.sessions || [];
        const active = list.find(s => s.is_active);
        _activeSessions[p.num] = active || null;
        renderCurrentCharging(p.num, active || list[0] || null);
    }));
}

// 渲染单个口的当前充电卡片：会话 id 不变时仅原地更新文本，不重建整卡与迷你图
function renderCurrentCharging(num, session) {
    const el = document.getElementById('currentC' + num);
    if (!el) return;
    const name = 'C' + num;
    const color = getPortColor(num);

    if (!session) {
        _activeSessions[num] = null;
        // 已是未充电占位则跳过，避免重复重建
        if (_renderedSessionId[num] === null) return;
        _renderedSessionId[num] = null;
        _renderedEnded[num] = undefined;
        destroyMiniChart(num);
        // 该口未开启记录时追加提示行
        const untracked = Array.isArray(_enabledPorts) && !_enabledPorts.includes(num);
        el.innerHTML = `
        <div class="charge-box charge-box-idle">
            <div>
                <div class="charge-idle-row">
                    <span class="dot" style="background:${color};"></span>
                    <span class="label">${name} 未充电</span>
                </div>
                ${untracked ? '<div class="charge-idle-hint">记录未开启，可在配置页开启</div>' : ''}
            </div>
        </div>`;
        return;
    }

    const ended = !session.is_active;
    // 已截止的会话是静态数据，不参与时长自更新与迷你图周期刷新
    if (ended) _activeSessions[num] = null;
    const sid = session.id;
    if (_renderedSessionId[num] === sid && _renderedEnded[num] === ended) {
        // 会话与状态均未变：活跃原地更新数值文本，截止为静态数据跳过
        if (!ended) updateChargeCardTexts(num, session);
        return;
    }

    // 会话从无到有、换了会话或活跃/截止状态翻转：整卡重建并初始化迷你图
    _renderedSessionId[num] = sid;
    _renderedEnded[num] = ended;
    destroyMiniChart(num);
    const startTs = session.start_time || 0;
    const durSec = ended
        ? (session.duration_sec || 0)
        : (startTs ? Math.max(0, Math.floor(Date.now() / 1000 - startTs)) : 0);
    const wh = session.total_wh || 0;
    // 实时功率：直接用实时电压电流乘积，与平均功率彻底分离；截止后无输出按 0 显示
    const power = ended ? 0 : (session.voltage || 0) * (session.current || 0);
    const durHtml = ended
        ? `共充电 <span id="chDur${num}">${fmtDuration(durSec)}</span><span class="charge-ended-tag">本次充电已截止</span>`
        : `已充电 <span id="chDur${num}">${fmtDuration(durSec)}</span>`;
    el.innerHTML = `
    <div class="charge-box${ended ? ' charge-box-ended' : ''}">
        <div class="charge-head">
            <div class="charge-head-left">
                <span class="charge-dot"></span>
                <span class="charge-name" style="color:${color};">${name}</span>
                <span class="charge-proto" id="chProto${num}" style="display:none;"></span>
            </div>
            <span class="charge-time"><span id="chTime${num}">开始于 ${fmtTime(startTs)}</span><span class="charge-duration">${durHtml}</span></span>
        </div>
        <div class="charge-body">
            <div class="charge-energy">
                <div class="charge-metric-label">累计电量</div>
                <div class="charge-metric-value"><span id="chWh${num}">${fmtWh(wh)}</span><span class="charge-metric-unit"> Wh</span></div>
                <div class="charge-metric-sub"><span id="chMah${num}">${whToMah(wh)}</span> mAh@3.7V</div>
            </div>
            <div class="charge-powers">
                <div class="charge-power">
                    <div class="charge-metric-label">实时功率</div>
                    <div class="charge-power-value"><span id="chPower${num}">${power.toFixed(1)}</span><span class="charge-metric-unit"> W</span></div>
                </div>
                <div class="charge-power">
                    <div class="charge-metric-label">平均功率</div>
                    <div class="charge-power-value"><span id="chAvgP${num}">${(session.avg_power_w || 0).toFixed(1)}</span><span class="charge-metric-unit"> W</span></div>
                </div>
            </div>
        </div>
        <div class="charge-mini-chart"><canvas id="chMiniChart${num}"></canvas><button type="button" class="mini-wh-toggle${_miniShowWh ? ' active' : ''}" id="chWhToggle${num}" onclick="toggleMiniWh()" title="显示/隐藏累计瓦时曲线">瓦时</button><div class="charge-mini-hint" id="chMiniHint${num}">采样中，曲线稍后出现</div></div>
    </div>`;
    initMiniChart(num, sid);
}

// 会话未变时原地更新卡片上的动态数值与协议
function updateChargeCardTexts(num, session) {
    const el = (id) => document.getElementById(id);
    const wh = session.total_wh || 0;
    if (el('chWh' + num)) el('chWh' + num).textContent = fmtWh(wh);
    if (el('chMah' + num)) el('chMah' + num).textContent = whToMah(wh);
    const power = (session.voltage || 0) * (session.current || 0);
    if (el('chPower' + num)) el('chPower' + num).textContent = power.toFixed(1);
    if (el('chAvgP' + num)) el('chAvgP' + num).textContent = (session.avg_power_w || 0).toFixed(1);
    // 开始时间随刷新重算，跨天后从 HH:MM 自动变为 昨天/M/D 前缀
    if (el('chTime' + num) && session.start_time) {
        el('chTime' + num).textContent = '开始于 ' + fmtTime(session.start_time);
    }
    const proto = el('chProto' + num);
    if (proto) {
        if (session.protocol) {
            proto.textContent = session.protocol;
            proto.style.display = '';
        } else {
            proto.textContent = '';
            proto.style.display = 'none';
        }
    }
}

// 每秒更新所有活跃卡片的持续时长文本（不发请求）
function updateDurations() {
    const now = Math.floor(Date.now() / 1000);
    for (const num of [1, 2]) {
        const s = _activeSessions[num];
        if (!s || !s.start_time) continue;
        const el = document.getElementById('chDur' + num);
        if (el) el.textContent = fmtDuration(now - s.start_time);
    }
}

// ── 历史按钮 toggle：折叠显示当前卡片，展开显示历史列表 ──

function toggleHistoryView() {
    if (_historyOpen) closeHistoryView();
    else openHistoryView();
}

// 打开历史视图：隐藏当前卡片，请求两个口各最近 5 次并渲染
async function openHistoryView() {
    _historyOpen = true;
    const btn = document.getElementById('btnChargeHistory');
    if (btn) btn.textContent = '收起';
    for (const num of [1, 2]) {
        const c = document.getElementById('currentC' + num);
        if (c) c.style.display = 'none';
    }
    const view = document.getElementById('chargeHistoryView');
    if (!view) return;
    // 复位到列表态再展示
    backToHistoryList();
    view.style.display = 'block';
    await Promise.all(CHARGE_PORTS.map(async p => {
        const data = await fetchSessions(p.key, 5);
        renderHistoryList(p.num, data.sessions || []);
    }));
}

// 折叠历史视图，回到当前充电卡片；若正处详情视图一并复位
function closeHistoryView() {
    _historyOpen = false;
    const btn = document.getElementById('btnChargeHistory');
    if (btn) btn.textContent = '历史记录';
    backToHistoryList();
    const view = document.getElementById('chargeHistoryView');
    if (view) view.style.display = 'none';
    for (const num of [1, 2]) {
        const c = document.getElementById('currentC' + num);
        if (c) c.style.display = '';
    }
}

// 清空全部充电记录：确认后调用清理接口，成功后重新拉取两口的列表
async function clearAllSessions() {
    if (!confirm('确定清空已结束的充电记录？充电中的数据将保留。')) return;
    try {
        const res = await fetch(`${API}/api/sessions/clear`, { method: 'POST' });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || '未知错误');
        await Promise.all(CHARGE_PORTS.map(async p => {
            const list = await fetchSessions(p.key, 5);
            renderHistoryList(p.num, list.sessions || []);
        }));
    } catch (e) {
        alert('清空失败: ' + e.message);
    }
}

// 渲染某口的历史列表
function renderHistoryList(num, sessions) {
    const el = document.getElementById('historyListC' + num);
    if (!el) return;
    const name = 'C' + num;
    const color = getPortColor(num);
    // 过滤掉无 end_time 且非活跃、或 0Wh 的脏数据
    const list = (sessions || []).filter(s => s.is_active || (s.end_time && s.total_wh > 0));
    let html = `
    <div class="history-group-head">
        <span class="dot" style="background:${color};"></span>
        <span class="history-group-name">${name}</span>
        <span class="history-group-count">最近 ${list.length} 次</span>
    </div>`;
    if (list.length === 0) {
        html += `<div class="history-empty">暂无记录</div>`;
        el.innerHTML = html;
        return;
    }
    html += list.map(s => {
        const isActive = !!s.is_active;
        const wh = fmtWh(s.total_wh || 0);
        const peak = (s.peak_power_w || 0).toFixed(1);
        const dur = s.duration_sec
            ? fmtDuration(s.duration_sec)
            : (s.start_time ? fmtDuration(Math.max(0, Math.floor(Date.now() / 1000 - s.start_time))) : '--');
        const timeRange = isActive
            ? `${fmtTime(s.start_time)} 起`
            : `${fmtTime(s.start_time)} ~ ${fmtTime(s.end_time)}`;
        const proto = s.protocol
            ? `<span class="charge-proto">${s.protocol}</span>` : '';
        const badge = isActive
            ? `<span class="charge-badge">充电中</span>` : '';
        const whColor = isActive ? 'var(--success,#34C759)' : 'var(--text)';
        return `
        <div class="charge-item" onclick="showSessionDetail(${s.id})">
            <div class="charge-item-main">
                <div class="charge-item-row">
                    <span class="charge-item-time">${timeRange}</span>
                    ${proto}${badge}
                </div>
                <span class="charge-item-dur">时长 ${dur}</span>
            </div>
            <div class="charge-item-right">
                <div class="charge-item-wh" style="color:${whColor};">${wh}<span class="charge-metric-unit"> Wh</span></div>
                <div class="charge-item-peak">峰值 ${peak}W</div>
            </div>
        </div>`;
    }).join('');
    el.innerHTML = html;
}

// ── 单次详情：在历史视图内原地替换列表 ──

// 展示单次会话详情：拉取明细点，前端梯形积分重算各项统计并画功率曲线
async function showSessionDetail(sessionId) {
    const data = await fetchSessionPoints(sessionId);
    if (!data.points || data.points.length === 0) return;

    const points = data.points;
    // 梯形积分重算瓦时
    const totalWh = points.reduce((sum, p, i) => {
        if (i === 0) return 0;
        const dt = (p.timestamp - points[i - 1].timestamp) / 3600;
        return sum + ((points[i - 1].power + p.power) / 2) * dt;
    }, 0);
    const duration = points[points.length - 1].timestamp - points[0].timestamp;
    const avgPower = duration > 0 ? (totalWh / (duration / 3600)) : 0;
    let peakPower = 0;
    for (let i = 0; i < points.length; i++) {
        if (points[i].power > peakPower) peakPower = points[i].power;
    }
    const avgVoltage = points.reduce((s, p) => s + p.voltage, 0) / points.length;
    const avgCurrent = points.reduce((s, p) => s + p.current, 0) / points.length;

    const wrap = document.getElementById('historyListWrap');
    const detail = document.getElementById('sessionDetail');
    if (!wrap || !detail) return;
    // 原地隐藏列表、显示详情
    wrap.style.display = 'none';
    detail.style.display = 'block';
    const el = (id) => document.getElementById(id);
    if (el('sdTitle')) el('sdTitle').textContent = `${fmtTime(points[0].timestamp)} → ${fmtTime(points[points.length - 1].timestamp)}`;
    if (el('sdDuration')) el('sdDuration').textContent = fmtDuration(duration);
    if (el('sdEnergy')) el('sdEnergy').textContent = fmtWh(totalWh);
    if (el('sdEnergyMah')) el('sdEnergyMah').textContent = `${whToMah(totalWh)} mAh@3.7V`;
    if (el('sdAvgP')) el('sdAvgP').textContent = avgPower.toFixed(1);
    if (el('sdPeakP')) el('sdPeakP').textContent = peakPower.toFixed(1);
    if (el('sdAvgV')) el('sdAvgV').textContent = avgVoltage.toFixed(1);
    if (el('sdAvgI')) el('sdAvgI').textContent = avgCurrent.toFixed(2);
    renderSessionChart(points);
}

// 返回历史列表：详情隐藏、列表恢复，并销毁详情图表
function backToHistoryList() {
    const wrap = document.getElementById('historyListWrap');
    const detail = document.getElementById('sessionDetail');
    if (wrap) wrap.style.display = '';
    if (detail) detail.style.display = 'none';
    if (_sessionChart) {
        _sessionChart.destroy();
        _sessionChart = null;
    }
}

// ── 当前卡片迷你功率曲线 ──

// 销毁某口迷你图实例
function destroyMiniChart(num) {
    if (_miniCharts[num]) {
        _miniCharts[num].destroy();
        _miniCharts[num] = null;
    }
}

// 初始化某口迷你图：拉取该会话明细点后渲染，过期数据直接丢弃
async function initMiniChart(num, sessionId) {
    const canvas = document.getElementById('chMiniChart' + num);
    if (!canvas || typeof Chart === 'undefined') return;
    const data = await fetchSessionPoints(sessionId);
    if (_renderedSessionId[num] !== sessionId) return;
    renderMiniChart(num, data.points || []);
}

// 迷你曲线：功率主线（端口色）+ 可选累计瓦时副线（橙色右轴），量程按数据自适应
function renderMiniChart(num, points) {
    const canvas = document.getElementById('chMiniChart' + num);
    const hint = document.getElementById('chMiniHint' + num);
    if (!canvas || typeof Chart === 'undefined') return;
    // 会话刚开始尚未写入采样点：隐藏空图显示占位说明
    if (!points || points.length === 0) {
        destroyMiniChart(num);
        if (hint) hint.style.display = 'flex';
        return;
    }
    if (hint) hint.style.display = 'none';
    const powers = points.map(p => p.power);
    // 逐点累计瓦时（梯形积分），与详情图同口径
    const cumWh = [];
    points.forEach((p, i) => {
        if (i === 0) { cumWh.push(0); return; }
        const dt = (p.timestamp - points[i - 1].timestamp) / 3600;
        cumWh.push(cumWh[i - 1] + ((points[i - 1].power + p.power) / 2) * dt);
    });
    const labels = buildTimeLabels(points);
    _miniPoints[num] = points;
    if (_miniCharts[num]) {
        _miniCharts[num].data.labels = labels;
        _miniCharts[num].data.datasets[0].data = powers;
        _miniCharts[num].data.datasets[0].pointRadius = points.length === 1 ? 2.5 : 0;
        if (_miniCharts[num].data.datasets[1]) {
            _miniCharts[num].data.datasets[1].data = cumWh;
            _miniCharts[num].data.datasets[1].pointRadius = points.length === 1 ? 2.5 : 0;
        }
        _miniCharts[num].update();
        return;
    }
    _miniCharts[num] = new Chart(canvas, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: '功率',
                data: powers,
                borderColor: getPortColor(num),
                backgroundColor: 'transparent',
                borderWidth: 1.5,
                fill: false,
                tension: 0.3,
                pointRadius: points.length === 1 ? 2.5 : 0,
                yAxisID: 'y',
            }, {
                label: '累计瓦时',
                data: cumWh,
                borderColor: 'rgba(255,152,0,0.85)',
                backgroundColor: 'transparent',
                borderWidth: 1.5,
                fill: false,
                tension: 0.3,
                pointRadius: points.length === 1 ? 2.5 : 0,
                yAxisID: 'y1',
                hidden: !_miniShowWh,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 200 },
            plugins: {
                legend: { display: false },
                tooltip: {
                    enabled: true,
                    mode: 'index',
                    intersect: false,
                    callbacks: {
                        title: function (items) {
                            const p = _miniPoints[num][items[0].dataIndex];
                            return p ? fmtTime(p.timestamp) : '';
                        },
                        label: function (ctx) {
                            return ctx.dataset.label === '累计瓦时'
                                ? '累计瓦时: ' + fmtWh(ctx.parsed.y) + ' Wh'
                                : '功率: ' + ctx.parsed.y.toFixed(1) + ' W';
                        },
                        footer: function (items) {
                            const p = _miniPoints[num][items[0].dataIndex];
                            if (!p) return '';
                            if (p.voltage && p.current) {
                                return '电压: ' + p.voltage.toFixed(1) + ' V · 电流: ' + p.current.toFixed(2) + ' A';
                            }
                            return '';
                        }
                    }
                }
            },
            scales: {
                x: {
                    display: true,
                    ticks: { autoSkip: false, maxRotation: 0, font: { size: 9 }, color: 'rgba(128,128,128,0.6)', includeBounds: false },
                    grid: { display: false }
                },
                y: {
                    display: true,
                    grace: '12%',
                    ticks: { maxTicksLimit: 3, font: { size: 9 }, color: 'rgba(128,128,128,0.6)', callback: v => fmtTick(v) + 'W' },
                    grid: { color: 'rgba(128,128,128,0.12)' }
                },
                y1: {
                    display: _miniShowWh,
                    position: 'right',
                    beginAtZero: true,
                    grace: '12%',
                    ticks: { maxTicksLimit: 3, font: { size: 9 }, color: 'rgba(255,152,0,0.6)', callback: v => fmtTick(v) + 'Wh' },
                    grid: { display: false }
                }
            },
            interaction: { intersect: false, mode: 'index' }
        }
    });
}

// 定时刷新活跃口的迷你功率曲线（活跃会话即拉取，未建图时也可从占位转为建图）
async function refreshActiveMiniCharts() {
    for (const p of CHARGE_PORTS) {
        const s = _activeSessions[p.num];
        if (!s) continue;
        if (_renderedSessionId[p.num] !== s.id) continue;
        const data = await fetchSessionPoints(s.id);
        if (_renderedSessionId[p.num] !== s.id) continue;
        renderMiniChart(p.num, data.points || []);
    }
}

// ── 详情功率曲线 ──

// 按时间跨度生成 X 轴标签，跨度越长标签越稀疏，最多约 6 个
function buildTimeLabels(points) {
    if (!points.length) return [];
    const span = points[points.length - 1].timestamp - points[0].timestamp;
    const stepSec = Math.max(60, Math.ceil(span / 5));
    const labels = [];
    let nextLabelTs = points[0].timestamp;
    for (let i = 0; i < points.length; i++) {
        if (points[i].timestamp >= nextLabelTs) {
            labels.push(fmtTime(points[i].timestamp));
            nextLabelTs = points[i].timestamp + stepSec;
        } else {
            labels.push('');
        }
    }
    return labels;
}

// 画功率 + 累计瓦时双曲线，协议切换处画虚线注释
function renderSessionChart(points) {
    const canvas = document.getElementById('sessionChart');
    if (!canvas) return;
    const labels = buildTimeLabels(points);
    const powers = points.map(p => p.power);
    // 逐点累计瓦时（梯形积分），与详情区统计同口径
    const cumWh = [];
    points.forEach((p, i) => {
        if (i === 0) { cumWh.push(0); return; }
        const dt = (p.timestamp - points[i - 1].timestamp) / 3600;
        cumWh.push(cumWh[i - 1] + ((points[i - 1].power + p.power) / 2) * dt);
    });

    // 找出协议切换点用于注释
    const protoChanges = [];
    let lastProto = '';
    points.forEach((p, i) => {
        const proto = p.protocol || '';
        if (proto && proto !== lastProto) {
            protoChanges.push({ index: i, proto });
            lastProto = proto;
        }
    });

    if (_sessionChart) _sessionChart.destroy();
    _sessionChart = new Chart(canvas, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: '功率',
                data: powers,
                borderColor: 'rgba(3,169,244,0.8)',
                backgroundColor: 'rgba(3,169,244,0.1)',
                borderWidth: 1.5,
                fill: true,
                tension: 0.3,
                pointRadius: 0,
                yAxisID: 'y',
            }, {
                label: '累计瓦时',
                data: cumWh,
                borderColor: 'rgba(255,152,0,0.85)',
                backgroundColor: 'transparent',
                borderWidth: 1.5,
                fill: false,
                tension: 0.3,
                pointRadius: 0,
                yAxisID: 'y1',
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            plugins: {
                legend: { display: true, labels: { boxWidth: 12, font: { size: 10 } } },
                tooltip: {
                    callbacks: {
                        title: function (items) {
                            const p = points[items[0].dataIndex];
                            return p ? fmtTime(p.timestamp) : '';
                        },
                        label: function (ctx) {
                            return ctx.dataset.label === '累计瓦时'
                                ? '累计瓦时: ' + fmtWh(ctx.parsed.y) + ' Wh'
                                : '功率: ' + ctx.parsed.y.toFixed(1) + ' W';
                        },
                        // footer 整个 tooltip 只触发一次，电压/电流/协议不随曲线重复
                        footer: function (items) {
                            const p = points[items[0].dataIndex];
                            if (!p) return '';
                            const lines = [];
                            if (p.voltage && p.current) {
                                lines.push('电压: ' + p.voltage.toFixed(1) + ' V · 电流: ' + p.current.toFixed(2) + ' A');
                            }
                            if (p.protocol) lines.push('协议: ' + p.protocol);
                            return lines;
                        }
                    }
                }
            },
            scales: {
                x: { display: true, ticks: { autoSkip: false, maxRotation: 0, font: { size: 10 }, color: 'rgba(128,128,128,0.5)', includeBounds: false }, grid: { display: false } },
                y: { display: true, position: 'left', ticks: { font: { size: 10 }, color: 'rgba(128,128,128,0.5)', callback: v => fmtTick(v) + 'W' }, grid: { color: 'rgba(128,128,128,0.1)' } },
                y1: { display: true, position: 'right', beginAtZero: true, ticks: { font: { size: 10 }, color: 'rgba(255,152,0,0.6)', callback: v => fmtTick(v) + 'Wh', maxTicksLimit: 5 }, grid: { display: false } }
            },
            interaction: { intersect: false, mode: 'index' }
        },
        plugins: [{
            id: 'protocolLines',
            afterDraw(chart) {
                if (protoChanges.length === 0) return;
                const ctx = chart.ctx;
                const xScale = chart.scales.x;
                protoChanges.forEach(c => {
                    if (c.index === 0) return;
                    const x = xScale.getPixelForValue(c.index);
                    const isDark = !document.body.classList.contains('light');
                    const lineColor = isDark ? 'rgba(255,255,255,0.25)' : 'rgba(0,0,0,0.2)';
                    const textColor = isDark ? 'rgba(255,255,255,0.7)' : 'rgba(0,0,0,0.6)';
                    ctx.save();
                    ctx.strokeStyle = lineColor;
                    ctx.setLineDash([4, 4]);
                    ctx.lineWidth = 1;
                    ctx.beginPath();
                    ctx.moveTo(x, chart.chartArea.top);
                    ctx.lineTo(x, chart.chartArea.bottom);
                    ctx.stroke();
                    ctx.fillStyle = textColor;
                    ctx.font = '10px sans-serif';
                    ctx.fillText(c.proto, x + 3, chart.chartArea.top + 12);
                    ctx.restore();
                });
            }
        }]
    });
}
