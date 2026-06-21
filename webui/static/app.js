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
const IFRAME_ZOOM_KEY = "rs-webui-iframe-zoom";
const IFRAME_ZOOMS = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2];
const IFRAME_ZOOM_DEFAULT = 0.9;
const RAIL_PINNED_KEY = "rs-webui-rail-pinned";
const RAIL_WIDTH_KEY = "rs-webui-rail-width";
const RAIL_WIDTH_DEFAULT = 200;
// Max prevents the rail from swallowing the terminal area on narrow
// viewports. Min is intentionally permissive — at 80px the footer
// dropdowns clip but project names + status dots stay legible, which
// is the only thing the rail actually has to show when shrunk to a
// status strip. Same posture as SPLIT_RATIO_MIN/MAX.
const RAIL_WIDTH_MIN = 80;
const RAIL_WIDTH_MAX = 480;
const PBKDF2_ITERATIONS = 600000;
const PROBE_INTERVAL_MS = 15000;
// Status polling cadence — only runs while the rail is open. Higher than
// PROBE_INTERVAL_MS because the data is filesystem-derived and changes on
// human / worker timescales (minutes), not network-up timescales (seconds).
const STATUS_INTERVAL_MS = 20000;
// Split-pane (W8): main-pane fraction bounds. 0.5 keeps the main pane at
// least half (below that, pin a different service); 0.9 leaves the side
// pane ~10% — enough room for a slim agent strip on widescreen monitors.
const SPLIT_RATIO_DEFAULT = 0.7;
const SPLIT_RATIO_MIN = 0.5;
const SPLIT_RATIO_MAX = 0.9;

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

const DEFAULT_THEME = "nord";

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
    statusTimer: null,
    theme: null,
    railPinned: false,       // persisted: keep rail in flex flow (push layout)
    railExpanded: false,     // in-memory: rail visible (overlay when unpinned)
    railWidth: RAIL_WIDTH_DEFAULT,  // persisted: rail width in px
    pinnedService: null,     // service id pinned to side pane for active project, or null
    splitRatio: SPLIT_RATIO_DEFAULT,  // main-pane fraction when split
    iframeZoom: IFRAME_ZOOM_DEFAULT,  // CSS transform scale applied to http-kind iframes
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
    closeProjectConfigBox();
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
    // JIT-attached projects (broker keyring) are transient: their SSH creds are
    // held in browser memory only and must NEVER reach the encrypted blob.
    // Strip them here — the single persist choke point — so the guarantee holds
    // regardless of what triggered the save.
    const persistable = {
        ...state.vault,
        projects: state.vault.projects.filter((p) => !p._jit),
    };
    const enc = await encryptVault(state.derivedKey, persistable);
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
    // Rail-visibility-gated status polling is started inside applyRailState;
    // no separate kickoff needed here.

    if (state.activeProject) {
        await activateProject(state.activeProject);
    }

    // Repopulate the sidebar from the broker's running set (non-blocking) so a
    // reload doesn't lose the transient (_jit) create/attach rows. No-ops when
    // not logged into Management.
    syncSidebarFromBroker();
}

// ---- rail expand / pin -----------------------------------------------------

function loadRailPinned() {
    return localStorage.getItem(RAIL_PINNED_KEY) === "1";
}

function loadRailWidth() {
    const v = parseInt(localStorage.getItem(RAIL_WIDTH_KEY) || "", 10);
    if (!isFinite(v)) return RAIL_WIDTH_DEFAULT;
    return Math.max(RAIL_WIDTH_MIN, Math.min(RAIL_WIDTH_MAX, v));
}

function applyRailWidth(px) {
    document.documentElement.style.setProperty("--rail-width", `${px}px`);
}

