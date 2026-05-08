// Research Sandbox WebUI — service-aware browser front for project supervisors.
// All persistent state lives in browser localStorage, encrypted with a
// PBKDF2-derived AES-GCM key. The decrypted vault and derived key live in
// JS memory only while unlocked; both are dropped on lock or refresh.
//
// Layout: vertical project rail × horizontal service tab strip. State
// expands ADS's single activeTab into (activeProject, activeService); the
// service tab strip is recomputed on each project switch from the
// intersection of /services (registry) and /services/<project> (enabled set).

const VAULT_KEY = "rs-webui-vault";
const THEME_KEY = "rs-webui-theme";
const RAIL_PINNED_KEY = "rs-webui-rail-pinned";
const PBKDF2_ITERATIONS = 600000;
const PROBE_INTERVAL_MS = 15000;

// ---- themes -----------------------------------------------------------------

const THEMES = {
    dark: {
        label: "Dark",
        xterm: {
            background: "#000000", foreground: "#e0e0e0",
            cursor: "#e0e0e0", cursorAccent: "#000000",
            selectionBackground: "rgba(255,255,255,0.25)",
            black: "#000000", red: "#cc0403", green: "#19cb00", yellow: "#cecb00",
            blue: "#0d73cc", magenta: "#cb1ed1", cyan: "#0dcdcd", white: "#dddddd",
            brightBlack: "#767676", brightRed: "#f2201f", brightGreen: "#23fd00",
            brightYellow: "#fffd00", brightBlue: "#1a8fff", brightMagenta: "#fd28ff",
            brightCyan: "#14ffff", brightWhite: "#ffffff",
        },
        css: {
            "--bg-base": "#1e1e1e", "--bg-card": "#2a2a2a", "--bg-active": "#1e1e1e",
            "--bg-input": "#1a1a1a", "--bg-input-focus-border": "#6c9",
            "--fg-base": "#e0e0e0", "--fg-muted": "#aaa", "--fg-faint": "#777",
            "--fg-accent": "#6c9",
            "--border": "#444", "--border-strong": "#555",
            "--btn-bg": "#4a7c4e", "--btn-bg-hover": "#5a8c5e",
            "--btn-secondary-bg": "#444", "--btn-secondary-bg-hover": "#555",
            "--btn-danger-bg": "#7c3a3a", "--btn-danger-bg-hover": "#8c4a4a",
            "--error-fg": "#e88",
            "--status-up": "#6c6", "--status-down": "#555", "--status-error": "#e66",
            "--terminal-bg": "#000", "--terminal-fg": "#e0e0e0",
        },
    },
    light: {
        label: "Light",
        xterm: {
            background: "#ffffff", foreground: "#2a2a2a",
            cursor: "#2a2a2a", cursorAccent: "#ffffff",
            selectionBackground: "rgba(0,0,0,0.18)",
            black: "#2a2a2a", red: "#c91b00", green: "#00c200", yellow: "#c7c400",
            blue: "#0225c7", magenta: "#ca30c7", cyan: "#00c5c7", white: "#c7c7c7",
            brightBlack: "#676767", brightRed: "#ff6e67", brightGreen: "#5ffa68",
            brightYellow: "#fffc67", brightBlue: "#6871ff", brightMagenta: "#ff77ff",
            brightCyan: "#60fdff", brightWhite: "#ffffff",
        },
        css: {
            "--bg-base": "#fafafa", "--bg-card": "#ececec", "--bg-active": "#ffffff",
            "--bg-input": "#ffffff", "--bg-input-focus-border": "#3a8a3a",
            "--fg-base": "#1f1f1f", "--fg-muted": "#555", "--fg-faint": "#888",
            "--fg-accent": "#3a8a3a",
            "--border": "#d0d0d0", "--border-strong": "#bbb",
            "--btn-bg": "#3a8a3a", "--btn-bg-hover": "#4a9a4a",
            "--btn-secondary-bg": "#d0d0d0", "--btn-secondary-bg-hover": "#bbb",
            "--btn-danger-bg": "#b03a3a", "--btn-danger-bg-hover": "#c04a4a",
            "--error-fg": "#a33",
            "--status-up": "#3a8a3a", "--status-down": "#aaa", "--status-error": "#c04040",
            "--terminal-bg": "#ffffff", "--terminal-fg": "#2a2a2a",
        },
    },
    "solarized-dark": {
        label: "Solarized Dark",
        xterm: {
            background: "#002b36", foreground: "#839496",
            cursor: "#93a1a1", cursorAccent: "#002b36",
            selectionBackground: "rgba(147,161,161,0.25)",
            black: "#073642", red: "#dc322f", green: "#859900", yellow: "#b58900",
            blue: "#268bd2", magenta: "#d33682", cyan: "#2aa198", white: "#eee8d5",
            brightBlack: "#002b36", brightRed: "#cb4b16", brightGreen: "#586e75",
            brightYellow: "#657b83", brightBlue: "#839496", brightMagenta: "#6c71c4",
            brightCyan: "#93a1a1", brightWhite: "#fdf6e3",
        },
        css: {
            "--bg-base": "#002b36", "--bg-card": "#073642", "--bg-active": "#002b36",
            "--bg-input": "#001f27", "--bg-input-focus-border": "#268bd2",
            "--fg-base": "#93a1a1", "--fg-muted": "#839496", "--fg-faint": "#657b83",
            "--fg-accent": "#2aa198",
            "--border": "#0a4452", "--border-strong": "#0e5a6f",
            "--btn-bg": "#268bd2", "--btn-bg-hover": "#3a9be0",
            "--btn-secondary-bg": "#0a4452", "--btn-secondary-bg-hover": "#0e5a6f",
            "--btn-danger-bg": "#dc322f", "--btn-danger-bg-hover": "#ec4240",
            "--error-fg": "#dc322f",
            "--status-up": "#859900", "--status-down": "#586e75", "--status-error": "#dc322f",
            "--terminal-bg": "#002b36", "--terminal-fg": "#839496",
        },
    },
    dracula: {
        label: "Dracula",
        xterm: {
            background: "#282a36", foreground: "#f8f8f2",
            cursor: "#f8f8f2", cursorAccent: "#282a36",
            selectionBackground: "rgba(68,71,90,0.7)",
            black: "#21222c", red: "#ff5555", green: "#50fa7b", yellow: "#f1fa8c",
            blue: "#bd93f9", magenta: "#ff79c6", cyan: "#8be9fd", white: "#f8f8f2",
            brightBlack: "#6272a4", brightRed: "#ff6e6e", brightGreen: "#69ff94",
            brightYellow: "#ffffa5", brightBlue: "#d6acff", brightMagenta: "#ff92df",
            brightCyan: "#a4ffff", brightWhite: "#ffffff",
        },
        css: {
            "--bg-base": "#282a36", "--bg-card": "#343746", "--bg-active": "#282a36",
            "--bg-input": "#21222c", "--bg-input-focus-border": "#bd93f9",
            "--fg-base": "#f8f8f2", "--fg-muted": "#bdbdc8", "--fg-faint": "#6272a4",
            "--fg-accent": "#bd93f9",
            "--border": "#44475a", "--border-strong": "#5c5f74",
            "--btn-bg": "#50fa7b", "--btn-bg-hover": "#69ff94",
            "--btn-secondary-bg": "#44475a", "--btn-secondary-bg-hover": "#5c5f74",
            "--btn-danger-bg": "#ff5555", "--btn-danger-bg-hover": "#ff6e6e",
            "--error-fg": "#ff5555",
            "--status-up": "#50fa7b", "--status-down": "#6272a4", "--status-error": "#ff5555",
            "--terminal-bg": "#282a36", "--terminal-fg": "#f8f8f2",
        },
    },
    nord: {
        label: "Nord",
        xterm: {
            background: "#2e3440", foreground: "#d8dee9",
            cursor: "#d8dee9", cursorAccent: "#2e3440",
            selectionBackground: "rgba(76,86,106,0.7)",
            black: "#3b4252", red: "#bf616a", green: "#a3be8c", yellow: "#ebcb8b",
            blue: "#81a1c1", magenta: "#b48ead", cyan: "#88c0d0", white: "#e5e9f0",
            brightBlack: "#4c566a", brightRed: "#bf616a", brightGreen: "#a3be8c",
            brightYellow: "#ebcb8b", brightBlue: "#81a1c1", brightMagenta: "#b48ead",
            brightCyan: "#8fbcbb", brightWhite: "#eceff4",
        },
        css: {
            "--bg-base": "#2e3440", "--bg-card": "#3b4252", "--bg-active": "#2e3440",
            "--bg-input": "#272c36", "--bg-input-focus-border": "#88c0d0",
            "--fg-base": "#d8dee9", "--fg-muted": "#a8b2c1", "--fg-faint": "#7884a0",
            "--fg-accent": "#88c0d0",
            "--border": "#434c5e", "--border-strong": "#4c566a",
            "--btn-bg": "#5e81ac", "--btn-bg-hover": "#7592b8",
            "--btn-secondary-bg": "#434c5e", "--btn-secondary-bg-hover": "#4c566a",
            "--btn-danger-bg": "#bf616a", "--btn-danger-bg-hover": "#cf717a",
            "--error-fg": "#bf616a",
            "--status-up": "#a3be8c", "--status-down": "#4c566a", "--status-error": "#bf616a",
            "--terminal-bg": "#2e3440", "--terminal-fg": "#d8dee9",
        },
    },
};

