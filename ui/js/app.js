/**
 * Janitza UMG 512-PRO Monitor - Frontend Application
 */

// Utility: Debounce function
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

class JanitzaMonitor {
    constructor() {
        this.ws = null;
        this.currentValues = {};
        this.valueHistory = {};  // Pentru chart widget - stochează ultimele N valori
        this.allRegisters = {};
        this.selectedRegisters = [];
        this.queryHistory = [];
        this.currentPage = 'dashboard';
        this.registerSearchPage = 1;
        this.registersPerPage = 50;
        this.maxHistoryPoints = 60;  // 60 puncte pentru chart

        // Config page state
        this.configTab = 'all';
        this.configSearch = '';
        this.config = {};  // Server config (MQTT, InfluxDB status etc.)

        // Monitor page state
        this.monitorData = {};  // { address: { name, unit, color, data: [{time, value}], min, max } }
        this.monitorColors = ['#0a84ff', '#30d158', '#ff453a', '#ffd60a', '#bf5af2', '#ff9f0a'];
        this.monitorColorIndex = 0;
        this.monitorPaused = false;
        this.monitorMaxPoints = 120;  // 2 minutes at 1s interval
        this.monitorCanvas = null;
        this.monitorCtx = null;
        this.monitorSearch = '';

        // Monitor zoom/pan state
        this.monitorZoom = 1;
        this.monitorPanX = 0;  // Pan offset in pixels
        this.monitorIsDragging = false;
        this.monitorDragStart = { x: 0, y: 0 };
        this.monitorLastPanX = 0;

        // Monitor tooltip state
        this.monitorGraphParams = null;  // Store graph params for tooltip calculations

        // Performance optimizations
        this._flattenedRegistersCache = null;
        this._flattenedRegistersCacheKey = null;
        this._monitorRAFPending = false;

        // Debounced functions
        this._debouncedRenderRegisters = debounce(() => this.renderRegistersTable(), 200);
        this._debouncedRenderSelectedList = debounce(() => this.renderSelectedRegistersList(), 200);
        this._debouncedRenderMonitorCategories = debounce(() => this.renderMonitorCategories(), 150);

        // Theme state
        this.theme = localStorage.getItem('janitza-theme') || 'auto';
        this.wasDisconnected = false;

        // Dashboard view state (cards or table)
        this.dashboardView = localStorage.getItem('janitza-dashboard-view') || 'cards';

        // Threshold templates for auto-fill based on measurement type
        this.thresholdTemplates = {
            voltage_ln: {
                dangerLow: 200, warningLow: 210, warningHigh: 245, dangerHigh: 253,
                type: 'value', unit: 'V'
            },
            voltage_ll: {
                dangerLow: 346, warningLow: 363, warningHigh: 424, dangerHigh: 438,
                type: 'value', unit: 'V'
            },
            frequency: {
                dangerLow: 49.0, warningLow: 49.5, warningHigh: 50.5, dangerHigh: 51.0,
                type: 'value', unit: 'Hz'
            },
            power_factor: {
                dangerLow: 0.75, warningLow: 0.85, warningHigh: null, dangerHigh: null,
                type: 'value', unit: ''
            },
            thd: {
                dangerLow: null, warningLow: null, warningHigh: 5, dangerHigh: 8,
                type: 'value', unit: '%'
            },
            current: {
                dangerLow: null, warningLow: null, warningHigh: 90, dangerHigh: 100,
                type: 'percent', unit: 'A'
            },
            power: {
                dangerLow: null, warningLow: null, warningHigh: 90, dangerHigh: 100,
                type: 'percent', unit: 'kW'
            }
        };

        this.init();
    }

    // ============ UI Helpers ============

    setButtonLoading(btn, loading, originalText = null) {
        if (loading) {
            btn.disabled = true;
            btn.dataset.originalText = btn.textContent;
            btn.innerHTML = '<span class="btn-spinner"></span> Saving...';
        } else {
            btn.disabled = false;
            btn.textContent = originalText || btn.dataset.originalText || 'Save';
        }
    }

    // ============ Value Color Coding ============

    /**
     * Detect measurement type from unit and name for template selection
     * Returns template key: voltage_ln, voltage_ll, frequency, power_factor, thd, current, power
     */
    detectMeasurementType(unit, name) {
        const unitLower = (unit || '').toLowerCase();
        const nameLower = (name || '').toLowerCase();

        // Voltage detection - distinguish L-N from L-L
        if (unitLower === 'v' || nameLower.includes('voltage') || nameLower.match(/u[_]?l/)) {
            // L-L voltage: Ull, U_ll, voltage_l1_l2, etc.
            if (nameLower.includes('ll') || nameLower.match(/l\d[_-]?l\d/) || nameLower.includes('_ll')) {
                return 'voltage_ll';
            }
            // L-N voltage: Uln, U_ln, voltage_l1_n, etc.
            return 'voltage_ln';
        }
        // Frequency detection
        if (unitLower === 'hz' || nameLower.includes('freq')) {
            return 'frequency';
        }
        // Power Factor detection
        if (nameLower.includes('power_factor') || nameLower.includes('cos') || nameLower.includes('pf')) {
            return 'power_factor';
        }
        // THD detection
        if (nameLower.includes('thd') || unitLower === '%thd' || unitLower === '% thd') {
            return 'thd';
        }
        // Current detection
        if (unitLower === 'a' || nameLower.includes('current') || nameLower.match(/i[_]?l/)) {
            return 'current';
        }
        // Power detection
        if (unitLower === 'w' || unitLower === 'kw' || unitLower === 'mw' || unitLower === 'va' || unitLower === 'kva' ||
            nameLower.includes('power') || nameLower.match(/p[_]?l/) || nameLower.match(/s[_]?l/)) {
            return 'power';
        }

        return null;
    }

    /**
     * Get threshold template for a measurement type
     */
    getThresholdTemplate(unit, name) {
        const type = this.detectMeasurementType(unit, name);
        if (type && this.thresholdTemplates[type]) {
            return { ...this.thresholdTemplates[type], templateType: type };
        }
        return null;
    }

    /**
     * Format value with automatic unit scaling (Wh→kWh, W→kW, VA→kVA, var→kvar)
     * Returns { value: number, unit: string, decimals: number }
     */
    formatValueWithUnit(value, unit) {
        if (typeof value !== 'number' || isNaN(value)) {
            return { value: value, unit: unit, decimals: 2 };
        }

        const absVal = Math.abs(value);

        // Energy: Wh → kWh → MWh
        if (unit === 'Wh') {
            if (absVal >= 1000000) return { value: value / 1000000, unit: 'MWh', decimals: 2 };
            if (absVal >= 1000) return { value: value / 1000, unit: 'kWh', decimals: 2 };
            return { value, unit, decimals: 1 };
        }
        if (unit === 'varh' || unit === 'VArh') {
            if (absVal >= 1000000) return { value: value / 1000000, unit: 'Mvarh', decimals: 2 };
            if (absVal >= 1000) return { value: value / 1000, unit: 'kvarh', decimals: 2 };
            return { value, unit, decimals: 1 };
        }
        if (unit === 'VAh') {
            if (absVal >= 1000000) return { value: value / 1000000, unit: 'MVAh', decimals: 2 };
            if (absVal >= 1000) return { value: value / 1000, unit: 'kVAh', decimals: 2 };
            return { value, unit, decimals: 1 };
        }

        // Power: W → kW → MW
        if (unit === 'W') {
            if (absVal >= 1000000) return { value: value / 1000000, unit: 'MW', decimals: 2 };
            if (absVal >= 10000) return { value: value / 1000, unit: 'kW', decimals: 2 };
            return { value, unit, decimals: 1 };
        }
        if (unit === 'VA') {
            if (absVal >= 1000000) return { value: value / 1000000, unit: 'MVA', decimals: 2 };
            if (absVal >= 10000) return { value: value / 1000, unit: 'kVA', decimals: 2 };
            return { value, unit, decimals: 1 };
        }
        if (unit === 'var' || unit === 'VAr') {
            if (absVal >= 1000000) return { value: value / 1000000, unit: 'Mvar', decimals: 2 };
            if (absVal >= 10000) return { value: value / 1000, unit: 'kvar', decimals: 2 };
            return { value, unit, decimals: 1 };
        }

        return { value, unit, decimals: 2 };
    }

    /**
     * Get CSS class for value based on register thresholds
     * Uses per-register thresholds if available, otherwise detects from type
     * Returns: 'value-normal', 'value-warning', 'value-danger', or 'value-success'
     */
    getValueColorClass(value, register) {
        if (typeof value !== 'number' || isNaN(value)) {
            return 'value-normal';
        }

        // Get thresholds - prefer per-register, fallback to template
        let thresholds = null;
        let measurementType = null;

        if (register && register.thresholds && register.thresholds.enabled) {
            thresholds = register.thresholds;
            measurementType = register.thresholds.templateType || this.detectMeasurementType(register.unit, register.name);
        } else if (register) {
            // Fallback to template-based detection
            const template = this.getThresholdTemplate(register.unit, register.name);
            if (template) {
                thresholds = template;
                measurementType = template.templateType;
            }
        }

        if (!thresholds) {
            return 'value-normal';
        }

        // Check danger thresholds first (they take priority)
        if (thresholds.dangerLow !== null && thresholds.dangerLow !== undefined && value < thresholds.dangerLow) {
            return 'value-danger';
        }
        if (thresholds.dangerHigh !== null && thresholds.dangerHigh !== undefined && value > thresholds.dangerHigh) {
            return 'value-danger';
        }

        // Check warning thresholds
        if (thresholds.warningLow !== null && thresholds.warningLow !== undefined && value < thresholds.warningLow) {
            return 'value-warning';
        }
        if (thresholds.warningHigh !== null && thresholds.warningHigh !== undefined && value > thresholds.warningHigh) {
            return 'value-warning';
        }

        // For power factor, show success when good (>0.95)
        if (measurementType === 'power_factor' && value >= 0.95) {
            return 'value-success';
        }

        // For THD, show success when very low (<2%)
        if (measurementType === 'thd' && value < 2) {
            return 'value-success';
        }

        return 'value-normal';
    }

    /**
     * Auto-fill threshold fields in Add/Edit modal based on detected type
     * @param {string} prefix - 'add' or 'edit'
     * @param {string} unit - Register unit
     * @param {string} name - Register name
     * @param {object} existingThresholds - Existing thresholds to use (for edit mode)
     */
    autoFillThresholds(prefix, unit, name, existingThresholds = null) {
        const detectedDiv = document.getElementById(`${prefix}ThresholdDetected`);
        const enabledCheckbox = document.getElementById(`${prefix}ThresholdEnabled`);

        // Get template based on detection
        const template = this.getThresholdTemplate(unit, name);
        const typeNames = {
            voltage_ln: 'Voltage L-N',
            voltage_ll: 'Voltage L-L',
            frequency: 'Frequency',
            power_factor: 'Power Factor',
            thd: 'THD',
            current: 'Current',
            power: 'Power'
        };

        if (template) {
            detectedDiv.innerHTML = `Detected: <span class="detected-type">${typeNames[template.templateType] || template.templateType}</span> - thresholds auto-filled`;
            detectedDiv.classList.add('visible');
        } else {
            detectedDiv.innerHTML = '';
            detectedDiv.classList.remove('visible');
        }

        // Use existing thresholds if provided, otherwise use template
        const thresholds = existingThresholds || template || {};

        // Enable checkbox
        enabledCheckbox.checked = existingThresholds ? existingThresholds.enabled !== false : !!template;

        // Fill the fields
        document.getElementById(`${prefix}ThreshDangerLow`).value = thresholds.dangerLow ?? '';
        document.getElementById(`${prefix}ThreshWarningLow`).value = thresholds.warningLow ?? '';
        document.getElementById(`${prefix}ThreshWarningHigh`).value = thresholds.warningHigh ?? '';
        document.getElementById(`${prefix}ThreshDangerHigh`).value = thresholds.dangerHigh ?? '';
    }

    /**
     * Read threshold values from modal form
     * @param {string} prefix - 'add' or 'edit'
     * @returns {object|null} Threshold object or null if disabled
     */
    readThresholdsFromForm(prefix) {
        const enabled = document.getElementById(`${prefix}ThresholdEnabled`).checked;

        const parseVal = (id) => {
            const val = document.getElementById(id).value;
            return val === '' ? null : parseFloat(val);
        };

        return {
            enabled,
            dangerLow: parseVal(`${prefix}ThreshDangerLow`),
            warningLow: parseVal(`${prefix}ThreshWarningLow`),
            warningHigh: parseVal(`${prefix}ThreshWarningHigh`),
            dangerHigh: parseVal(`${prefix}ThreshDangerHigh`)
        };
    }

    // ============ Theme System ============