// Splitter on the rail's right edge — same pointer-capture pattern as
// the W8 terminal-area splitter so the drag survives moving the cursor
// across iframes / xterm canvases.
function installRailSplitterDrag(splitter) {
    let dragging = false;
    let pointerId = null;
    const onMove = (ev) => {
        if (!dragging) return;
        const rail = splitter.parentElement;
        if (!rail) return;
        const rect = rail.getBoundingClientRect();
        let w = ev.clientX - rect.left;
        w = Math.max(RAIL_WIDTH_MIN, Math.min(RAIL_WIDTH_MAX, w));
        applyRailWidth(w);
        state.railWidth = w;
    };
    const onUp = () => {
        if (!dragging) return;
        dragging = false;
        splitter.classList.remove("dragging");
        try { if (pointerId != null) splitter.releasePointerCapture(pointerId); } catch (_) {}
        pointerId = null;
        splitter.removeEventListener("pointermove", onMove);
        splitter.removeEventListener("pointerup", onUp);
        splitter.removeEventListener("pointercancel", onUp);
        document.body.style.userSelect = "";
        localStorage.setItem(RAIL_WIDTH_KEY, String(Math.round(state.railWidth)));
        // Pinned rail shifts the terminal area's width — refit xterms.
        setTimeout(() => {
            for (const t of Object.values(state.terminals)) {
                if (t.fitAddon) { try { t.fitAddon.fit(); } catch (_) {} }
            }
        }, 0);
    };
    splitter.onpointerdown = (ev) => {
        ev.preventDefault();
        // Don't bubble — the rail itself doesn't have a click handler, but
        // the dashboard-level toggle on the Projects tab is right next to
        // the splitter in overlay mode; stop here to be defensive.
        ev.stopPropagation();
        dragging = true;
        pointerId = ev.pointerId;
        splitter.classList.add("dragging");
        try { splitter.setPointerCapture(ev.pointerId); } catch (_) {}
        splitter.addEventListener("pointermove", onMove);
        splitter.addEventListener("pointerup", onUp);
        splitter.addEventListener("pointercancel", onUp);
        document.body.style.userSelect = "none";
    };
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

    // Status polling lifecycle is tied to rail visibility — no point
    // walking project trees while the rail's hidden.
    scheduleStatusPolling();

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

// Auto-collapse the floating rail on any interaction outside it. Two
// fire paths because iframe clicks don't bubble out of the iframe:
//   - pointerdown on the parent document handles clicks on xterm /
//     service tabs / terminal area chrome.
//   - window blur + document.activeElement === IFRAME handles the case
//     where the click landed inside code-server (or any http tab).
// Pinned rail is excluded — pinning is the explicit "keep it open"
// affordance and stays put regardless of where the user clicks.
function installRailOutsideClickHandlers() {
    document.addEventListener("pointerdown", (ev) => {
        if (!state.railExpanded || state.railPinned) return;
        // Modal in front owns the interaction; don't collapse behind it.
        if (document.querySelector(".modal-backdrop")) return;
        const path = ev.composedPath ? ev.composedPath() : [];
        for (const node of path) {
            if (!node || !node.classList) continue;
            // Click inside the rail itself — let inner handlers run.
            if (node.classList.contains("project-rail")) return;
            // The config box is a body child (floats outside the rail) but
            // is logically part of it — interacting with it must not
            // collapse the rail. Same for the gear that opens it.
            if (node.classList.contains("project-config-box")) return;
            if (node.classList.contains("project-config-btn")) return;
            // Click on the Projects tab — its own onclick toggles the
            // rail. Letting our outside handler also fire here would
            // double-toggle (tab opens, then we close).
            if (node.classList.contains("projects-tab")) return;
        }
        state.railExpanded = false;
        applyRailState();
    });

    window.addEventListener("blur", () => {
        if (!state.railExpanded || state.railPinned) return;
        // Give the browser a tick to settle focus into the iframe before
        // we check activeElement. Without the timeout, blur fires while
        // the active element is still the parent body.
        setTimeout(() => {
            if (document.activeElement && document.activeElement.tagName === "IFRAME") {
                state.railExpanded = false;
                applyRailState();
            }
        }, 0);
    });
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

// Rebuild the sidebar rail in place after a management op changes the project
// set (create adds a row, destroy removes one). Targeted — NOT renderDashboard,
// which clearBody()s and would tear down any open service tabs. A management box,
// if still open, is a position:fixed overlay above the rail, so refreshing the
// rail behind it is invisible until the box closes.
function refreshProjectRail() {
    const old = document.querySelector(".project-rail");
    if (!old) return;
    old.replaceWith(makeProjectRail());
    applyRailState();
    schedulePolling();
}

// Fetch a project's SSH coordinates from the broker (JIT keyring) and add/refresh
// its sidebar entry as a transient (_jit) bookmark — same shape mgmtAttach uses,
// but it neither activates nor re-renders. Best-effort: returns true on success,
// false on any failure (the caller's op already succeeded; the row is a bonus).
async function attachIntoVault(name) {
    let res;
    try {
        res = await fetch(`/broker/project/${encodeURIComponent(name)}/attach`,
            { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    } catch (e) { return false; }
    if (!res.ok) return false;
    let body; try { body = await res.json(); } catch (e) { return false; }
    if (!body.ok || !body.result) return false;
    const info = body.result;   // {name, host, port, username, password}
    const existing = state.vault.projects.find((p) => p.name === info.name);
    if (existing) {
        existing.host = info.host; existing.port = info.port;
        existing.username = info.username; existing.password = info.password;
    } else {
        state.vault.projects.push({
            name: info.name, host: info.host, port: info.port,
            username: info.username, password: info.password, _jit: true,
        });
    }
    return true;
}

// Merge the broker's RUNNING projects into the sidebar. The created/attached
// rows are transient (_jit — creds in memory only, never persisted, so SSH
// passwords stay out of localStorage), so a page reload drops them; this re-adds
// them from the authoritative broker list. Needs a Management session (the
// cookie survives the reload) — silently no-ops when logged out, leaving the
// rail as whatever persisted vault bookmarks exist. Best-effort + non-blocking:
// the rail re-renders once the running set lands.
async function syncSidebarFromBroker(prefetched) {
    let list = prefetched;
    if (!Array.isArray(list)) {
        let res;
        try { res = await fetch("/broker/projects"); } catch (e) { return; }
        if (!res.ok) return;                     // 401/403/503 → no session
        let body; try { body = await res.json(); } catch (e) { return; }
        if (!body.ok || !Array.isArray(body.result)) return;
        list = body.result;
    }
    const running = list.filter((p) => p.state === "running");
    let added = 0;
    await Promise.all(running.map(async (p) => {
        if (state.vault.projects.some((v) => v.name === p.project)) return;
        if (await attachIntoVault(p.project)) added++;
    }));
    if (added) refreshProjectRail();
}

function makeProjectRail() {
    const rail = el("aside", { class: "project-rail" });
    const splitter = el("div", { class: "rail-splitter", title: "Drag to resize" });
    installRailSplitterDrag(splitter);
    rail.appendChild(splitter);
    // Management lives ABOVE the bookmarks, divided off: it's the host's live
    // project list (broker), distinct from the vault bookmarks below it.
    rail.appendChild(makeManagementSection());
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
        makeIframeZoomSelector(),
        makeThemeSelector(),
        el("button", { class: "lock-btn", onclick: lockVault }, ["Lock vault"]),
    ]);
    rail.appendChild(footer);
    return rail;
}

// ---- management (broker-driven host lifecycle) -----------------------------
// A sidebar entry, separated from the vault bookmarks, that opens the host's
// authoritative project list (via the login-gated broker relay) in the main
// area. The browser holds only an opaque session cookie; the broker token
// lives server-side. start/stop are confirm-gated (they recreate / interrupt
// a supervisor — costly, deliberate).

function makeManagementSection() {
    const entry = el("div", {
        class: "management-entry",
        title: "Host project management (broker)",
    }, [el("span", { class: "mgmt-gear" }, ["⚙"]), el("span", {}, ["Management"])]);
    entry.onclick = (ev) => { ev.stopPropagation(); openManagement(); };
    return el("div", { class: "rail-management" }, [entry]);
}

function openManagement() {
    const mainArea = document.querySelector(".main-area");
    if (!mainArea) return;
    const term = document.getElementById("terminal-area");
    if (term) term.style.display = "none";
    let view = document.getElementById("management-view");
    if (!view) {
        view = el("div", { class: "management-view", id: "management-view" });
        mainArea.appendChild(view);
    }
    view.style.display = "";
    // Management and a project tab are mutually exclusive — clear the rail's
    // active project so focus moves cleanly to Management (activateProject does
    // the reverse via closeManagement).
    document.querySelectorAll(".project-rail .project").forEach((r) => r.classList.remove("active"));
    document.querySelectorAll(".management-entry").forEach((e) => e.classList.add("active"));
    renderManagementInto(view);
}

function closeManagement() {
    const view = document.getElementById("management-view");
    if (view) view.style.display = "none";
    const term = document.getElementById("terminal-area");
    if (term) term.style.display = "";
    document.querySelectorAll(".management-entry").forEach((e) => e.classList.remove("active"));
}

async function renderManagementInto(view) {
    view.innerHTML = "";
    view.appendChild(el("div", { class: "mgmt-loading" }, ["Loading…"]));
    let res;
    try {
        res = await fetch("/broker/projects");
    } catch (e) {
        return renderMgmtUnavailable(view);
    }
    if (res.status === 401) return renderMgmtLogin(view);
    if (res.status === 403) return renderMgmtRejected(view);
    if (res.status === 503) return renderMgmtUnavailable(view);
    let body;
    try { body = await res.json(); } catch (e) { return renderMgmtUnavailable(view); }
    if (!res.ok || !body.ok) return renderMgmtUnavailable(view);
    renderMgmtTable(view, body.result || []);
    // Reuse the authoritative list to repopulate the sidebar's running set
    // (so opening / logging into Management surfaces running projects there too).
    syncSidebarFromBroker(body.result || []);
}

function mgmtCard(view, children) {
    view.innerHTML = "";
    view.appendChild(el("div", { class: "mgmt-center" }, [
        el("div", { class: "card mgmt-card" }, children),
    ]));
}

function renderMgmtLogin(view) {
    const pw = el("input", { type: "password", autocomplete: "current-password" });
    const errEl = el("div", { class: "error" });
    const submit = el("button", { class: "btn" }, ["Log in"]);
    const doLogin = async () => {
        errEl.textContent = "";
        let res;
        try {
            res = await fetch("/broker/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ password: pw.value }),
            });
        } catch (e) { errEl.textContent = "Broker unreachable."; return; }
        if (res.status === 200) return renderManagementInto(view);
        if (res.status === 429) {
            const ra = res.headers.get("Retry-After");
            errEl.textContent = `Too many attempts. Wait ${ra || "a moment"}s.`;
            return;
        }
        if (res.status === 401) { errEl.textContent = "Wrong management password."; return; }
        if (res.status === 403) return renderMgmtRejected(view);
        errEl.textContent = "Broker unavailable.";
    };
    submit.onclick = doLogin;
    pw.onkeydown = (e) => { if (e.key === "Enter") doLogin(); };
    mgmtCard(view, [
        el("h2", {}, ["Log in to management"]),
        el("div", { class: "field" }, [el("label", {}, ["Management password"]), pw]),
        el("div", { class: "btn-row" }, [submit]),
        errEl,
        el("div", { class: "hint" }, [
            "Set on the host with ",
            el("code", {}, ["research broker passwd"]),
            ". Separate from your vault password.",
        ]),
    ]);
    setTimeout(() => pw.focus(), 50);
}

function renderMgmtUnavailable(view) {
    mgmtCard(view, [
        el("h2", {}, ["Management unavailable"]),
        el("p", {}, ["The broker isn’t reachable. Start it on the host:"]),
        el("pre", {}, ["research broker start"]),
        el("div", { class: "hint" }, [
            "Management is opt-in; the rest of the webui is unaffected.",
        ]),
    ]);
}

function renderMgmtRejected(view) {
    mgmtCard(view, [
        el("h2", {}, ["Broker rejected the webui"]),
        el("p", {}, [
            "The broker rejected the webui’s identity. The webui must run as ",
            "the same user as the broker (uid match). The broker log names the ",
            "mismatch.",
        ]),
    ]);
}

function renderMgmtTable(view, projects) {
    view.innerHTML = "";
    const create = el("button", { class: "btn-small" }, ["+ New project"]);
    create.onclick = () => mgmtCreateDialog(view);
    const refresh = el("button", { class: "btn-small" }, ["Refresh"]);
    refresh.onclick = () => renderManagementInto(view);
    const logout = el("button", { class: "btn-small" }, ["Log out"]);
    logout.onclick = () => mgmtLogout(view);
    view.appendChild(el("div", { class: "mgmt-header" }, [
        el("h2", {}, ["Management — host projects (live)"]),
        el("div", { class: "mgmt-toolbar" }, [create, refresh, logout]),
    ]));
    if (projects.length === 0) {
        view.appendChild(el("div", { class: "mgmt-empty" }, ["No projects on this host."]));
        return;
    }
    const rows = [el("div", { class: "mgmt-row mgmt-row-head" }, [
        el("span", {}, ["Project"]), el("span", {}, ["State"]),
        el("span", {}, ["SSH"]), el("span", {}, ["Size"]), el("span", {}, ["Actions"]),
    ])];
    const fill = {};   // project name → {badge, size} elements to populate async
    for (const p of projects) {
        const running = p.state === "running";
        const badge = el("span", { class: "type-badge" });          // filled by mgmtFillStatus
        const sizeEl = el("span", { class: "mgmt-size" }, ["…"]);
        fill[p.project] = { badge, size: sizeEl };
        const power = el("button", { class: "btn-small" }, [running ? "Stop" : "Start"]);
        power.onclick = () => mgmtAction(view, p.project, running ? "stop" : "start");
        // Attach + Update need a live supervisor (bridge endpoint / recreate);
        // only offered while running. Destroy is always available.
        const actions = [power];
        if (running) {
            const attach = el("button", { class: "btn-small" }, ["Attach"]);
            attach.onclick = () => mgmtAttach(view, p.project, attach);
            const update = el("button", { class: "btn-small" }, ["Update"]);
            update.onclick = () => mgmtUpdate(view, p.project);
            actions.push(attach, update);
        }
        const destroy = el("button", { class: "btn-small btn-danger" }, ["Destroy"]);
        destroy.onclick = () => mgmtDestroyDialog(view, p.project);
        actions.push(destroy);
        rows.push(el("div", { class: "mgmt-row" }, [
            el("span", { class: "mgmt-name" }, [el("span", { class: "mgmt-name-text" }, [p.project]), badge]),
            el("span", { class: running ? "state-running" : "state-stopped" }, [p.state]),
            el("span", { class: "mgmt-ssh" }, [p.ssh || "—"]),
            sizeEl,
            el("span", { class: "mgmt-actions" }, actions),
        ]));
    }
    view.appendChild(el("div", { class: "mgmt-table" }, rows));
    mgmtFillStatus(fill);
}

// The broker `list` carries name/state/ssh; the project flavour + disk size come
// off the same /projects/status data plane the rail uses (read from the
// /projects:ro mount, joined here by name).
async function mgmtFillStatus(fill) {
    const names = Object.keys(fill);
    if (names.length === 0) return;
    let data;
    try {
        const res = await fetch(`/projects/status?names=${encodeURIComponent(names.join(","))}`);
        if (!res.ok) return;
        data = await res.json();
    } catch (e) { return; }
    for (const [name, st] of Object.entries(data)) {
        const ref = fill[name];
        if (!ref) continue;
        if (st && st.error === "not_found") {
            ref.size.textContent = "—";
            setTypeBadge(ref.badge, null);
            continue;
        }
        ref.size.textContent = formatBytes((st && st.disk_bytes) || 0);
        setTypeBadge(ref.badge, st && st.flavor);
    }
}

// Paint a project-type badge: normal text + a type-coloured border, or empty
// (collapsed) when the flavour is unknown.
function setTypeBadge(elm, flavor) {
    const cls = flavor === "sandbox" ? " type-sandbox"
        : flavor === "research" ? " type-research" : "";
    elm.className = "type-badge" + cls;
    elm.textContent = flavor || "";
}

function mgmtAction(view, name, action) {
    const desc = action === "start"
        ? `Start "${name}". This recreates the supervisor (fresh container, re-staged images) and takes a moment. Running work is unaffected.`
        : `Stop "${name}". This interrupts any running work in the supervisor.`;
    mgmtConfirmThenTail(view, {
        title: `${action === "start" ? "Start" : "Stop"} project ${name}`,
        verb: action,
        confirmLabel: action === "start" ? "Start" : "Stop",
        body: [el("p", {}, [desc])],
        request: () => fetch(
            `/broker/project/${encodeURIComponent(name)}/${action}`,
            { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }),
    });
}

async function mgmtLogout(view) {
    try {
        await fetch("/broker/logout",
            { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    } catch (e) { /* logging out is best-effort */ }
    renderMgmtLogin(view);
}

// An auth/availability status the caller should defer to (re-render the right
// card), or null if the response carries a real verb result to handle.
function mgmtStatusRedirect(view, status) {
    if (status === 401) return () => renderMgmtLogin(view);
    if (status === 403) return () => renderMgmtRejected(view);
    if (status === 503) return () => renderMgmtUnavailable(view);
    return null;
}

function mgmtErrText(body) {
    const e = body && body.error;
    return (e && (e.message || e.kind)) || "unknown error";
}

// ---- two-phase op box: confirm → live progress tail ------------------------
// One floating box for every write action (start / stop / update / create /
// destroy). Phase 1 confirms (and, for destroy, collects the type-name + step-up
// password). Phase 2 fires the op, gets an op_id, and tails the broker's view
// log live to a terminal milestone, then shows the result + a Close button.
// Input validation surfaces in phase 1 (client checks + the broker's synchronous
// op_id/field validation at the POST); the verb's own execution outcome — incl.
// destroy's step-up password re-verification, which the broker runs async —
// surfaces in phase 2.

// Poll cadence for the op view-log tail. Milestones are coarse (≈5–8 per op over
// a 10–30s lifecycle), so sub-second polling is plenty live without hammering
// the webui; at 2× (1.2s) progress feels laggy, at ½ (300ms) it's needless load
// for a single operator driving one op at a time.
const OP_POLL_INTERVAL_MS = 600;

function opSleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// Expected milestone checklist per verb — rendered UP FRONT (all rows pending)
// so the operator sees what's still to come, each row flipping to a green ✓ as
// its milestone lands. Keys match the rscore progress.step() keys. The terminal
// "done" record is NOT a row — it's the foot button that enables on completion.
// Conditional stages a verb may or may not emit (e.g. update's enable/disable/
// refresh on a recreate, or create's data-dir setup) are deliberately NOT
// pre-listed — they append already-checked as they arrive, so a missing optional
// stage never leaves a stuck pending row.
const OP_CHECKLISTS = {
    create: [
        { key: "validate", label: "checking prerequisites" },
        { key: "network", label: "creating project network" },
        { key: "create-container", label: "creating supervisor container" },
        { key: "stage-images", label: "staging inner images" },
        { key: "wire", label: "enabling workers and sandboxes" },
    ],
    destroy: [
        { key: "validate", label: "locating project" },
        { key: "router", label: "removing router rules" },
        { key: "remove-container", label: "removing container" },
        { key: "cleanup", label: "removing workspace, volume and network" },
    ],
    start: [
        { key: "validate", label: "checking project" },
        { key: "recreate", label: "recreating supervisor" },
    ],
    stop: [
        { key: "validate", label: "checking project" },
        { key: "stop", label: "stopping container" },
    ],
    update: [
        { key: "validate", label: "validating update" },
        { key: "recreate", label: "recreating supervisor" },
    ],
};

// Human message for a failed op, from its structured result envelope.
function mgmtOpFailMsg(result) {
    const err = result && result.error;
    const kind = err && err.kind;
    if (kind === "step_up_required") return "Wrong password.";
    if (kind === "broker_unavailable") return "Broker unreachable.";
    if (err && err.message) return err.message;
    return kind || "operation failed";
}

// Phase 2: swap `card` to the expected-stage CHECKLIST and tail op `opId` to
// completion, flipping each row to ✓ as its milestone lands. Status
// (GET /broker/op/<id>) is the source of truth for completion — it covers the
// no-log failure paths (broker_unavailable, step-up reject, internal) where the
// broker never wrote a view file. A log terminal is a fallback for the
// webui-restarted-mid-op case where OP_RUNS was lost (status → "unknown").
async function mgmtTailOp(view, backdrop, card, opId, title, verb, onDone) {
    const checklist = OP_CHECKLISTS[verb] || [];
    const listEl = el("div", { class: "op-checklist" });
    const items = {};   // stepKey → { row, icon }
    const addRow = (key, label) => {
        const icon = el("span", { class: "op-check-icon" }, ["○"]);
        const row = el("div", { class: "op-check pending" },
                       [icon, el("span", { class: "op-check-label" }, [label])]);
        listEl.appendChild(row);
        items[key] = { row, icon };
        return items[key];
    };
    for (const it of checklist) addRow(it.key, it.label);
    const markDone = (key) => {
        // A conditional stage not pre-listed (e.g. update enable/disable) appends
        // already-checked as it arrives.
        const ref = items[key] || addRow(key, key);
        ref.row.classList.remove("pending");
        ref.row.classList.add("ok");
        ref.icon.textContent = "✓";
    };

    // The failure reason — populated ONLY on failure; the running/done state is
    // conveyed by the checklist + the foot button, with no status chatter above.
    const failEl = el("div", { class: "op-fail" });
    // "Done" is the foot button, DISABLED until the op reaches a terminal state,
    // so the box can't be dismissed mid-op. The _run_op catch-all guarantees the
    // status reaches a terminal value within the op timeout, so it always enables
    // in-session.
    const doneBtn = el("button", { class: "btn", disabled: "" }, ["Working…"]);
    doneBtn.onclick = () => {
        if (doneBtn.disabled) return;
        backdrop.remove();
        renderManagementInto(view);
    };
    card.innerHTML = "";
    card.appendChild(el("h2", {}, [title]));
    card.appendChild(listEl);
    card.appendChild(failEl);
    card.appendChild(el("div", { class: "btn-row" }, [doneBtn]));

    let from = 0, done = false, result = null, ok = false;
    let sawTerminal = false, terminalOk = false;
    const drainLog = async () => {
        const r = await fetch(`/broker/op/${encodeURIComponent(opId)}/log?from=${from}`);
        const redirect = mgmtStatusRedirect(view, r.status);
        if (redirect) return redirect;          // truthy → caller dismisses + redirects
        const b = await r.json();
        if (b.started !== false && b.data) {
            from = b.next;
            for (const line of b.data.split("\n")) {
                if (!line.trim()) continue;
                let rec; try { rec = JSON.parse(line); } catch (e) { continue; }
                if (rec.status === "done") { sawTerminal = true; terminalOk = true; }
                else if (rec.status === "failed") { sawTerminal = true; terminalOk = false; }
                else markDone(rec.step);
            }
        }
        return null;
    };

    while (!done) {
        // 1. Drain new view-log bytes, flipping each landed stage to ✓.
        try {
            const redirect = await drainLog();
            if (redirect) { backdrop.remove(); return redirect(); }
        } catch (e) { /* transient; the status poll below decides completion */ }
        // 2. Status — authoritative for completion.
        try {
            const r = await fetch(`/broker/op/${encodeURIComponent(opId)}`);
            const redirect = mgmtStatusRedirect(view, r.status);
            if (redirect) { backdrop.remove(); return redirect(); }
            const sb = await r.json();
            if (sb.state === "ok" || sb.state === "failed") {
                result = sb.result || null;
                ok = sb.state === "ok";
                done = true;
                break;
            }
            // state "unknown" → OP_RUNS lost (webui restart); fall back to a log
            // terminal if we saw one, else keep polling for the file to appear.
            if (sb.state === "unknown" && sawTerminal) {
                ok = terminalOk; done = true; break;
            }
        } catch (e) { /* transient */ }
        await opSleep(OP_POLL_INTERVAL_MS);
    }
    // Final drain: the terminal milestone may have landed between this tick's
    // /log and /status fetches, so the trailing stage row is settled.
    try { await drainLog(); } catch (e) { /* best-effort */ }
    if (!ok) failEl.textContent = "Failed — " + mgmtOpFailMsg(result);
    doneBtn.textContent = "Done";
    doneBtn.disabled = false;
    if (onDone) { try { await onDone(ok, result); } catch (e) { /* best-effort */ } }
}

// Build the phase-1 confirm card; on confirm, fire `cfg.request()`, then hand
// the returned op_id to mgmtTailOp for phase 2. Shared by all five write actions.
function mgmtConfirmThenTail(view, cfg) {
    // A dialog is already open — a fast double-click on a row button would
    // otherwise stack two backdrops (harmless, since the confirm gates
    // execution, but two to dismiss). Same guard the service-control path uses.
    if (document.querySelector(".modal-backdrop")) return null;
    const backdrop = el("div", { class: "modal-backdrop" });
    const errEl = el("div", { class: "error" });
    const cancel = el("button", { class: "btn btn-secondary" }, ["Cancel"]);
    cancel.onclick = () => backdrop.remove();
    const go = el("button", { class: cfg.danger ? "btn btn-danger" : "btn" },
                 [cfg.confirmLabel]);
    const card = el("div", { class: "card" }, [
        el("h2", {}, [cfg.title]),
        ...cfg.body,
        el("div", { class: "btn-row" }, [cancel, go]),
        errEl,
    ]);
    go.onclick = async () => {
        errEl.textContent = "";
        const verr = cfg.validate ? cfg.validate() : null;
        if (verr) { errEl.textContent = verr; return; }
        go.disabled = true; cancel.disabled = true;
        const orig = cfg.confirmLabel; go.textContent = "…";
        let res;
        try { res = await cfg.request(); }
        catch (e) {
            go.disabled = false; cancel.disabled = false; go.textContent = orig;
            errEl.textContent = "Broker unreachable."; return;
        }
        const redirect = mgmtStatusRedirect(view, res.status);
        if (redirect) { backdrop.remove(); return redirect(); }
        let body; try { body = await res.json(); } catch (e) { body = {}; }
        if (!body.ok || !body.op_id) {
            go.disabled = false; cancel.disabled = false; go.textContent = orig;
            errEl.textContent = "Failed: " + mgmtErrText(body); return;
        }
        await mgmtTailOp(view, backdrop, card, body.op_id,
                         cfg.tailTitle || cfg.title, cfg.verb, cfg.onDone);
    };
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    if (cfg.focus) setTimeout(() => cfg.focus(), 50);
    return backdrop;
}

// ---- create -----------------------------------------------------------------
// Minimal create form: name · workflow · egress · a few --enable presets. The
// broker's CREATE_WEBUI_FIELDS allow-list is the real input boundary (it drops
// any path/host-shaped field); full CLI-flag parity is deliberately deferred.
// The user picks a WORKFLOW (substrate + flavor are derived from its manifest,
// server-side). The options are hardcoded to the built-ins for now; the
// catalog-driven picker is STAGE_WEBUI_WORKFLOWS.

const MGMT_ENABLE_PRESETS = ["websearcher", "wrangler", "echo"];

function mgmtCreateDialog(view) {
    const nameI = el("input", { type: "text", autocomplete: "off" });
    const workflowS = el("select", {}, [
        el("option", { value: "research" }, ["research"]),
        el("option", { value: "box-host" }, ["box-host"]),
        el("option", { value: "empty" }, ["empty"]),
    ]);
    const egressS = el("select", {}, [
        el("option", { value: "open" }, ["open"]),
        el("option", { value: "locked" }, ["locked"]),
    ]);
    const checks = MGMT_ENABLE_PRESETS.map((p) => {
        const cb = el("input", { type: "checkbox", value: p });
        return { p, cb, label: el("label", { class: "mgmt-check" }, [cb, " " + p]) };
    });
    mgmtConfirmThenTail(view, {
        title: "New project",
        tailTitle: "Creating project",
        verb: "create",
        confirmLabel: "Create",
        body: [
            el("div", { class: "field" }, [el("label", {}, ["Project name"]), nameI]),
            el("div", { class: "field" }, [el("label", {}, ["Workflow"]), workflowS]),
            el("div", { class: "field" }, [el("label", {}, ["Egress"]), egressS]),
            el("div", { class: "field" }, [
                el("label", {}, ["Enable"]),
                el("div", { class: "mgmt-checks" }, checks.map((c) => c.label)),
            ]),
            el("div", { class: "hint" }, [
                "Creating stages container images and can take 10–30s (longer cold).",
            ]),
        ],
        validate: () => nameI.value.trim() ? null : "Project name is required.",
        request: () => fetch("/broker/project", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: nameI.value.trim(), workflow: workflowS.value, egress: egressS.value,
                enable: checks.filter((c) => c.cb.checked).map((c) => c.p),
            }),
        }),
        // On success, surface the new (running) project in the sidebar — JIT-fetch
        // its creds the same way Attach does, then refresh the rail behind the box.
        onDone: async (ok) => {
            if (!ok) return;
            if (await attachIntoVault(nameI.value.trim())) refreshProjectRail();
        },
        focus: () => nameI.focus(),
    });
}