const DEFAULT_THEME = "dark";

function loadStoredTheme() {
    const id = localStorage.getItem(THEME_KEY);
    return THEMES[id] ? id : DEFAULT_THEME;
}

function applyTheme(id) {
    const theme = THEMES[id] || THEMES[DEFAULT_THEME];
    for (const [k, v] of Object.entries(theme.css)) {
        document.documentElement.style.setProperty(k, v);
    }
    for (const t of Object.values(state.terminals)) {
        if (t.term) t.term.options.theme = theme.xterm;
    }
    state.theme = id;
    localStorage.setItem(THEME_KEY, id);
}

function currentXtermTheme() {
    return THEMES[state.theme || DEFAULT_THEME].xterm;
}

const state = {
    derivedKey: null,        // CryptoKey | null
    salt: null,              // Uint8Array | null
    vault: null,             // { version, projects, settings } | null
    activeProject: null,     // string | null
    activeService: null,     // string | null
    serviceRegistry: null,   // { [serviceId]: spec } from /services
    projectServices: {},     // { [projectName]: { [serviceId]: spec } }
    terminals: {},           // "${project}:${service}" -> { term, fitAddon, ws, container, project, service }
    probeTimer: null,
    theme: null,
    railPinned: false,       // persisted: keep rail in flex flow (push layout)
    railExpanded: false,     // in-memory: rail visible (overlay when unpinned)
};