    initTheme() {
        // Apply saved theme immediately
        this.applyTheme(this.theme);

        // Listen for system preference changes
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
            if (this.theme === 'auto') {
                this.applyTheme('auto');
            }
        });

        // Setup toggle buttons
        this.setupThemeToggle();
    }

    applyTheme(theme) {
        this.theme = theme;
        localStorage.setItem('janitza-theme', theme);

        let effectiveTheme = theme;
        if (theme === 'auto') {
            effectiveTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
        }

        document.documentElement.setAttribute('data-theme', effectiveTheme);
        this.updateThemeIcons();
    }

    updateThemeIcons() {
        const toggle = document.getElementById('themeToggle');
        if (toggle) {
            const icon = toggle.querySelector('i');
            if (icon) {
                // Update icon based on current effective theme
                const effectiveTheme = this.theme === 'auto'
                    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
                    : this.theme;
                icon.className = effectiveTheme === 'dark' ? 'bi bi-moon-fill' : 'bi bi-sun-fill';
            }
        }
    }

    setupThemeToggle() {
        const toggle = document.getElementById('themeToggle');

        // Simple toggle: cycles dark → light → auto → dark
        toggle?.addEventListener('click', () => {
            const cycle = { dark: 'light', light: 'auto', auto: 'dark' };
            this.applyTheme(cycle[this.theme]);
        });
    }

    // ============ Connection Banner ============

    showConnectionBanner(type, text) {
        const banner = document.getElementById('connectionBanner');
        const bannerText = document.getElementById('connectionBannerText');

        if (banner && bannerText) {
            banner.className = `connection-banner ${type} visible`;
            bannerText.textContent = text;
        }
    }

    hideConnectionBanner() {
        const banner = document.getElementById('connectionBanner');
        if (banner) {
            banner.classList.remove('visible');
        }
    }

    // ============ Modal Helpers ============

    openModal(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.classList.add('active');
            document.body.classList.add('modal-open');
        }
    }

    closeModal(modalId) {
        const modal = modalId ? document.getElementById(modalId) : document.querySelector('.modal.active');
        if (modal) {
            modal.classList.remove('active');
            document.body.classList.remove('modal-open');
        }
    }

    // ============ Toast Notifications ============

    showToast(type, title, message, duration = 4000) {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;

        const icons = {
            success: '&#10003;',
            error: '&#10007;',
            info: '&#8505;',
            warning: '&#9888;'
        };

        toast.innerHTML = `
            <span class="toast-icon">${icons[type] || icons.info}</span>
            <div class="toast-content">
                <div class="toast-title">${title}</div>
                ${message ? `<div class="toast-message">${message}</div>` : ''}
            </div>
            <button class="toast-close">&times;</button>
        `;

        toast.querySelector('.toast-close').addEventListener('click', () => {
            toast.classList.add('fade-out');
            setTimeout(() => toast.remove(), 300);
        });

        container.appendChild(toast);

        setTimeout(() => {
            if (toast.parentNode) {
                toast.classList.add('fade-out');
                setTimeout(() => toast.remove(), 300);
            }
        }, duration);
    }

    async init() {
        // Initialize theme FIRST to prevent flash
        this.initTheme();

        // Setup navigation
        this.setupNavigation();

        // Setup event listeners
        this.setupEventListeners();

        // Connect WebSocket
        this.connectWebSocket();

        // Load initial data
        await this.loadConfig();
        await this.loadStatus();
        await this.loadAllRegisters();
        await this.loadSelectedRegisters();

        // Start status polling
        setInterval(() => this.loadStatus(), 5000);
    }

    async loadConfig() {
        try {
            const response = await fetch('/api/config');
            this.config = await response.json();
        } catch (error) {
            console.error('Failed to load config:', error);
        }
    }

    setupNavigation() {
        document.querySelectorAll('.nav-tab').forEach(tab => {
            tab.addEventListener('click', (e) => {
                e.preventDefault();
                const page = tab.dataset.page;
                this.navigateTo(page);
            });
        });
    }

    navigateTo(page) {
        // Update nav tabs
        document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
        document.querySelector(`.nav-tab[data-page="${page}"]`)?.classList.add('active');

        // Update pages
        document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
        document.getElementById(`${page}Page`)?.classList.add('active');

        this.currentPage = page;

        // stop any virtual-meter observability polling when leaving the page
        if (page !== 'vmeters' && this._stopVmPolls) this._stopVmPolls();

        // Page-specific init
        if (page === 'registers') {
            this.renderRegistersTable();
        } else if (page === 'config') {
            this.loadSettingsConfig();
            this.updateConfigTabs();
            this.renderSelectedRegistersList();
            this.renderPollGroups();
            this.renderStatusDetails();
        } else if (page === 'monitor') {
            this.initMonitorPage();
        } else if (page === 'vmeters') {
            this.renderVirtualMeters();
        }
    }

    async renderVirtualMeters() {
        const el = document.getElementById('vmetersContent');
        if (!el) return;
        this._stopVmPolls();                          // re-render → drop stale timers
        const refreshBtn = document.getElementById('vmRefreshBtn');
        if (refreshBtn && !refreshBtn._wired) {
            refreshBtn._wired = true;
            refreshBtn.addEventListener('click', () => this.renderVirtualMeters());
        }
        let data;
        try {
            data = await (await fetch('/api/virtual-meters')).json();
        } catch (e) {
            el.innerHTML = '<p style="color:#c0392b;">Could not load virtual meters.</p>';
            return;
        }
        const insts = data.instances || [];
        let templates = [];
        try { templates = (await (await fetch('/api/virtual-meters/templates')).json()).templates || []; } catch (e) {}
        const configured = new Set(insts.map(i => i.template));
        const pr = data.port_range || {};
        this._vmPortRange = pr;                       // reused by the template editor
        const opts = templates.filter(t => !configured.has(t.id))
            .map(t => `<option value="${this._esc(t.id)}">${this._esc(t.name)}</option>`).join('');
        const noFree = pr.next_free == null;
        const canAdd = opts && !noFree;
        const portHint = (pr.start != null)
            ? `published range ${pr.start}–${pr.end}${pr.used && pr.used.length ? ' · used: ' + pr.used.join(', ') : ''}`
            : '';
        const addBar = `
            <div class="settings-card" style="margin-bottom:14px;">
              <div class="settings-card-body" style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;">
                <div><label class="form-label">Template</label><br>
                  <select id="vmAddTemplate" class="input" style="min-width:280px;" ${opts ? '' : 'disabled'}>
                    ${opts || '<option>— all templates already added —</option>'}</select></div>
                <div><label class="form-label">Port</label><br>
                  <input id="vmAddPort" class="input" type="number" value="${pr.next_free ?? ''}"
                    min="${pr.start ?? ''}" max="${pr.end ?? ''}" style="width:90px;"></div>
                <div><label class="form-label">Unit</label><br>
                  <input id="vmAddUnit" class="input" type="number" value="1" style="width:64px;"></div>
                <button class="btn" id="vmAddBtn" ${canAdd ? '' : 'disabled'}><i class="bi bi-plus-lg"></i> Add instance</button>
              </div>
              ${portHint ? `<div class="settings-card-body" style="padding-top:0;color:#8a94a0;font-size:12px;">${noFree ? '<span style="color:#e08e0b;">No free port in range — widen VMETER_PORT_END.</span> ' : ''}${portHint}</div>` : ''}
            </div>`;
        const cards = !insts.length
            ? '<p style="color:#8a94a0;">No virtual meters configured.</p>'
            : insts.map(m => {
            const on = m.running;
            const badge = m.state === 'ok'
                ? '<span style="color:#1a8f4c;font-weight:600;">● LISTENING</span>'
                : m.state === 'stale'
                ? '<span style="color:#e08e0b;font-weight:600;" title="source stale — meter stopped responding (consumer fail-safe)">● STALE</span>'
                : m.state === 'down'
                ? '<span style="color:#c0392b;font-weight:600;" title="enabled but not serving — crashed or failed to start">● DOWN</span>'
                : (m.enabled ? '<span style="color:#e08e0b;font-weight:600;">● starting…</span>'
                             : '<span style="color:#8a94a0;">○ disabled</span>');
            const prev = Object.entries(m.preview || {})
                .map(([k, v]) => `<tr><td style="padding:2px 10px 2px 0;color:#8a94a0;">${this._esc(k)}</td>`
                    + `<td style="padding:2px 0;text-align:right;font-variant-numeric:tabular-nums;">`
                    + `${v === null || v === undefined ? '—' : (typeof v === 'number' ? v.toFixed(2) : this._esc(v))}</td></tr>`)
                .join('');
            const conns = m.connections || [];
            const connRows = conns.length
                ? conns.map(c => `<tr><td style="padding:2px 14px 2px 0;color:#5a6470;font-family:monospace;">${this._esc(c.ip)}${c.port ? ':' + c.port : ''}</td>`
                    + `<td style="padding:2px 0;color:#8a94a0;font-variant-numeric:tabular-nums;white-space:nowrap;" title="connection uptime">up ${this._dur(c.connected_s)}</td></tr>`).join('')
                : '<tr><td style="color:#8a94a0;">no active connections</td></tr>';
            const summary = `:${m.port ?? '—'} · ${conns.length} conn${conns.length === 1 ? '' : 's'}`
                + (m.running ? ` · ${m.requests ?? 0} req · ${m.req_rate ?? 0}/s` : '');
            return `
            <div class="settings-card vm-acc" style="margin-bottom:12px;">
              <div class="settings-card-header vm-acc-head" role="button" tabindex="0" aria-expanded="false" style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;">
                <div style="display:flex;align-items:center;gap:10px;min-width:0;">
                  <i class="bi bi-chevron-right vm-acc-chev" style="transition:transform .15s ease;color:#8a94a0;"></i>
                  <h3 style="margin:0;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"><i class="bi bi-hdd-network"></i> ${this._esc(m.name || m.template)}</h3>
                  ${badge}
                </div>
                <div style="display:flex;align-items:center;gap:14px;" onclick="event.stopPropagation()">
                  <span style="font-size:12px;color:#8a94a0;font-variant-numeric:tabular-nums;white-space:nowrap;">${summary}</span>
                  <label class="toggle-switch" title="Enable / disable">
                    <input type="checkbox" data-vm="${this._esc(m.template)}" ${m.enabled ? 'checked' : ''}>
                    <span class="toggle-slider"></span>
                  </label>
                  <button class="btn btn-ghost btn-sm" data-vm-del="${this._esc(m.template)}" title="Remove instance"><i class="bi bi-trash"></i></button>
                </div>
              </div>
              <div class="settings-card-body vm-acc-body" hidden>
                <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:12px;font-size:13px;">
                  <div>template <b>${this._esc(m.template)}</b></div>
                  <div>port <b>${m.port ?? '—'}</b> · unit <b>${m.unit_id ?? 1}</b></div>
                  ${m.running ? `<div>req <b>${m.requests ?? 0}</b> · <b>${m.req_rate ?? 0}</b>/s</div>` : ''}
                  ${m.errors ? `<div style="color:#c0392b;">${m.errors} errors</div>` : ''}
                  ${m.last_error ? `<div style="color:#c77700;" title="${this._esc(m.last_error.message || '')}"><i class="bi bi-exclamation-triangle"></i> ${this._esc(m.last_error.kind || '')}</div>` : ''}
                </div>
                <div style="display:flex;gap:36px;flex-wrap:wrap;">
                  <div style="font-size:12.5px;">
                    <div style="color:#8a94a0;margin-bottom:4px;"><i class="bi bi-plug"></i> Connections (${conns.length})</div>
                    <table>${connRows}</table></div>
                  ${prev ? `<div style="font-size:12.5px;"><div style="color:#8a94a0;margin-bottom:4px;">Live values served</div>
                    <table>${prev}</table></div>` : ''}
                </div>
              </div>
            </div>`;
        }).join('');
        // ── Templates section (define / edit / delete) ──
        const tmplCards = templates.map(t => `
            <div class="settings-card" style="margin-bottom:10px;">
              <div class="settings-card-body" style="display:flex;justify-content:space-between;align-items:center;gap:12px;">
                <div><b>${this._esc(t.name)}</b><br>
                  <span style="color:#8a94a0;font-size:12px;">id <code>${this._esc(t.id)}</code> · ${t.kind} · ${t.registers} registers</span></div>
                <div style="display:flex;gap:8px;">
                  <button class="btn btn-sm" data-vm-edit="${this._esc(t.id)}"><i class="bi bi-pencil"></i> Edit</button>
                  <button class="btn btn-ghost btn-sm" data-vm-export="${this._esc(t.id)}" title="Export YAML"><i class="bi bi-download"></i></button>
                  <button class="btn btn-ghost btn-sm" data-vm-tpldel="${this._esc(t.id)}" title="Delete template"><i class="bi bi-trash"></i></button>
                </div>
              </div>
            </div>`).join('');
        const tmplSection = `
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                <h3 style="margin:0;"><i class="bi bi-diagram-3"></i> Templates</h3>
                <div style="display:flex;gap:8px;">
                  <button class="btn btn-ghost btn-sm" id="vmImportBtn"><i class="bi bi-upload"></i> Import</button>
                  <button class="btn btn-sm" id="vmNewTplBtn"><i class="bi bi-plus-lg"></i> New template</button>
                </div>
              </div>
              <p style="color:#8a94a0;font-size:12.5px;margin:0 0 10px;">Map live Janitza registers into the register layout a consumer expects. Editing a template that an instance uses reloads it live. Export shares a template as YAML; Import validates before saving.</p>
              <input type="file" id="vmImportFile" accept=".yaml,.yml" style="display:none;">
              ${tmplCards || '<p style="color:#8a94a0;">No templates yet.</p>'}`;
        // ── meters who are running (for the Logs/Stats meter picker) ──
        const runningInsts = insts.filter(m => m.running);
        const meterOpts = runningInsts.map(m =>
            `<option value="${this._esc(m.template)}">${this._esc(m.name || m.template)} · :${m.port}</option>`).join('')
            || '<option value="">— no running meters —</option>';
        el.innerHTML = `
            <div class="config-main-tabs" id="vmSubtabs">
              <button class="config-main-tab active" data-vmtab="meters"><i class="bi bi-hdd-network"></i> Meters</button>
              <button class="config-main-tab" data-vmtab="templates"><i class="bi bi-diagram-3"></i> Templates</button>
              <button class="config-main-tab" data-vmtab="logs"><i class="bi bi-list-columns-reverse"></i> Logs</button>
              <button class="config-main-tab" data-vmtab="stats"><i class="bi bi-graph-up"></i> Stats &amp; Debug</button>
            </div>
            <div data-vmpanel="meters">${addBar}${cards}</div>
            <div data-vmpanel="templates" hidden>${tmplSection}</div>
            <div data-vmpanel="logs" hidden>
              <style>
                .vm-log-row{cursor:pointer;}
                .vm-log-row:hover{background:rgba(59,130,246,.08);}
                .vm-log-row.active{background:rgba(59,130,246,.16);}
                .vm-log-row.err{background:rgba(192,57,43,.06);}
                .vm-log-row .vm-view{opacity:0;color:#3b82f6;font-weight:600;white-space:nowrap;transition:opacity .1s;}
                .vm-log-row:hover .vm-view,.vm-log-row.active .vm-view{opacity:1;}
              </style>
              <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap;">
                <label class="form-label">Meter</label>
                <select id="vmLogMeter" class="input" style="min-width:240px;">${meterOpts}</select>
                <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:#8a94a0;">
                  <input type="checkbox" id="vmLogLive" checked> live</label>
                <span id="vmLogMeta" style="color:#8a94a0;font-size:12.5px;margin-left:auto;"></span>
              </div>
              <div style="display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap;">
                <div id="vmLogContent" style="flex:2 1 380px;min-width:320px;"><p style="color:#8a94a0;">Select a running meter.</p></div>
                <aside id="vmDecodePanel" style="flex:1 1 320px;min-width:300px;position:sticky;top:8px;align-self:flex-start;">
                  <div class="settings-card" style="padding:16px;color:#8a94a0;">
                    <div style="font-size:13px;margin-bottom:4px;"><i class="bi bi-braces"></i> Decode</div>
                    <div style="font-size:12.5px;line-height:1.5;">Click any row on the left to decode that Modbus read — raw registers → value → the source variable each maps to.</div>
                  </div>
                </aside>
              </div>
            </div>
            <div data-vmpanel="stats" hidden>
              <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap;">
                <label class="form-label">Meter</label>
                <select id="vmStatMeter" class="input" style="min-width:240px;">${meterOpts}</select>
                <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:#8a94a0;">
                  <input type="checkbox" id="vmStatLive" checked> live</label>
              </div>
              <div id="vmStatContent"><p style="color:#8a94a0;">Select a running meter.</p></div>
            </div>`;
        this._wireVmSubtabs(el);
        // wire template editor buttons
        const newTplBtn = document.getElementById('vmNewTplBtn');
        if (newTplBtn) newTplBtn.addEventListener('click', () => this.openTemplateEditor(null));
        el.querySelectorAll('button[data-vm-edit]').forEach(b =>
            b.addEventListener('click', () => this.openTemplateEditor(b.dataset.vmEdit)));
        el.querySelectorAll('button[data-vm-tpldel]').forEach(b =>
            b.addEventListener('click', () => this.deleteTemplate(b.dataset.vmTpldel)));
        el.querySelectorAll('button[data-vm-export]').forEach(b =>
            b.addEventListener('click', () => this.exportTemplate(b.dataset.vmExport)));
        // accordion: click a meter header to expand/collapse its body
        el.querySelectorAll('.vm-acc-head').forEach(h => {
            const toggle = () => {
                const card = h.closest('.vm-acc');
                const body = card && card.querySelector('.vm-acc-body');
                const chev = h.querySelector('.vm-acc-chev');
                if (!body) return;
                const opening = body.hidden;
                body.hidden = !opening;
                h.setAttribute('aria-expanded', String(opening));
                if (chev) chev.style.transform = opening ? 'rotate(90deg)' : '';
            };
            h.addEventListener('click', toggle);
            h.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
            });
        });
        // wire import (file picker → POST yaml)
        const importBtn = document.getElementById('vmImportBtn');
        const importFile = document.getElementById('vmImportFile');
        if (importBtn && importFile) {
            importBtn.addEventListener('click', () => importFile.click());
            importFile.addEventListener('change', () => this.importTemplate(importFile));
        }
        // wire add-instance
        const addBtn = document.getElementById('vmAddBtn');
        if (addBtn) addBtn.addEventListener('click', async () => {
            const template = document.getElementById('vmAddTemplate').value;
            const port = parseInt(document.getElementById('vmAddPort').value, 10);
            const unit_id = parseInt(document.getElementById('vmAddUnit').value, 10) || 1;
            if (pr.start != null && (port < pr.start || port > pr.end)) {
                this.showToast('error', 'Port out of range', `Use a port in ${pr.start}–${pr.end} (published on the host).`);
                return;
            }
            addBtn.disabled = true;
            try {
                const r = await fetch('/api/virtual-meters', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ template, port, unit_id, enabled: false })
                });
                if (r.ok) this.showToast('success', 'Virtual meter', `${template} added`);
                else this.showToast('error', 'Add failed', (await r.json()).detail || '');
            } catch (e) { this.showToast('error', 'Add failed', template); }
            setTimeout(() => this.renderVirtualMeters(), 600);
        });
        // wire delete
        el.querySelectorAll('button[data-vm-del]').forEach(b => {
            b.addEventListener('click', async () => {
                const tmpl = b.dataset.vmDel;
                if (!confirm(`Remove virtual meter "${tmpl}"?`)) return;
                try {
                    await fetch(`/api/virtual-meters/${encodeURIComponent(tmpl)}`, { method: 'DELETE' });
                    this.showToast('success', 'Virtual meter', `${tmpl} removed`);
                } catch (e) { this.showToast('error', 'Remove failed', tmpl); }
                setTimeout(() => this.renderVirtualMeters(), 600);
            });
        });
        // wire toggles
        el.querySelectorAll('input[data-vm]').forEach(cb => {
            cb.addEventListener('change', async () => {
                const tmpl = cb.dataset.vm, on = cb.checked;
                cb.disabled = true;
                try {
                    await fetch(`/api/virtual-meters/${encodeURIComponent(tmpl)}/toggle?on=${on}`, { method: 'POST' });
                    this.showToast('success', 'Virtual meter', `${tmpl} ${on ? 'enabled' : 'disabled'}`);
                } catch (e) {
                    this.showToast('error', 'Toggle failed', tmpl);
                }
                setTimeout(() => this.renderVirtualMeters(), 1200);
            });
        });
    }

    _esc(s) {
        return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
            .replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    _dur(s) {
        s = Math.max(0, Math.floor(s || 0));
        if (s < 60) return s + 's';
        if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
        if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
        return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
    }

    // ── Virtual-meter sub-tabs (Meters / Templates / Logs / Stats) ──────────
    _wireVmSubtabs(el) {
        const tabs = el.querySelectorAll('#vmSubtabs .config-main-tab');
        const show = (name) => {
            tabs.forEach(t => t.classList.toggle('active', t.dataset.vmtab === name));
            el.querySelectorAll('[data-vmpanel]').forEach(p => { p.hidden = p.dataset.vmpanel !== name; });
            this._stopVmPolls();
            if (name === 'logs') this.renderVmLogs();
            else if (name === 'stats') this.renderVmStats();
        };
        tabs.forEach(t => t.addEventListener('click', () => show(t.dataset.vmtab)));
        const lm = el.querySelector('#vmLogMeter'), sm = el.querySelector('#vmStatMeter');
        if (lm) lm.addEventListener('change', () => this.renderVmLogs());
        if (sm) sm.addEventListener('change', () => this.renderVmStats());
        const ll = el.querySelector('#vmLogLive'), sl = el.querySelector('#vmStatLive');
        if (ll) ll.addEventListener('change', () => this.renderVmLogs());
        if (sl) sl.addEventListener('change', () => this.renderVmStats());
    }

    _stopVmPolls() {
        if (this._vmLogTimer) { clearInterval(this._vmLogTimer); this._vmLogTimer = null; }
        if (this._vmStatTimer) { clearInterval(this._vmStatTimer); this._vmStatTimer = null; }
    }

    async exportTemplate(id) {
        try {
            const r = await fetch(`/api/virtual-meters/template/${encodeURIComponent(id)}/export`);
            if (!r.ok) { this.showToast('error', 'Export failed', id); return; }
            const d = await r.json();
            const blob = new Blob([d.yaml], { type: 'text/yaml' });
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob); a.download = d.filename || `${id}.yaml`;
            document.body.appendChild(a); a.click(); a.remove();
            URL.revokeObjectURL(a.href);
            this.showToast('success', 'Exported', d.filename || id);
        } catch (e) { this.showToast('error', 'Export failed', id); }
    }

    async importTemplate(fileInput) {
        const f = fileInput.files && fileInput.files[0];
        if (!f) return;
        fileInput.value = '';
        let text;
        try { text = await f.text(); } catch (e) { this.showToast('error', 'Read failed', f.name); return; }
        const post = (overwrite) => fetch('/api/virtual-meters/templates/import', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ yaml: text, overwrite })
        });
        try {
            let r = await post(false);
            if (r.status === 409) {
                if (!confirm('A template with this id already exists. Overwrite it?')) return;
                r = await post(true);
            }
            const d = await r.json().catch(() => ({}));
            if (r.ok) { this.showToast('success', 'Imported', `${d.id} · ${d.registers} registers`); this.renderVirtualMeters(); }
            else this.showToast('error', 'Import failed', d.detail || `HTTP ${r.status}`);
        } catch (e) { this.showToast('error', 'Import failed', f.name); }
    }

    async renderVmLogs() {
        const sel = document.getElementById('vmLogMeter');
        const out = document.getElementById('vmLogContent');
        const meta = document.getElementById('vmLogMeta');
        const live = document.getElementById('vmLogLive');
        if (!sel || !out) return;
        const id = sel.value;
        if (!id) { out.innerHTML = '<p style="color:#8a94a0;">No running meter selected.</p>'; return; }
        if (this._vmLogLastId !== id) {           // meter changed → drop stale decode selection
            this._vmLogLastId = id;
            this._vmDecodeSel = null;
            const panel = document.getElementById('vmDecodePanel');
            if (panel) panel.innerHTML = `<div class="settings-card" style="padding:16px;color:#8a94a0;">
                <div style="font-size:13px;margin-bottom:4px;"><i class="bi bi-braces"></i> Decode</div>
                <div style="font-size:12.5px;line-height:1.5;">Click any row on the left to decode that Modbus read — raw registers → value → the source variable each maps to.</div></div>`;
        }
        if (!out._decodeWired) {                  // delegated: click a row → decode in side panel
            out._decodeWired = true;
            out.addEventListener('click', (e) => {
                const row = e.target.closest('.vm-log-row');
                if (!row) return;
                out.querySelectorAll('.vm-log-row.active').forEach(r => r.classList.remove('active'));
                row.classList.add('active');
                const cur = document.getElementById('vmLogMeter');
                this._vmDecodeSel = { addr: +row.dataset.addr, count: +row.dataset.count };
                this.renderDecodeInto(cur ? cur.value : id, this._vmDecodeSel.addr, this._vmDecodeSel.count);
            });
        }
        const load = async () => {
            if (document.hidden) return;          // don't poll a backgrounded tab
            let d;
            try { d = await (await fetch(`/api/virtual-meters/${encodeURIComponent(id)}/stats?limit=200`)).json(); }
            catch (e) { out.innerHTML = '<p style="color:#c0392b;">Could not load.</p>'; return; }
            if (d.error) { out.innerHTML = `<p style="color:#8a94a0;">${this._esc(d.error)}</p>`; return; }
            if (meta) meta.textContent = `${d.total} reqs · ${d.errors} errors · :${d.port} unit ${d.unit_id}`;
            const qs = (d.queries || []).slice().reverse();
            const rows = qs.map(q => {
                const ms = String(Math.floor((q.ts % 1) * 1000)).padStart(3, '0');
                const t = new Date(q.ts * 1000).toLocaleTimeString('en-GB', { hour12: false }) + '.' + ms;
                const resp = q.resp ? q.resp.slice(0, 6).join(' ') + (q.count > 6 ? ' …' : '') : '—';
                const res = q.err ? '<span style="color:#c0392b;font-weight:600;">EXC</span>'
                                  : '<span style="color:#1a8f4c;">OK</span>';
                return `<tr class="vm-log-row${q.err ? ' err' : ''}" data-addr="${q.addr}" data-count="${q.count}" title="click to decode this read" style="border-bottom:1px solid var(--border-light);">
                  <td style="padding:3px 10px 3px 0;color:#8a94a0;font-variant-numeric:tabular-nums;">${t}</td>
                  <td style="padding:3px 10px 3px 0;">FC${q.fc}</td>
                  <td style="padding:3px 10px 3px 0;font-variant-numeric:tabular-nums;">${q.addr}</td>
                  <td style="padding:3px 10px 3px 0;font-variant-numeric:tabular-nums;">${q.count}</td>
                  <td style="padding:3px 10px 3px 0;">${res}</td>
                  <td style="padding:3px 10px 3px 0;color:#8a94a0;font-variant-numeric:tabular-nums;">${q.lat_us}µs</td>
                  <td style="padding:3px 10px 3px 0;font-family:monospace;font-size:12px;color:#5a6470;">${this._esc(resp)}</td>
                  <td style="padding:3px 0;text-align:right;"><span class="vm-view">decode ›</span></td></tr>`;
            }).join('');
            out.innerHTML = `<div style="max-height:62vh;overflow:auto;">
              <table style="width:100%;font-size:12.5px;border-collapse:collapse;">
                <thead><tr style="text-align:left;color:#8a94a0;border-bottom:2px solid var(--border);position:sticky;top:0;background:var(--bg-secondary);">
                  <th style="padding:4px 10px 4px 0;">time</th><th>fc</th><th>addr</th><th>count</th><th>result</th><th>latency</th><th>response</th><th></th>
                </tr></thead><tbody>${rows || '<tr><td colspan="8" style="color:#8a94a0;padding:8px 0;">No queries yet.</td></tr>'}</tbody></table></div>`;
            // keep the decoded row highlighted across live refreshes
            if (this._vmDecodeSel) {
                const sel = out.querySelector(`.vm-log-row[data-addr="${this._vmDecodeSel.addr}"][data-count="${this._vmDecodeSel.count}"]`);
                if (sel) sel.classList.add('active');
            }
        };
        await load();
        this._stopVmPolls();
        if (live && live.checked) this._vmLogTimer = setInterval(load, 2000);
    }

    async renderVmStats() {
        const sel = document.getElementById('vmStatMeter');
        const out = document.getElementById('vmStatContent');
        const live = document.getElementById('vmStatLive');
        if (!sel || !out) return;
        const id = sel.value;
        if (!id) { out.innerHTML = '<p style="color:#8a94a0;">No running meter selected.</p>'; return; }
        const load = async () => {
            if (document.hidden) return;          // don't poll a backgrounded tab
            let d;
            try { d = await (await fetch(`/api/virtual-meters/${encodeURIComponent(id)}/stats?limit=1`)).json(); }
            catch (e) { out.innerHTML = '<p style="color:#c0392b;">Could not load.</p>'; return; }
            if (d.error) { out.innerHTML = `<p style="color:#8a94a0;">${this._esc(d.error)}</p>`; return; }
            const upt = d.first_ts ? Math.max(1, Math.round(Date.now() / 1000 - d.first_ts)) : 0;
            const rps = upt ? d.total / upt : 0;
            const kb = (n) => n > 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B';
            const cards = [
                ['Total requests', d.total, ''],
                ['Errors', d.errors, d.errors ? '#c0392b' : '#1a8f4c'],
                ['Avg rate', rps.toFixed(2) + '/s', ''],
                ['RX', kb(d.bytes_rx), ''],
                ['TX', kb(d.bytes_tx), ''],
                ['Uptime', upt + ' s', ''],
            ].map(([k, v, c]) => `<div class="settings-card" style="flex:1;min-width:120px;padding:12px;">
                <div style="color:#8a94a0;font-size:12px;">${k}</div>
                <div style="font-size:22px;font-weight:600;color:${c || 'inherit'};font-variant-numeric:tabular-nums;">${v}</div></div>`).join('');
            const rate = d.rate || [];
            const spark = this._sparkline(rate.map(r => r[1]), 560, 72);
            const top = d.top_addrs || [];
            const maxc = Math.max(1, ...top.map(t => t[1]));
            const bars = top.map(([addr, c]) => `<div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12.5px;">
                <code style="width:70px;text-align:right;color:#5a6470;">${addr}</code>
                <div style="flex:1;background:var(--border-light);border-radius:3px;overflow:hidden;"><div style="width:${(c / maxc * 100).toFixed(1)}%;background:#3b82f6;height:14px;"></div></div>
                <span style="width:52px;text-align:right;font-variant-numeric:tabular-nums;color:#8a94a0;">${c}</span></div>`).join('');
            const evColor = { error: '#c0392b', warn: '#c77700', info: '#5a6470' };
            const events = (d.events || []).slice().reverse();   // newest first
            const evRows = events.map(e => {
                const t = e.ts ? new Date(e.ts * 1000).toLocaleTimeString('en-GB') : '';
                const c = evColor[e.level] || '#5a6470';
                return `<tr>
                  <td style="white-space:nowrap;color:#8a94a0;font-variant-numeric:tabular-nums;">${t}</td>
                  <td><span style="display:inline-block;padding:1px 7px;border-radius:3px;font-size:11px;font-weight:600;color:#fff;background:${c};">${this._esc(e.level || '')}</span></td>
                  <td><code style="color:#5a6470;">${this._esc(e.kind || '')}</code></td>
                  <td style="color:var(--text);">${this._esc(e.message || '')}</td></tr>`;
            }).join('');
            out.innerHTML = `
              <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px;">${cards}</div>
              <div class="settings-card" style="padding:14px;margin-bottom:16px;">
                <div style="color:#8a94a0;font-size:12.5px;margin-bottom:8px;">Requests / second (last ${rate.length}s)</div>${spark}</div>
              <div class="settings-card" style="padding:14px;margin-bottom:16px;">
                <div style="color:#8a94a0;font-size:12.5px;margin-bottom:8px;">Recent events &amp; errors (last ${events.length}) — what happened to this meter</div>
                ${events.length ? `<div style="overflow-x:auto;"><table class="data-table" style="width:100%;font-size:12.5px;">
                  <thead><tr><th>Time</th><th>Level</th><th>Kind</th><th>Message</th></tr></thead>
                  <tbody>${evRows}</tbody></table></div>`
                  : '<span style="color:#1a8f4c;">No errors — meter has been healthy.</span>'}</div>
              <div class="settings-card" style="padding:14px;">
                <div style="color:#8a94a0;font-size:12.5px;margin-bottom:8px;">Most-read registers (where the consumer looks)</div>
                ${bars || '<span style="color:#8a94a0;">No reads yet.</span>'}</div>`;
        };
        await load();
        this._stopVmPolls();
        if (live && live.checked) this._vmStatTimer = setInterval(load, 2000);
    }

    _sparkline(values, w, h) {
        if (!values || !values.length) return '<span style="color:#8a94a0;">No data yet.</span>';
        const max = Math.max(1, ...values), n = values.length;
        const step = n > 1 ? w / (n - 1) : w;
        const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(h - (v / max) * (h - 6) - 3).toFixed(1)}`).join(' ');
        const area = `0,${h} ${pts} ${((n - 1) * step).toFixed(1)},${h}`;
        return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none" role="img" aria-label="requests per second, peak ${max}" style="display:block;">
          <polygon points="${area}" fill="rgba(59,130,246,0.13)"/>
          <polyline points="${pts}" fill="none" stroke="#3b82f6" stroke-width="1.5"/>
          <text x="3" y="13" font-size="11" fill="#8a94a0">peak ${max}/s</text></svg>`;
    }

    // ── Decode a logged read: raw words → value → the source variable ──────
    _buildDecodeBody(d) {
        const rows = (d.registers || []).map(r => {
            const words = (r.words || []).join(' ');
            const val = (r.value === null || r.value === undefined) ? '—' : r.value;
            const scale = (r.scale !== 1) ? ` <span style="opacity:.6">×${r.scale}</span>` : '';
            return `<tr style="border-bottom:1px solid var(--border-light);">
              <td style="padding:3px 10px 3px 0;font-variant-numeric:tabular-nums;color:#8a94a0;white-space:nowrap;">${r.addr} <span style="opacity:.55;">0x${(r.addr).toString(16)}</span></td>
              <td style="padding:3px 10px 3px 0;font-family:monospace;">${this._esc(r.source)}</td>
              <td style="padding:3px 10px 3px 0;color:#8a94a0;white-space:nowrap;">${this._esc(r.type)}${scale}</td>
              <td style="padding:3px 10px 3px 0;font-family:monospace;font-size:11.5px;color:#5a6470;">${this._esc(words)}</td>
              <td style="padding:3px 0;font-weight:600;font-variant-numeric:tabular-nums;">${this._esc(val)}</td></tr>`;
        }).join('');
        return `<div style="overflow:auto;"><table style="width:100%;border-collapse:collapse;font-size:12.5px;">
            <thead><tr style="text-align:left;color:#8a94a0;border-bottom:2px solid var(--border);">
              <th style="padding:4px 10px 4px 0;">addr</th><th>source / variable</th><th>type</th><th>raw words</th><th>value</th>
            </tr></thead><tbody>${rows || '<tr><td colspan="5" style="color:#8a94a0;padding:8px 0;">No template registers map into this range.</td></tr>'}</tbody></table></div>`;
    }

    async renderDecodeInto(id, addr, count) {
        const panel = document.getElementById('vmDecodePanel');
        if (!panel || !id || !Number.isFinite(addr)) return;
        const wrap = (inner) => `<div class="settings-card" style="padding:14px;">
            <h4 style="margin:0 0 8px;font-size:13.5px;"><i class="bi bi-braces"></i> Decode · addr ${addr} · count ${count}</h4>${inner}</div>`;
        panel.innerHTML = wrap('<p style="color:#8a94a0;font-size:12.5px;margin:0;">Decoding…</p>');
        let d;
        try {
            d = await (await fetch(`/api/virtual-meters/${encodeURIComponent(id)}/decode?addr=${addr}&count=${count}`)).json();
        } catch (e) { panel.innerHTML = wrap('<p style="color:#c0392b;font-size:12.5px;margin:0;">Decode failed.</p>'); return; }
        if (d.error) { panel.innerHTML = wrap(`<p style="color:#8a94a0;font-size:12.5px;margin:0;">${this._esc(d.error)}</p>`); return; }
        panel.innerHTML = wrap(this._buildDecodeBody(d));
    }

    async openTemplateEditor(templateId) {
        if (!this._vmSources) {
            try {
                const s = await (await fetch('/api/virtual-meters/sources')).json();
                this._vmSources = s.sources || [];
                this._vmTypes = (s.types && s.types.length) ? s.types
                    : ['int16', 'uint16', 'int32', 'uint32', 'int64', 'uint64', 'float', 'double', 'string'];
                if (s.port_range) this._vmPortRange = s.port_range;
            } catch (e) {
                this._vmSources = [];
                this._vmTypes = ['int16', 'uint16', 'int32', 'uint32', 'float', 'string'];
            }
        }
        const isNew = !templateId;
        let tpl = {
            id: '', name: '', byte_order: 'big',
            transport: { port: 1502, unit_id: 1, bind: '0.0.0.0' }, registers: [], in_use: false
        };
        if (!isNew) {
            try {
                tpl = await (await fetch(`/api/virtual-meters/template/${encodeURIComponent(templateId)}`)).json();
                if (tpl.error) { this.showToast('error', 'Load failed', tpl.error); return; }
            } catch (e) { this.showToast('error', 'Load failed', templateId); return; }
        }
        document.getElementById('vmTplTitle').textContent = isNew ? 'New template' : `Edit: ${tpl.name}`;
        this._renderTemplateForm(tpl, isNew);
        this.openModal('vmTemplateModal');
    }

    _renderTemplateForm(tpl, isNew) {
        const body = document.getElementById('vmTplBody');
        const t = tpl.transport || {};
        const pr = this._vmPortRange || {};
        const defPort = isNew ? (pr.next_free ?? t.port ?? 1502) : (t.port ?? 1502);
        const portHint = (pr.start != null) ? `published range ${pr.start}–${pr.end}` : '';
        body.innerHTML = `
          <div class="form-row">
            <div class="form-group"><label class="form-label">Template id</label>
              <input id="vmfId" class="input" value="${this._esc(tpl.id || '')}" ${isNew ? '' : 'readonly'} placeholder="my_meter (a-z 0-9 _)"></div>
            <div class="form-group"><label class="form-label">Name</label>
              <input id="vmfName" class="input" value="${this._esc(tpl.name || '')}" placeholder="My Custom Meter"></div>
          </div>
          <div class="form-row">
            <div class="form-group"><label class="form-label">Byte order</label>
              <select id="vmfByteOrder" class="input">
                <option value="big" ${tpl.byte_order === 'big' ? 'selected' : ''}>big (high word first)</option>
                <option value="little" ${tpl.byte_order === 'little' ? 'selected' : ''}>little (low word first)</option>
              </select></div>
            <div class="form-group"><label class="form-label">Port</label>
              <input id="vmfPort" class="input" type="number" value="${defPort}" min="${pr.start ?? ''}" max="${pr.end ?? ''}">
              ${portHint ? `<span class="hint-text" style="font-size:11px;">${portHint}</span>` : ''}</div>
            <div class="form-group"><label class="form-label">Unit id</label>
              <input id="vmfUnit" class="input" type="number" value="${t.unit_id ?? 1}"></div>
            <div class="form-group"><label class="form-label">Bind</label>
              <input id="vmfBind" class="input" value="${this._esc(t.bind || '0.0.0.0')}"></div>
          </div>
          ${tpl.in_use ? '<p class="hint-text" style="color:#e08e0b;"><i class="bi bi-exclamation-triangle"></i> Used by an instance — saving reloads it live.</p>' : ''}
          <div class="form-section">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <h4 style="margin:0;">Registers</h4>
              <button class="btn btn-sm" id="vmAddRowBtn"><i class="bi bi-plus-lg"></i> Add register</button>
            </div>
            <div style="overflow-x:auto;margin-top:8px;">
              <table style="width:100%;border-collapse:collapse;font-size:12.5px;">
                <thead><tr style="text-align:left;color:#8a94a0;">
                  <th style="padding:4px 6px;">Address</th><th>Type</th><th>Source</th>
                  <th>Value / Register</th><th>Scale</th><th>Len</th><th>Note</th><th></th>
                </tr></thead>
                <tbody id="vmRegRows"></tbody>
              </table>
            </div>
            <p class="hint-text">Address in hex (e.g. <code>0x0028</code>) or decimal. Scale: raw = value × scale (consumer reads raw ÷ scale). Length applies to <code>string</code> only.</p>
          </div>`;
        (tpl.registers || []).forEach(r => this._addTemplateRow(r));
        if (!(tpl.registers || []).length) this._addTemplateRow();
        document.getElementById('vmAddRowBtn').addEventListener('click', () => this._addTemplateRow());
        document.getElementById('vmTplSaveBtn').onclick = () => this.saveTemplate();
    }

    _addTemplateRow(reg) {
        reg = reg || { addr: 0, type: 'int32', scale: 1, source_kind: 'const', source: 0, length: 1, note: '' };
        const tbody = document.getElementById('vmRegRows');
        const tr = document.createElement('tr');
        tr.style.borderTop = '1px solid var(--border, #e5e7eb)';
        const typeOpts = this._vmTypes.map(ty => `<option ${ty === reg.type ? 'selected' : ''}>${ty}</option>`).join('');
        const srcOpts = this._vmSources.map(s => {
            const v = (s.value === null || s.value === undefined) ? '—'
                : (typeof s.value === 'number' ? s.value.toFixed(1) : s.value);
            return `<option value="${this._esc(s.name)}" ${s.name === reg.source ? 'selected' : ''}>`
                + `${this._esc(s.name)} — ${this._esc(s.label || '')} (${v}${s.unit ? ' ' + this._esc(s.unit) : ''})</option>`;
        }).join('');
        const addrHex = '0x' + Number(reg.addr || 0).toString(16).padStart(4, '0');
        const isLive = reg.source_kind === 'live';
        tr.innerHTML = `
          <td style="padding:3px 6px;"><input class="input input-sm vm-addr" style="width:82px;" value="${addrHex}"></td>
          <td><select class="input input-sm vm-type" style="width:92px;">${typeOpts}</select></td>
          <td><select class="input input-sm vm-kind" style="width:110px;">
            <option value="const" ${reg.source_kind === 'const' ? 'selected' : ''}>Const num</option>
            <option value="const_str" ${reg.source_kind === 'const_str' ? 'selected' : ''}>Const text</option>
            <option value="live" ${isLive ? 'selected' : ''}>Live register</option>
          </select></td>
          <td>
            <select class="input input-sm vm-src-live" style="min-width:240px;${isLive ? '' : 'display:none;'}">${srcOpts || '<option value="">(no live registers)</option>'}</select>
            <input class="input input-sm vm-src-val" style="width:160px;${isLive ? 'display:none;' : ''}" value="${isLive ? '' : this._esc(String(reg.source ?? ''))}">
          </td>
          <td><input class="input input-sm vm-scale" style="width:64px;" value="${reg.scale ?? 1}"></td>
          <td><input class="input input-sm vm-len" type="number" style="width:52px;" value="${reg.length ?? 1}"></td>
          <td><input class="input input-sm vm-note" style="width:130px;" value="${this._esc(reg.note || '')}"></td>
          <td><button class="btn btn-ghost btn-sm vm-row-del" title="Remove"><i class="bi bi-x-lg"></i></button></td>`;
        tbody.appendChild(tr);
        const kindSel = tr.querySelector('.vm-kind');
        const liveSel = tr.querySelector('.vm-src-live');
        const valInp = tr.querySelector('.vm-src-val');
        kindSel.addEventListener('change', () => {
            const live = kindSel.value === 'live';
            liveSel.style.display = live ? '' : 'none';
            valInp.style.display = live ? 'none' : '';
        });
        tr.querySelector('.vm-row-del').addEventListener('click', () => tr.remove());
    }

    async saveTemplate() {
        const id = document.getElementById('vmfId').value.trim();
        const name = document.getElementById('vmfName').value.trim();
        const byte_order = document.getElementById('vmfByteOrder').value;
        const port = parseInt(document.getElementById('vmfPort').value, 10);
        const unit_id = parseInt(document.getElementById('vmfUnit').value, 10);
        const bind = document.getElementById('vmfBind').value.trim() || '0.0.0.0';
        if (!/^[a-z0-9_]+$/.test(id)) { this.showToast('error', 'Invalid id', 'Use a-z 0-9 _ only'); return; }
        if (!name) { this.showToast('error', 'Name required', ''); return; }
        const registers = [];
        document.querySelectorAll('#vmRegRows tr').forEach(tr => {
            const kind = tr.querySelector('.vm-kind').value;
            registers.push({
                addr: tr.querySelector('.vm-addr').value.trim(),
                type: tr.querySelector('.vm-type').value,
                scale: parseFloat(tr.querySelector('.vm-scale').value) || 1,
                length: parseInt(tr.querySelector('.vm-len').value, 10) || 1,
                note: tr.querySelector('.vm-note').value.trim(),
                source_kind: kind,
                source: kind === 'live' ? tr.querySelector('.vm-src-live').value
                    : tr.querySelector('.vm-src-val').value,
            });
        });
        if (!registers.length) { this.showToast('error', 'No registers', 'Add at least one register'); return; }
        const btn = document.getElementById('vmTplSaveBtn');
        btn.disabled = true;
        try {
            const r = await fetch(`/api/virtual-meters/template/${encodeURIComponent(id)}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id, name, byte_order, port, unit_id, bind, registers })
            });
            const j = await r.json();
            if (r.ok) {
                this.showToast('success', 'Template saved',
                    `${id} (${j.registers} registers)${j.reloaded ? ' — reloaded live' : ''}`);
                this.closeModal('vmTemplateModal');
                this.renderVirtualMeters();
            } else {
                this.showToast('error', 'Save failed', j.detail || 'validation error');
            }
        } catch (e) { this.showToast('error', 'Save failed', String(e)); }
        finally { btn.disabled = false; }
    }

    async deleteTemplate(id) {
        if (!confirm(`Delete template "${id}"? (only if no instance uses it)`)) return;
        try {
            const r = await fetch(`/api/virtual-meters/template/${encodeURIComponent(id)}`, { method: 'DELETE' });
            const j = await r.json();
            if (r.ok) this.showToast('success', 'Template deleted', id);
            else this.showToast('error', 'Delete failed', j.detail || '');
        } catch (e) { this.showToast('error', 'Delete failed', id); }
        this.renderVirtualMeters();
    }

    updateConfigTabs() {
        const container = document.getElementById('configTabs');

        // Get categories from influxdb_measurement or derive from label/unit
        const categories = new Map();
        categories.set('all', 0);

        this.selectedRegisters.forEach(reg => {
            categories.set('all', categories.get('all') + 1);

            // Derive category from measurement or unit
            let cat = (reg.influxdb_measurement || '').toLowerCase();
            if (!cat) {
                // Fallback: derive from unit
                const unit = (reg.unit || '').toLowerCase();
                if (unit === 'v') cat = 'voltage';
                else if (unit === 'a') cat = 'current';
                else if (unit === 'w' || unit === 'kw') cat = 'power';
                else if (unit === 'wh' || unit === 'kwh') cat = 'energy';
                else if (unit === 'hz') cat = 'frequency';
                else if (unit === 'var' || unit === 'kvar') cat = 'reactive';
                else if (unit === 'va' || unit === 'kva') cat = 'apparent';
                else cat = 'other';
            }

            // Store category on register for filtering
            reg._category = cat;
            categories.set(cat, (categories.get(cat) || 0) + 1);
        });

        // Sort categories: all first, then alphabetically
        const sortedCats = ['all', ...Array.from(categories.keys()).filter(k => k !== 'all').sort()];

        // Generate tabs HTML
        container.innerHTML = sortedCats
            .filter(cat => categories.get(cat) > 0)
            .map(cat => {
                const count = categories.get(cat);
                const label = cat === 'all' ? 'All' : cat.charAt(0).toUpperCase() + cat.slice(1);
                const isActive = this.configTab === cat ? 'active' : '';
                return `<button class="config-tab ${isActive}" data-tab="${cat}">${label} <span class="count">(${count})</span></button>`;
            })
            .join('');

        // Rebind click events
        container.querySelectorAll('.config-tab').forEach(btn => {
            btn.addEventListener('click', () => {
                container.querySelectorAll('.config-tab').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.configTab = btn.dataset.tab;
                this.renderSelectedRegistersList();
            });
        });
    }

    setupEventListeners() {
        // Register search (debounced)
        document.getElementById('registerSearch').addEventListener('input', (e) => {
            this.registerSearchPage = 1;
            this._debouncedRenderRegisters();
        });

        document.getElementById('categoryFilter').addEventListener('change', () => {
            this.registerSearchPage = 1;
            this.renderRegistersTable(); // Immediate for dropdown
        });

        // Query Modal button (in Registers page header)
        document.getElementById('queryRegisterBtn').addEventListener('click', () => this.openQueryModal());

        // Query button (in modal)
        document.getElementById('queryBtn').addEventListener('click', () => this.queryRegister());

        // Enter key for query
        document.getElementById('queryAddress').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') this.queryRegister();
        });

        // Save registers button
        document.getElementById('saveRegistersBtn').addEventListener('click', () => this.saveSelectedRegisters());

        // Edit Modal
        document.getElementById('modalSave').addEventListener('click', () => this.saveRegisterEdit());
        document.getElementById('editWidget').addEventListener('change', (e) => this.toggleGaugeOptions('edit', e.target.value));

        // Add Modal
        document.getElementById('addModalSave').addEventListener('click', () => this.saveNewRegister());
        document.getElementById('addWidget').addEventListener('change', (e) => this.toggleGaugeOptions('add', e.target.value));

        // Config Page - Search (debounced)
        document.getElementById('configSearch').addEventListener('input', (e) => {
            this.configSearch = e.target.value.toLowerCase();
            this._debouncedRenderSelectedList();
        });

        // Raw Config Modal
        document.getElementById('rawConfigBtn').addEventListener('click', () => this.openRawConfigModal());
        document.getElementById('rawConfigFormat').addEventListener('click', () => this.formatRawConfig());
        document.getElementById('rawConfigSave').addEventListener('click', () => this.saveRawConfig());

        // Live validation for raw config editor
        document.getElementById('rawConfigEditor').addEventListener('input', () => this.validateRawConfig());

        // Customize Dashboard Modal
        document.getElementById('customizeDashBtn').addEventListener('click', () => this.openCustomizeDashModal());
        document.getElementById('customizeDashSave').addEventListener('click', () => this.saveCustomizeDash());
        document.getElementById('dashboardViewToggle')?.addEventListener('click', () => this.toggleDashboardView());

        // Status indicator clicks
        document.getElementById('statusModbus').addEventListener('click', () => this.showStatusDetail('modbus'));
        document.getElementById('statusMqtt').addEventListener('click', () => this.showStatusDetail('mqtt'));
        document.getElementById('statusInflux').addEventListener('click', () => this.showStatusDetail('influxdb'));
        const vmPill = document.getElementById('statusVmeter');
        if (vmPill) vmPill.addEventListener('click', () => this.navigateTo('vmeters'));

        // Global keyboard shortcuts
        document.addEventListener('keydown', (e) => this.handleKeyboardShortcuts(e));

        // Config page main tabs (Settings/Registers)
        this.setupConfigMainTabs();

        // Settings form listeners
        this.setupSettingsListeners();
    }

    handleKeyboardShortcuts(e) {
        // Escape - close active modal
        if (e.key === 'Escape') {
            const activeModal = document.querySelector('.modal.active');
            if (activeModal) {
                e.preventDefault();
                this.closeModal();
            }
        }

        // Enter - confirm in modals (but not in textareas)
        if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA') {
            const registerModal = document.getElementById('registerModal');
            const addModal = document.getElementById('addRegisterModal');
            const customizeModal = document.getElementById('customizeDashModal');

            if (registerModal && registerModal.classList.contains('active')) {
                e.preventDefault();
                this.saveRegisterEdit();
            } else if (addModal && addModal.classList.contains('active')) {
                e.preventDefault();
                this.saveNewRegister();
            } else if (customizeModal && customizeModal.classList.contains('active')) {
                e.preventDefault();
                this.saveCustomizeDash();
            }
        }
    }

    connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
            this.updateConnectionStatus(true);

            // Show reconnected banner if was disconnected
            if (this.wasDisconnected) {
                this.showConnectionBanner('connected', '✓ Connection restored');
                setTimeout(() => this.hideConnectionBanner(), 2500);
                this.wasDisconnected = false;
            }
        };

        this.ws.onclose = () => {
            this.updateConnectionStatus(false);
            this.wasDisconnected = true;

            // Show disconnect banner
            this.showConnectionBanner('disconnected', '⚠ Connection lost. Reconnecting...');

            // Reconnect after 3 seconds
            setTimeout(() => this.connectWebSocket(), 3000);
        };

        this.ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };

        this.ws.onmessage = (event) => {
            const msg = JSON.parse(event.data);
            this.handleWebSocketMessage(msg);
        };
    }

    handleWebSocketMessage(msg) {
        if (msg.type === 'init' || msg.type === 'data') {
            // Update current values and history
            if (msg.values) {
                const timestamp = Date.now();
                for (const [addr, data] of Object.entries(msg.values)) {
                    this.currentValues[addr] = data;

                    // Store history for chart widgets
                    if (!this.valueHistory[addr]) {
                        this.valueHistory[addr] = [];
                    }
                    if (typeof data.value === 'number') {
                        this.valueHistory[addr].push({ time: timestamp, value: data.value });
                        // Keep only last N points
                        if (this.valueHistory[addr].length > this.maxHistoryPoints) {
                            this.valueHistory[addr].shift();
                        }
                    }
                }
            }

            // Update last update time
            if (msg.timestamp) {
                const lastUpdateEl = document.getElementById('lastUpdate');
                if (lastUpdateEl) {
                    lastUpdateEl.textContent = 'Last update: ' + new Date(msg.timestamp).toLocaleTimeString();
                }
            }

            // Update dashboard if active
            if (this.currentPage === 'dashboard') {
                this.updateDashboard();
            }

            // Update registers table if active
            if (this.currentPage === 'registers') {
                this.updateRegistersValues();
            }

            // Update monitor if active
            if (this.currentPage === 'monitor') {
                this.onMonitorDataUpdate();
            }
        } else if (msg.type === 'ping') {
            this.ws.send(JSON.stringify({ type: 'pong' }));
        }
    }

    updateConnectionStatus(connected) {
        const status = document.getElementById('connectionStatus');
        if (!status) return;

        const icon = status.querySelector('i');
        if (connected) {
            status.classList.add('connected');
            status.classList.remove('disconnected');
            status.innerHTML = '<i class="bi bi-circle-fill"></i> Connected';
        } else {
            status.classList.remove('connected');
            status.classList.add('disconnected');
            status.innerHTML = '<i class="bi bi-circle-fill"></i> Disconnected';
        }
    }

    async _updateVmeterPill() {
        const pill = document.getElementById('statusVmeter');
        if (!pill) return;
        let h;
        try { h = await (await fetch('/health')).json(); } catch (e) { return; }
        const total = h.enabled_meters || 0;
        if (total === 0) { pill.hidden = true; return; }   // no virtual meters → hide the pill
        pill.hidden = false;
        const meters = h.meters || [];
        const online = meters.filter(m => m.state === 'ok').length;
        const down = meters.filter(m => m.state === 'down').length;
        const stale = total - online - down;
        const cnt = document.getElementById('vmeterCount');
        if (cnt) cnt.textContent = `${online}/${total}`;
        pill.classList.remove('connected', 'disconnected', 'disabled');
        if (down > 0) pill.classList.add('disconnected');        // red — a meter is genuinely down
        else if (online === total) pill.classList.add('connected'); // green — all serving + fresh
        else pill.classList.add('disabled');                     // grey — some stale (source fail-safe)
        pill.title = `Virtual meters: ${online} ok` + (stale ? `, ${stale} stale` : '')
            + (down ? `, ${down} down` : '') + ` of ${total} — click to open`;
    }

    async loadStatus() {
        try {
            const response = await fetch('/api/status');
            const status = await response.json();

            const modbus = status.modbus || {};
            const mqtt = status.mqtt || {};
            const influx = status.influxdb || {};

            // Update status indicators in titlebar
            const statusModbus = document.getElementById('statusModbus');
            const statusMqtt = document.getElementById('statusMqtt');
            const statusInflux = document.getElementById('statusInflux');

            if (statusModbus) {
                statusModbus.classList.toggle('connected', modbus.connected);
                statusModbus.classList.toggle('disconnected', !modbus.connected);
            }

            if (statusMqtt) {
                if (!mqtt.enabled) {
                    statusMqtt.classList.add('disabled');
                    statusMqtt.classList.remove('connected', 'disconnected');
                } else {
                    statusMqtt.classList.remove('disabled');
                    statusMqtt.classList.toggle('connected', mqtt.connected);
                    statusMqtt.classList.toggle('disconnected', !mqtt.connected);
                }
            }

            if (statusInflux) {
                if (!influx.enabled) {
                    statusInflux.classList.add('disabled');
                    statusInflux.classList.remove('connected', 'disconnected');
                } else {
                    statusInflux.classList.remove('disabled');
                    statusInflux.classList.toggle('connected', influx.connected);
                    statusInflux.classList.toggle('disconnected', !influx.connected);
                }
            }

            this._updateVmeterPill();

            // Update stats bar values
            const statRegisters = document.getElementById('statRegisters');
            if (statRegisters) {
                statRegisters.textContent = modbus.total_registers || '--';
            }

            const statPollRate = document.getElementById('statPollRate');
            if (statPollRate) {
                statPollRate.textContent = modbus.poll_rate || '--';
            }

            const statMqttMsg = document.getElementById('statMqttMsg');
            if (statMqttMsg) {
                statMqttMsg.textContent = mqtt.messages_published || '--';
            }

            const statInfluxPts = document.getElementById('statInfluxPts');
            if (statInfluxPts) {
                statInfluxPts.textContent = influx.writes_total || '--';
            }

            // Store for status page
            this.status = status;

            if (this.currentPage === 'config') {
                this.renderStatusDetails();
            }

        } catch (error) {
            console.error('Failed to load status:', error);
        }
    }

    async loadAllRegisters() {
        try {
            const response = await fetch('/api/registers/all');
            this.allRegisters = await response.json();

            // Populate category filter
            const filter = document.getElementById('categoryFilter');
            const measurements = this.allRegisters.measurements || {};

            Object.keys(measurements).forEach(cat => {
                const option = document.createElement('option');
                option.value = cat;
                option.textContent = cat.charAt(0).toUpperCase() + cat.slice(1);
                filter.appendChild(option);
            });

        } catch (error) {
            console.error('Failed to load all registers:', error);
        }
    }

    async loadSelectedRegisters() {
        try {
            const response = await fetch('/api/registers/selected');
            const data = await response.json();
            this.selectedRegisters = data.registers || [];
            this.pollGroups = data.poll_groups || {};

            this.updateDashboard();

        } catch (error) {
            console.error('Failed to load selected registers:', error);
        }
    }

    updateDashboard() {
        const grid = document.getElementById('dashboardGrid');

        // Get registers that should be on dashboard and sort by order
        const dashboardRegs = this.selectedRegisters
            .filter(r => r.ui_show_on_dashboard)
            .sort((a, b) => {
                const orderA = a.ui_config?.dashboard_order ?? 999;
                const orderB = b.ui_config?.dashboard_order ?? 999;
                return orderA - orderB;
            });

        // Check if we should render table view or cards view
        if (this.dashboardView === 'table') {
            this.renderDashboardTable(grid, dashboardRegs);
            return;
        }

        // Cards view (default)
        // Get current widget addresses (data-address is on the wrapper div)
        const existingWidgets = new Map();
        grid.querySelectorAll('.widget-card[data-address]').forEach(el => {
            existingWidgets.set(parseInt(el.dataset.address), el);
        });

        // Track which addresses should exist
        const targetAddresses = new Set(dashboardRegs.map(r => r.address));

        // Remove widgets that shouldn't exist anymore
        existingWidgets.forEach((el, addr) => {
            if (!targetAddresses.has(addr)) {
                el.remove();
            }
        });

        // Remove empty state if it exists and we have registers
        const emptyState = grid.querySelector('.empty-state');
        if (emptyState && dashboardRegs.length > 0) {
            emptyState.remove();
        }

        // Update or create widgets in correct order
        dashboardRegs.forEach((reg, index) => {
            const value = this.currentValues[reg.address];
            const numValue = value?.value;
            const existingCard = existingWidgets.get(reg.address);

            if (existingCard) {
                // Check if widget type changed - if so, recreate it
                const currentType = existingCard.classList.contains('widget-gauge') ? 'gauge'
                    : existingCard.classList.contains('widget-chart') ? 'chart' : 'value';
                const targetType = reg.ui_widget || 'value';

                if (currentType !== targetType) {
                    // Widget type changed - replace with new widget
                    const newCard = this.createWidgetCard(reg, numValue);
                    existingCard.replaceWith(newCard);
                } else {
                    // Widget exists - just update the value (incremental update)
                    this.updateWidgetValue(existingCard, reg, numValue);

                    // Check if order is correct
                    const currentIndex = [...grid.querySelectorAll('.widget-card')].indexOf(existingCard);
                    if (currentIndex !== index) {
                        // Move to correct position
                        const children = grid.querySelectorAll('.widget-card');
                        if (index < children.length) {
                            grid.insertBefore(existingCard, children[index]);
                        } else {
                            grid.appendChild(existingCard);
                        }
                    }
                }
            } else {
                // Create new widget
                const card = this.createWidgetCard(reg, numValue);
                const children = grid.querySelectorAll('.widget-card');
                if (index < children.length) {
                    grid.insertBefore(card, children[index]);
                } else {
                    grid.appendChild(card);
                }
            }
        });

        // Show empty state if no registers
        if (dashboardRegs.length === 0 && !grid.querySelector('.empty-state')) {
            grid.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">📊</div>
                    <div class="empty-state-title">No widgets on dashboard</div>
                    <div class="empty-state-desc">
                        Add registers to your dashboard to monitor values in real-time.
                    </div>
                    <button class="empty-state-action" onclick="app.navigateTo('registers')">
                        📋 Go to Registers
                    </button>
                </div>
            `;
        }
    }

    createWidgetCard(reg, numValue) {
        const fmt = this.formatValueWithUnit(numValue, reg.unit);
        const displayValue = typeof fmt.value === 'number' ? fmt.value.toFixed(fmt.decimals) : '--';

        // Widget card (CSS Grid handles responsive layout)
        const card = document.createElement('div');
        card.className = `widget-card widget-${reg.ui_widget || 'value'}`;
        if (reg.ui_config?.wide) card.classList.add('widget-wide');
        card.dataset.address = reg.address;

        // Poll group badge class
        const pollClass = `poll-${reg.poll_group}`;

        // Header with edit button
        const header = `
            <div class="widget-header">
                <div class="widget-header-left">
                    <span class="widget-label">${reg.label}</span>
                </div>
                <div class="widget-header-right">
                    <span class="badge ${pollClass}">${reg.poll_group}</span>
                    <button class="widget-edit-btn" title="Edit widget">
                        <i class="bi bi-pencil"></i>
                    </button>
                </div>
            </div>
        `;

        // Render based on widget type
        let content = '';
        switch (reg.ui_widget) {
            case 'gauge':
                content = this.renderGaugeWidget(reg, numValue);
                break;
            case 'chart':
                content = this.renderChartWidget(reg);
                break;
            default: // 'value'
                const colorClass = this.getValueColorClass(numValue, reg);
                content = `
                    <div class="widget-value">
                        <span class="value-number ${colorClass}">${displayValue}</span><span class="widget-unit">${fmt.unit}</span>
                    </div>
                `;
        }

        // Footer with register name
        const footer = `<div class="widget-footer">${reg.name}</div>`;

        card.innerHTML = header + content + footer;

        // Add edit button click handler
        const editBtn = card.querySelector('.widget-edit-btn');
        if (editBtn) {
            editBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.editRegister(reg);
            });
        }

        return card;
    }

    updateWidgetValue(card, reg, numValue) {
        const widgetType = reg.ui_widget || 'value';

        switch (widgetType) {
            case 'gauge':
                this.updateGaugeWidget(card, reg, numValue);
                break;
            case 'chart':
                this.updateChartWidget(card, reg);
                break;
            default: // 'value'
                const valueEl = card.querySelector('.value-number');
                if (valueEl) {
                    const fmt = this.formatValueWithUnit(numValue, reg.unit);
                    const displayValue = typeof fmt.value === 'number' ? fmt.value.toFixed(fmt.decimals) : '--';
                    if (valueEl.textContent !== displayValue) {
                        valueEl.textContent = displayValue;
                    }
                    // Update unit display (may change with scaling)
                    const unitEl = card.querySelector('.widget-unit');
                    if (unitEl && unitEl.textContent !== fmt.unit) {
                        unitEl.textContent = fmt.unit;
                    }
                    // Update color class
                    const newColorClass = this.getValueColorClass(numValue, reg);
                    valueEl.classList.remove('value-normal', 'value-warning', 'value-danger', 'value-success');
                    valueEl.classList.add(newColorClass);
                }
        }
    }

    // ============ Dashboard Table View ============

    toggleDashboardView() {
        this.dashboardView = this.dashboardView === 'cards' ? 'table' : 'cards';
        localStorage.setItem('janitza-dashboard-view', this.dashboardView);

        // Update toggle button icon
        const toggleBtn = document.getElementById('dashboardViewToggle');
        if (toggleBtn) {
            const icon = toggleBtn.querySelector('i');
            if (icon) {
                icon.className = this.dashboardView === 'table' ? 'bi bi-grid-3x3-gap' : 'bi bi-table';
            }
        }

        // Force full re-render
        const grid = document.getElementById('dashboardGrid');
        grid.innerHTML = '';
        this.updateDashboard();
    }

    renderDashboardTable(container, registers) {
        // Remove any existing cards (switching from cards to table)
        const existingCards = container.querySelectorAll('.widget-card');
        existingCards.forEach(el => el.remove());

        // Check for existing table
        let table = container.querySelector('.dashboard-table');

        if (!table) {
            // Create table structure
            container.innerHTML = `
                <div class="table-container dashboard-table-container">
                    <table class="dashboard-table">
                        <thead>
                            <tr>
                                <th>Label</th>
                                <th>Value</th>
                                <th>Unit</th>
                                <th>Poll Group</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody id="dashboardTableBody"></tbody>
                    </table>
                </div>
            `;
            table = container.querySelector('.dashboard-table');
        }

        const tbody = container.querySelector('#dashboardTableBody');

        if (registers.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">📊</div>
                    <div class="empty-state-title">No widgets on dashboard</div>
                    <div class="empty-state-desc">
                        Add registers to your dashboard to monitor values in real-time.
                    </div>
                    <button class="empty-state-action" onclick="app.navigateTo('registers')">
                        📋 Go to Registers
                    </button>
                </div>
            `;
            return;
        }

        // Build table rows
        const rows = registers.map(reg => {
            const value = this.currentValues[reg.address];
            const numValue = value?.value;
            const fmt = this.formatValueWithUnit(numValue, reg.unit);
            const displayValue = typeof fmt.value === 'number' ? fmt.value.toFixed(fmt.decimals) : '--';
            const colorClass = this.getValueColorClass(numValue, reg);
            const pollClass = `poll-${reg.poll_group}`;

            return `
                <tr data-address="${reg.address}">
                    <td>
                        <div class="table-label">${reg.label}</div>
                        <div class="table-name">${reg.name}</div>
                    </td>
                    <td>
                        <span class="table-value ${colorClass}">${displayValue}</span>
                    </td>
                    <td class="table-unit">${fmt.unit}</td>
                    <td>
                        <span class="badge ${pollClass}">${reg.poll_group}</span>
                    </td>
                    <td>
                        <button class="btn-action" title="Edit" onclick="app.editRegister(app.selectedRegisters.find(r => r.address === ${reg.address}))">
                            <i class="bi bi-pencil"></i>
                        </button>
                    </td>
                </tr>
            `;
        }).join('');

        tbody.innerHTML = rows;
    }

    updateGaugeWidget(card, reg, value) {
        const { min, max } = this.getGaugeRange(reg);
        const color = this.getGaugeColor(value, reg);

        // Update gauge arc
        let percent = 0;
        if (typeof value === 'number' && max > min) {
            percent = Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100));
        }

        const radius = 45;
        const circumference = Math.PI * radius;
        const offset = circumference - (percent / 100) * circumference;

        const valuePath = card.querySelector('.gauge-value');
        if (valuePath) {
            valuePath.style.strokeDashoffset = offset;
            valuePath.style.stroke = color;
        }

        // Update number and unit display
        const numberEl = card.querySelector('.gauge-number');
        const unitEl = card.querySelector('.gauge-unit');
        if (numberEl) {
            const fmt = this.formatValueWithUnit(value, reg.unit);
            const displayValue = typeof fmt.value === 'number' ? fmt.value.toFixed(fmt.decimals) : '--';
            if (numberEl.textContent !== displayValue) {
                numberEl.textContent = displayValue;
            }
            if (unitEl && unitEl.textContent !== fmt.unit) {
                unitEl.textContent = fmt.unit;
            }
        }
    }

    updateChartWidget(card, reg) {
        const history = this.valueHistory[String(reg.address)] || [];

        // Check if we need to replace placeholder with actual chart
        const placeholder = card.querySelector('.chart-placeholder');
        if (placeholder && history.length >= 2) {
            // Replace entire widget content with chart
            const chartContainer = card.querySelector('.widget-chart');
            if (chartContainer) {
                chartContainer.innerHTML = this.getChartContent(reg, history);
            }
            return;
        }

        if (history.length < 2) {
            return; // Not enough data yet
        }

        // Calculate min/max for scaling
        const values = history.map(h => h.value);
        const minVal = Math.min(...values);
        const maxVal = Math.max(...values);
        const range = maxVal - minVal || 1;

        // Generate SVG path
        const width = 200;
        const height = 60;
        const points = history.map((h, i) => {
            const x = (i / (history.length - 1)) * width;
            const y = height - ((h.value - minVal) / range) * height;
            return `${x},${y}`;
        });
        const pathD = 'M ' + points.join(' L ');

        // Update path
        const pathEl = card.querySelector('.chart-line');
        if (pathEl) {
            pathEl.setAttribute('d', pathD);
        }

        // Update current value
        const currentEl = card.querySelector('.chart-current');
        if (currentEl) {
            const currentValue = history[history.length - 1]?.value;
            const displayValue = typeof currentValue === 'number' ? currentValue.toFixed(2) : '--';
            currentEl.innerHTML = `${displayValue} <span>${reg.unit}</span>`;
        }

        // Update range
        const rangeEl = card.querySelector('.chart-range');
        if (rangeEl) {
            rangeEl.innerHTML = `<span>${minVal.toFixed(1)}</span><span>${maxVal.toFixed(1)}</span>`;
        }
    }

    getChartContent(reg, history) {
        const values = history.map(h => h.value);
        const minVal = Math.min(...values);
        const maxVal = Math.max(...values);
        const range = maxVal - minVal || 1;

        const width = 200;
        const height = 60;
        const points = history.map((h, i) => {
            const x = (i / (history.length - 1)) * width;
            const y = height - ((h.value - minVal) / range) * height;
            return `${x},${y}`;
        });
        const pathD = 'M ' + points.join(' L ');

        const currentValue = history[history.length - 1]?.value;
        const displayValue = typeof currentValue === 'number' ? currentValue.toFixed(2) : '--';

        return `
            <div class="chart-current">${displayValue} <span>${reg.unit}</span></div>
            <svg viewBox="0 0 ${width} ${height}" class="chart-svg" preserveAspectRatio="none">
                <path class="chart-line" d="${pathD}" />
            </svg>
            <div class="chart-range">
                <span>${minVal.toFixed(1)}</span>
                <span>${maxVal.toFixed(1)}</span>
            </div>
        `;
    }

    getGaugeRange(reg) {
        // Derive min/max from thresholds if not set in ui_config
        let min = reg.ui_config?.min;
        let max = reg.ui_config?.max;

        if ((min == null || max == null) && reg.thresholds && reg.thresholds.enabled) {
            const t = reg.thresholds;
            const vals = [t.dangerLow, t.warningLow, t.warningHigh, t.dangerHigh].filter(v => v != null);
            if (vals.length > 0) {
                const tMin = Math.min(...vals);
                const tMax = Math.max(...vals);
                const margin = (tMax - tMin) * 0.15;
                if (min == null) min = Math.floor(tMin - margin);
                if (max == null) max = Math.ceil(tMax + margin);
            }
        }

        return { min: min ?? 0, max: max ?? 100 };
    }

    getGaugeColor(value, reg) {
        // Use thresholds for color if available
        if (typeof value === 'number' && reg.thresholds && reg.thresholds.enabled) {
            const colorClass = this.getValueColorClass(value, reg);
            const colorMap = {
                'value-danger': 'var(--color-danger, #ef4444)',
                'value-warning': 'var(--color-warning, #f59e0b)',
                'value-success': 'var(--color-success, #22c55e)',
                'value-normal': reg.ui_config?.color || 'var(--accent-blue)',
            };
            return colorMap[colorClass] || colorMap['value-normal'];
        }
        return reg.ui_config?.color || 'var(--accent-blue)';
    }

    renderGaugeWidget(reg, value) {
        const { min, max } = this.getGaugeRange(reg);
        const color = this.getGaugeColor(value, reg);

        // Calculate percentage (0-100) using raw value against raw range
        let percent = 0;
        if (typeof value === 'number' && max > min) {
            percent = Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100));
        }

        // SVG arc parameters
        const radius = 45;
        const circumference = Math.PI * radius; // Semi-circle
        const offset = circumference - (percent / 100) * circumference;

        // Format display value with unit scaling
        const fmt = this.formatValueWithUnit(value, reg.unit);
        const displayValue = typeof fmt.value === 'number' ? fmt.value.toFixed(fmt.decimals) : '--';
        const displayUnit = fmt.unit;

        return `
            <div class="widget-gauge">
                <svg viewBox="0 0 100 60" class="gauge-svg">
                    <!-- Background arc -->
                    <path class="gauge-bg" d="M 5 55 A 45 45 0 0 1 95 55" />
                    <!-- Value arc -->
                    <path class="gauge-value" d="M 5 55 A 45 45 0 0 1 95 55"
                          style="stroke: ${color}; stroke-dasharray: ${circumference}; stroke-dashoffset: ${offset};" />
                </svg>
                <div class="gauge-reading">
                    <span class="gauge-number">${displayValue}</span>
                    <span class="gauge-unit">${displayUnit}</span>
                </div>
                <div class="gauge-range">
                    <span>${min}</span>
                    <span>${max}</span>
                </div>
            </div>
        `;
    }

    renderChartWidget(reg) {
        const history = this.valueHistory[String(reg.address)] || [];
        const canvasId = `chart-${reg.address}`;

        if (history.length < 2) {
            return `
                <div class="widget-chart">
                    <div class="chart-placeholder">Collecting data...</div>
                </div>
            `;
        }

        // Calculate min/max for scaling
        const values = history.map(h => h.value);
        const minVal = Math.min(...values);
        const maxVal = Math.max(...values);
        const range = maxVal - minVal || 1;

        // Generate SVG path
        const width = 200;
        const height = 60;
        const points = history.map((h, i) => {
            const x = (i / (history.length - 1)) * width;
            const y = height - ((h.value - minVal) / range) * height;
            return `${x},${y}`;
        });
        const pathD = 'M ' + points.join(' L ');

        const currentValue = history[history.length - 1]?.value;
        const fmt = this.formatValueWithUnit(currentValue, reg.unit);
        const displayValue = typeof fmt.value === 'number' ? fmt.value.toFixed(fmt.decimals) : '--';
        const fmtMin = this.formatValueWithUnit(minVal, reg.unit);
        const fmtMax = this.formatValueWithUnit(maxVal, reg.unit);

        return `
            <div class="widget-chart">
                <div class="chart-current">${displayValue} <span>${fmt.unit}</span></div>
                <svg viewBox="0 0 ${width} ${height}" class="chart-svg" preserveAspectRatio="none">
                    <path class="chart-line" d="${pathD}" />
                </svg>
                <div class="chart-range">
                    <span>${typeof fmtMin.value === 'number' ? fmtMin.value.toFixed(fmtMin.decimals) : minVal.toFixed(1)} ${fmtMin.unit !== reg.unit ? fmtMin.unit : ''}</span>
                    <span>${typeof fmtMax.value === 'number' ? fmtMax.value.toFixed(fmtMax.decimals) : maxVal.toFixed(1)} ${fmtMax.unit !== reg.unit ? fmtMax.unit : ''}</span>
                </div>
            </div>
        `;
    }

    renderRegistersTable() {
        const tbody = document.getElementById('registersTableBody');
        const searchQuery = document.getElementById('registerSearch').value.toLowerCase();
        const categoryFilter = document.getElementById('categoryFilter').value;

        // Flatten all registers
        const allRegs = this.flattenRegisters();

        // Filter
        const filtered = allRegs.filter(reg => {
            const matchesSearch = !searchQuery ||
                reg.name.toLowerCase().includes(searchQuery) ||
                reg.address.toString().includes(searchQuery) ||
                (reg.unit && reg.unit.toLowerCase().includes(searchQuery)) ||
                (reg.description && reg.description.toLowerCase().includes(searchQuery));

            const matchesCategory = !categoryFilter || reg.category === categoryFilter;

            return matchesSearch && matchesCategory;
        });

        // Paginate
        const start = (this.registerSearchPage - 1) * this.registersPerPage;
        const paginated = filtered.slice(start, start + this.registersPerPage);

        // Render
        tbody.innerHTML = '';
        paginated.forEach(reg => {
            const configuredReg = this.selectedRegisters.find(s => s.address === reg.address);
            const isConfigured = !!configuredReg;
            const currentValue = this.currentValues[reg.address];

            const tr = document.createElement('tr');
            tr.dataset.address = reg.address;
            if (isConfigured) {
                tr.classList.add('configured');
            }

            // Build badges for monitored registers
            let badges = '';
            if (isConfigured) {
                const pollClass = `poll-${configuredReg.poll_group}`;
                badges = `
                    <span class="badge configured">Monitored</span>
                    <span class="badge ${pollClass}">${configuredReg.poll_group}</span>
                `;
            }

            // Build action buttons
            let actions = '';
            if (isConfigured) {
                // Configured register: Query, Edit, Remove
                actions = `
                    <button class="btn-action query" data-address="${reg.address}" title="Query Now">&#128269;</button>
                    <button class="btn-action edit" data-address="${reg.address}" title="Edit Config">&#9998;</button>
                    <button class="btn-action remove" data-address="${reg.address}" title="Remove">&#10005;</button>
                `;
            } else {
                // Not configured: Query, Add, Quick Add
                actions = `
                    <button class="btn-action query" data-address="${reg.address}" title="Query Now">&#128269;</button>
                    <button class="btn-action add" data-address="${reg.address}" title="Add to Config">&#43;</button>
                    <button class="btn-action quick-add" data-address="${reg.address}" title="Quick Add">&#9889;</button>
                `;
            }

            tr.innerHTML = `
                <td class="address">${reg.address}</td>
                <td class="description-cell">
                    <div class="reg-description">${reg.description || '-'}</div>
                    ${badges ? `<div class="badges">${badges}</div>` : ''}
                </td>
                <td class="name-cell">
                    <span class="reg-name-mono">${reg.name}</span>
                </td>
                <td>${reg.unit || '-'}</td>
                <td>${reg.category}${reg.subtype ? '/' + reg.subtype : ''}</td>
                <td class="value">${currentValue ? currentValue.value?.toFixed(2) : '-'}</td>
                <td class="actions-cell">${actions}</td>
            `;

            // Attach event listeners
            tr.querySelector('.query').addEventListener('click', () => this.queryRegisterNow(reg));

            if (isConfigured) {
                tr.querySelector('.edit').addEventListener('click', () => this.editRegister(configuredReg));
                tr.querySelector('.remove').addEventListener('click', () => this.removeRegisterFromTable(reg.address));
            } else {
                tr.querySelector('.add').addEventListener('click', () => this.openAddModal(reg));
                tr.querySelector('.quick-add').addEventListener('click', () => this.quickAddRegister(reg));
            }

            tbody.appendChild(tr);
        });

        // Render pagination
        this.renderPagination(filtered.length);
    }

    flattenRegisters() {
        // Cache key based on allRegisters object reference
        const cacheKey = JSON.stringify(Object.keys(this.allRegisters.measurements || {}));

        if (this._flattenedRegistersCache && this._flattenedRegistersCacheKey === cacheKey) {
            return this._flattenedRegistersCache;
        }

        const result = [];
        const measurements = this.allRegisters.measurements || {};

        for (const [catName, catData] of Object.entries(measurements)) {
            if (catData.entries) {
                catData.entries.forEach(e => {
                    result.push({ ...e, category: catName });
                });
            }
            if (catData.subtypes) {
                for (const [subName, subData] of Object.entries(catData.subtypes)) {
                    (subData.entries || []).forEach(e => {
                        result.push({ ...e, category: catName, subtype: subName });
                    });
                }
            }
        }

        this._flattenedRegistersCache = result.sort((a, b) => a.address - b.address);
        this._flattenedRegistersCacheKey = cacheKey;

        return this._flattenedRegistersCache;
    }

    renderPagination(totalItems) {
        const container = document.getElementById('registersPagination');
        const totalPages = Math.ceil(totalItems / this.registersPerPage);

        container.innerHTML = '';

        if (totalPages <= 1) return;

        // Prev button
        const prevLi = document.createElement('li');
        prevLi.className = `page-item ${this.registerSearchPage === 1 ? 'disabled' : ''}`;
        const prevBtn = document.createElement('a');
        prevBtn.className = 'page-link';
        prevBtn.href = '#';
        prevBtn.textContent = 'Prev';
        prevBtn.addEventListener('click', (e) => {
            e.preventDefault();
            if (this.registerSearchPage > 1) {
                this.registerSearchPage--;
                this.renderRegistersTable();
            }
        });
        prevLi.appendChild(prevBtn);
        container.appendChild(prevLi);

        // Page numbers
        for (let i = 1; i <= Math.min(totalPages, 10); i++) {
            const li = document.createElement('li');
            li.className = `page-item ${i === this.registerSearchPage ? 'active' : ''}`;
            const btn = document.createElement('a');
            btn.className = 'page-link';
            btn.href = '#';
            btn.textContent = i;
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                this.registerSearchPage = i;
                this.renderRegistersTable();
            });
            li.appendChild(btn);
            container.appendChild(li);
        }

        // Next button
        const nextLi = document.createElement('li');
        nextLi.className = `page-item ${this.registerSearchPage === totalPages ? 'disabled' : ''}`;
        const nextBtn = document.createElement('a');
        nextBtn.className = 'page-link';
        nextBtn.href = '#';
        nextBtn.textContent = 'Next';
        nextBtn.addEventListener('click', (e) => {
            e.preventDefault();
            if (this.registerSearchPage < totalPages) {
                this.registerSearchPage++;
                this.renderRegistersTable();
            }
        });
        nextLi.appendChild(nextBtn);
        container.appendChild(nextLi);
    }

    updateRegistersValues() {
        document.querySelectorAll('#registersTableBody tr').forEach(tr => {
            const address = parseInt(tr.dataset.address);
            if (address) {
                const value = this.currentValues[address];
                const valueCell = tr.querySelector('.value');
                if (valueCell && value) {
                    valueCell.textContent = value.value?.toFixed(2) || '-';
                }
            }
        });
    }

    // ============ Register Actions ============

    async queryRegisterNow(reg) {
        try {
            const response = await fetch('/api/query/register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    address: reg.address,
                    data_type: reg.data_type || 'float'
                })
            });

            if (!response.ok) {
                throw new Error('Query failed');
            }

            const data = await response.json();
            const displayValue = typeof data.value === 'number'
                ? data.value.toFixed(4)
                : data.value;

            this.showToast('info', reg.name, `${displayValue} ${reg.unit || ''}`);

            // Update value in table
            const tr = document.querySelector(`#registersTableBody tr[data-address="${reg.address}"]`);
            if (tr) {
                const valueCell = tr.querySelector('.value');
                if (valueCell) {
                    valueCell.textContent = displayValue;
                    valueCell.classList.add('flash');
                    setTimeout(() => valueCell.classList.remove('flash'), 1000);
                }
            }

        } catch (error) {
            this.showToast('error', 'Query Failed', `Could not read register ${reg.address}`);
        }
    }

    // ============ Smart Defaults Generation ============

    generateRegisterDefaults(reg) {
        const cat = (reg.category || '').toLowerCase();
        const desc = reg.description || reg.name;

        // 1. Determine poll group based on category
        let pollGroup = 'normal';
        if (cat.includes('energy')) {
            pollGroup = 'slow';
        } else if (cat.includes('power') || cat.includes('voltage') || cat.includes('current')) {
            pollGroup = 'realtime';
        }

        // 2. Generate clean label from description
        const label = desc;

        // 3. Generate MQTT topic: category/cleaned_description
        const topicBase = cat.replace(/\s+/g, '_').toLowerCase();
        const topicName = desc
            .toLowerCase()
            .replace(/[,;]/g, '')
            .replace(/\s+/g, '_')
            .replace(/[^a-z0-9_]/g, '')
            .replace(/_+/g, '_')
            .replace(/^_|_$/g, '');
        const mqttTopic = `${topicBase}/${topicName}`;

        // 4. Generate InfluxDB measurement from category
        const measurement = cat.replace(/\s+/g, '_').toLowerCase();

        // 5. Extract tags from description
        const tags = {};

        // Extract phase (L1, L2, L3, N)
        const phaseMatch = desc.match(/L([1-3N])/i);
        if (phaseMatch) {
            tags.phase = 'L' + phaseMatch[1].toUpperCase();
        }

        // Extract type indicators
        if (desc.toLowerCase().includes('active')) tags.type = 'active';
        else if (desc.toLowerCase().includes('reactive')) tags.type = 'reactive';
        else if (desc.toLowerCase().includes('apparent')) tags.type = 'apparent';

        // Line-to-neutral vs line-to-line
        if (desc.match(/L\d-N/i)) tags.connection = 'line_neutral';
        else if (desc.match(/L\d-L\d/i)) tags.connection = 'line_line';

        // Total/Sum indicator
        if (desc.toLowerCase().includes('total') || desc.toLowerCase().includes('sum')) {
            tags.aggregate = 'total';
        }

        return {
            pollGroup,
            label,
            mqttTopic,
            measurement,
            tags
        };
    }

    quickAddRegister(reg) {
        // Ensure we have description from allRegisters
        if (!reg.description) {
            // Try to find description from allRegisters
            const allRegs = this.flattenRegisters();
            const fullReg = allRegs.find(r => r.address === reg.address);
            if (fullReg && fullReg.description) {
                reg.description = fullReg.description;
            }
        }

        const defaults = this.generateRegisterDefaults(reg);

        // Create register config with smart defaults
        const newReg = {
            address: reg.address,
            name: reg.name,
            description: reg.description || '',
            label: defaults.label,
            unit: reg.unit || '',
            data_type: reg.data_type || 'float',
            poll_group: defaults.pollGroup,
            mqtt_enabled: true,
            mqtt_topic: defaults.mqttTopic,
            influxdb_enabled: true,
            influxdb_measurement: defaults.measurement,
            influxdb_tags: defaults.tags,
            ui_show_on_dashboard: true,
            ui_widget: 'value',
            ui_config: {}
        };

        this.selectedRegisters.push(newReg);
        this.saveSelectedRegistersQuiet();
        this.showToast('success', 'Added', `${defaults.label} added with ${defaults.pollGroup} polling`);
        this.renderRegistersTable();
    }

    removeRegisterFromTable(address) {
        const reg = this.selectedRegisters.find(r => r.address === address);
        if (reg) {
            this.selectedRegisters = this.selectedRegisters.filter(r => r.address !== address);
            this.saveSelectedRegistersQuiet();
            this.showToast('info', 'Removed', `${reg.name} removed from configuration`);
            this.renderRegistersTable();
        }
    }

    async saveSelectedRegistersQuiet() {
        try {
            const response = await fetch('/api/registers/selected', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(this.selectedRegisters)
            });

            if (!response.ok) {
                throw new Error('Save failed');
            }
        } catch (error) {
            this.showToast('error', 'Save Failed', error.message);
        }
    }

    // ============ Add Modal ============

    openAddModal(reg) {
        // Ensure we have description from allRegisters
        if (!reg.description) {
            const allRegs = this.flattenRegisters();
            const fullReg = allRegs.find(r => r.address === reg.address);
            if (fullReg && fullReg.description) {
                reg.description = fullReg.description;
            }
        }

        // Generate smart defaults
        const defaults = this.generateRegisterDefaults(reg);

        // Store register data in hidden fields
        document.getElementById('addAddress').value = reg.address;
        document.getElementById('addName').value = reg.name;
        document.getElementById('addUnit').value = reg.unit || '';
        document.getElementById('addDataType').value = reg.data_type || 'float';
        document.getElementById('addCategory').value = reg.category || '';

        // Store description in a data attribute on the modal
        const modal = document.getElementById('addRegisterModal');
        modal.dataset.description = reg.description || '';

        // Display info - show description prominently
        document.getElementById('addAddressDisplay').textContent = reg.address;
        document.getElementById('addNameDisplay').textContent = reg.description || reg.name;

        // Set defaults from smart generation
        document.getElementById('addLabel').value = defaults.label;
        document.getElementById('addPollGroup').value = defaults.pollGroup;

        // Set MQTT defaults
        document.getElementById('addWidget').value = 'value';
        document.getElementById('addMqttEnabled').checked = true;
        document.getElementById('addMqttTopic').value = defaults.mqttTopic;

        // Set InfluxDB defaults
        document.getElementById('addInfluxEnabled').checked = true;
        document.getElementById('addInfluxMeasurement').value = defaults.measurement;
        document.getElementById('addInfluxTags').value = JSON.stringify(defaults.tags, null, 2);

        // Auto-fill thresholds based on detected type
        this.autoFillThresholds('add', reg.unit, reg.name);

        // Show modal
        this.openModal('addRegisterModal');
    }

    closeAddModal() {
        this.closeModal('addRegisterModal');
    }

    openQueryModal() {
        // Reset the form
        document.getElementById('queryAddress').value = '';
        document.getElementById('queryDataType').value = 'float';
        document.getElementById('queryResultContainer').style.display = 'none';
        document.getElementById('queryResult').innerHTML = '';
        this.openModal('queryModal');
        // Focus on address input
        setTimeout(() => document.getElementById('queryAddress').focus(), 100);
    }

    closeQueryModal() {
        this.closeModal('queryModal');
    }

    showStatusDetail(service) {
        const titleEl = document.getElementById('statusDetailTitle');
        const bodyEl = document.getElementById('statusDetailBody');

        if (!this.status) {
            bodyEl.innerHTML = '<p>Status not available</p>';
            this.openModal('statusDetailModal');
            return;
        }

        let title = '';
        let html = '<div class="status-detail-list">';

        if (service === 'modbus') {
            const data = this.status.modbus || {};
            title = '<i class="bi bi-hdd-network"></i> Modbus Status';
            html += `
                <div class="status-detail-row">
                    <span class="status-detail-label">Status</span>
                    <span class="status-detail-value ${data.connected ? 'success' : 'error'}">
                        ${data.connected ? 'Connected' : 'Disconnected'}
                    </span>
                </div>
                <div class="status-detail-row">
                    <span class="status-detail-label">Host</span>
                    <span class="status-detail-value mono">${data.host || '-'}:${data.port || '-'}</span>
                </div>
                <div class="status-detail-row">
                    <span class="status-detail-label">Unit ID</span>
                    <span class="status-detail-value mono">${data.unit_id || '-'}</span>
                </div>
                <div class="status-detail-row">
                    <span class="status-detail-label">Registers</span>
                    <span class="status-detail-value">${data.total_registers || 0}</span>
                </div>
                <div class="status-detail-row">
                    <span class="status-detail-label">Poll Rate</span>
                    <span class="status-detail-value">${data.poll_rate || '-'}/sec</span>
                </div>
                <div class="status-detail-row">
                    <span class="status-detail-label">Errors</span>
                    <span class="status-detail-value ${data.errors > 0 ? 'error' : ''}">${data.errors || 0}</span>
                </div>
            `;
        } else if (service === 'mqtt') {
            const data = this.status.mqtt || {};
            title = '<i class="bi bi-broadcast"></i> MQTT Status';
            if (!data.enabled) {
                html += `
                    <div class="status-detail-row">
                        <span class="status-detail-label">Status</span>
                        <span class="status-detail-value">Disabled</span>
                    </div>
                `;
            } else {
                html += `
                    <div class="status-detail-row">
                        <span class="status-detail-label">Status</span>
                        <span class="status-detail-value ${data.connected ? 'success' : 'error'}">
                            ${data.connected ? 'Connected' : 'Disconnected'}
                        </span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">Broker</span>
                        <span class="status-detail-value mono">${data.broker || '-'}:${data.port || '-'}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">Prefix</span>
                        <span class="status-detail-value mono">${data.prefix || '-'}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">Messages</span>
                        <span class="status-detail-value">${data.messages_published || 0}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">Skipped</span>
                        <span class="status-detail-value">${data.messages_skipped || 0}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">Mode</span>
                        <span class="status-detail-value">${data.publish_mode || 'changed'}</span>
                    </div>
                    ${data.messages_skipped > 0 ? `
                    <div class="status-detail-hint">
                        <i class="bi bi-info-circle"></i>
                        Skipped = unchanged values (publish mode: ${data.publish_mode || 'changed'})
                    </div>
                    ` : ''}
                `;
            }
        } else if (service === 'influxdb') {
            const data = this.status.influxdb || {};
            title = '<i class="bi bi-database"></i> InfluxDB Status';
            if (!data.enabled) {
                html += `
                    <div class="status-detail-row">
                        <span class="status-detail-label">Status</span>
                        <span class="status-detail-value">Disabled</span>
                    </div>
                `;
            } else {
                html += `
                    <div class="status-detail-row">
                        <span class="status-detail-label">Status</span>
                        <span class="status-detail-value ${data.connected ? 'success' : 'error'}">
                            ${data.connected ? 'Connected' : 'Disconnected'}
                        </span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">URL</span>
                        <span class="status-detail-value mono">${data.url || '-'}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">Bucket</span>
                        <span class="status-detail-value mono">${data.bucket || '-'}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">Points Written</span>
                        <span class="status-detail-value">${data.writes_total || 0}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">Skipped</span>
                        <span class="status-detail-value">${data.writes_skipped || 0}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">Errors</span>
                        <span class="status-detail-value ${data.writes_failed > 0 ? 'error' : ''}">${data.writes_failed || 0}</span>
                    </div>
                    <div class="status-detail-row">
                        <span class="status-detail-label">Mode</span>
                        <span class="status-detail-value">${data.publish_mode || 'changed'}</span>
                    </div>
                    ${data.writes_skipped > 0 ? `
                    <div class="status-detail-hint">
                        <i class="bi bi-info-circle"></i>
                        Skipped = ${data.publish_mode === 'changed' ? 'unchanged values' : 'rate limited'} (publish mode: ${data.publish_mode || 'changed'})
                    </div>
                    ` : ''}
                `;
            }
        }

        html += '</div>';
        titleEl.innerHTML = title;
        bodyEl.innerHTML = html;
        this.openModal('statusDetailModal');
    }

    closeStatusDetailModal() {
        this.closeModal('statusDetailModal');
    }

    saveNewRegister() {
        const address = parseInt(document.getElementById('addAddress').value);
        const modal = document.getElementById('addRegisterModal');

        // Check if already monitored
        if (this.selectedRegisters.some(r => r.address === address)) {
            this.showToast('warning', 'Already Monitored', 'This register is already being monitored');
            return;
        }

        // Build register config
        const newReg = {
            address: address,
            name: document.getElementById('addName').value,
            description: modal.dataset.description || '',
            label: document.getElementById('addLabel').value,
            unit: document.getElementById('addUnit').value,
            data_type: document.getElementById('addDataType').value || 'float',
            poll_group: document.getElementById('addPollGroup').value,
            mqtt_enabled: document.getElementById('addMqttEnabled').checked,
            mqtt_topic: document.getElementById('addMqttTopic').value,
            influxdb_enabled: document.getElementById('addInfluxEnabled').checked,
            influxdb_measurement: document.getElementById('addInfluxMeasurement').value,
            influxdb_tags: {},
            ui_show_on_dashboard: true,
            ui_widget: document.getElementById('addWidget').value,
            ui_config: (() => {
                const cfg = {};
                const min = document.getElementById('addGaugeMin').value;
                const max = document.getElementById('addGaugeMax').value;
                if (min !== '') cfg.min = parseFloat(min);
                if (max !== '') cfg.max = parseFloat(max);
                cfg.color = document.getElementById('addGaugeColor').value;
                return cfg;
            })(),
            thresholds: this.readThresholdsFromForm('add')
        };

        // Parse InfluxDB tags
        const tagsStr = document.getElementById('addInfluxTags').value;
        if (tagsStr) {
            try {
                newReg.influxdb_tags = JSON.parse(tagsStr);
            } catch (e) {
                this.showToast('error', 'Invalid JSON', 'InfluxDB tags must be valid JSON');
                return;
            }
        }

        // Add and save
        this.selectedRegisters.push(newReg);
        this.saveSelectedRegistersQuiet();
        this.showToast('success', 'Added', `${newReg.label} added to configuration`);
        this.closeAddModal();
        this.renderRegistersTable();
    }

    async queryRegister() {
        const address = parseInt(document.getElementById('queryAddress').value);
        const dataType = document.getElementById('queryDataType').value;

        if (!address) {
            this.showToast('warning', 'Missing Address', 'Please enter a register address');
            return;
        }

        const resultContainer = document.getElementById('queryResultContainer');
        const resultDiv = document.getElementById('queryResult');

        // Show the result container
        resultContainer.style.display = 'block';
        resultDiv.innerHTML = '<div class="loading"><i class="bi bi-arrow-repeat spin"></i> Querying register...</div>';

        // Look up register info from our database
        const allRegs = this.flattenRegisters();
        const regInfo = allRegs.find(r => r.address === address);
        const isConfigured = this.selectedRegisters.some(r => r.address === address);

        try {
            const response = await fetch('/api/query/register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ address, data_type: dataType })
            });

            if (!response.ok) {
                throw new Error('Query failed');
            }

            const data = await response.json();

            const displayValue = typeof data.value === 'number' ?
                data.value.toFixed(4) : data.value;

            // Build detailed result HTML
            let html = `
                <div class="query-result-header">
                    <div class="result-value">${displayValue}</div>
                    <div class="result-unit">${regInfo?.unit || ''}</div>
                </div>
            `;

            if (regInfo) {
                html += `
                    <div class="result-details">
                        <div class="detail-row">
                            <span class="detail-label">Description</span>
                            <span class="detail-value">${regInfo.description || '-'}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">Name</span>
                            <span class="detail-value mono">${regInfo.name}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">Address</span>
                            <span class="detail-value mono">${data.address}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">Category</span>
                            <span class="detail-value">${regInfo.category}${regInfo.subtype ? ' / ' + regInfo.subtype : ''}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">Data Type</span>
                            <span class="detail-value">${data.data_type}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">Timestamp</span>
                            <span class="detail-value">${new Date(data.timestamp).toLocaleString()}</span>
                        </div>
                    </div>
                    <div class="result-actions">
                        ${isConfigured
                            ? '<span class="badge configured"><i class="bi bi-check-circle"></i> Monitored</span>'
                            : `<button class="btn btn-primary btn-sm" id="queryConfigureBtn">
                                <i class="bi bi-plus-circle"></i> Add to Monitoring
                               </button>
                               <button class="btn btn-ghost btn-sm" id="queryQuickAddBtn">
                                <i class="bi bi-lightning"></i> Quick Add
                               </button>`
                        }
                    </div>
                `;
            } else {
                html += `
                    <div class="result-details">
                        <div class="detail-row">
                            <span class="detail-label">Address</span>
                            <span class="detail-value mono">${data.address}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">Data Type</span>
                            <span class="detail-value">${data.data_type}</span>
                        </div>
                        <div class="detail-row">
                            <span class="detail-label">Timestamp</span>
                            <span class="detail-value">${new Date(data.timestamp).toLocaleString()}</span>
                        </div>
                        <div class="result-note">
                            <i class="bi bi-info-circle"></i> This address is not in the known registers database.
                        </div>
                    </div>
                `;
            }

            resultDiv.innerHTML = html;

            // Add event listeners for action buttons
            if (regInfo && !isConfigured) {
                document.getElementById('queryConfigureBtn')?.addEventListener('click', () => {
                    this.closeQueryModal();
                    this.openAddModal(regInfo);
                });
                document.getElementById('queryQuickAddBtn')?.addEventListener('click', () => {
                    this.quickAddRegister(regInfo);
                    this.closeQueryModal();
                    this.showToast('success', 'Added', `${regInfo.name} added to monitoring`);
                });
            }

            // Add to history
            this.queryHistory.unshift({
                address: data.address,
                value: displayValue,
                dataType: data.data_type,
                timestamp: data.timestamp,
                description: regInfo?.description
            });

            this.renderQueryHistory();

        } catch (error) {
            resultDiv.innerHTML = `<div class="result-error"><i class="bi bi-exclamation-triangle"></i> Error: ${error.message}</div>`;
        }
    }

    renderQueryHistory() {
        const container = document.getElementById('queryHistory');
        if (!container) return;
        container.innerHTML = '';

        this.queryHistory.slice(0, 20).forEach(item => {
            const div = document.createElement('div');
            div.className = 'history-item';
            div.innerHTML = `
                <span class="address">${item.address}</span>
                <span class="value">${item.value}</span>
                <span class="time">${new Date(item.timestamp).toLocaleTimeString()}</span>
            `;
            container.appendChild(div);
        });
    }

    renderSelectedRegistersList() {
        const container = document.getElementById('selectedRegistersList');

        // Filter registers by tab (category) and search
        let filtered = this.selectedRegisters.filter(reg => {
            // Tab filter (by category)
            if (this.configTab !== 'all' && reg._category !== this.configTab) {
                return false;
            }
            // Search filter
            if (this.configSearch) {
                const searchStr = `${reg.address} ${reg.label} ${reg.name} ${reg.description || ''} ${reg.unit || ''} ${reg.poll_group}`.toLowerCase();
                if (!searchStr.includes(this.configSearch)) {
                    return false;
                }
            }
            return true;
        });

        // Update register count
        const countEl = document.getElementById('registerCount');
        if (countEl) {
            const total = this.selectedRegisters.length;
            const shown = filtered.length;
            countEl.textContent = shown === total
                ? `${total} register${total !== 1 ? 's' : ''}`
                : `${shown} of ${total} registers`;
        }

        if (filtered.length === 0) {
            container.innerHTML = `<div class="empty-state">${this.selectedRegisters.length === 0
                ? 'No registers selected. Go to Registers page to add some.'
                : 'No registers match the current filter.'}</div>`;
            return;
        }

        // Create compact table
        container.innerHTML = `
            <table class="selected-registers-table">
                <thead>
                    <tr>
                        <th>Addr</th>
                        <th>Label</th>
                        <th>Unit</th>
                        <th>Poll</th>
                        <th class="center">MQTT</th>
                        <th class="center">Influx</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="selectedRegistersBody"></tbody>
            </table>
        `;

        const tbody = document.getElementById('selectedRegistersBody');

        filtered.forEach(reg => {
            const tr = document.createElement('tr');

            // MQTT tooltip
            const mqttTooltip = reg.mqtt_enabled && reg.mqtt_topic
                ? `Topic: ${reg.mqtt_topic}`
                : (reg.mqtt_enabled ? 'Enabled' : 'Disabled');

            // InfluxDB tooltip
            let influxTooltip = 'Disabled';
            if (reg.influxdb_enabled) {
                influxTooltip = reg.influxdb_measurement || 'Enabled';
                if (reg.influxdb_tags && Object.keys(reg.influxdb_tags).length > 0) {
                    influxTooltip += ` [${Object.entries(reg.influxdb_tags).map(([k,v]) => `${k}=${v}`).join(', ')}]`;
                }
            }

            tr.innerHTML = `
                <td class="addr-cell">${reg.address}</td>
                <td class="label-cell">
                    <span class="reg-label">${reg.label}</span>
                    <span class="reg-name">${reg.name}</span>
                </td>
                <td class="unit-cell">${reg.unit || '-'}</td>
                <td><span class="badge poll-${reg.poll_group}">${reg.poll_group}</span></td>
                <td class="center">
                    <span class="status-icon ${reg.mqtt_enabled ? 'active' : ''}" title="${mqttTooltip}">
                        ${reg.mqtt_enabled ? '&#10003;' : '&#10005;'}
                    </span>
                </td>
                <td class="center">
                    <span class="status-icon ${reg.influxdb_enabled ? 'active' : ''}" title="${influxTooltip}">
                        ${reg.influxdb_enabled ? '&#10003;' : '&#10005;'}
                    </span>
                </td>
                <td class="actions-cell">
                    <button class="btn-action edit" title="Edit">&#9998;</button>
                    <button class="btn-action remove" title="Remove">&#10006;</button>
                </td>
            `;

            tr.querySelector('.edit').addEventListener('click', () => this.editRegister(reg));
            tr.querySelector('.remove').addEventListener('click', () => this.removeRegister(reg.address));

            tbody.appendChild(tr);
        });
    }

    renderPollGroups() {
        const container = document.getElementById('pollGroups');
        if (!container) return;
        container.innerHTML = '';

        for (const [name, config] of Object.entries(this.pollGroups)) {
            const div = document.createElement('div');
            div.className = 'poll-group-card';
            div.innerHTML = `
                <div class="name">${name}</div>
                <div class="interval">${config.interval}s</div>
                <div class="desc">${config.description}</div>
            `;
            container.appendChild(div);
        }
    }

    renderStatusDetails() {
        const container = document.getElementById('statusDetails');
        if (!container || !this.status) return;

        const modbus = this.status.modbus || {};
        const mqtt = this.status.mqtt || {};
        const influx = this.status.influxdb || {};

        container.innerHTML = `
            <div class="status-block">
                <h4>
                    <span class="status-indicator ${modbus.connected ? 'ok' : 'error'}"></span>
                    Modbus
                </h4>
                <div class="detail-row">
                    <span class="label">Host</span>
                    <span class="value">${modbus.host}:${modbus.port}</span>
                </div>
                <div class="detail-row">
                    <span class="label">Successful reads</span>
                    <span class="value">${modbus.successful_reads || 0}</span>
                </div>
                <div class="detail-row">
                    <span class="label">Failed reads</span>
                    <span class="value">${modbus.failed_reads || 0}</span>
                </div>
            </div>

            <div class="status-block">
                <h4>
                    <span class="status-indicator ${mqtt.connected ? 'ok' : (mqtt.enabled ? 'error' : '')}"></span>
                    MQTT ${mqtt.enabled ? '' : '(Disabled)'}
                </h4>
                <div class="detail-row">
                    <span class="label">Broker</span>
                    <span class="value">${mqtt.broker}:${mqtt.port}</span>
                </div>
                <div class="detail-row">
                    <span class="label">Published</span>
                    <span class="value">${mqtt.messages_published || 0}</span>
                </div>
                <div class="detail-row">
                    <span class="label">Skipped</span>
                    <span class="value">${mqtt.messages_skipped || 0}</span>
                </div>
                <div class="detail-row">
                    <span class="label">Mode</span>
                    <span class="value">${mqtt.publish_mode || '-'}</span>
                </div>
            </div>

            <div class="status-block">
                <h4>
                    <span class="status-indicator ${influx.connected ? 'ok' : (influx.enabled ? 'error' : '')}"></span>
                    InfluxDB ${influx.enabled ? '' : '(Disabled)'}
                </h4>
                <div class="detail-row">
                    <span class="label">URL</span>
                    <span class="value">${influx.url || '-'}</span>
                </div>
                <div class="detail-row">
                    <span class="label">Bucket</span>
                    <span class="value">${influx.bucket || '-'}</span>
                </div>
                <div class="detail-row">
                    <span class="label">Writes</span>
                    <span class="value">${influx.writes_total || 0}</span>
                </div>
                <div class="detail-row">
                    <span class="label">Mode</span>
                    <span class="value">${influx.publish_mode || '-'}</span>
                </div>
            </div>
        `;
    }

    // ============ Settings Config (Modbus, MQTT, InfluxDB) ============

    async loadSettingsConfig() {
        try {
            // Load all configs in parallel
            const [modbusRes, mqttRes, influxRes, envRes] = await Promise.all([
                fetch('/api/config/modbus'),
                fetch('/api/config/mqtt'),
                fetch('/api/config/influxdb'),
                fetch('/api/config/env-overrides')
            ]);

            const modbus = await modbusRes.json();
            const mqtt = await mqttRes.json();
            const influx = await influxRes.json();
            const envOverrides = await envRes.json();

            // Store original values for change detection
            this.originalConfig = { modbus, mqtt, influx };
            this.envOverrides = envOverrides;

            // Populate Modbus fields
            document.getElementById('cfgModbusHost').value = modbus.host || '';
            document.getElementById('cfgModbusPort').value = modbus.port || 502;
            document.getElementById('cfgModbusUnitId').value = modbus.unit_id || 1;
            document.getElementById('cfgModbusTimeout').value = modbus.timeout || 3;
            document.getElementById('cfgModbusRetryAttempts').value = modbus.retry_attempts || 3;
            document.getElementById('cfgModbusRetryDelay').value = modbus.retry_delay || 1.0;

            // Populate MQTT fields
            document.getElementById('cfgMqttEnabled').checked = mqtt.enabled;
            document.getElementById('cfgMqttBroker').value = mqtt.broker || '';
            document.getElementById('cfgMqttPort').value = mqtt.port || 1883;
            document.getElementById('cfgMqttUsername').value = mqtt.username || '';
            document.getElementById('cfgMqttPrefix').value = mqtt.topic_prefix || '';
            document.getElementById('cfgMqttPublishMode').value = mqtt.publish_mode || 'changed';
            document.getElementById('cfgMqttQos').value = mqtt.qos || 0;
            document.getElementById('cfgMqttRetain').checked = mqtt.retain !== false;
            document.getElementById('cfgMqttHaEnabled').checked = mqtt.ha_discovery_enabled !== false;
            document.getElementById('cfgMqttHaPrefix').value = mqtt.ha_discovery_prefix || 'homeassistant';
            document.getElementById('cfgMqttHaDeviceName').value = mqtt.ha_device_name || '';

            // Populate InfluxDB fields
            document.getElementById('cfgInfluxEnabled').checked = influx.enabled;
            document.getElementById('cfgInfluxUrl').value = influx.url || '';
            document.getElementById('cfgInfluxOrg').value = influx.org || '';
            document.getElementById('cfgInfluxBucket').value = influx.bucket || '';
            document.getElementById('cfgInfluxWriteInterval').value = influx.write_interval || 5;
            document.getElementById('cfgInfluxPublishMode').value = influx.publish_mode || 'changed';

            // Show ENV override warnings
            this.showEnvOverrides(envOverrides);

            // Update status dots
            this.updateSettingsStatusDots();

            // Toggle settings body visibility based on enabled
            this.toggleSettingsBody('mqtt', mqtt.enabled);
            this.toggleSettingsBody('influx', influx.enabled);

        } catch (error) {
            console.error('Failed to load settings:', error);
            this.showToast('Failed to load settings', 'error');
        }
    }

    showEnvOverrides(overrides) {
        // Map config paths to field IDs
        const mapping = {
            'modbus.host': 'envModbusHost',
            'mqtt.broker': 'envMqttBroker',
            'influxdb.url': 'envInfluxUrl'
        };

        Object.keys(mapping).forEach(path => {
            const el = document.getElementById(mapping[path]);
            if (el) {
                if (overrides[path]) {
                    el.textContent = `ENV: ${path.toUpperCase().replace('.', '_')}=${overrides[path]}`;
                    el.classList.add('visible');
                } else {
                    el.classList.remove('visible');
                }
            }
        });
    }

    updateSettingsStatusDots() {
        const modbusDot = document.getElementById('modbusStatusDot');
        if (modbusDot && this.status) {
            const connected = this.status.modbus?.connected;
            modbusDot.className = `status-dot ${connected ? 'connected' : 'disconnected'}`;
        }
    }

    toggleSettingsBody(service, enabled) {
        const body = document.getElementById(`${service}SettingsBody`);
        if (body) {
            if (enabled) {
                body.classList.remove('disabled');
            } else {
                body.classList.add('disabled');
            }
        }
    }

    setupConfigMainTabs() {
        document.querySelectorAll('.config-main-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                const tabName = tab.dataset.tab;

                // Update active tab
                document.querySelectorAll('.config-main-tab').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');

                // Show/hide content
                document.getElementById('settingsTabContent').style.display = tabName === 'settings' ? 'block' : 'none';
                document.getElementById('registersTabContent').style.display = tabName === 'registers' ? 'block' : 'none';
            });
        });
    }

    setupSettingsListeners() {
        // MQTT enabled toggle
        document.getElementById('cfgMqttEnabled')?.addEventListener('change', (e) => {
            this.toggleSettingsBody('mqtt', e.target.checked);
            this.checkConfigChanged();
        });

        // InfluxDB enabled toggle
        document.getElementById('cfgInfluxEnabled')?.addEventListener('change', (e) => {
            this.toggleSettingsBody('influx', e.target.checked);
            this.checkConfigChanged();
        });

        // Apply config button
        document.getElementById('applyConfigBtn')?.addEventListener('click', () => {
            this.applyConfiguration();
        });

        // Add change listeners to all settings fields
        const settingsFields = [
            'cfgModbusHost', 'cfgModbusPort', 'cfgModbusUnitId', 'cfgModbusTimeout',
            'cfgModbusRetryAttempts', 'cfgModbusRetryDelay',
            'cfgMqttEnabled', 'cfgMqttBroker', 'cfgMqttPort', 'cfgMqttUsername', 'cfgMqttPassword',
            'cfgMqttPrefix', 'cfgMqttPublishMode', 'cfgMqttQos', 'cfgMqttRetain',
            'cfgMqttHaEnabled', 'cfgMqttHaPrefix', 'cfgMqttHaDeviceName',
            'cfgInfluxEnabled', 'cfgInfluxUrl', 'cfgInfluxToken', 'cfgInfluxOrg',
            'cfgInfluxBucket', 'cfgInfluxWriteInterval', 'cfgInfluxPublishMode'
        ];

        settingsFields.forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                el.addEventListener('change', () => this.saveSettingsDebounced());
                if (el.type === 'text' || el.type === 'number' || el.type === 'password') {
                    el.addEventListener('input', () => this.saveSettingsDebounced());
                }
            }
        });
    }

    saveSettingsDebounced() {
        clearTimeout(this._saveSettingsTimeout);
        this._saveSettingsTimeout = setTimeout(() => {
            this.saveAllSettings();
        }, 500);
    }

    async saveAllSettings() {
        try {
            // Gather Modbus config
            const modbusConfig = {
                host: document.getElementById('cfgModbusHost').value,
                port: parseInt(document.getElementById('cfgModbusPort').value) || 502,
                unit_id: parseInt(document.getElementById('cfgModbusUnitId').value) || 1,
                timeout: parseInt(document.getElementById('cfgModbusTimeout').value) || 3,
                retry_attempts: parseInt(document.getElementById('cfgModbusRetryAttempts').value) || 3,
                retry_delay: parseFloat(document.getElementById('cfgModbusRetryDelay').value) || 1.0
            };

            // Gather MQTT config
            const mqttConfig = {
                enabled: document.getElementById('cfgMqttEnabled').checked,
                broker: document.getElementById('cfgMqttBroker').value,
                port: parseInt(document.getElementById('cfgMqttPort').value) || 1883,
                username: document.getElementById('cfgMqttUsername').value,
                password: document.getElementById('cfgMqttPassword').value || undefined,
                topic_prefix: document.getElementById('cfgMqttPrefix').value,
                publish_mode: document.getElementById('cfgMqttPublishMode').value,
                qos: parseInt(document.getElementById('cfgMqttQos').value) || 0,
                retain: document.getElementById('cfgMqttRetain').checked,
                ha_discovery_enabled: document.getElementById('cfgMqttHaEnabled').checked,
                ha_discovery_prefix: document.getElementById('cfgMqttHaPrefix').value,
                ha_device_name: document.getElementById('cfgMqttHaDeviceName').value
            };

            // Gather InfluxDB config
            const influxConfig = {
                enabled: document.getElementById('cfgInfluxEnabled').checked,
                url: document.getElementById('cfgInfluxUrl').value,
                token: document.getElementById('cfgInfluxToken').value || undefined,
                org: document.getElementById('cfgInfluxOrg').value,
                bucket: document.getElementById('cfgInfluxBucket').value,
                write_interval: parseInt(document.getElementById('cfgInfluxWriteInterval').value) || 5,
                publish_mode: document.getElementById('cfgInfluxPublishMode').value
            };

            // Save all configs
            await Promise.all([
                fetch('/api/config/modbus', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(modbusConfig)
                }),
                fetch('/api/config/mqtt', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(mqttConfig)
                }),
                fetch('/api/config/influxdb', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(influxConfig)
                })
            ]);

            // Show apply banner
            this.showApplyBanner();

        } catch (error) {
            console.error('Failed to save settings:', error);
            this.showToast('Failed to save settings', 'error');
        }
    }

    checkConfigChanged() {
        // Simple check - could be enhanced
        this.showApplyBanner();
    }

    showApplyBanner() {
        const banner = document.getElementById('applyConfigBanner');
        if (banner) {
            banner.style.display = 'flex';
        }
    }

    hideApplyBanner() {
        const banner = document.getElementById('applyConfigBanner');
        if (banner) {
            banner.style.display = 'none';
        }
    }

    async applyConfiguration() {
        const btn = document.getElementById('applyConfigBtn');
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<i class="bi bi-arrow-repeat spin"></i> Applying...';
        }

        try {
            const response = await fetch('/api/config/apply', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });

            const result = await response.json();

            if (result.status === 'ok') {
                this.showToast('Configuration applied successfully', 'success');
                this.hideApplyBanner();

                // Reload status to update connection states
                await this.loadStatus();
                this.updateSettingsStatusDots();
                this.renderStatusDetails();
            } else {
                this.showToast('Failed to apply configuration', 'error');
            }

        } catch (error) {
            console.error('Failed to apply configuration:', error);
            this.showToast('Failed to apply configuration', 'error');
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<i class="bi bi-arrow-repeat"></i> Apply Configuration';
            }
        }
    }

    editRegister(reg) {
        document.getElementById('editAddress').value = reg.address;
        document.getElementById('editLabel').value = reg.label;
        document.getElementById('editPollGroup').value = reg.poll_group;
        document.getElementById('editWidget').value = reg.ui_widget;
        document.getElementById('editMqttEnabled').checked = reg.mqtt_enabled;
        document.getElementById('editMqttTopic').value = reg.mqtt_topic;
        document.getElementById('editInfluxEnabled').checked = reg.influxdb_enabled;
        document.getElementById('editInfluxMeasurement').value = reg.influxdb_measurement;
        document.getElementById('editInfluxTags').value = JSON.stringify(reg.influxdb_tags || {});

        // Gauge options
        document.getElementById('editGaugeMin').value = reg.ui_config?.min ?? '';
        document.getElementById('editGaugeMax').value = reg.ui_config?.max ?? '';
        document.getElementById('editGaugeColor').value = reg.ui_config?.color || '#3b82f6';
        this.toggleGaugeOptions('edit', reg.ui_widget);

        // Fill thresholds - use existing or auto-detect
        this.autoFillThresholds('edit', reg.unit, reg.name, reg.thresholds);

        this.openModal('registerModal');
    }

    toggleGaugeOptions(prefix, widgetType) {
        const el = document.getElementById(`${prefix}GaugeOptions`);
        if (el) {
            el.style.display = (widgetType === 'gauge') ? 'block' : 'none';
        }
    }

    closeRegisterModal() {
        this.closeModal('registerModal');
    }

    saveRegisterEdit() {
        const address = parseInt(document.getElementById('editAddress').value);
        const reg = this.selectedRegisters.find(r => r.address === address);

        if (reg) {
            reg.label = document.getElementById('editLabel').value;
            reg.poll_group = document.getElementById('editPollGroup').value;
            reg.ui_widget = document.getElementById('editWidget').value;
            reg.mqtt_enabled = document.getElementById('editMqttEnabled').checked;
            reg.mqtt_topic = document.getElementById('editMqttTopic').value;
            reg.influxdb_enabled = document.getElementById('editInfluxEnabled').checked;
            reg.influxdb_measurement = document.getElementById('editInfluxMeasurement').value;

            try {
                reg.influxdb_tags = JSON.parse(document.getElementById('editInfluxTags').value || '{}');
            } catch (e) {
                reg.influxdb_tags = {};
            }

            // Save gauge options
            if (!reg.ui_config) reg.ui_config = {};
            const gaugeMin = document.getElementById('editGaugeMin').value;
            const gaugeMax = document.getElementById('editGaugeMax').value;
            reg.ui_config.min = gaugeMin !== '' ? parseFloat(gaugeMin) : undefined;
            reg.ui_config.max = gaugeMax !== '' ? parseFloat(gaugeMax) : undefined;
            reg.ui_config.color = document.getElementById('editGaugeColor').value;

            // Save thresholds
            reg.thresholds = this.readThresholdsFromForm('edit');
        }

        this.closeModal();
        this.renderSelectedRegistersList();
        this.updateDashboard();
        this.saveSelectedRegisters();
    }

    removeRegister(address) {
        this.selectedRegisters = this.selectedRegisters.filter(r => r.address !== address);
        this.renderSelectedRegistersList();
    }

    async saveSelectedRegisters() {
        const btn = document.getElementById('saveRegistersBtn');
        this.setButtonLoading(btn, true);

        try {
            // Save registers to file
            const response = await fetch('/api/registers/selected', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(this.selectedRegisters)
            });

            if (!response.ok) {
                throw new Error('Save failed');
            }

            // Auto-reload registers in backend (no restart needed)
            const reloadResponse = await fetch('/api/config/reload-registers', {
                method: 'POST'
            });

            if (reloadResponse.ok) {
                this.showToast('success', 'Configuration Saved', 'Registers reloaded successfully.');
            } else {
                this.showToast('success', 'Configuration Saved', 'Saved. Apply configuration to reload.');
            }

        } catch (error) {
            this.showToast('error', 'Save Failed', error.message);
        } finally {
            this.setButtonLoading(btn, false, 'Save');
        }
    }

    // ============ Raw Config Modal ============

    openRawConfigModal() {
        const modal = document.getElementById('rawConfigModal');
        const editor = document.getElementById('rawConfigEditor');

        // Prepare config data for display
        const configData = this.selectedRegisters.map(reg => ({
            address: reg.address,
            name: reg.name,
            label: reg.label,
            unit: reg.unit,
            description: reg.description,
            data_type: reg.data_type,
            poll_group: reg.poll_group,
            mqtt_enabled: reg.mqtt_enabled,
            mqtt_topic: reg.mqtt_topic,
            influxdb_enabled: reg.influxdb_enabled,
            influxdb_measurement: reg.influxdb_measurement,
            influxdb_tags: reg.influxdb_tags,
            ui_show_on_dashboard: reg.ui_show_on_dashboard,
            ui_widget: reg.ui_widget,
            ui_config: reg.ui_config
        }));

        editor.value = JSON.stringify(configData, null, 2);
        this.openModal('rawConfigModal');
        this.validateRawConfig();
    }

    closeRawConfigModal() {
        this.closeModal('rawConfigModal');
    }

    formatRawConfig() {
        const editor = document.getElementById('rawConfigEditor');
        try {
            const parsed = JSON.parse(editor.value);
            editor.value = JSON.stringify(parsed, null, 2);
            this.validateRawConfig();
        } catch (e) {
            // Can't format invalid JSON
        }
    }

    validateRawConfig() {
        const editor = document.getElementById('rawConfigEditor');
        const statusEl = document.getElementById('editorStatus');

        try {
            const parsed = JSON.parse(editor.value);

            if (!Array.isArray(parsed)) {
                throw new Error('Configuration must be an array');
            }

            // Validate each register
            for (let i = 0; i < parsed.length; i++) {
                const reg = parsed[i];
                if (!reg.address || typeof reg.address !== 'number') {
                    throw new Error(`Register ${i + 1}: missing or invalid 'address'`);
                }
                if (!reg.name || typeof reg.name !== 'string') {
                    throw new Error(`Register ${i + 1}: missing or invalid 'name'`);
                }
            }

            statusEl.textContent = `Valid JSON - ${parsed.length} registers`;
            statusEl.className = 'editor-status valid';
            return true;

        } catch (e) {
            statusEl.textContent = `Error: ${e.message}`;
            statusEl.className = 'editor-status invalid';
            return false;
        }
    }

    async saveRawConfig() {
        if (!this.validateRawConfig()) {
            this.showToast('error', 'Invalid JSON', 'Please fix the errors before saving.');
            return;
        }

        const btn = document.getElementById('rawConfigSave');
        this.setButtonLoading(btn, true);

        try {
            const parsed = JSON.parse(document.getElementById('rawConfigEditor').value);

            const response = await fetch('/api/registers/selected', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(parsed)
            });

            if (!response.ok) {
                throw new Error('Save failed');
            }

            this.closeRawConfigModal();
            await this.loadSelectedRegisters();
            this.updateConfigTabs();
            this.renderSelectedRegistersList();
            this.showToast('success', 'Configuration Saved', 'Changes applied successfully.');

        } catch (error) {
            this.showToast('error', 'Save Failed', error.message);
        } finally {
            this.setButtonLoading(btn, false, 'Save & Apply');
        }
    }

    // ============ Customize Dashboard ============

    openCustomizeDashModal() {
        const modal = document.getElementById('customizeDashModal');
        const list = document.getElementById('customizeList');

        // Sort by dashboard_order if exists
        const sortedRegs = [...this.selectedRegisters].sort((a, b) => {
            const orderA = a.ui_config?.dashboard_order ?? 999;
            const orderB = b.ui_config?.dashboard_order ?? 999;
            return orderA - orderB;
        });

        // Build list of all selected registers
        let html = '';
        sortedRegs.forEach((reg, index) => {
            const checked = reg.ui_show_on_dashboard ? 'checked' : '';
            const widgetType = reg.ui_widget || 'value';
            const isWide = reg.ui_config?.wide ? 'active' : '';

            html += `
                <div class="customize-item" data-address="${reg.address}" draggable="true">
                    <i class="bi bi-grip-vertical customize-drag-handle"></i>
                    <input type="checkbox" data-address="${reg.address}" ${checked}>
                    <div class="customize-item-info">
                        <div class="customize-item-label">${reg.label || reg.name}</div>
                        <div class="customize-item-details">${reg.name} · ${reg.unit || 'N/A'}</div>
                    </div>
                    <div class="customize-item-controls">
                        <select class="customize-select" data-address="${reg.address}" data-field="widget">
                            <option value="value" ${widgetType === 'value' ? 'selected' : ''}>Value</option>
                            <option value="gauge" ${widgetType === 'gauge' ? 'selected' : ''}>Gauge</option>
                            <option value="chart" ${widgetType === 'chart' ? 'selected' : ''}>Chart</option>
                        </select>
                        <button class="customize-size-toggle ${isWide}" data-address="${reg.address}" title="Wide widget">
                            <i class="bi bi-arrows-expand"></i> Wide
                        </button>
                    </div>
                </div>
            `;
        });

        if (this.selectedRegisters.length === 0) {
            html = '<div class="empty-state">No registers monitored. Add registers in the Registers page first.</div>';
        }

        list.innerHTML = html;

        // Setup drag-drop
        this.setupCustomizeDragDrop(list);

        // Setup size toggle buttons
        list.querySelectorAll('.customize-size-toggle').forEach(btn => {
            btn.addEventListener('click', () => {
                btn.classList.toggle('active');
            });
        });

        this.openModal('customizeDashModal');
    }

    setupCustomizeDragDrop(list) {
        let draggedItem = null;

        list.querySelectorAll('.customize-item').forEach(item => {
            item.addEventListener('dragstart', (e) => {
                draggedItem = item;
                item.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
            });

            item.addEventListener('dragend', () => {
                item.classList.remove('dragging');
                list.querySelectorAll('.customize-item').forEach(i => i.classList.remove('drag-over'));
                draggedItem = null;
            });

            item.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                if (item !== draggedItem) {
                    item.classList.add('drag-over');
                }
            });

            item.addEventListener('dragleave', () => {
                item.classList.remove('drag-over');
            });

            item.addEventListener('drop', (e) => {
                e.preventDefault();
                item.classList.remove('drag-over');
                if (draggedItem && draggedItem !== item) {
                    const allItems = [...list.querySelectorAll('.customize-item')];
                    const draggedIdx = allItems.indexOf(draggedItem);
                    const targetIdx = allItems.indexOf(item);

                    if (draggedIdx < targetIdx) {
                        item.parentNode.insertBefore(draggedItem, item.nextSibling);
                    } else {
                        item.parentNode.insertBefore(draggedItem, item);
                    }
                }
            });
        });
    }

    closeCustomizeDashModal() {
        this.closeModal('customizeDashModal');
    }

    async saveCustomizeDash() {
        const list = document.getElementById('customizeList');
        const items = list.querySelectorAll('.customize-item');
        const btn = document.getElementById('customizeDashSave');

        // Update selectedRegisters based on order, visibility, widget type, and size
        items.forEach((item, index) => {
            const address = parseInt(item.dataset.address);
            const reg = this.selectedRegisters.find(r => r.address === address);
            if (reg) {
                // Visibility
                const checkbox = item.querySelector('input[type="checkbox"]');
                reg.ui_show_on_dashboard = checkbox?.checked ?? false;

                // Widget type
                const widgetSelect = item.querySelector('.customize-select');
                if (widgetSelect) {
                    reg.ui_widget = widgetSelect.value;
                }

                // Size (wide)
                const sizeToggle = item.querySelector('.customize-size-toggle');
                if (!reg.ui_config) reg.ui_config = {};
                reg.ui_config.wide = sizeToggle?.classList.contains('active') ?? false;

                // Order
                reg.ui_config.dashboard_order = index;
            }
        });

        this.setButtonLoading(btn, true);

        // Save to server
        try {
            const response = await fetch('/api/registers/selected', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(this.selectedRegisters)
            });

            if (!response.ok) throw new Error('Failed to save');

            this.closeCustomizeDashModal();
            this.showToast('success', 'Dashboard Updated', 'Layout and settings saved');

            // Refresh dashboard - clear and recreate
            document.getElementById('dashboardGrid').innerHTML = '';
            this.updateDashboard();

        } catch (error) {
            this.showToast('error', 'Save Failed', error.message);
        } finally {
            this.setButtonLoading(btn, false, 'Save');
        }
    }

    // ============ Monitor Page ============

    initMonitorPage() {
        // Initialize canvas
        this.monitorCanvas = document.getElementById('monitorCanvas');
        this.monitorCtx = this.monitorCanvas.getContext('2d');

        // Render categories sidebar
        this.renderMonitorCategories();

        // Setup drag and drop
        this.setupMonitorDragDrop();

        // Setup event listeners
        this.setupMonitorEventListeners();

        // Update table and legend
        this.updateMonitorTable();
        this.updateMonitorLegend();

        // Handle resize
        window.addEventListener('resize', () => this.resizeMonitorCanvas());

        // Delay canvas resize to ensure layout is complete
        requestAnimationFrame(() => {
            this.resizeMonitorCanvas();
        });

        // Show onboarding hint on first visit
        if (!localStorage.getItem('janitza-monitor-visited')) {
            this.showMonitorOnboarding();
        }
    }

    showMonitorOnboarding() {
        const main = document.querySelector('.monitor-main');
        if (!main || document.getElementById('monitorHint')) return;

        const hint = document.createElement('div');
        hint.className = 'hint-banner';
        hint.id = 'monitorHint';
        hint.innerHTML = `
            <span class="hint-banner-icon">💡</span>
            <span class="hint-banner-text">
                <strong>Getting started:</strong> Drag registers from the left sidebar onto the graph to monitor them in real-time.
                Use mouse wheel to zoom and drag to pan when zoomed.
            </span>
            <button class="hint-banner-dismiss" onclick="app.dismissMonitorHint()">✕</button>
        `;

        main.insertBefore(hint, main.firstChild);
    }

    dismissMonitorHint() {
        localStorage.setItem('janitza-monitor-visited', 'true');
        document.getElementById('monitorHint')?.remove();
    }

    resizeMonitorCanvas() {
        if (!this.monitorCanvas) return;
        const container = document.getElementById('monitorDropzone');
        if (!container) return;

        const rect = container.getBoundingClientRect();
        // Account for padding/border
        const width = Math.floor(rect.width);
        const height = Math.floor(rect.height);

        if (width > 0 && height > 0) {
            this.monitorCanvas.width = width;
            this.monitorCanvas.height = height;
            this.drawMonitorGraph();
        }
    }

    renderMonitorCategories() {
        const container = document.getElementById('monitorCategories');

        // Use only selected registers (the ones being polled by Modbus)
        // Group them by category
        const categories = new Map();

        this.selectedRegisters.forEach(reg => {
            // Derive category from influxdb_measurement or unit
            let cat = (reg.influxdb_measurement || '').toLowerCase();
            if (!cat) {
                const unit = (reg.unit || '').toLowerCase();
                if (unit === 'v') cat = 'voltage';
                else if (unit === 'a') cat = 'current';
                else if (unit === 'w' || unit === 'kw') cat = 'power';
                else if (unit === 'wh' || unit === 'kwh') cat = 'energy';
                else if (unit === 'hz') cat = 'frequency';
                else if (unit === 'var' || unit === 'kvar') cat = 'reactive';
                else if (unit === 'va' || unit === 'kva') cat = 'apparent';
                else cat = 'other';
            }

            if (!categories.has(cat)) {
                categories.set(cat, []);
            }
            categories.get(cat).push(reg);
        });

        let html = '';

        // Sort categories alphabetically
        const sortedCats = Array.from(categories.keys()).sort();

        for (const catName of sortedCats) {
            const items = categories.get(catName);

            // Filter by search
            const filteredItems = items.filter(item => {
                if (!this.monitorSearch) return true;
                const searchStr = `${item.name} ${item.label || ''} ${item.description || ''} ${item.unit || ''}`.toLowerCase();
                return searchStr.includes(this.monitorSearch);
            });

            if (filteredItems.length === 0) continue;

            const displayName = catName.charAt(0).toUpperCase() + catName.slice(1);
            html += `
                <div class="monitor-category expanded" data-category="${catName}">
                    <div class="monitor-category-header">
                        <span class="arrow">&#9654;</span>
                        <span>${displayName}</span>
                        <span style="margin-left: auto; opacity: 0.5;">(${filteredItems.length})</span>
                    </div>
                    <div class="monitor-category-items">
                        ${filteredItems.map(item => {
                            const onGraph = this.monitorData[item.address] ? 'on-graph' : '';
                            return `
                                <div class="monitor-item ${onGraph}"
                                     draggable="true"
                                     data-address="${item.address}"
                                     data-name="${item.name}"
                                     data-description="${item.label || item.description || item.name}"
                                     data-unit="${item.unit || ''}"
                                     data-datatype="${item.data_type || 'float'}">
                                    <span class="item-name">${item.label || item.description || item.name}</span>
                                    <span class="item-unit">${item.unit || ''}</span>
                                </div>
                            `;
                        }).join('')}
                    </div>
                </div>
            `;
        }

        if (!html) {
            container.innerHTML = `
                <div class="empty-state" style="padding: 40px 20px;">
                    <div class="empty-state-icon">📈</div>
                    <div class="empty-state-title">No registers monitored</div>
                    <div class="empty-state-desc">
                        Add registers to monitoring to see real-time data.
                    </div>
                    <button class="empty-state-action" onclick="app.navigateTo('registers')">
                        📋 Browse Registers
                    </button>
                </div>
            `;
            return;
        }

        container.innerHTML = html;

        // Setup category toggle
        container.querySelectorAll('.monitor-category-header').forEach(header => {
            header.addEventListener('click', () => {
                header.parentElement.classList.toggle('expanded');
            });
        });

        // Setup draggable items and click handlers
        container.querySelectorAll('.monitor-item').forEach(item => {
            // Click to add
            item.addEventListener('click', () => {
                if (item.classList.contains('on-graph')) {
                    this.showToast('info', 'Already Added', 'This register is already on the graph');
                    return;
                }
                this.addToMonitor({
                    address: parseInt(item.dataset.address),
                    name: item.dataset.name,
                    description: item.dataset.description,
                    unit: item.dataset.unit,
                    dataType: item.dataset.datatype
                });
            });

            // Drag to add
            item.addEventListener('dragstart', (e) => {
                if (item.classList.contains('on-graph')) {
                    e.preventDefault();
                    return;
                }
                e.dataTransfer.setData('text/plain', JSON.stringify({
                    address: parseInt(item.dataset.address),
                    name: item.dataset.name,
                    description: item.dataset.description,
                    unit: item.dataset.unit,
                    dataType: item.dataset.datatype
                }));
                item.classList.add('dragging');
            });

            item.addEventListener('dragend', () => {
                item.classList.remove('dragging');
            });
        });
    }

    setupMonitorDragDrop() {
        const dropzone = document.getElementById('monitorDropzone');

        dropzone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropzone.classList.add('drag-over');
        });

        dropzone.addEventListener('dragleave', () => {
            dropzone.classList.remove('drag-over');
        });

        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('drag-over');

            try {
                const data = JSON.parse(e.dataTransfer.getData('text/plain'));
                this.addToMonitor(data);
            } catch (err) {
                console.error('Drop error:', err);
            }
        });
    }

    setupMonitorEventListeners() {
        // Search
        const searchInput = document.getElementById('monitorSearch');
        if (searchInput) {
            searchInput.addEventListener('input', (e) => {
                this.monitorSearch = e.target.value.toLowerCase();
                this.renderMonitorCategories();
            });
        }

        // Pause button
        const pauseBtn = document.getElementById('monitorPause');
        if (pauseBtn) {
            pauseBtn.addEventListener('click', () => {
                this.monitorPaused = !this.monitorPaused;
                pauseBtn.innerHTML = this.monitorPaused ? '&#9654;' : '&#9208;';
                pauseBtn.title = this.monitorPaused ? 'Resume' : 'Pause';
            });
        }

        // Clear button
        const clearBtn = document.getElementById('monitorClear');
        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                // Remove on-graph class from all items first
                document.querySelectorAll('.monitor-item.on-graph').forEach(item => {
                    item.classList.remove('on-graph');
                });

                this.monitorData = {};
                this.monitorColorIndex = 0;
                this.monitorZoom = 1;
                this.monitorPanX = 0;
                this.updateMonitorTable();
                this.updateMonitorLegend();
                this.updateDropzoneHint();
                this.drawMonitorGraph();
            });
        }

        // Zoom buttons
        const zoomInBtn = document.getElementById('zoomIn');
        if (zoomInBtn) {
            zoomInBtn.addEventListener('click', () => {
                this.monitorZoom = Math.min(10, this.monitorZoom * 1.2);
                this.drawMonitorGraph();
            });
        }

        const zoomOutBtn = document.getElementById('zoomOut');
        if (zoomOutBtn) {
            zoomOutBtn.addEventListener('click', () => {
                this.monitorZoom = Math.max(1, this.monitorZoom / 1.2);
                if (this.monitorZoom === 1) this.monitorPanX = 0;
                this.drawMonitorGraph();
            });
        }

        const zoomResetBtn = document.getElementById('zoomReset');
        if (zoomResetBtn) {
            zoomResetBtn.addEventListener('click', () => {
                this.monitorZoom = 1;
                this.monitorPanX = 0;
                this.drawMonitorGraph();
            });
        }

        // Zoom and Pan on canvas
        const canvas = this.monitorCanvas;
        if (canvas) {
            // Mouse wheel zoom
            canvas.addEventListener('wheel', (e) => {
                e.preventDefault();
                const zoomFactor = e.deltaY > 0 ? 0.9 : 1.1;
                const newZoom = Math.max(1, Math.min(10, this.monitorZoom * zoomFactor));

                // Zoom towards mouse position
                const rect = canvas.getBoundingClientRect();
                const mouseX = e.clientX - rect.left;
                const graphCenter = canvas.width / 2;

                // Adjust pan to keep mouse position stable
                if (newZoom !== this.monitorZoom) {
                    const zoomChange = newZoom / this.monitorZoom;
                    this.monitorPanX = (this.monitorPanX - mouseX) * zoomChange + mouseX;
                    this.monitorZoom = newZoom;
                    this.drawMonitorGraph();
                }
            });

            // Mouse drag for pan
            canvas.addEventListener('mousedown', (e) => {
                if (this.monitorZoom > 1) {
                    this.monitorIsDragging = true;
                    this.monitorDragStart = { x: e.clientX, y: e.clientY };
                    this.monitorLastPanX = this.monitorPanX;
                    canvas.style.cursor = 'grabbing';
                }
            });

            canvas.addEventListener('mousemove', (e) => {
                if (this.monitorIsDragging) {
                    const dx = e.clientX - this.monitorDragStart.x;
                    this.monitorPanX = this.monitorLastPanX + dx;
                    this.drawMonitorGraph();
                }
            });

            canvas.addEventListener('mouseup', () => {
                this.monitorIsDragging = false;
                canvas.style.cursor = this.monitorZoom > 1 ? 'grab' : 'default';
            });

            canvas.addEventListener('mouseleave', () => {
                this.monitorIsDragging = false;
                canvas.style.cursor = 'default';
            });

            // Double-click to reset zoom
            canvas.addEventListener('dblclick', () => {
                this.monitorZoom = 1;
                this.monitorPanX = 0;
                this.drawMonitorGraph();
            });

            // Tooltip on hover
            canvas.addEventListener('mousemove', (e) => this.showMonitorTooltip(e));
            canvas.addEventListener('mouseleave', () => this.hideMonitorTooltip());
        }
    }

    showMonitorTooltip(e) {
        const canvas = this.monitorCanvas;
        const params = this.monitorGraphParams;

        // Don't show tooltip while dragging or if no data
        if (!canvas || !params || this.monitorIsDragging || Object.keys(this.monitorData).length === 0) {
            this.hideMonitorTooltip();
            return;
        }

        const rect = canvas.getBoundingClientRect();
        // Scale mouse position to canvas internal coordinates
        const scaleX = canvas.width / rect.width;
        const scaleY = canvas.height / rect.height;
        const mouseX = (e.clientX - rect.left) * scaleX;
        const mouseY = (e.clientY - rect.top) * scaleY;

        // Check if mouse is in graph area
        if (mouseX < params.marginLeft || mouseX > params.width - params.marginRight ||
            mouseY < params.marginTop || mouseY > params.marginTop + params.graphHeight) {
            this.hideMonitorTooltip();
            return;
        }

        // Convert mouse X to time (accounting for zoom and pan)
        const centerX = params.marginLeft + params.graphWidth / 2;
        const baseX = (mouseX - this.monitorPanX - centerX) / this.monitorZoom + centerX;
        const normalizedX = (baseX - params.marginLeft) / params.graphWidth;
        const mouseTime = params.minTime + normalizedX * params.timeRange;

        // Find values closest to this time for each monitored variable
        let tooltipLines = [];
        const timeStr = new Date(mouseTime).toLocaleTimeString();
        tooltipLines.push(`<div class="tooltip-time">${timeStr}</div>`);

        for (const [address, info] of Object.entries(this.monitorData)) {
            if (info.data.length === 0) continue;

            // Find closest point
            let closest = null;
            let minDiff = Infinity;
            for (const point of info.data) {
                const diff = Math.abs(point.time - mouseTime);
                if (diff < minDiff) {
                    minDiff = diff;
                    closest = point;
                }
            }

            if (closest && minDiff < params.timeRange * 0.1) {  // Within 10% of time range
                tooltipLines.push(`
                    <div class="tooltip-row">
                        <span class="tooltip-color" style="background:${info.color}"></span>
                        <span class="tooltip-name">${info.name}</span>
                        <span class="tooltip-value">${closest.value.toFixed(2)} ${info.unit}</span>
                    </div>
                `);
            }
        }

        if (tooltipLines.length <= 1) {
            this.hideMonitorTooltip();
            return;
        }

        // Get or create tooltip element
        let tooltip = document.getElementById('monitorTooltip');
        if (!tooltip) {
            tooltip = document.createElement('div');
            tooltip.id = 'monitorTooltip';
            tooltip.className = 'monitor-tooltip';
            document.body.appendChild(tooltip);
        }

        tooltip.innerHTML = tooltipLines.join('');
        tooltip.style.display = 'block';

        // Position tooltip
        const tooltipX = e.clientX + 15;
        const tooltipY = e.clientY - 10;
        tooltip.style.left = tooltipX + 'px';
        tooltip.style.top = tooltipY + 'px';

        // Keep tooltip in viewport
        const tooltipRect = tooltip.getBoundingClientRect();
        if (tooltipRect.right > window.innerWidth) {
            tooltip.style.left = (e.clientX - tooltipRect.width - 15) + 'px';
        }
        if (tooltipRect.bottom > window.innerHeight) {
            tooltip.style.top = (e.clientY - tooltipRect.height - 10) + 'px';
        }

        // Draw crosshair on canvas
        this.drawMonitorCrosshair(mouseX, mouseY);
    }

    hideMonitorTooltip() {
        const tooltip = document.getElementById('monitorTooltip');
        if (tooltip) {
            tooltip.style.display = 'none';
        }
        // Clear crosshair
        if (this._showCrosshair) {
            this._showCrosshair = false;
            this.drawMonitorGraph();
        }
    }

    drawMonitorCrosshair(x, y) {
        const ctx = this.monitorCtx;
        const params = this.monitorGraphParams;
        if (!ctx || !params) return;

        // Store crosshair position for next redraw
        this._crosshairX = x;
        this._crosshairY = y;
        this._showCrosshair = true;

        // Redraw graph (will include crosshair)
        this.drawMonitorGraph();
    }

    drawCrosshairOverlay() {
        if (!this._showCrosshair) return;

        const ctx = this.monitorCtx;
        const params = this.monitorGraphParams;
        if (!ctx || !params) return;

        const x = this._crosshairX;
        const y = this._crosshairY;

        ctx.save();
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.4)';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);

        // Vertical line
        ctx.beginPath();
        ctx.moveTo(x, params.marginTop);
        ctx.lineTo(x, params.marginTop + params.graphHeight);
        ctx.stroke();

        // Horizontal line
        ctx.beginPath();
        ctx.moveTo(params.marginLeft, y);
        ctx.lineTo(params.width - params.marginRight, y);
        ctx.stroke();

        ctx.restore();
    }

    addToMonitor(data) {
        // Check if max 6 variables
        if (Object.keys(this.monitorData).length >= 6) {
            this.showToast('warning', 'Maximum Reached', 'You can monitor up to 6 values at a time');
            return;
        }

        // Check if already on graph
        if (this.monitorData[data.address]) {
            this.showToast('info', 'Already Added', 'This register is already being monitored');
            return;
        }

        // Get next color
        const color = this.monitorColors[this.monitorColorIndex % this.monitorColors.length];
        this.monitorColorIndex++;

        // Add to monitor data
        this.monitorData[data.address] = {
            name: data.description || data.name,
            unit: data.unit,
            color: color,
            data: [],
            min: null,
            max: null
        };

        // Mark item in sidebar as on-graph
        const item = document.querySelector(`.monitor-item[data-address="${data.address}"]`);
        if (item) {
            item.classList.add('on-graph');
        }

        // Update UI
        this.updateMonitorTable();
        this.updateMonitorLegend();
        this.updateDropzoneHint();

        this.showToast('success', 'Added', `${data.description || data.name} added to monitor`);
    }

    removeFromMonitor(address) {
        if (this.monitorData[address]) {
            const name = this.monitorData[address].name;
            delete this.monitorData[address];

            // Reset color index if all items removed
            if (Object.keys(this.monitorData).length === 0) {
                this.monitorColorIndex = 0;
            }

            // Unmark item in sidebar
            const item = document.querySelector(`.monitor-item[data-address="${address}"]`);
            if (item) {
                item.classList.remove('on-graph');
            }

            // Update UI
            this.updateMonitorTable();
            this.updateMonitorLegend();
            this.updateDropzoneHint();
            this.drawMonitorGraph();

            this.showToast('info', 'Removed', `${name} removed from monitor`);
        }
    }

    updateDropzoneHint() {
        const hint = document.getElementById('dropzoneHint');
        const dropzone = document.getElementById('monitorDropzone');
        const hasData = Object.keys(this.monitorData).length > 0;

        if (hint) {
            hint.classList.toggle('hidden', hasData);
        }
        if (dropzone) {
            dropzone.classList.toggle('has-data', hasData);
        }
    }

    updateMonitorTable() {
        const tbody = document.getElementById('monitorTableBody');
        const emptyMsg = document.getElementById('monitorTableEmpty');
        const hasData = Object.keys(this.monitorData).length > 0;

        if (emptyMsg) {
            emptyMsg.classList.toggle('hidden', hasData);
        }

        if (!tbody) return;

        if (!hasData) {
            tbody.innerHTML = '';
            return;
        }

        let html = '';
        for (const [address, info] of Object.entries(this.monitorData)) {
            const current = this.currentValues[address]?.value;
            const currentDisplay = typeof current === 'number' ? current.toFixed(3) : '--';
            const minDisplay = info.min !== null ? info.min.toFixed(3) : '--';
            const maxDisplay = info.max !== null ? info.max.toFixed(3) : '--';

            html += `
                <tr data-address="${address}">
                    <td class="color-cell">
                        <div class="color-dot" style="background: ${info.color}"></div>
                    </td>
                    <td>${info.name}</td>
                    <td class="value-cell">${currentDisplay}</td>
                    <td class="min-cell">${minDisplay}</td>
                    <td class="max-cell">${maxDisplay}</td>
                    <td class="unit-cell">${info.unit}</td>
                    <td class="actions-cell">
                        <button class="btn-remove" data-address="${address}" title="Remove">&#10005;</button>
                    </td>
                </tr>
            `;
        }

        tbody.innerHTML = html;

        // Attach remove handlers
        tbody.querySelectorAll('.btn-remove').forEach(btn => {
            btn.addEventListener('click', () => {
                this.removeFromMonitor(parseInt(btn.dataset.address));
            });
        });
    }

    updateMonitorLegend() {
        const legend = document.getElementById('monitorLegend');
        if (!legend) return;

        let html = '';
        for (const [address, info] of Object.entries(this.monitorData)) {
            html += `
                <div class="legend-item">
                    <span class="legend-color" style="background: ${info.color}"></span>
                    <span>${info.name}</span>
                </div>
            `;
        }

        legend.innerHTML = html;
    }

    updateMonitorData() {
        if (this.monitorPaused) return;

        const now = Date.now();

        for (const [address, info] of Object.entries(this.monitorData)) {
            const value = this.currentValues[address]?.value;

            if (typeof value === 'number') {
                // Add data point
                info.data.push({ time: now, value: value });

                // Keep max points
                if (info.data.length > this.monitorMaxPoints) {
                    info.data.shift();
                }

                // Update min/max
                if (info.min === null || value < info.min) {
                    info.min = value;
                }
                if (info.max === null || value > info.max) {
                    info.max = value;
                }
            }
        }
    }

    drawMonitorGraph() {
        if (!this.monitorCtx || !this.monitorCanvas) return;

        const ctx = this.monitorCtx;
        const width = this.monitorCanvas.width;
        const height = this.monitorCanvas.height;

        // Clear canvas
        ctx.fillStyle = '#1a1a1e';
        ctx.fillRect(0, 0, width, height);

        const hasData = Object.keys(this.monitorData).length > 0;
        if (!hasData) return;

        // Collect all data points
        let allPoints = [];
        for (const info of Object.values(this.monitorData)) {
            if (info.data.length === 0) continue;
            allPoints = allPoints.concat(info.data);
        }

        if (allPoints.length === 0) return;

        // Calculate ranges
        const times = allPoints.map(p => p.time);
        const values = allPoints.map(p => p.value);

        const minTime = Math.min(...times);
        const maxTime = Math.max(...times);
        const minValue = Math.min(...values);
        const maxValue = Math.max(...values);

        const timeRange = maxTime - minTime || 1000; // At least 1 second
        const valueRange = maxValue - minValue || 1;
        const padding = valueRange * 0.1;

        const yMin = minValue - padding;
        const yMax = maxValue + padding;
        const yRange = yMax - yMin;

        // Graph area
        const marginLeft = 70;
        const marginRight = 20;
        const marginTop = 20;
        const marginBottom = 35;

        const graphWidth = width - marginLeft - marginRight;
        const graphHeight = height - marginTop - marginBottom;

        if (graphWidth <= 0 || graphHeight <= 0) return;

        // Helper: convert value to Y pixel (higher value = lower Y in canvas)
        const valueToY = (val) => {
            const normalized = (val - yMin) / yRange; // 0 to 1
            return marginTop + graphHeight * (1 - normalized); // Flip for canvas
        };

        // Helper: convert time to X pixel (with zoom and pan)
        const timeToX = (t) => {
            const normalized = (t - minTime) / timeRange;
            const baseX = marginLeft + graphWidth * normalized;
            // Apply zoom and pan
            const centerX = marginLeft + graphWidth / 2;
            return centerX + (baseX - centerX) * this.monitorZoom + this.monitorPanX;
        };

        // Draw grid lines
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
        ctx.lineWidth = 1;

        // Horizontal grid lines (5 lines)
        for (let i = 0; i <= 4; i++) {
            const val = yMin + (yRange / 4) * i;
            const y = valueToY(val);

            ctx.beginPath();
            ctx.moveTo(marginLeft, y);
            ctx.lineTo(width - marginRight, y);
            ctx.stroke();

            // Y-axis labels
            ctx.fillStyle = 'rgba(255, 255, 255, 0.5)';
            ctx.font = '11px Inter, sans-serif';
            ctx.textAlign = 'right';
            ctx.fillText(val.toFixed(1), marginLeft - 8, y + 4);
        }

        // Vertical grid lines (zoomed)
        for (let i = 0; i <= 4; i++) {
            const t = minTime + (timeRange / 4) * i;
            const x = timeToX(t);
            if (x >= marginLeft && x <= width - marginRight) {
                ctx.beginPath();
                ctx.moveTo(x, marginTop);
                ctx.lineTo(x, marginTop + graphHeight);
                ctx.stroke();
            }
        }

        // Set clipping region for graph area
        ctx.save();
        ctx.beginPath();
        ctx.rect(marginLeft, marginTop, graphWidth, graphHeight);
        ctx.clip();

        // Draw lines for each variable
        for (const info of Object.values(this.monitorData)) {
            if (info.data.length < 2) continue;

            // Sort data by time to ensure correct line drawing
            const sortedData = [...info.data].sort((a, b) => a.time - b.time);

            ctx.strokeStyle = info.color;
            ctx.lineWidth = 2;
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';
            ctx.beginPath();

            sortedData.forEach((point, idx) => {
                const x = timeToX(point.time);
                const y = valueToY(point.value);

                if (idx === 0) {
                    ctx.moveTo(x, y);
                } else {
                    ctx.lineTo(x, y);
                }
            });

            ctx.stroke();
        }

        ctx.restore(); // Remove clipping

        // Draw time axis labels
        ctx.fillStyle = 'rgba(255, 255, 255, 0.5)';
        ctx.font = '11px Inter, sans-serif';
        ctx.textAlign = 'center';

        for (let i = 0; i <= 4; i++) {
            const t = minTime + (timeRange / 4) * i;
            const x = timeToX(t);
            if (x >= marginLeft - 30 && x <= width - marginRight + 30) {
                const timeStr = new Date(t).toLocaleTimeString();
                ctx.fillText(timeStr, Math.max(marginLeft, Math.min(width - marginRight, x)), height - 12);
            }
        }

        // Draw border around graph area
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.1)';
        ctx.lineWidth = 1;
        ctx.strokeRect(marginLeft, marginTop, graphWidth, graphHeight);

        // Show zoom indicator if zoomed
        if (this.monitorZoom > 1) {
            ctx.fillStyle = 'rgba(255, 255, 255, 0.7)';
            ctx.font = '12px Inter, sans-serif';
            ctx.textAlign = 'right';
            ctx.fillText(`${this.monitorZoom.toFixed(1)}x (scroll to zoom, drag to pan, dblclick to reset)`, width - marginRight, marginTop - 5);
        }

        // Store graph params for tooltip
        this.monitorGraphParams = {
            marginLeft, marginRight, marginTop, marginBottom,
            graphWidth, graphHeight,
            minTime, maxTime, timeRange,
            yMin, yMax, yRange,
            width, height
        };

        // Draw crosshair overlay if active
        this.drawCrosshairOverlay();
    }

    // Update monitor when WebSocket data arrives
    onMonitorDataUpdate() {
        if (this.currentPage !== 'monitor') return;
        if (Object.keys(this.monitorData).length === 0) return;

        this.updateMonitorData();
        this.updateMonitorTableValues();

        // Use RAF to prevent too many redraws
        if (!this._monitorRAFPending) {
            this._monitorRAFPending = true;
            requestAnimationFrame(() => {
                this.drawMonitorGraph();
                this._monitorRAFPending = false;
            });
        }
    }

    updateMonitorTableValues() {
        const tbody = document.getElementById('monitorTableBody');
        if (!tbody) return;

        for (const [address, info] of Object.entries(this.monitorData)) {
            const tr = tbody.querySelector(`tr[data-address="${address}"]`);
            if (!tr) continue;

            const current = this.currentValues[address]?.value;
            const currentCell = tr.querySelector('.value-cell');
            const minCell = tr.querySelector('.min-cell');
            const maxCell = tr.querySelector('.max-cell');

            if (currentCell && typeof current === 'number') {
                currentCell.textContent = current.toFixed(3);
            }
            if (minCell && info.min !== null) {
                minCell.textContent = info.min.toFixed(3);
            }
            if (maxCell && info.max !== null) {
                maxCell.textContent = info.max.toFixed(3);
            }
        }
    }
}

// Initialize app
document.addEventListener('DOMContentLoaded', () => {
    window.app = new JanitzaMonitor();
});