// ---- JIT keyring attach -----------------------------------------------------
// Fetch the project's SSH coordinates from the broker on demand and open it as
// a TRANSIENT project (creds in memory only — persistVault strips _jit entries,
// so they never reach the encrypted blob). No host-side `research webui import`.

async function mgmtAttach(view, name, btn) {
    btn.disabled = true; const orig = btn.textContent; btn.textContent = "…";
    let res;
    try {
        res = await fetch(`/broker/project/${encodeURIComponent(name)}/attach`,
            { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    } catch (e) {
        btn.disabled = false; btn.textContent = orig; alert("Broker unreachable."); return;
    }
    const redirect = mgmtStatusRedirect(view, res.status);
    if (redirect) return redirect();
    let body; try { body = await res.json(); } catch (e) { body = {}; }
    if (!body.ok) {
        btn.disabled = false; btn.textContent = orig;
        alert("Attach failed: " + mgmtErrText(body)); return;
    }
    const info = body.result;   // {name, host, port, username, password}
    const existing = state.vault.projects.find((p) => p.name === info.name);
    if (existing) {
        // Refresh creds in place; preserve its persisted/transient status.
        existing.host = info.host; existing.port = info.port;
        existing.username = info.username; existing.password = info.password;
    } else {
        state.vault.projects.push({
            name: info.name, host: info.host, port: info.port,
            username: info.username, password: info.password, _jit: true,
        });
    }
    closeManagement();
    await renderDashboard();
    await activateProject(info.name);
}

// ---- update (file-only recreate) -------------------------------------------

function mgmtUpdate(view, name) {
    mgmtConfirmThenTail(view, {
        title: `Update project ${name}`,
        tailTitle: `Updating ${name}`,
        verb: "update",
        confirmLabel: "Update",
        body: [el("p", {}, [
            `Update "${name}". This recreates the supervisor with the latest ` +
            "workspace templates (fresh container, re-staged images). Running " +
            "work is interrupted. No image rebuild.",
        ])],
        request: () => fetch(`/broker/project/${encodeURIComponent(name)}/update`,
            { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }),
    });
}

// ---- destroy (type-name confirm + step-up re-auth) -------------------------

function mgmtDestroyDialog(view, name) {
    const nameI = el("input", { type: "text", placeholder: name, autocomplete: "off" });
    const pwI = el("input", { type: "password", autocomplete: "current-password" });
    mgmtConfirmThenTail(view, {
        title: "Destroy project",
        tailTitle: `Destroying ${name}`,
        verb: "destroy",
        confirmLabel: "Destroy",
        danger: true,
        body: [
            el("p", {}, [
                `This permanently deletes "${name}" — its container, workspace, ` +
                "volume, and network. This cannot be undone.",
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Type the project name to confirm"]), nameI,
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Re-enter management password"]), pwI,
            ]),
        ],
        validate: () => {
            if (nameI.value.trim() !== name) return "Type the project name exactly to confirm.";
            if (!pwI.value) return "Re-enter your management password.";
            return null;
        },
        // The step-up password rides the request; the broker re-verifies it
        // async, so a wrong password surfaces as a FAILED op in phase 2
        // ("Failed — Wrong password."), not an inline phase-1 error.
        request: () => fetch(`/broker/project/${encodeURIComponent(name)}/destroy`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password: pwI.value }),
        }),
        onDone: async (ok) => {
            if (!ok) return;
            // Tear down the gone project's open terminals/websockets (no dead
            // reconnect spam), drop it from the sidebar + persisted vault, then
            // refresh the rail behind the box so the row disappears.
            teardownProjectState(name);
            state.vault.projects = state.vault.projects.filter((p) => p.name !== name);
            try { await persistVault(); } catch (e) { /* best-effort */ }
            refreshProjectRail();
        },
        focus: () => nameI.focus(),
    });
}