// ---- utilities -------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
const ub64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
const tkey = (project, service) => `${project}:${service}`;

function el(tag, attrs = {}, children = []) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") e.className = v;
        else if (k === "onclick") e.onclick = v;
        else if (k === "oninput") e.oninput = v;
        else if (k === "onkeydown") e.onkeydown = v;
        else e.setAttribute(k, v);
    }
    for (const c of children) {
        if (c == null) continue;
        e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return e;
}

function clearBody() {
    document.body.innerHTML = "";
}

// ---- crypto ----------------------------------------------------------------

async function deriveKey(password, salt) {
    const enc = new TextEncoder();
    const baseKey = await crypto.subtle.importKey(
        "raw", enc.encode(password), "PBKDF2", false, ["deriveKey"],
    );
    return crypto.subtle.deriveKey(
        { name: "PBKDF2", salt, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
        baseKey,
        { name: "AES-GCM", length: 256 },
        false,
        ["encrypt", "decrypt"],
    );
}

async function encryptVault(key, vault) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const enc = new TextEncoder();
    const ciphertext = await crypto.subtle.encrypt(
        { name: "AES-GCM", iv }, key, enc.encode(JSON.stringify(vault)),
    );
    return { iv: b64(iv), ciphertext: b64(ciphertext) };
}

async function decryptVault(key, ivB64, ctB64) {
    const dec = new TextDecoder();
    const plaintext = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: ub64(ivB64) }, key, ub64(ctB64),
    );
    return JSON.parse(dec.decode(plaintext));
}

// ---- vault persistence -----------------------------------------------------

function loadStored() {
    const raw = localStorage.getItem(VAULT_KEY);
    return raw ? JSON.parse(raw) : null;
}

function saveStored(stored) {
    localStorage.setItem(VAULT_KEY, JSON.stringify(stored));
}

async function persistVault() {
    const enc = await encryptVault(state.derivedKey, state.vault);
    saveStored({ salt: b64(state.salt), ...enc });
}

// ---- screens ---------------------------------------------------------------

function renderSetup() {
    clearBody();
    const pw1 = el("input", { type: "password", autocomplete: "new-password" });
    const pw2 = el("input", { type: "password", autocomplete: "new-password" });
    const errEl = el("div", { class: "error" });

    const submit = el("button", { class: "btn" }, ["Create vault"]);
    submit.onclick = async () => {
        if (pw1.value.length < 8) {
            errEl.textContent = "Password must be at least 8 characters.";
            return;
        }
        if (pw1.value !== pw2.value) {
            errEl.textContent = "Passwords do not match.";
            return;
        }
        try {
            const salt = crypto.getRandomValues(new Uint8Array(16));
            state.derivedKey = await deriveKey(pw1.value, salt);
            state.salt = salt;
            state.vault = { version: 1, projects: [], settings: {} };
            await persistVault();
            await renderDashboard();
        } catch (e) {
            errEl.textContent = "Setup failed: " + e.message;
        }
    };

    const card = el("div", { class: "card" }, [
        el("h2", {}, ["Set master password"]),
        el("p", {}, [
            "This password encrypts your saved supervisor credentials. There is no recovery — if you forget it, you'll need to re-add each project.",
        ]),
        el("div", { class: "field" }, [el("label", {}, ["Master password"]), pw1]),
        el("div", { class: "field" }, [el("label", {}, ["Confirm password"]), pw2]),
        el("div", { class: "btn-row" }, [submit]),
        errEl,
    ]);
    document.body.appendChild(el("div", { id: "app" }, [
        el("div", { class: "center-screen" }, [card]),
    ]));
    setTimeout(() => pw1.focus(), 50);
}

function renderUnlock() {
    clearBody();
    const pw = el("input", { type: "password", autocomplete: "current-password" });
    const errEl = el("div", { class: "error" });

    const submit = el("button", { class: "btn" }, ["Unlock"]);
    submit.onclick = async () => {
        try {
            const stored = loadStored();
            const salt = ub64(stored.salt);
            const key = await deriveKey(pw.value, salt);
            const vault = await decryptVault(key, stored.iv, stored.ciphertext);
            state.derivedKey = key;
            state.salt = salt;
            state.vault = vault;
            await renderDashboard();
        } catch (e) {
            errEl.textContent = "Wrong password.";
        }
    };
    pw.onkeydown = (e) => { if (e.key === "Enter") submit.click(); };

    const card = el("div", { class: "card" }, [
        el("h2", {}, ["Unlock vault"]),
        el("div", { class: "field" }, [el("label", {}, ["Master password"]), pw]),
        el("div", { class: "btn-row" }, [submit]),
        errEl,
    ]);
    document.body.appendChild(el("div", { id: "app" }, [
        el("div", { class: "center-screen" }, [card]),
    ]));
    setTimeout(() => pw.focus(), 50);
}