function makeProjectRow(project) {
    const dot = el("span", { class: "status-dot" });
    const name = el("span", { class: "name" }, [project.name]);
    // No per-row remove/destroy: project lifecycle is the Management panel's job
    // (broker create/destroy). The rail just reflects the running set — create
    // adds a row, destroy removes it (see mgmtConfirmThenTail onDone hooks).
    const head = el("div", { class: "project-head" }, [dot, name]);
    // Second line: a project-type badge (research/sandbox, filled by
    // fetchProjectsStatus) + the worker-activity figures, plus the always-
    // present config gear. The gear opens the per-project floating config box.
    // The badge starts empty (collapsed) until the flavour lands; the gear
    // keeps the line non-empty so it shows from row construction. Disk size
    // lives in the broker Management table now, not here.
    const typeBadge = el("span", { class: "type-badge" });
    const statusText = el("span", { class: "status-text" });
    const configBtn = el("span", {
        class: "project-config-btn",
        title: "Project settings",
    }, ["⚙"]);
    configBtn.onclick = (ev) => {
        ev.stopPropagation();
        openProjectConfigBox(project, configBtn);
    };
    const statusLine = el("div", { class: "project-status-line" }, [typeBadge, statusText, configBtn]);
    const statusMeta = el("div", { class: "project-status-meta" });
    const row = el("div", {
        class: "project",
        "data-name": project.name,
        onclick: () => activateProject(project.name),
    }, [head, statusLine, statusMeta]);
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

// Per-project status sub-lines — only polled while the rail is open.
// Server reads from a RO bind-mount of PROJECTS_DIR; no per-supervisor
// HTTP, no docker socket. See PLAN/STAGE_WEBUI_W3_status_rail.md.
function scheduleStatusPolling() {
    if (state.statusTimer) {
        clearInterval(state.statusTimer);
        state.statusTimer = null;
    }
    if (!state.vault || state.vault.projects.length === 0) return;
    const expanded = state.railPinned || state.railExpanded;
    if (!expanded) return;
    fetchProjectsStatus();
    state.statusTimer = setInterval(fetchProjectsStatus, STATUS_INTERVAL_MS);
}

async function fetchProjectsStatus() {
    if (!state.vault || state.vault.projects.length === 0) return;
    const names = state.vault.projects.map((p) => p.name).join(",");
    try {
        const res = await fetch(`/projects/status?names=${encodeURIComponent(names)}`);
        if (!res.ok) return;
        const data = await res.json();
        for (const [name, status] of Object.entries(data)) {
            applyProjectStatus(name, status);
        }
    } catch (_) {
        // ignore — next tick will retry
    }
}

function applyProjectStatus(name, status) {
    const row = document.querySelector(`.project[data-name="${CSS.escape(name)}"]`);
    if (!row) return;
    // Write the disk/worker figures into the text span only — the sibling
    // config gear in .project-status-line must survive every poll.
    const line1 = row.querySelector(".project-status-line .status-text");
    const line2 = row.querySelector(".project-status-meta");
    const badge = row.querySelector(".project-status-line .type-badge");
    if (!line1 || !line2) return;
    if (status.error === "not_found") {
        line1.textContent = "";
        line2.textContent = "missing on disk";
        line2.removeAttribute("title");
        if (badge) setTypeBadge(badge, null);
        return;
    }
    if (badge) setTypeBadge(badge, status.flavor);
    line1.textContent = formatStatusLine1(status);
    line2.textContent = formatStatusLine2(status);
    if (status.latest && status.latest.path) {
        line2.title = status.latest.path;
    } else {
        line2.removeAttribute("title");
    }
}

// ▶️ = workers currently running (no DONE marker yet).
// ⏹ = workers that have stopped (DONE was touched on exit; this says
// nothing about success vs failure — the worker entrypoint touches
// DONE on every exit path).
function formatStatusLine1(s) {
    // Worker-activity figures only — disk size moved to the Management table.
    const parts = [];
    if ((s.workers_running || 0) > 0) parts.push(`▶️ ${s.workers_running}`);
    if ((s.workers_done || 0) > 0) parts.push(`⏹ ${s.workers_done}`);
    return parts.join("  ");
}

function formatStatusLine2(s) {
    if (!s.latest) return "";
    const ageSec = Math.max(0, Math.floor((Date.now() - s.latest.ts_ms) / 1000));
    if (ageSec > 7 * 86400) return "idle";
    const display = displayLatestPath(s.latest.path);
    return display ? `${display} ${formatAgo(ageSec)}` : formatAgo(ageSec);
}

// Workspace-relative path → short, rail-friendly label. The full path is
// preserved in the element's `title` (hover tooltip) for users who want
// to see exactly which file the timestamp came from.
function displayLatestPath(path) {
    if (!path) return "";
    let m = /^workers\/([^/]+)\/work\/(.+)$/.exec(path);
    if (m) {
        const basename = m[2].split("/").pop();
        return `${m[1]} · ${basename}`;
    }
    m = /^workers\/([^/]+)\/?$/.exec(path);
    if (m) return m[1];
    m = /^logbook\/(.+)$/.exec(path);
    if (m) return m[1];
    return path;
}

function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    const units = ["kB", "MB", "GB", "TB"];
    let v = n / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v >= 10 ? `${Math.round(v)} ${units[i]}` : `${v.toFixed(1)} ${units[i]}`;
}

function formatAgo(sec) {
    if (sec < 60) return `${sec}s`;
    const mins = Math.floor(sec / 60);
    if (mins < 60) return `${mins}m`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h`;
    return `${Math.floor(hours / 24)}d`;
}

function lockVault() {
    if (state.probeTimer) { clearInterval(state.probeTimer); state.probeTimer = null; }
    if (state.statusTimer) { clearInterval(state.statusTimer); state.statusTimer = null; }
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
    state.pinnedService = null;
    state.splitRatio = SPLIT_RATIO_DEFAULT;
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

// ---- iframe zoom (rail footer dropdown) ------------------------------------

function loadIframeZoom() {
    const v = parseFloat(localStorage.getItem(IFRAME_ZOOM_KEY) || "");
    return IFRAME_ZOOMS.includes(v) ? v : IFRAME_ZOOM_DEFAULT;
}

function applyIframeZoomVar(z) {
    document.documentElement.style.setProperty("--iframe-zoom", String(z));
}

function setIframeZoom(z) {
    state.iframeZoom = z;
    applyIframeZoomVar(z);
    localStorage.setItem(IFRAME_ZOOM_KEY, String(z));
}

function makeIframeZoomSelector() {
    const sel = document.createElement("select");
    // Shares the .theme-select styling (footer dropdowns look uniform).
    sel.className = "theme-select";
    sel.title = "Editor zoom (affects code-server / any iframe tab)";
    for (const z of IFRAME_ZOOMS) {
        const opt = document.createElement("option");
        opt.value = String(z);
        opt.textContent = `Editor ${Math.round(z * 100)}%`;
        if (Math.abs(z - state.iframeZoom) < 1e-6) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.onchange = () => setIframeZoom(parseFloat(sel.value));
    return sel;
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

// Render-free teardown of a project's in-page state — close its terminals +
// websockets (so a destroyed container stops drawing reconnect attempts), drop
// its cached services, and clear it as active if it was. Callers handle the
// vault entry + re-render. Used when a project is destroyed via Management.
function teardownProjectState(name) {
    for (const k of Object.keys(state.terminals)) {
        if (k.startsWith(`${name}:`)) {
            const t = state.terminals[k];
            try { if (t.ws) t.ws.close(); } catch (_) {}
            try { if (t.term) t.term.dispose(); } catch (_) {}
            delete state.terminals[k];
        }
    }
    delete state.projectServices[name];
    if (state.activeProject === name) {
        state.activeProject = null;
        state.activeService = null;
    }
}

// ---- split pane (W8) -------------------------------------------------------

// Where a new .terminal-instance should be appended given the current pin
// state. The unsplit case returns .terminal-area itself — preserves today's
// DOM exactly.
function paneFor(serviceId) {
    const area = document.getElementById("terminal-area");
    if (!area) return null;
    if (state.pinnedService && serviceId === state.pinnedService) {
        return area.querySelector(".side-pane") || area;
    }
    return area.querySelector(".main-pane") || area;
}

// Mutate .terminal-area between unsplit and split shapes idempotently.
// Called whenever pin state changes or a project becomes active. Existing
// .terminal-instance children get re-parented into the right pane; .hidden
// classes are recomputed for the active project's terminals so the pinned
// one stays visible and the active one shows in the main pane.
function applySplitLayout() {
    const area = document.getElementById("terminal-area");
    if (!area) return;
    const pinned = state.pinnedService;
    const isSplit = area.classList.contains("split");

    if (!pinned && isSplit) {
        // Collapse: hoist pane children back up to .terminal-area, drop wrappers.
        const main = area.querySelector(".main-pane");
        const side = area.querySelector(".side-pane");
        const splitter = area.querySelector(".pane-splitter");
        if (main) while (main.firstChild) area.appendChild(main.firstChild);
        if (side) while (side.firstChild) area.appendChild(side.firstChild);
        if (main) main.remove();
        if (side) side.remove();
        if (splitter) splitter.remove();
        area.classList.remove("split");
        area.style.removeProperty("--split-ratio");
    } else if (pinned && !isSplit) {
        // Expand: wrap existing children into .main-pane, attach splitter + .side-pane.
        const main = el("div", { class: "pane main-pane" });
        const splitter = el("div", { class: "pane-splitter" });
        const side = el("div", { class: "pane side-pane" });
        // Move all area children into main-pane except the floating search bar,
        // which is absolutely positioned and stays at area level.
        const movable = Array.from(area.children).filter(
            (c) => !c.classList.contains("search-bar"),
        );
        for (const c of movable) main.appendChild(c);
        area.appendChild(main);
        area.appendChild(splitter);
        area.appendChild(side);
        area.classList.add("split");
        installSplitterDrag(splitter);
    }

    if (pinned) {
        area.style.setProperty("--split-ratio", `${state.splitRatio * 100}%`);
        const main = area.querySelector(".main-pane");
        const side = area.querySelector(".side-pane");
        const pinnedKey = tkey(state.activeProject, pinned);
        const pinnedT = state.terminals[pinnedKey];
        if (pinnedT && pinnedT.container && side && pinnedT.container.parentElement !== side) {
            side.appendChild(pinnedT.container);
        }
        // Ensure non-pinned containers for the active project live in main-pane.
        for (const [k, t] of Object.entries(state.terminals)) {
            if (k === pinnedKey || !t.container) continue;
            if (!k.startsWith(`${state.activeProject}:`)) continue;
            if (main && t.container.parentElement !== main) main.appendChild(t.container);
        }
    }

    // Recompute .hidden: pinned terminal always visible, plus the active one.
    const activeKey = state.activeService ? tkey(state.activeProject, state.activeService) : null;
    const pinnedKey = pinned ? tkey(state.activeProject, pinned) : null;
    for (const [k, t] of Object.entries(state.terminals)) {
        if (!t.container) continue;
        if (!state.activeProject || !k.startsWith(`${state.activeProject}:`)) continue;
        if (k === pinnedKey || k === activeKey) t.container.classList.remove("hidden");
        else t.container.classList.add("hidden");
    }

    // xterm needs an explicit refit after its container changes size.
    // Iframes reflow via their own ResizeObservers.
    setTimeout(() => {
        for (const t of Object.values(state.terminals)) {
            if (t.fitAddon) { try { t.fitAddon.fit(); } catch (_) {} }
        }
    }, 0);
}

function installSplitterDrag(splitter) {
    // Pointer capture is load-bearing: without it, a pointermove that
    // crosses into an iframe (code-server) gets delivered to the iframe's
    // browsing context instead of bubbling to the document, and the drag
    // appears to freeze until the cursor re-enters the top-bar. Capturing
    // the pointer to the splitter element routes every subsequent move /
    // up for that pointer id to the splitter regardless of what's under
    // the cursor.
    let dragging = false;
    let pointerId = null;
    const onMove = (ev) => {
        if (!dragging) return;
        const area = document.getElementById("terminal-area");
        if (!area) return;
        const rect = area.getBoundingClientRect();
        if (rect.width <= 0) return;
        let ratio = (ev.clientX - rect.left) / rect.width;
        ratio = Math.max(SPLIT_RATIO_MIN, Math.min(SPLIT_RATIO_MAX, ratio));
        area.style.setProperty("--split-ratio", `${ratio * 100}%`);
        state.splitRatio = ratio;
    };
    const onUp = async (ev) => {
        if (!dragging) return;
        dragging = false;
        splitter.classList.remove("dragging");
        try { if (pointerId != null) splitter.releasePointerCapture(pointerId); } catch (_) {}
        pointerId = null;
        splitter.removeEventListener("pointermove", onMove);
        splitter.removeEventListener("pointerup", onUp);
        splitter.removeEventListener("pointercancel", onUp);
        document.body.style.userSelect = "";
        // Refit on drag-end only; per-frame refits during the drag would
        // thrash xterm's measurements.
        setTimeout(() => {
            for (const t of Object.values(state.terminals)) {
                if (t.fitAddon) { try { t.fitAddon.fit(); } catch (_) {} }
            }
        }, 0);
        await persistPinForActiveProject();
    };
    splitter.onpointerdown = (ev) => {
        ev.preventDefault();
        dragging = true;
        pointerId = ev.pointerId;
        splitter.classList.add("dragging");
        try { splitter.setPointerCapture(ev.pointerId); } catch (_) {}
        splitter.addEventListener("pointermove", onMove);
        splitter.addEventListener("pointerup", onUp);
        splitter.addEventListener("pointercancel", onUp);
        document.body.style.userSelect = "none";
    };
}

async function persistPinForActiveProject() {
    if (!state.activeProject) return;
    const project = state.vault.projects.find((p) => p.name === state.activeProject);
    if (!project) return;
    if (state.pinnedService) project.pinned_service = state.pinnedService;
    else delete project.pinned_service;
    if (Math.abs(state.splitRatio - SPLIT_RATIO_DEFAULT) > 1e-6) {
        project.split_ratio = state.splitRatio;
    } else {
        delete project.split_ratio;
    }
    await persistVault();
}

async function togglePin(serviceId) {
    state.pinnedService = state.pinnedService === serviceId ? null : serviceId;
    // Pinning the currently-active service means main pane has nothing
    // to show. Fall back to the first remaining service.
    if (state.pinnedService && state.activeService === state.pinnedService) {
        const enabled = state.projectServices[state.activeProject] || {};
        const fallback = Object.keys(enabled).find((id) => id !== state.pinnedService);
        state.activeService = fallback || null;
    }
    applySplitLayout();
    const enabled = state.projectServices[state.activeProject] || {};
    renderServiceTabs(state.activeProject, enabled);
    if (state.activeService) activateService(state.activeService);
    // Auto-open the pinned service so the side pane isn't empty.
    if (state.pinnedService) {
        const pinnedKey = tkey(state.activeProject, state.pinnedService);
        if (!state.terminals[pinnedKey]) activateService(state.pinnedService);
    }
    await persistPinForActiveProject();
}

// ---- project / service activation ------------------------------------------

async function activateProject(name) {
    closeManagement();   // leaving the management view for a project tab
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

    // Load per-project pin state from the vault entry. Missing fields
    // decode cleanly to "no pin, default ratio" — vault schema stays v1.
    const project = state.vault.projects.find((p) => p.name === name);
    const desiredPin = project?.pinned_service || null;
    // Drop the pin if its service is no longer enabled — e.g. operator
    // disabled it between sessions — or if the user has hidden that tab.
    // Cheaper than a vault migration.
    const hiddenSet = new Set(project?.hidden_services || []);
    state.pinnedService = desiredPin && enabled[desiredPin] && !hiddenSet.has(desiredPin)
        ? desiredPin : null;
    const ratio = project?.split_ratio;
    state.splitRatio = typeof ratio === "number" ? ratio : SPLIT_RATIO_DEFAULT;

    renderServiceTabs(name, enabled);
    applySplitLayout();

    // Pick a default service: keep the previous one if it's still in the
    // enabled set and not pinned, else first always-on (xterm), else first
    // non-pinned entry.
    const visible = visibleServiceIds(name, enabled);
    if (visible.length === 0) {
        state.activeService = null;
        showWelcome();
        return;
    }
    let next = state.activeService;
    if (!enabled[next] || !visible.includes(next) || next === state.pinnedService) {
        next = visible.find((id) => id !== state.pinnedService && enabled[id].always_on)
            || visible.find((id) => id !== state.pinnedService)
            || visible[0];
    }
    activateService(next);

    // Auto-open the pinned service so the side pane isn't empty after
    // a fresh page load with a pin already persisted.
    if (state.pinnedService && state.pinnedService !== next) {
        const pinnedKey = tkey(name, state.pinnedService);
        if (!state.terminals[pinnedKey]) activateService(state.pinnedService);
    }
}

// ---- per-project config box (tab visibility, future settings) --------------

// `hidden_services` on the vault project entry lists service ids the user
// hid via the config box. Visibility is a pure client-side filter — the
// services stay enabled on the supervisor. We intersect against the live
// enabled set (so stale ids from a since-disabled service are harmless) and
// enforce a floor of one: a project never presents an empty tab strip.
function visibleServiceIds(projectName, enabled) {
    const ids = Object.keys(enabled);
    const project = state.vault.projects.find((p) => p.name === projectName);
    const hidden = new Set((project && project.hidden_services) || []);
    const visible = ids.filter((id) => !hidden.has(id));
    return visible.length > 0 ? visible : ids;
}

let projectConfigBox = null;

function closeProjectConfigBox() {
    if (!projectConfigBox) return;
    projectConfigBox.remove();
    projectConfigBox = null;
    document.removeEventListener("pointerdown", onConfigOutsidePointer, true);
}

// Capture-phase outside-click dismiss. Excludes the box itself and any
// config gear (the gear's own click toggles the box; letting this close it
// first would make the gear a no-op while one is open).
function onConfigOutsidePointer(ev) {
    if (!projectConfigBox) return;
    const path = ev.composedPath ? ev.composedPath() : [];
    for (const node of path) {
        if (node === projectConfigBox) return;
        if (node && node.classList && node.classList.contains("project-config-btn")) return;
    }
    closeProjectConfigBox();
}

function openProjectConfigBox(project, anchorEl) {
    // Toggle: a second click on the same row's gear closes the box; a click
    // on a different row's gear swaps to that project's box.
    const wasForThis = projectConfigBox && projectConfigBox.dataset.project === project.name;
    closeProjectConfigBox();
    if (wasForThis) return;

    const box = makeProjectConfigBox(project);
    box.dataset.project = project.name;
    document.body.appendChild(box);
    projectConfigBox = box;

    // position: fixed, anchored under the gear, clamped to the viewport. The
    // rail can shrink to RAIL_WIDTH_MIN, so the box overlays outside the
    // rail rather than trying to fit inside it.
    const r = anchorEl.getBoundingClientRect();
    const bw = box.offsetWidth;
    const bh = box.offsetHeight;
    let left = r.left;
    let top = r.bottom + 4;
    if (left + bw > window.innerWidth - 8) left = window.innerWidth - 8 - bw;
    if (left < 8) left = 8;
    if (top + bh > window.innerHeight - 8) top = Math.max(8, r.top - 4 - bh);
    box.style.left = `${Math.round(left)}px`;
    box.style.top = `${Math.round(top)}px`;

    document.addEventListener("pointerdown", onConfigOutsidePointer, true);
}

function makeProjectConfigBox(project) {
    const box = el("div", { class: "project-config-box" });
    box.appendChild(el("div", { class: "config-title" }, [project.name]));

    const section = el("div", { class: "config-section" });
    section.appendChild(el("div", { class: "config-section-label" }, ["Tabs"]));

    const enabled = state.projectServices[project.name];
    if (!enabled || Object.keys(enabled).length === 0) {
        section.appendChild(el("div", { class: "config-empty" }, [
            "No tabs yet — open this project once to load its services.",
        ]));
        box.appendChild(section);
        return box;
    }

    const hidden = new Set(project.hidden_services || []);
    const rows = [];
    // Floor: when only one tab is left visible, disable that checkbox so it
    // can't be unchecked. The others stay enabled so the user can re-show
    // tabs. A disabled checkbox can't fire change, so the floor is enforced
    // by construction rather than by reverting after the fact.
    const refreshFloor = () => {
        const checked = rows.filter((r) => r.cb.checked).length;
        for (const r of rows) {
            r.cb.disabled = checked <= 1 && r.cb.checked;
            r.label.classList.toggle("floor", r.cb.disabled);
        }
    };

    for (const id of Object.keys(enabled)) {
        const svc = enabled[id];
        const cb = el("input", { type: "checkbox" });
        cb.checked = !hidden.has(id);
        const label = el("label", { class: "config-check" }, [
            cb, el("span", {}, [svc.label || id]),
        ]);
        cb.onchange = async () => {
            await setServiceHidden(project, id, !cb.checked);
            refreshFloor();
        };
        rows.push({ cb, label });
        section.appendChild(label);
    }
    refreshFloor();

    box.appendChild(section);
    box.appendChild(el("div", { class: "config-hint" }, [
        "Hidden tabs stay enabled on the supervisor — this only controls what shows here.",
    ]));
    return box;
}

async function setServiceHidden(project, serviceId, hide) {
    const hidden = new Set(project.hidden_services || []);
    if (hide) hidden.add(serviceId);
    else hidden.delete(serviceId);
    if (hidden.size > 0) project.hidden_services = Array.from(hidden);
    else delete project.hidden_services;

    // Hiding the pinned service unpins it — a tab the user just hid
    // shouldn't keep claiming the side pane.
    if (hide && state.activeProject === project.name && state.pinnedService === serviceId) {
        state.pinnedService = null;
        delete project.pinned_service;
        applySplitLayout();
    }
    await persistVault();

    if (state.activeProject !== project.name) return;
    const enabled = state.projectServices[project.name] || {};
    renderServiceTabs(project.name, enabled);
    // If the active service was just hidden, fall back to a visible one.
    const visible = visibleServiceIds(project.name, enabled);
    if (!visible.includes(state.activeService) && visible.length > 0) {
        activateService(visible[0]);
    }
}

function renderServiceTabs(projectName, enabled) {
    const strip = document.getElementById("service-tabs");
    if (!strip) return;
    strip.innerHTML = "";
    strip.appendChild(makeProjectsTab());
    // Active-project label — visual anchor so the user can tell at a
    // glance which project the visible service tabs belong to. Same
    // font-size as the rail rows, weighted bold.
    strip.appendChild(el("div", { class: "active-project" }, [projectName]));
    const ids = visibleServiceIds(projectName, enabled);
    if (ids.length === 0) {
        strip.appendChild(el("div", { class: "empty" }, [
            "No services enabled for this project.",
        ]));
        return;
    }
    for (const id of ids) {
        const svc = enabled[id];
        const isPinned = id === state.pinnedService;
        const pinBtn = el("button", {
            class: "pin-tab-btn",
            title: isPinned ? "Unpin from side" : "Pin to side",
        }, ["⇥"]);
        pinBtn.onclick = (ev) => { ev.stopPropagation(); togglePin(id); };
        const tab = el("div", {
            class: isPinned ? "tab pinned" : "tab",
            "data-service": id,
            onclick: () => activateService(id),
        }, [el("span", {}, [svc.label || id]), pinBtn]);
        strip.appendChild(tab);
    }
}

function activateService(serviceId) {
    if (!state.activeProject) return;

    const project = state.vault.projects.find((p) => p.name === state.activeProject);
    if (!project) return;
    const enabled = state.projectServices[state.activeProject] || {};
    const svc = enabled[serviceId];
    if (!svc) return;

    const isPinned = serviceId === state.pinnedService;

    // The pinned tab represents the side pane, not main-pane activation,
    // so it never gets the .active underline.
    if (!isPinned) {
        document.querySelectorAll(".service-tabs .tab").forEach((t) => t.classList.remove("active"));
        const tabEl = document.querySelector(`.service-tabs .tab[data-service="${CSS.escape(serviceId)}"]`);
        if (tabEl) tabEl.classList.add("active");
        state.activeService = serviceId;
    }

    // Hide everything except the active and the pinned terminal.
    const activeKey = state.activeService ? tkey(state.activeProject, state.activeService) : null;
    const pinnedKey = state.pinnedService ? tkey(state.activeProject, state.pinnedService) : null;
    for (const [k, t] of Object.entries(state.terminals)) {
        if (!t.container) continue;
        if (k === activeKey || k === pinnedKey) t.container.classList.remove("hidden");
        else t.container.classList.add("hidden");
    }
    const welcome = document.getElementById("welcome");
    if (welcome) welcome.style.display = "none";

    const key = tkey(state.activeProject, serviceId);
    const existing = state.terminals[key];
    if (existing && !existing.disconnected) {
        if (existing.container) existing.container.classList.remove("hidden");
        if (existing.fitAddon) existing.fitAddon.fit();
        if (existing.term) existing.term.focus();
        return;
    }
    if (existing && existing.disconnected) {
        // Tear down the dead terminal so the open path below creates a
        // fresh one. Scroll buffer is lost on reconnect — acceptable.
        try { if (existing.ws) existing.ws.close(); } catch (_) {}
        try { if (existing.term) existing.term.dispose(); } catch (_) {}
        try { if (existing.container) existing.container.remove(); } catch (_) {}
        delete state.terminals[key];
    }

    if (svc.kind === "ssh") {
        openSshTerminal(project, serviceId, svc);
    } else if (svc.kind === "http") {
        openHttpService(project, serviceId, svc);
    } else {
        const parent = paneFor(serviceId) || document.getElementById("terminal-area");
        const placeholder = el("div", { class: "welcome" }, [
            `Unknown service kind: ${svc.kind}`,
        ]);
        parent.appendChild(placeholder);
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
    // Inset wrapper: gives the visual breathing room WITHOUT putting
    // padding on the element xterm-fit measures. See style.css comment
    // on .terminal-pad for the fit-addon quirk this works around.
    const pad = el("div", { class: "terminal-pad" });
    container.appendChild(pad);
    (paneFor(serviceId) || document.getElementById("terminal-area")).appendChild(container);

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
    term.open(pad);
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
        const k = tkey(project.name, serviceId);
        // Mark for teardown on the next activateService(serviceId) — the
        // fast-path early-return would otherwise just re-show the stale,
        // disconnected terminal without reopening the WS.
        if (state.terminals[k]) state.terminals[k].disconnected = true;
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
    const container = el("div", { class: "terminal-instance http-instance" });
    const status = el("div", { class: "http-status" }, ["Authenticating…"]);
    container.appendChild(status);
    (paneFor(serviceId) || document.getElementById("terminal-area")).appendChild(container);

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
    state.railWidth = loadRailWidth();
    applyRailWidth(state.railWidth);
    state.iframeZoom = loadIframeZoom();
    applyIframeZoomVar(state.iframeZoom);
    installRailOutsideClickHandlers();
    if (loadStored()) renderUnlock();
    else renderSetup();
});