async function fetchServiceRegistry() {
    if (state.serviceRegistry) return state.serviceRegistry;
    try {
        const res = await fetch("/services");
        state.serviceRegistry = await res.json();
    } catch (e) {
        state.serviceRegistry = {};
    }
    return state.serviceRegistry;
}

async function fetchProjectServices(projectName) {
    if (state.projectServices[projectName]) return state.projectServices[projectName];
    try {
        const res = await fetch(`/services/${encodeURIComponent(projectName)}`);
        state.projectServices[projectName] = await res.json();
    } catch (e) {
        state.projectServices[projectName] = {};
    }
    return state.projectServices[projectName];
}

async function renderDashboard() {
    clearBody();
    await fetchServiceRegistry();

    const rail = makeProjectRail();
    const tabStrip = el("div", { class: "service-tabs", id: "service-tabs" }, [
        makeProjectsTab(),
    ]);
    const termArea = el("div", { class: "terminal-area", id: "terminal-area" });
    const welcomeText = state.vault.projects.length === 0
        ? "No projects yet. Click the Projects tab to add one."
        : "Click the Projects tab to attach.";
    termArea.appendChild(el("div", { class: "welcome", id: "welcome" }, [welcomeText]));

    const main = el("div", { class: "main-area" }, [tabStrip, termArea]);
    const dashboard = el("div", { class: "dashboard" }, [rail, main]);
    document.body.appendChild(el("div", { id: "app" }, [dashboard]));

    applyRailState();
    schedulePolling();

    if (state.activeProject) {
        await activateProject(state.activeProject);
    }
}

// ---- rail expand / pin -----------------------------------------------------

function loadRailPinned() {
    return localStorage.getItem(RAIL_PINNED_KEY) === "1";
}

function makeProjectsTab() {
    const expanded = state.railPinned || state.railExpanded;
    const chev = el("span", {
        class: "projects-chevron",
        id: "projects-chevron",
    }, [expanded ? "◀" : "▶"]);
    const tab = el("div", {
        class: "tab projects-tab",
        title: expanded ? "Hide projects" : "Show projects",
    }, [chev, el("span", {}, ["Projects"])]);
    tab.onclick = (ev) => { ev.stopPropagation(); toggleRailExpanded(); };
    return tab;
}

function makePinButton() {
    const btn = el("button", { class: "pin-btn", title: "Pin sidebar" });
    btn.innerHTML = '<svg viewBox="0 0 16 16" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M9.828.722a.5.5 0 0 1 .354.146l4.95 4.95a.5.5 0 0 1 0 .707c-.48.48-1.072.588-1.503.588-.177 0-.335-.018-.46-.039l-3.134 3.134a5.927 5.927 0 0 1 .16 1.013c.046.702-.032 1.687-.72 2.375a.5.5 0 0 1-.707 0l-2.829-2.828-3.182 3.182c-.195.195-1.219.902-1.414.707-.195-.195.512-1.22.707-1.414l3.182-3.182-2.828-2.829a.5.5 0 0 1 0-.707c.688-.688 1.673-.767 2.375-.72a5.922 5.922 0 0 1 1.013.16l3.134-3.133a2.772 2.772 0 0 1-.04-.461c0-.43.108-1.022.589-1.503A.5.5 0 0 1 9.828.722z"/></svg>';
    btn.onclick = (ev) => { ev.stopPropagation(); togglePinned(); };
    return btn;
}

function applyRailState() {
    const dashboard = document.querySelector(".dashboard");
    if (!dashboard) return;
    const expanded = state.railPinned || state.railExpanded;
    dashboard.classList.toggle("pinned", state.railPinned);
    dashboard.classList.toggle("expanded", expanded);

    const pinBtn = dashboard.querySelector(".rail-header .pin-btn");
    if (pinBtn) {
        pinBtn.classList.toggle("pinned", state.railPinned);
        pinBtn.title = state.railPinned ? "Unpin sidebar" : "Pin sidebar";
    }
    const chevron = document.getElementById("projects-chevron");
    if (chevron) chevron.textContent = expanded ? "◀" : "▶";
    const projectsTab = dashboard.querySelector(".projects-tab");
    if (projectsTab) projectsTab.title = expanded ? "Hide projects" : "Show projects";

    // Layout shift only happens when pinned toggles; refit the active terminal.
    const t = activeTerminal();
    if (t && t.fitAddon) {
        setTimeout(() => { try { t.fitAddon.fit(); } catch (_) {} }, 0);
    }
}

function toggleRailExpanded() {
    // Tab handle is the universal show/hide control. If pinned, collapsing
    // also unpins — keeping pinned-but-collapsed is incoherent.
    if (state.railPinned) {
        state.railPinned = false;
        localStorage.setItem(RAIL_PINNED_KEY, "0");
        state.railExpanded = false;
    } else {
        state.railExpanded = !state.railExpanded;
    }
    applyRailState();
}

function togglePinned() {
    state.railPinned = !state.railPinned;
    localStorage.setItem(RAIL_PINNED_KEY, state.railPinned ? "1" : "0");
    // Pinning auto-expands; unpinning auto-collapses so the user
    // recovers horizontal space in a single click.
    state.railExpanded = state.railPinned;
    applyRailState();
}

// ---- project rail ----------------------------------------------------------

function makeProjectRail() {
    const rail = el("aside", { class: "project-rail" });
    const header = el("div", { class: "rail-header" }, [
        el("span", {}, ["Projects"]),
        makePinButton(),
    ]);
    rail.appendChild(header);
    for (const p of state.vault.projects) {
        rail.appendChild(makeProjectRow(p));
    }
    rail.appendChild(el("button", {
        class: "add-project",
        onclick: openAddProjectModal,
    }, ["+ Add project"]));
    rail.appendChild(el("div", { class: "rail-spacer" }));

    const footer = el("div", { class: "rail-footer" }, [
        makeThemeSelector(),
        el("button", { class: "lock-btn", onclick: lockVault }, ["Lock vault"]),
    ]);
    rail.appendChild(footer);
    return rail;
}

function makeProjectRow(project) {
    const dot = el("span", { class: "status-dot" });
    const name = el("span", { class: "name" }, [project.name]);
    const closeX = el("span", { class: "close-x", title: "Remove project" }, ["×"]);
    closeX.onclick = (ev) => {
        ev.stopPropagation();
        if (confirm(`Remove project "${project.name}"? (Supervisor and its container are not affected.)`)) {
            removeProject(project.name);
        }
    };
    const row = el("div", {
        class: "project",
        "data-name": project.name,
        onclick: () => activateProject(project.name),
    }, [dot, name, closeX]);
    return row;
}

function schedulePolling() {
    if (state.probeTimer) clearInterval(state.probeTimer);
    const probeAll = () => {
        for (const p of state.vault.projects) probeProject(p);
    };
    probeAll();
    state.probeTimer = setInterval(probeAll, PROBE_INTERVAL_MS);
}

async function probeProject(project) {
    try {
        const url = `/probe?host=${encodeURIComponent(project.host)}&port=${project.port}`;
        const res = await fetch(url);
        const data = await res.json();
        const row = document.querySelector(`.project[data-name="${CSS.escape(project.name)}"]`);
        if (!row) return;
        row.classList.toggle("up", !!data.up);
        row.classList.toggle("down", !data.up);
    } catch (_) {
        // ignore probe errors
    }
}

function lockVault() {
    if (state.probeTimer) { clearInterval(state.probeTimer); state.probeTimer = null; }
    for (const t of Object.values(state.terminals)) {
        try { if (t.ws) t.ws.close(); } catch (_) {}
        try { if (t.term) t.term.dispose(); } catch (_) {}
    }
    state.derivedKey = null;
    state.vault = null;
    state.salt = null;
    state.terminals = {};
    state.activeProject = null;
    state.activeService = null;
    state.projectServices = {};
    state.railExpanded = state.railPinned;
    renderUnlock();
}

// ---- search bar ------------------------------------------------------------

let searchBarEl = null;
let searchInputEl = null;

function ensureSearchBar() {
    if (searchBarEl) return searchBarEl;
    const input = el("input", { type: "text", placeholder: "Search…", spellcheck: "false" });
    const prev = el("button", { class: "search-btn", title: "Previous (Shift+Enter)" }, ["↑"]);
    const next = el("button", { class: "search-btn", title: "Next (Enter)" }, ["↓"]);
    const close = el("button", { class: "search-btn", title: "Close (Esc)" }, ["×"]);

    const bar = el("div", { class: "search-bar hidden" }, [input, prev, next, close]);

    const find = (forward) => {
        const t = activeTerminal();
        if (!t || !t.searchAddon || !input.value) return;
        const opts = { regex: false, wholeWord: false, caseSensitive: false };
        if (forward) t.searchAddon.findNext(input.value, opts);
        else t.searchAddon.findPrevious(input.value, opts);
    };
    input.oninput = () => find(true);
    input.onkeydown = (e) => {
        if (e.key === "Enter") { find(!e.shiftKey); e.preventDefault(); }
        else if (e.key === "Escape") { closeSearchBar(); e.preventDefault(); }
    };
    next.onclick = () => find(true);
    prev.onclick = () => find(false);
    close.onclick = closeSearchBar;

    searchBarEl = bar;
    searchInputEl = input;
    return bar;
}

function activeTerminal() {
    if (!state.activeProject || !state.activeService) return null;
    return state.terminals[tkey(state.activeProject, state.activeService)];
}

function openSearchBar() {
    const bar = ensureSearchBar();
    const termArea = document.getElementById("terminal-area");
    if (termArea && bar.parentElement !== termArea) termArea.appendChild(bar);
    bar.classList.remove("hidden");
    searchInputEl.focus();
    searchInputEl.select();
}

function closeSearchBar() {
    if (searchBarEl) searchBarEl.classList.add("hidden");
    const t = activeTerminal();
    if (t && t.term) t.term.focus();
}

function makeThemeSelector() {
    const sel = document.createElement("select");
    sel.className = "theme-select";
    sel.title = "Theme";
    for (const [id, t] of Object.entries(THEMES)) {
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = t.label;
        if (id === state.theme) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.onchange = () => applyTheme(sel.value);
    return sel;
}

// ---- add / remove project --------------------------------------------------

function openAddProjectModal() {
    const importTa = el("textarea", { placeholder: "Paste `research webui import <project>` output (optional)" });
    const nameI = el("input", { type: "text" });
    const hostI = el("input", { type: "text" });
    const portI = el("input", { type: "number", min: "1", max: "65535", value: "22" });
    const userI = el("input", { type: "text", value: "research" });
    const passI = el("input", { type: "password", autocomplete: "new-password" });
    const errEl = el("div", { class: "error" });

    importTa.oninput = () => {
        const s = importTa.value.trim();
        if (!s) return;
        try {
            const decoded = JSON.parse(atob(s));
            if (decoded.name) nameI.value = decoded.name;
            if (decoded.host) hostI.value = decoded.host;
            if (decoded.port) portI.value = decoded.port;
            if (decoded.username) userI.value = decoded.username;
            if (decoded.password) passI.value = decoded.password;
        } catch (_) { /* ignore non-import-string content */ }
    };

    const backdrop = el("div", { class: "modal-backdrop" });
    const cancel = el("button", { class: "btn btn-secondary" }, ["Cancel"]);
    cancel.onclick = () => backdrop.remove();

    const save = el("button", { class: "btn" }, ["Add"]);
    save.onclick = async () => {
        const name = nameI.value.trim();
        const host = hostI.value.trim();
        const port = parseInt(portI.value, 10);
        const username = userI.value.trim() || "research";
        const password = passI.value;
        if (!name || !host || !port || !password) {
            errEl.textContent = "Name, host, port, and password are required.";
            return;
        }
        if (state.vault.projects.some((p) => p.name === name)) {
            errEl.textContent = "A project with that name already exists.";
            return;
        }
        state.vault.projects.push({ name, host, port, username, password });
        try {
            await persistVault();
            backdrop.remove();
            await renderDashboard();
        } catch (e) {
            errEl.textContent = "Save failed: " + e.message;
        }
    };

    const card = el("div", { class: "card" }, [
        el("h2", {}, ["Add project"]),
        el("div", { class: "field" }, [
            el("label", {}, ["Import string (optional)"]),
            importTa,
            el("div", { class: "hint" }, ["Paste the base64 string from `research webui import <project>` to auto-fill the fields."]),
        ]),
        el("div", { class: "field" }, [el("label", {}, ["Project name"]), nameI]),
        el("div", { class: "field" }, [el("label", {}, ["Host"]), hostI]),
        el("div", { class: "field" }, [el("label", {}, ["SSH port"]), portI]),
        el("div", { class: "field" }, [el("label", {}, ["Username"]), userI]),
        el("div", { class: "field" }, [el("label", {}, ["Password"]), passI]),
        el("div", { class: "btn-row" }, [cancel, save]),
        errEl,
    ]);
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
}

async function removeProject(name) {
    for (const k of Object.keys(state.terminals)) {
        if (k.startsWith(`${name}:`)) {
            const t = state.terminals[k];
            try { if (t.ws) t.ws.close(); } catch (_) {}
            try { if (t.term) t.term.dispose(); } catch (_) {}
            delete state.terminals[k];
        }
    }
    state.vault.projects = state.vault.projects.filter((p) => p.name !== name);
    delete state.projectServices[name];
    if (state.activeProject === name) {
        state.activeProject = null;
        state.activeService = null;
    }
    await persistVault();
    await renderDashboard();
}

// ---- project / service activation ------------------------------------------

async function activateProject(name) {
    document.querySelectorAll(".project-rail .project").forEach((r) => r.classList.remove("active"));
    const row = document.querySelector(`.project[data-name="${CSS.escape(name)}"]`);
    if (row) row.classList.add("active");

    // Unpinned + expanded means "I just opened the rail to switch projects" —
    // collapse it again now that the switch is done so the user gets their
    // horizontal space back. Pinned rail stays put.
    if (!state.railPinned && state.railExpanded) {
        state.railExpanded = false;
        applyRailState();
    }

    state.activeProject = name;
    const enabled = await fetchProjectServices(name);
    renderServiceTabs(name, enabled);

    // Pick a default service: keep the previous one if it's still in the
    // enabled set, else use the first always-on (xterm), else the first
    // entry. activateService handles re-rendering the content area.
    const ids = Object.keys(enabled);
    if (ids.length === 0) {
        state.activeService = null;
        showWelcome();
        return;
    }
    let next = state.activeService;
    if (!enabled[next]) {
        next = ids.find((id) => enabled[id].always_on) || ids[0];
    }
    activateService(next);
}

function renderServiceTabs(projectName, enabled) {
    const strip = document.getElementById("service-tabs");
    if (!strip) return;
    strip.innerHTML = "";
    strip.appendChild(makeProjectsTab());
    const ids = Object.keys(enabled);
    if (ids.length === 0) {
        strip.appendChild(el("div", { class: "empty" }, [
            "No services enabled for this project.",
        ]));
        return;
    }
    for (const id of ids) {
        const svc = enabled[id];
        const tab = el("div", {
            class: "tab",
            "data-service": id,
            onclick: () => activateService(id),
        }, [svc.label || id]);
        strip.appendChild(tab);
    }
}

function activateService(serviceId) {
    if (!state.activeProject) return;

    document.querySelectorAll(".service-tabs .tab").forEach((t) => t.classList.remove("active"));
    const tabEl = document.querySelector(`.service-tabs .tab[data-service="${CSS.escape(serviceId)}"]`);
    if (tabEl) tabEl.classList.add("active");

    state.activeService = serviceId;

    for (const t of Object.values(state.terminals)) {
        if (t.container) t.container.classList.add("hidden");
    }
    const welcome = document.getElementById("welcome");
    if (welcome) welcome.style.display = "none";

    const project = state.vault.projects.find((p) => p.name === state.activeProject);
    if (!project) return;
    const enabled = state.projectServices[state.activeProject] || {};
    const svc = enabled[serviceId];
    if (!svc) return;

    const key = tkey(state.activeProject, serviceId);
    if (state.terminals[key]) {
        const t = state.terminals[key];
        if (t.container) t.container.classList.remove("hidden");
        if (t.fitAddon) t.fitAddon.fit();
        if (t.term) t.term.focus();
        return;
    }

    if (svc.kind === "ssh") {
        openSshTerminal(project, serviceId, svc);
    } else if (svc.kind === "http") {
        openHttpService(project, serviceId, svc);
    } else {
        const termArea = document.getElementById("terminal-area");
        const placeholder = el("div", { class: "welcome" }, [
            `Unknown service kind: ${svc.kind}`,
        ]);
        termArea.appendChild(placeholder);
    }
}

function showWelcome() {
    for (const t of Object.values(state.terminals)) {
        if (t.container) t.container.classList.add("hidden");
    }
    const strip = document.getElementById("service-tabs");
    if (strip) strip.innerHTML = "";
    const welcome = document.getElementById("welcome");
    if (welcome) welcome.style.display = "";
}

// ---- ssh-kind terminal -----------------------------------------------------

function openSshTerminal(project, serviceId, svc) {
    const container = el("div", { class: "terminal-instance" });
    document.getElementById("terminal-area").appendChild(container);

    const term = new Terminal({
        cursorBlink: true,
        fontFamily: "ui-monospace, Menlo, Consolas, monospace",
        fontSize: 13,
        theme: currentXtermTheme(),
        scrollback: 5000,
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(new WebLinksAddon.WebLinksAddon(
        (event, uri) => window.open(uri, "_blank", "noopener,noreferrer"),
    ));
    const searchAddon = new SearchAddon.SearchAddon();
    term.loadAddon(searchAddon);
    term.open(container);
    try {
        const webgl = new WebglAddon.WebglAddon();
        webgl.onContextLoss(() => webgl.dispose());
        term.loadAddon(webgl);
    } catch (_) {
        // WebGL unavailable; xterm falls back to canvas/DOM renderer.
    }
    fitAddon.fit();
    term.focus();
    term.attachCustomKeyEventHandler((ev) => {
        if (ev.type === "keydown" && ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey
            && (ev.key === "f" || ev.key === "F")) {
            ev.preventDefault();
            openSearchBar();
            return false;
        }
        return true;
    });
    term.onSelectionChange(() => {
        const sel = term.getSelection();
        if (sel) navigator.clipboard.writeText(sel).catch(() => {});
    });
    // OSC 52 — tmux/byobu (with set-clipboard on) emits this after every copy,
    // which lets users copy from inside mouse mode without bypassing it.
    term.parser.registerOscHandler(52, (data) => {
        const semi = data.indexOf(";");
        if (semi < 0) return false;
        const payload = data.slice(semi + 1);
        if (payload === "?") return true; // query — silently ignored for security
        try {
            const text = atob(payload);
            if (text) navigator.clipboard.writeText(text).catch(() => {});
            return true;
        } catch (_) {
            return false;
        }
    });

    const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${wsProto}//${location.host}/ws/${encodeURIComponent(project.name)}/${encodeURIComponent(serviceId)}`;
    const ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";

    const key = tkey(project.name, serviceId);
    state.terminals[key] = {
        term, fitAddon, searchAddon, ws, container, project, service: serviceId,
    };

    ws.onopen = () => {
        ws.send(JSON.stringify({
            type: "connect",
            host: project.host,
            port: project.port || svc.default_port || 22,
            username: project.username || "research",
            password: project.password,
            fingerprint: project.host_key_fingerprint || null,
            rows: term.rows,
            cols: term.cols,
        }));
    };

    ws.onmessage = async (ev) => {
        if (typeof ev.data === "string") {
            let ctrl;
            try { ctrl = JSON.parse(ev.data); } catch (_) { return; }
            await handleControl(project, serviceId, term, ws, ctrl);
        } else {
            term.write(new Uint8Array(ev.data));
        }
    };

    ws.onclose = () => {
        term.writeln("\r\n\x1b[90m[disconnected — click the tab again to reconnect]\x1b[0m");
    };

    term.onData((d) => {
        if (ws.readyState === WebSocket.OPEN) {
            ws.send(new TextEncoder().encode(d));
        }
    });

    term.onResize(({ rows, cols }) => {
        if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "resize", rows, cols }));
        }
    });

    window.addEventListener("resize", () => {
        if (state.activeProject === project.name && state.activeService === serviceId) {
            fitAddon.fit();
        }
    });
}

// ---- http-kind iframe ------------------------------------------------------

async function openHttpService(project, serviceId, svc) {
    const termArea = document.getElementById("terminal-area");
    const container = el("div", { class: "terminal-instance http-instance" });
    const status = el("div", { class: "http-status" }, ["Authenticating…"]);
    container.appendChild(status);
    termArea.appendChild(container);

    const key = tkey(project.name, serviceId);
    state.terminals[key] = {
        kind: "http", container, project, service: serviceId,
    };

    // POST /session/<project> to mint the cookie before mounting the iframe.
    // The fingerprint, if any, is included so the server's TOFU check
    // mirrors the SSH path; on mismatch the user gets the same
    // accept-the-new-key prompt as xterm.
    const credentials = {
        host: project.host,
        port: project.port || 22,
        username: project.username || "research",
        password: project.password,
        fingerprint: project.host_key_fingerprint || null,
    };

    let resp;
    try {
        resp = await fetch(`/session/${encodeURIComponent(project.name)}`, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(credentials),
        });
    } catch (e) {
        status.textContent = `Connect failed: ${e.message || e}`;
        return;
    }

    if (resp.status === 401) {
        let body = {};
        try { body = await resp.json(); } catch (_) {}
        if (body.type === "fingerprint_mismatch") {
            const accept = confirm(
                `Host key for "${project.name}" has CHANGED.\n\n` +
                `Stored: ${project.host_key_fingerprint}\n` +
                `Actual: ${body.actual}\n\n` +
                `Accept the new key?\n\n` +
                `Click OK only if you intentionally recreated the supervisor.`,
            );
            if (accept) {
                project.host_key_fingerprint = body.actual;
                await persistVault();
                // Tear down this placeholder; user clicks the tab again to retry.
                delete state.terminals[key];
                container.remove();
                status.textContent = "Host key updated — click the tab to reconnect.";
                return;
            }
            status.textContent = "Host key mismatch — connection rejected.";
            return;
        }
        if (body.type === "auth_failed") {
            status.textContent = "Auth failed — check the saved password.";
            return;
        }
        status.textContent = `Auth error (${resp.status}).`;
        return;
    }

    if (!resp.ok) {
        const text = await resp.text().catch(() => "");
        status.textContent = `Session error: ${resp.status} ${text}`;
        return;
    }

    let body;
    try { body = await resp.json(); } catch (_) { body = {}; }
    if (body.fingerprint && !project.host_key_fingerprint) {
        project.host_key_fingerprint = body.fingerprint;
        await persistVault();
    }

    // Mount the iframe. Trailing slash on the URL is load-bearing for
    // code-server's relative-URL discipline; the server-side proxy 301s
    // the no-slash form, but pre-emptively constructing the slash form
    // avoids an extra round-trip.
    const iframe = el("iframe", {
        class: "http-iframe",
        src: `/proxy/${encodeURIComponent(project.name)}/${encodeURIComponent(serviceId)}/`,
        // Only the absolute minimum sandbox the upstream needs. code-server
        // needs scripts, same-origin (cookies), forms, popups (its
        // command-palette opens windows for some commands), modals, and
        // clipboard. Drop top-navigation: prevents iframe-escape.
        sandbox: [
            "allow-scripts", "allow-same-origin", "allow-forms",
            "allow-popups", "allow-popups-to-escape-sandbox",
            "allow-modals", "allow-downloads",
        ].join(" "),
    });
    container.removeChild(status);
    container.appendChild(iframe);

    state.terminals[key].iframe = iframe;
}

async function handleControl(project, serviceId, term, ws, ctrl) {
    if (ctrl.type === "connected") {
        if (!project.host_key_fingerprint) {
            project.host_key_fingerprint = ctrl.fingerprint;
            await persistVault();
            term.writeln(`\r\n\x1b[90m[connected — host key recorded: ${ctrl.fingerprint}]\x1b[0m`);
        } else {
            term.writeln(`\r\n\x1b[90m[connected]\x1b[0m`);
        }
    } else if (ctrl.type === "fingerprint_mismatch") {
        const accept = confirm(
            `Host key for "${project.name}" has CHANGED.\n\n` +
            `Stored: ${project.host_key_fingerprint}\n` +
            `Actual: ${ctrl.actual}\n\n` +
            `Accept the new key?\n\n` +
            `Click OK only if you intentionally recreated the supervisor (e.g. \`research project update\`) — otherwise this could be a man-in-the-middle.`,
        );
        if (accept) {
            project.host_key_fingerprint = ctrl.actual;
            await persistVault();
            term.writeln("\r\n\x1b[33m[host key updated; click the tab to reconnect]\x1b[0m");
            const k = tkey(project.name, serviceId);
            const t = state.terminals[k];
            if (t) {
                try { t.ws.close(); } catch (_) {}
                delete state.terminals[k];
            }
        } else {
            term.writeln("\r\n\x1b[31m[host key mismatch — connection rejected]\x1b[0m");
        }
    } else if (ctrl.type === "auth_failed") {
        term.writeln("\r\n\x1b[31m[auth failed — check the saved password]\x1b[0m");
    } else if (ctrl.type === "error") {
        term.writeln(`\r\n\x1b[31m[error: ${ctrl.msg}]\x1b[0m`);
    }
}

// ---- bootstrap -------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => {
    applyTheme(loadStoredTheme());
    state.railPinned = loadRailPinned();
    state.railExpanded = state.railPinned;
    if (loadStored()) renderUnlock();
    else renderSetup();
});
