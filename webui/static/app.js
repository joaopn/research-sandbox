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
// Unified login, split derivations: the ONE master password yields (a) the
// vault key (PBKDF2 over the random per-vault salt above — never transmitted)
// and (b) a LOGIN PROOF (PBKDF2 over this fixed public domain-separation
// constant) which is the only thing sent to the webui/broker. Different salts
// mean the server side can never derive the vault key from the proof.
// MIRROR-PAIR LOCKSTEP with cli/broker_auth.py's LOGIN_PROOF_SALT /
// LOGIN_PROOF_ITERATIONS — the broker cannot be imported from here; the bash
// acceptance harness keeps the two implementations honest. Both sides MUST
// stay equal or every login fails.
const LOGIN_PROOF_SALT_STR = "rs-broker-login-v1";
const LOGIN_PROOF_ITERATIONS = 600000;
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
// Mobile shell (bottom-nav single-pane layout). Mode is a device-local
// preference like the theme — localStorage, never the vault: "auto" (absent)
// follows the breakpoint, "desktop"/"mobile" pin it (the Settings Layout
// selector + escape hatch both directions).
const MOBILE_MODE_KEY = "rs-webui-mobile-mode";
// Below this width the desktop chrome is already unusable — a 200px rail +
// 36px tab strip + split panes leave no working terminal area, and every
// control is mouse-sized. 768px is the conventional portrait-tablet/phone
// boundary: portrait phones and small tablets get the mobile shell,
// landscape tablets and desktops keep the full layout.
const MOBILE_BREAKPOINT_PX = 768;

function mobileModeActive() {
    const override = localStorage.getItem(MOBILE_MODE_KEY);
    if (override === "mobile") return true;
    if (override === "desktop") return false;
    return window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT_PX}px)`).matches;
}

// The root class is the single CSS switch: every mobile rule in style.css is
// scoped under html.mobile, so applying/removing it here is what flips the
// stylesheet's personality. Set before the first render (unlock/setup cards
// are styled by it too).
function applyMobileClass() {
    document.documentElement.classList.toggle("mobile", mobileModeActive());
}

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
    loginProof: null,        // string | null — broker login derivation; memory-only
                             // sibling of derivedKey (set at setup/post-decrypt
                             // unlock, cleared on lock, NEVER persisted)
    vault: null,             // { version, projects, settings } | null
    activeProject: null,     // string | null
    hostPage: null,          // "workflows" | "development" | "management" | "settings" | null
    explainIndex: null,      // string[] of workflows with a rendered Explain doc (/static/explain/index.json); null = unfetched
    activeService: null,     // string | null
    serviceRegistry: null,   // { [serviceId]: spec } from /services
    projectServices: {},     // { [projectName]: { [serviceId]: spec } }
    projectLastService: {},  // { [projectName]: serviceId } — per-project landing memory (in-memory; reload/restart resets to the editor default)
    terminals: {},           // "${project}:${service}" -> { term, fitAddon, ws, container, project, service }
    probeTimer: null,
    statusTimer: null,
    servicesTimer: null,
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

// The login proof: base64 of 256 PBKDF2 bits over the fixed public salt.
// Canonical wire form — must match cli/broker_auth.py::derive_login_proof
// byte for byte (standard-alphabet padded base64). Needs its own importKey:
// the vault key's import above grants only ["deriveKey"].
async function deriveLoginProof(password) {
    const enc = new TextEncoder();
    const baseKey = await crypto.subtle.importKey(
        "raw", enc.encode(password), "PBKDF2", false, ["deriveBits"],
    );
    const bits = await crypto.subtle.deriveBits(
        { name: "PBKDF2", salt: enc.encode(LOGIN_PROOF_SALT_STR),
          iterations: LOGIN_PROOF_ITERATIONS, hash: "SHA-256" },
        baseKey, 256,
    );
    return b64(bits);
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
            state.loginProof = await deriveLoginProof(pw1.value);
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
            "One password for everything: it encrypts your saved supervisor credentials and logs you into Management. There is no recovery — if you forget it, you'll need to re-add each project.",
        ]),
        el("p", { class: "hint" }, [
            "For Management, set the same password on the host with ",
            el("code", {}, ["research broker passwd"]),
            " (at least 8 characters).",
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
            // Derive the broker login proof only AFTER decrypt succeeds: a
            // wrong password must never leave a proof behind that a later
            // auto-login would burn against the broker's rate limiter.
            state.loginProof = await deriveLoginProof(pw.value);
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

async function renderDashboard(opts = {}) {
    clearBody();
    await fetchServiceRegistry();

    const rail = makeProjectRail();
    const tabStrip = el("div", { class: "service-tabs", id: "service-tabs" }, [
        makeProjectsTab(),
    ]);
    const termArea = el("div", { class: "terminal-area", id: "terminal-area" });
    termArea.appendChild(el("div", { class: "welcome", id: "welcome" }, [welcomeText()]));

    const main = el("div", { class: "main-area" }, [tabStrip, termArea]);
    // Terminal key bar (mobile only): a flex sibling BELOW the terminal area,
    // hidden until an ssh-kind service is active (updateMobileKeybar).
    if (mobileModeActive()) main.appendChild(makeTermKeybar());
    const dashboard = el("div", { class: "dashboard" }, [rail, main]);
    document.body.appendChild(el("div", { id: "app" }, [dashboard]));

    applyRailState();
    schedulePolling();
    scheduleServicesRefresh();
    // Rail-visibility-gated status polling is started inside applyRailState;
    // no separate kickoff needed here.

    // Project-less strips need the fallback opener chip (softlock guard).
    ensureMobileChip();

    if (state.activeProject) {
        await activateProject(state.activeProject);
    } else if (mobileModeActive()) {
        // No project to land on — open the Projects view so the phone
        // doesn't boot onto a bare welcome with a dead-looking nav.
        setMobileProjectsView(true);
    }

    // Unified login: one best-effort broker login with the unlock-derived
    // proof, AWAITED before the sidebar sync — fired non-blocking, the sync
    // would race the session mint and silently no-op on first unlock. A down
    // broker or a password mismatch is tolerated (Management stays opt-in;
    // the Management page surfaces the mismatch card when opened).
    // skipBrokerLogin: shell re-renders (mobile/desktop mode flip, Settings
    // Layout change) reuse the existing session cookie — re-firing the login
    // per flip would break the once-per-unlock contract below and, on a
    // vault↔broker password mismatch, burn the global login limiter on
    // every resize across the breakpoint. syncSidebarFromBroker is
    // 401-silent, so a dead session just skips the sync.
    if (opts.skipBrokerLogin) {
        syncSidebarFromBroker();
    } else {
        tryBrokerLogin().then(() => syncSidebarFromBroker());
    }
}

// Tear down EVERY terminal (all projects) ahead of a full shell re-render.
// clearBody() detaches the containers; a surviving state.terminals entry
// would then hit activateService's fast-path, un-hide a detached container,
// and leave the terminal area blank until the ws happens to drop. Byobu
// sessions persist server-side, so closed terminals reconnect on the next
// activation (scrollback is lost — accepted for a mode flip).
// Deliberately keeps state.activeProject: the re-render re-activates it.
function teardownAllTerminals() {
    for (const k of Object.keys(state.terminals)) {
        const t = state.terminals[k];
        try { if (t.ws) t.ws.close(); } catch (_) {}
        try { if (t.term) t.term.dispose(); } catch (_) {}
        delete state.terminals[k];
    }
}

// Full shell re-render on a layout-mode change (breakpoint crossing in auto
// mode, or the Settings Layout selector). hostPage is cleared because the
// rebuilt DOM has no open host page — a stale value would make the next
// host-nav tap hit the toggle-closed branch and appear dead.
async function rerenderShell() {
    teardownAllTerminals();
    // The key-bar Ctrl latch must not survive the rebuild: the bar is
    // destroyed without a hide transition here, and on a mobile→desktop flip
    // no new bar exists to ever clear it — the next typed character would be
    // silently transformed into a control char.
    clearKeybarCtrlLatch();
    state.hostPage = null;
    applyMobileClass();
    await renderDashboard({ skipBrokerLogin: true });
}

// Auto mode follows the breakpoint live (window resize / rotation). The
// effective-mode comparison makes this a no-op when a manual override pins
// the layout — mobileModeActive() ignores the media query then.
function installMobileModeWatcher() {
    const mq = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT_PX}px)`);
    mq.addEventListener("change", () => {
        const was = document.documentElement.classList.contains("mobile");
        if (was === mobileModeActive()) return;
        if (!document.querySelector(".dashboard")) { applyMobileClass(); return; }
        rerenderShell();
    });
}

// One POST /broker/login with the in-memory proof. Exactly one attempt per
// call — callers fire it once per unlock / Management open / explicit Retry,
// never in a loop, so it cannot trip the webui's global login rate limiter.
// Returns true on a live management session, false otherwise.
async function tryBrokerLogin() {
    if (!state.loginProof) return false;
    try {
        const res = await fetch("/broker/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ proof: state.loginProof }),
        });
        return res.status === 200;
    } catch (e) {
        return false;   // broker/webui unreachable — Management is opt-in
    }
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
    // Mobile: rail visibility is owned by .mobile-projects (setMobileProjectsView).
    // The desktop expanded/pinned classes must never appear — the floating-
    // overlay rule (.dashboard.expanded:not(.pinned)) would fight the
    // full-area mobile rail on position/z-index. Stripping them here also
    // makes the rail outside-click collapse handlers inert on mobile.
    if (mobileModeActive()) {
        dashboard.classList.remove("pinned", "expanded");
        return;
    }
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
    scheduleServicesRefresh();
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
    const header = el("div", { class: "rail-header" }, [
        el("span", {}, ["Projects"]),
        makePinButton(),
    ]);
    rail.appendChild(header);
    for (const p of state.vault.projects) {
        rail.appendChild(makeProjectRow(p));
    }
    // (Import-an-existing-project moved into the New Project page as a box —
    // openAddProjectModal is reached from there now, not a rail button.)
    rail.appendChild(el("div", { class: "rail-spacer" }));
    // Workflows + Management + Settings sit together at the BOTTOM of the rail —
    // the host-side create + lifecycle + UI surfaces, distinct from the vault
    // bookmarks above.
    rail.appendChild(makeBottomNav());

    // Theme + Editor-zoom moved to the Settings page; the footer keeps Lock vault.
    const footer = el("div", { class: "rail-footer" }, [
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

// The bottom rail nav: Workflows / Management / Settings, each on its OWN line
// (the .rail-nav-group stacks them vertically). Workflows + Management are
// host-broker surfaces; Settings is a local UI page (theme + editor-zoom).
function makeBottomNav() {
    const mk = (cls, icon, label, title, open) => {
        const e = el("div", { class: "nav-entry " + cls, title },
                     [el("span", { class: "nav-icon" }, [icon]), el("span", {}, [label])]);
        e.onclick = (ev) => { ev.stopPropagation(); open(); };
        return e;
    };
    // Explain docs are ephemeral floating popovers now (dismissed on outside
    // click), not persistent rail tabs — so the bottom nav has no doc entries.
    return el("div", { class: "rail-nav-group" }, [
        mk("workflows-entry", "🛍", "New Project",
           "New Project — pick a workflow, or import an existing project", openWorkflows),
        mk("development-entry", "⑂", "Development",
           "Development — dev repos, PRs, fetch commands (shared Gitea)", openDevelopment),
        mk("management-entry", "🗂", "Management",
           "Host project management (broker)", openManagement),
        mk("settings-entry", "⚙", "Settings", "UI settings + software", openSettings),
    ]);
}

// Re-render just the bottom nav group in place (no full rail rebuild) so a host
// page change updates its rail entries immediately.
function refreshBottomNav() {
    const existing = document.querySelector(".rail-nav-group");
    if (existing) existing.replaceWith(makeBottomNav());
    // makeBottomNav builds every entry inactive; re-derive the active highlight
    // from the current host page.
    const hp = state.hostPage;
    if (hp === "workflows" || hp === "management" || hp === "settings") {
        setActiveNavEntry(hp + "-entry");
    }
}

// ---- mobile shell: one top strip + single-pane views ------------------------
// The service-tab strip is the ONLY mobile chrome: it carries the project-name
// chip (tap = toggle the full-screen Projects view, which slides in UNDER the
// strip) plus the WHITELISTED enabled surfaces — enabled ∩ {cli, reader}
// (the whitelist lives in the html.mobile CSS hide rule; code-server / box
// editors / port-<n> stay desktop-only). The strip sits outside every iframe,
// so there is always a reachable escape from an embedded surface. Enabling /
// disabling surfaces happens in the per-row ⚙ config box (Projects view),
// same as desktop; a newly-enabled surface's tab appears on the next services
// poll like any other tab.

// The ONLY mutator of the .mobile-projects class. Status polling is gated on
// "projects list visible" (mobile: this view; desktop: rail pinned/expanded),
// so every visibility change re-derives it here.
function setMobileProjectsView(on) {
    const dashboard = document.querySelector(".dashboard");
    if (!dashboard) return;
    dashboard.classList.toggle("mobile-projects", on);
    scheduleStatusPolling();
    updateMobileKeybar();
    // Close transition: #terminal-area was display:none while the view was
    // open, so any viewport change during it (rotation, soft keyboard) made
    // the global refit a no-op on a 0x0 container — refit the now-visible
    // active terminal. Deferred so the reflow lands first (the applyRailState
    // idiom); harmless duplicate on the activateProject close path.
    if (!on) {
        const t = activeTerminal();
        if (t && t.fitAddon) {
            setTimeout(() => { try { t.fitAddon.fit(); } catch (_) {} }, 0);
        }
    }
}

// The chip's tap handler. Leaves an open host page FIRST: enterHostView
// display:none's #terminal-area, and the Projects view would fight the host
// page over the screen. closeManagement is synchronous (restores the terminal
// area, unhides tabs, nulls hostPage, clears highlights), so leave-then-act
// has no race.
function mobileToggleProjects() {
    if (state.hostPage) {
        // After a host-page leave this tap means "show me the projects" —
        // open, don't toggle.
        closeManagement();
        setMobileProjectsView(true);
        return;
    }
    const dashboard = document.querySelector(".dashboard");
    const on = !!(dashboard && dashboard.classList.contains("mobile-projects"));
    setMobileProjectsView(!on);
}

// The chip is the ONLY drawer opener on mobile, but the real chip is born in
// renderServiceTabs (per-project) and showWelcome's wipe clears the strip —
// so a project-less state (first spawn, zero-visible fallback) would leave
// an EMPTY strip and, combined with the outside-tap close below, a SOFTLOCK:
// the drawer dismissed with nothing left to reopen it. Keep a placeholder
// chip in any project-less strip; idempotent (no-op when a chip exists), and
// renderServiceTabs replaces the whole strip with the real chip on project
// activation.
function ensureMobileChip() {
    if (!mobileModeActive()) return;
    const strip = document.getElementById("service-tabs");
    if (!strip || strip.querySelector(".active-project")) return;
    const chip = el("div", { class: "active-project" }, ["Projects"]);
    chip.onclick = () => {
        if (!mobileModeActive()) return;
        mobileToggleProjects();
    };
    strip.appendChild(chip);
}

// The desktop twin of ensureMobileChip, and it exists for the same softlock.
// The Projects TAB is the only opener of an unpinned (auto-collapsed) rail, it
// is the strip's first child, and it is created ONLY by renderDashboard /
// renderServiceTabs — neither of which runs on activateProject's zero-visible
// landing (a project whose supervisor is down, or whose service probe failed).
// showWelcome's wipe therefore removes it with nothing left to re-create it:
// rail collapsed, opener gone, recovery only by page reload. Guarded + first-
// child so a later renderServiceTabs produces the same DOM order.
function ensureProjectsTab() {
    if (mobileModeActive()) return;          // there the chip is the opener; this tab is CSS-hidden
    const strip = document.getElementById("service-tabs");
    if (!strip || strip.querySelector(".projects-tab")) return;
    strip.insertBefore(makeProjectsTab(), strip.firstChild);
}

// Drawer-scrim semantics: a tap on the exposed service pane beside the open
// drawer CLOSES it instead of typing into a half-visible terminal. Mirrors
// installRailOutsideClickHandlers' exclusion walk — the drawer itself, the
// chip (its own onclick toggles; firing here too would double-toggle), the
// per-row config gear + its floating box, and any modal all keep the drawer
// open. Known accepted edge: taps INSIDE an iframe (reader) don't bubble to
// the document and won't close the drawer.
function installMobileProjectsOutsideClose() {
    document.addEventListener("pointerdown", (ev) => {
        if (!mobileModeActive()) return;
        const dashboard = document.querySelector(".dashboard.mobile-projects");
        if (!dashboard) return;
        const path = ev.composedPath ? ev.composedPath() : [];
        for (const node of path) {
            if (!node || !node.classList) continue;
            if (node.classList.contains("project-rail")) return;
            if (node.classList.contains("active-project")) return;
            if (node.classList.contains("project-config-btn")) return;
            if (node.classList.contains("project-config-box")) return;
            if (node.classList.contains("modal-backdrop")) return;
        }
        setMobileProjectsView(false);
    });
}

// ---- mobile terminal key bar ------------------------------------------------
// Keys the soft keyboard lacks, for driving byobu/claude from a phone:
// Esc · Tab · Ctrl(latch) · arrows · F1-F4 (byobu window nav) · Paste.
// Lives as a .main-area flex sibling BELOW #terminal-area (the terminal
// instances are absolutely positioned INSIDE the area, so the bar must sit
// outside it); created only on mobile, visibility derived by
// updateMobileKeybar. Sends ride the same TextEncoder→ws path as term.onData.

let keybarCtrlLatch = false;

function clearKeybarCtrlLatch() {
    keybarCtrlLatch = false;
    const btn = document.querySelector(".term-keybar .key-ctrl");
    if (btn) btn.classList.remove("active");
}

// One-shot Ctrl, consumed by the NEXT typed character (called from the
// term.onData path). Uppercasing maps a-z into the @-_ control range;
// anything outside it passes through unchanged but still spends the latch —
// a latch that survives would turn a much-later ordinary keystroke into an
// unintended control char (e.g. a stray SIGINT).
function consumeKeybarCtrl(d) {
    if (!keybarCtrlLatch || d.length !== 1) return d;
    clearKeybarCtrlLatch();
    const code = d.toUpperCase().charCodeAt(0);
    if (code >= 64 && code <= 95) return String.fromCharCode(code & 0x1f);
    return d;
}

// Raw-sequence sender for the non-typed keys. Clears the latch first: EVERY
// bar action is "the next key" for the one-shot Ctrl (an Esc/arrow tap must
// not leave the latch armed for a later keystroke).
function keybarSend(seq) {
    clearKeybarCtrlLatch();
    const t = activeTerminal();
    if (t && t.ws && t.ws.readyState === WebSocket.OPEN) {
        t.ws.send(new TextEncoder().encode(seq));
    }
}

function makeTermKeybar() {
    const bar = el("div", { class: "term-keybar hidden" });
    const key = (label, cls, onTap) => {
        const b = el("button", { class: "key " + cls }, [label]);
        // Keep the xterm textarea focused so the soft keyboard stays up.
        b.addEventListener("pointerdown", (ev) => ev.preventDefault());
        b.onclick = onTap;
        return b;
    };
    // Arrows honor DECCKM (byobu/vim set application cursor keys; plain
    // bash doesn't): SS3 in application mode, CSI otherwise.
    const arrow = (letter) => () => {
        const t = activeTerminal();
        const app = !!(t && t.term && t.term.modes && t.term.modes.applicationCursorKeysMode);
        keybarSend((app ? "\x1bO" : "\x1b[") + letter);
    };
    const ss3 = (letter) => () => keybarSend("\x1bO" + letter);
    const ctrlBtn = key("Ctrl", "key-ctrl", () => {
        keybarCtrlLatch = !keybarCtrlLatch;
        ctrlBtn.classList.toggle("active", keybarCtrlLatch);
    });
    const paste = key("Paste", "key-paste", async () => {
        // Spend the latch BEFORE pasting — a 1-char clipboard would otherwise
        // be transformed by the onData consumer.
        clearKeybarCtrlLatch();
        const t = activeTerminal();
        if (!t || !t.term) return;
        try {
            const text = await navigator.clipboard.readText();
            if (text) t.term.paste(text);
        } catch (_) {
            // clipboard permission denied / unavailable — no-op
        }
    });
    // No ←/→ (PI-cut: up/down covers the menu-driving need; two fewer keys
    // is what lets the row fit a phone width and justify instead of scroll).
    bar.append(
        key("Esc", "key-esc", () => keybarSend("\x1b")),
        key("Tab", "key-tab", () => keybarSend("\x09")),
        ctrlBtn,
        key("↓", "key-down", arrow("B")),
        key("↑", "key-up", arrow("A")),
        key("F1", "key-f1", ss3("P")),
        key("F2", "key-f2", ss3("Q")),
        key("F3", "key-f3", ss3("R")),
        key("F4", "key-f4", ss3("S")),
        paste,
    );
    return bar;
}

// Derived visibility: mobile + an ssh-kind active service + no host page +
// Projects view closed. On every visibility CHANGE the active terminal gets
// a deferred refit (#terminal-area gains/loses the bar's height). Early
// return on a missing bar makes every desktop call a no-op.
function updateMobileKeybar() {
    const bar = document.querySelector(".term-keybar");
    if (!bar) return;
    let show = false;
    if (mobileModeActive() && !state.hostPage && state.activeProject && state.activeService) {
        const dashboard = document.querySelector(".dashboard");
        const picking = !!(dashboard && dashboard.classList.contains("mobile-projects"));
        const svc = (state.projectServices[state.activeProject] || {})[state.activeService];
        show = !picking && !!svc && svc.kind === "ssh";
    }
    if (bar.classList.contains("hidden") === !show) return;   // no change
    bar.classList.toggle("hidden", !show);
    if (!show) clearKeybarCtrlLatch();
    const t = activeTerminal();
    if (t && t.fitAddon) {
        setTimeout(() => { try { t.fitAddon.fit(); } catch (_) {} }, 0);
    }
}

// Mobile landing preference — MOBILE-ONLY (the three desktop auto-activation
// sites keep their existing expressions verbatim behind mobileModeActive()
// ternaries). Never returns a visual-surface id other than "reader": those
// tabs are hidden on mobile, and activating one would fill the screen with
// an iframe that has no visible tab.
function mobileCliService(name, visible, enabled) {
    const isCli = (id) => !!enabled[id] && surfaceOf(id, enabled[id]) === "cli";
    const remembered = state.projectLastService[name];
    if (remembered && visible.includes(remembered) && isCli(remembered)) return remembered;
    if (visible.includes("supervisor") && isCli("supervisor")) return "supervisor";
    return visible.find(isCli) || null;
}
function mobileLandingService(name, visible, enabled) {
    const remembered = state.projectLastService[name];
    if (remembered === "reader" && visible.includes("reader")) return "reader";
    const cli = mobileCliService(name, visible, enabled);
    if (cli) return cli;
    return visible.includes("reader") ? "reader" : null;
}

// Shared host-page chrome: a non-project page (Workflows / Management / Settings)
// takes over the main area — hide the terminal AND the per-project service tabs
// (there's no active project context), clear the active project row, and reuse
// the single #management-view pane. closeManagement (called by activateProject)
// reverses it.
function setServiceTabsHidden(hidden) {
    // Hide the service tabs AND the group divider in the global host view —
    // the divider is neither a .tab nor data-service-bearing, so without it
    // a floating rule would strand after the active-project label.
    document.querySelectorAll("#service-tabs .tab[data-service], #service-tabs .tab-group-divider")
        .forEach((t) => { t.style.display = hidden ? "none" : ""; });
}
function setActiveNavEntry(cls) {
    document.querySelectorAll(".rail-nav-group .nav-entry")
        .forEach((e) => e.classList.remove("active"));
    document.querySelectorAll("." + cls).forEach((e) => e.classList.add("active"));
}
function enterHostView() {
    const mainArea = document.querySelector(".main-area");
    if (!mainArea) return null;
    const term = document.getElementById("terminal-area");
    if (term) term.style.display = "none";
    setServiceTabsHidden(true);
    let view = document.getElementById("management-view");
    if (!view) {
        view = el("div", { class: "management-view", id: "management-view" });
        mainArea.appendChild(view);
    }
    view.style.display = "";
    document.querySelectorAll(".project-rail .project").forEach((r) => r.classList.remove("active"));
    // Mobile: host pages are opened from inside the Projects view — swap it
    // out so the two don't fight over the screen (desktop: class never set).
    if (mobileModeActive()) setMobileProjectsView(false);
    return view;
}

function openManagement() {
    if (state.hostPage === "management") return leaveHostView();
    state.hostPage = "management";
    const view = enterHostView();
    if (!view) return;
    setActiveNavEntry("management-entry");
    renderManagementInto(view);
}

function openSettings() {
    if (state.hostPage === "settings") return leaveHostView();
    state.hostPage = "settings";
    const view = enterHostView();
    if (!view) return;
    setActiveNavEntry("settings-entry");
    renderSettingsInto(view);
}

// Clicking the already-open host tab deselects it (temporary-tab feel): drop
// back to the project that was open before, or the welcome area if none.
function leaveHostView() {
    state.hostPage = null;
    if (state.activeProject) {
        activateProject(state.activeProject);
    } else {
        closeManagement();
    }
}

// Settings page: the local UI settings (theme + editor-zoom, client-side) plus
// the broker-gated Software section (dists / image fleet / pins + build lane),
// folded in here rather than a separate rail entry. The software section renders
// into its OWN sub-container so its self-contained fetch+gate+build machinery
// (renderSoftwareInto) never disturbs the local settings above it.
function renderSettingsInto(view) {
    view.innerHTML = "";
    const swSub = el("div", { class: "sw-embed" });
    view.appendChild(el("div", { class: "settings-screen" }, [
        el("h2", { class: "workflows-title" }, ["Settings"]),
        el("div", { class: "field" }, [
            el("label", {}, ["Theme"]),
            makeThemeSelector(),
        ]),
        el("div", { class: "field" }, [
            el("label", {}, ["Layout"]),
            makeLayoutSelector(),
        ]),
        el("div", { class: "field" }, [
            el("label", {}, ["Editor zoom"]),
            makeIframeZoomSelector(),
        ]),
        swSub,
    ]));
    renderSoftwareInto(swSub);   // async, fire-and-forget; fills swSub when ready
}

// Software / images host page — read-only status of the host's agent/editor
// dists, built image fleet, and effective version pins (base + local override),
// via the login-gated broker `software_status` read. Pull/rebuild/refresh (the
// write side) land in later slices; this slice surfaces status only.
// No live Management session (rare — Management auto-logs-in at vault unlock): a
// LIGHTWEIGHT note in the section rather than the full login card auto-embedded in
// Settings. The "Log in" button shows the login on demand (into this same
// sub-container), so the full card only appears on an explicit click.
function renderSoftwareLoginNote(view) {
    view.innerHTML = "";
    const login = el("button", { class: "btn-small" }, ["Log in"]);
    login.onclick = () => renderMgmtLogin(view, renderSoftwareInto);
    view.appendChild(el("div", { class: "sw-embed-note" }, [
        el("span", {}, ["Log in to Management to manage software (dists, images, pins)."]),
        login,
    ]));
}

async function renderSoftwareInto(view) {
    view.innerHTML = "";
    view.appendChild(el("div", { class: "mgmt-loading" }, ["Loading software status…"]));
    let res;
    try {
        res = await fetch("/broker/software");
    } catch (e) { return renderMgmtUnavailable(view); }
    if (res.status === 401) return renderSoftwareLoginNote(view);
    if (res.status === 403) return renderMgmtRejected(view);
    if (res.status === 503) return renderMgmtUnavailable(view);
    let body;
    try { body = await res.json(); } catch (e) { return renderMgmtUnavailable(view); }
    if (!res.ok || !body.ok || !body.result) {
        return renderMgmtVerbError(view, body, renderSoftwareInto);
    }
    renderSoftwareScreen(view, body.result);
}

function renderSoftwareScreen(view, result) {
    view.innerHTML = "";
    const agents = Array.isArray(result.agents) ? result.agents : [];
    const editor = result.editor || {};
    const reader = result.reader || {};
    const images = Array.isArray(result.images) ? result.images : [];
    const pins = Array.isArray(result.pins) ? result.pins : [];
    const dockerOk = !!result.docker_ok;

    const refresh = el("button", { class: "btn-small" }, ["Refresh"]);
    refresh.onclick = () => renderSoftwareInto(view);
    const rebuildBtn = el("button", { class: "btn-small" }, ["Rebuild all"]);
    rebuildBtn.onclick = () => mgmtBuildDialog(view, {
        title: "Rebuild the image fleet",
        tailTitle: "Rebuilding the image fleet",
        confirmLabel: "Rebuild all",
        payload: { verb: "rebuild" },
        body: [el("p", {}, ["Rebuild every image at the current effective pins — a long operation (often 10–15 min), one image at a time on the host. The rest of the webui stays responsive while it runs."])],
    });
    view.appendChild(el("div", { class: "mgmt-header" }, [
        el("h2", {}, ["Software — dists, images, pins"]),
        el("div", { class: "mgmt-toolbar" }, [refresh, rebuildBtn]),
    ]));

    const cell = (v, cls) =>
        el("span", cls ? { class: cls } : {}, [v == null || v === "" ? "—" : String(v)]);
    const distStatus = (d) => {
        if (!d.present) return el("span", { class: "sw-muted" }, ["not pulled"]);
        if (d.effective_pin == null) return el("span", { class: "sw-muted" }, ["cached"]);
        return d.matches_pin
            ? el("span", { class: "sw-ok" }, ["up to date"])
            : el("span", { class: "sw-warn" }, ["stale — re-pull"]);
    };
    const pullBtn = (payload, tip) => {
        const b = el("button", { class: "btn-small" }, ["Pull"]);
        b.onclick = () => mgmtBuildDialog(view, {
            title: "Pull dist", tailTitle: "Pulling dist", confirmLabel: "Pull",
            payload: payload, body: [el("p", {}, [tip])],
        });
        return b;
    };
    // Refresh = preview upstream, then (only if newer) bump the untracked override
    // pin + re-pull via the build lane. distLabel names the dist in the dialog.
    const refreshBtn = (distLabel, checkPayload, buildPayload) => {
        const b = el("button", { class: "btn-small" }, ["Refresh"]);
        b.onclick = () => mgmtRefreshDialog(view, {
            title: "Refresh " + distLabel, tailTitle: "Refreshing " + distLabel,
            distLabel: distLabel, checkPayload: checkPayload, buildPayload: buildPayload,
        });
        return b;
    };

    // --- Dists (agents + the editor) ---
    const distRows = [el("div", { class: "sw-row sw-row-head" }, [
        el("span", {}, ["Dist"]), el("span", {}, ["Present"]),
        el("span", {}, ["Cached"]), el("span", {}, ["Effective pin"]),
        el("span", {}, ["Status"]), el("span", {}, [""]),
    ])];
    for (const a of agents) {
        distRows.push(el("div", { class: "sw-row" }, [
            el("span", { class: "sw-name" }, ["agent: " + a.agent]),
            cell(a.present ? "yes" : "no"),
            cell(a.cached_version, "sw-mono"),
            cell(a.effective_pin, "sw-mono"),
            distStatus(a),
            el("span", { class: "sw-act" }, [
                pullBtn(
                    { verb: "agent_pull", agent: a.agent },
                    "Rebuild this agent dist at the effective pin, in a throwaway build container (a few minutes)."),
                refreshBtn(
                    "agent: " + a.agent,
                    { verb: "agent_refresh_check", agent: a.agent },
                    { verb: "agent_refresh", agent: a.agent }),
            ]),
        ]));
    }
    const nExt = Object.keys(editor.extensions || {}).length;
    distRows.push(el("div", { class: "sw-row" }, [
        el("span", { class: "sw-name" }, ["editor: code-server" + (nExt ? ` (+${nExt} ext)` : "")]),
        cell(editor.present ? "yes" : "no"),
        cell(editor.cached_version, "sw-mono"),
        cell(editor.effective_pin, "sw-mono"),
        distStatus(editor),
        el("span", { class: "sw-act" }, [
            pullBtn(
                { verb: "editor_pull" },
                "Rebuild the editor dist at the effective pin, in a throwaway build container (a few minutes)."),
            refreshBtn(
                "editor: code-server",
                { verb: "editor_refresh_check" },
                { verb: "editor_refresh" }),
        ]),
    ]));
    // Reader dist (STAGE_READER): nbconvert is the primary/refreshable pin;
    // markdown rides bundled. The cell shows the nbconvert version.
    distRows.push(el("div", { class: "sw-row" }, [
        el("span", { class: "sw-name" }, ["reader: nbconvert"
            + (reader.markdown_version ? ` (+md ${reader.markdown_version})` : "")]),
        cell(reader.present ? "yes" : "no"),
        cell(reader.cached_version, "sw-mono"),
        cell(reader.effective_pin, "sw-mono"),
        distStatus(reader),
        el("span", { class: "sw-act" }, [
            pullBtn(
                { verb: "reader_pull" },
                "Rebuild the reader dist (nbconvert + markdown) at the effective pins, in a throwaway build container (a few minutes)."),
            refreshBtn(
                "reader: nbconvert",
                { verb: "reader_refresh_check" },
                { verb: "reader_refresh" }),
        ]),
    ]));
    view.appendChild(el("div", { class: "sw-section" }, [
        el("h3", {}, ["Dists"]),
        el("div", { class: "sw-table sw-dists" }, distRows),
    ]));

    // --- Image fleet ---
    const imgChildren = [el("h3", {}, ["Image fleet"])];
    if (!dockerOk) {
        imgChildren.push(el("div", { class: "sw-banner" },
            ["Docker unreachable — image presence can't be read on this host."]));
    } else {
        const imgRows = [el("div", { class: "sw-row sw-row-head" }, [
            el("span", {}, ["Image"]), el("span", {}, ["Built"]),
        ])];
        for (const im of images) {
            imgRows.push(el("div", { class: "sw-row" }, [
                el("span", { class: "sw-mono" }, [im.tag]),
                im.present
                    ? el("span", { class: "sw-ok" }, ["present"])
                    : el("span", { class: "sw-warn" }, ["absent"]),
            ]));
        }
        imgChildren.push(el("div", { class: "sw-table sw-images" }, imgRows));
    }
    view.appendChild(el("div", { class: "sw-section" }, imgChildren));

    // --- Version pins (effective: base overlaid with the local override) ---
    const pinRows = [el("div", { class: "sw-row sw-row-head" }, [
        el("span", {}, ["Pin"]), el("span", {}, ["Value"]), el("span", {}, ["Source"]),
    ])];
    for (const p of pins) {
        pinRows.push(el("div", { class: "sw-row" }, [
            el("span", { class: "sw-mono" }, [p.key]),
            el("span", { class: "sw-mono" }, [p.value]),
            p.override
                ? el("span", { class: "sw-badge override", title: "from versions.local.env" }, ["local"])
                : el("span", { class: "sw-badge base", title: "from versions.env" }, ["base"]),
        ]));
    }
    view.appendChild(el("div", { class: "sw-section" }, [
        el("h3", {}, ["Version pins"]),
        el("div", { class: "sw-table sw-pins" }, pinRows),
    ]));
}

function closeManagement() {
    state.hostPage = null;
    const view = document.getElementById("management-view");
    if (view) view.style.display = "none";
    const term = document.getElementById("terminal-area");
    if (term) term.style.display = "";
    setServiceTabsHidden(false);
    document.querySelectorAll(".rail-nav-group .nav-entry")
        .forEach((e) => e.classList.remove("active"));
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
    if (!res.ok || !body.ok) {
        return renderMgmtVerbError(view, body, renderManagementInto);
    }
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

// Unified login: no Management password form — the proof derived from the
// master password at unlock is auto-submitted, ONE attempt per call (a call
// happens per Management/Workflows open on 401 and per explicit Retry click,
// never in a loop, so the global login rate limiter can't be tripped). A 401
// here means the broker's stored secret was set to a DIFFERENT password than
// the vault's — surfaced as a mismatch card; the fix is host-side. `onSuccess`
// preserves the caller's landing page exactly as before (the Workflows 401
// path routes back to Workflows, not the Management table).
async function renderMgmtLogin(view, onSuccess) {
    const route = onSuccess || renderManagementInto;
    mgmtCard(view, [el("div", { class: "mgmt-loading" }, ["Connecting to Management…"])]);
    let res = null;
    if (state.loginProof) {
        try {
            res = await fetch("/broker/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ proof: state.loginProof }),
            });
        } catch (e) { return renderMgmtUnavailable(view); }
    }
    if (res && res.status === 200) return route(view);
    if (res && res.status === 403) return renderMgmtRejected(view);
    if (res && res.status === 503) return renderMgmtUnavailable(view);
    // 401 (password mismatch), 429 (rate-limited), or a missing proof:
    // explain + a manual Retry (each click = one limiter-visible attempt).
    const retry = el("button", { class: "btn" }, ["Retry"]);
    retry.onclick = () => renderMgmtLogin(view, onSuccess);
    let msg;
    if (res && res.status === 429) {
        const ra = res.headers.get("Retry-After");
        msg = el("p", {}, [`Too many login attempts. Wait ${ra || "a moment"}s, then retry.`]);
    } else {
        msg = el("p", {}, [
            "Your master password doesn't match the broker's operator password. ",
            "On the host, run ",
            el("code", {}, ["research broker passwd"]),
            " and enter your vault (master) password — then retry.",
        ]);
    }
    mgmtCard(view, [
        el("h2", {}, ["Management login failed"]),
        msg,
        el("div", { class: "btn-row" }, [retry]),
    ]);
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

// The broker ANSWERED and the verb refused or failed — a clean {ok:false,
// error:{kind,message}} envelope, which arrives with HTTP 200 (the relay maps
// its own failures to 401/403/503, peeled off before this). Rendering that as
// "the broker isn't reachable" sends the operator to restart a daemon that is
// perfectly fine and hides the real reason — the single most misleading signal
// on the debugging path. Show the actual error, and offer a retry.
function renderMgmtVerbError(view, body, retry) {
    const again = el("button", { class: "btn btn-secondary" }, ["Retry"]);
    again.onclick = () => retry(view);
    mgmtCard(view, [
        el("h2", {}, ["Couldn’t load this page"]),
        el("p", { class: "error" }, [mgmtErrText(body)]),
        el("div", { class: "hint" }, [
            "The broker is reachable — it refused or failed this request.",
        ]),
        el("div", { class: "btn-row" }, [again]),
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
    // The Workflows page is the create entry point now (the workflow picker is
    // its card grid); this button just routes there. mgmtCreateDialog is only
    // ever opened from a workflows card, with a chosen workflow manifest.
    const create = el("button", { class: "btn-small" }, ["+ New project"]);
    create.onclick = () => openWorkflows();
    const refresh = el("button", { class: "btn-small" }, ["Refresh"]);
    refresh.onclick = () => renderManagementInto(view);
    // No Log-out control: under unified login the session model is "locked
    // vault = logged out" — Lock vault (rail footer) revokes the broker
    // session; a separate Management logout would just auto-re-login.
    view.appendChild(el("div", { class: "mgmt-header" }, [
        el("h2", {}, ["Management — host projects (live)"]),
        el("div", { class: "mgmt-toolbar" }, [create, refresh]),
    ]));
    if (projects.length === 0) {
        view.appendChild(el("div", { class: "mgmt-empty" }, ["No projects on this host."]));
        appendInfraSection(view);   // async, fire-and-forget
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
    appendInfraSection(view);   // async, fire-and-forget (its own sub-block)
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
        setTypeBadge(ref.badge, st && st.workflow);
    }
}

// Paint a project badge: the WORKFLOW the user picked (sandbox/research/sandbox-dind/
// BYO) + a colour keyed off the label, or empty (collapsed) when unknown. We
// label by workflow, not the derived flavour, so a docker `sandbox` box reads
// "sandbox" instead of mislabelling as "research" (substrate stays hidden, Q7).
// Legacy markers without a workflow fall back to the flavour string server-side.
function setTypeBadge(elm, label) {
    const cls = label === "research" ? " type-research"
        : (label === "sandbox-dind" || label === "sandbox") ? " type-sandbox"
        : label ? " type-box" : "";
    elm.className = "type-badge" + cls;
    elm.textContent = label || "";
}

// ---- Development host page (STAGE_DEV_GITEA S3) ----------------------------
// Two sub-tabs: "Gitea" (the gitea web UI on its own origin port, session
// minted via the Management-anchored /broker/dev/gitea-session) and "Fetch" (the
// RS-rendered open-PR/branch list with click-to-copy rs-fetch commands, fed by
// one /broker/dev read per open — Q6, no poller). The Fetch tab is the
// guaranteed copy surface; the agent's final-comment code block inside Gitea
// (with Gitea's own copy button) is the convenience layer.

let devActiveTab = "gitea";

function openDevelopment() {
    if (state.hostPage === "development") return leaveHostView();
    state.hostPage = "development";
    const view = enterHostView();
    if (!view) return;
    setActiveNavEntry("development-entry");
    renderDevelopmentInto(view);
}

function renderDevelopmentInto(view) {
    view.innerHTML = "";
    const body = el("div", { class: "dev-body" });
    const tabGitea = el("button", { class: "btn-small dev-tab-btn" }, ["Gitea"]);
    const tabFetch = el("button", { class: "btn-small dev-tab-btn" }, ["Fetch"]);
    const mark = () => {
        tabGitea.classList.toggle("active", devActiveTab === "gitea");
        tabFetch.classList.toggle("active", devActiveTab === "fetch");
    };
    tabGitea.onclick = () => { devActiveTab = "gitea"; mark(); renderDevGiteaTab(view, body); };
    tabFetch.onclick = () => { devActiveTab = "fetch"; mark(); renderDevFetchTab(view, body); };
    view.appendChild(el("div", { class: "mgmt-header" }, [
        el("h2", {}, ["Development"]),
        el("div", { class: "mgmt-toolbar dev-tabs" }, [tabGitea, tabFetch]),
    ]));
    view.appendChild(body);
    mark();
    if (devActiveTab === "fetch") renderDevFetchTab(view, body);
    else renderDevGiteaTab(view, body);
}

async function renderDevGiteaTab(view, body) {
    body.innerHTML = "";
    body.appendChild(el("div", { class: "mgmt-loading" }, ["Opening Gitea…"]));
    // Gate on gitea RUNNING before minting a session / loading the iframe — a
    // stopped gitea would otherwise proxy the iframe into an unreachable
    // upstream (the _proxy_http 502 is the backstop; this is the graceful path).
    let dev;
    try {
        const r = await fetch("/broker/dev");
        if (r.status === 401) return renderMgmtLogin(view, renderDevelopmentInto);
        dev = await r.json();
    } catch (e) { return renderMgmtUnavailable(body); }
    const gstate = (dev && dev.ok && dev.result && dev.result.gitea) || {};
    if (!gstate.running) {
        body.innerHTML = "";
        const enable = el("button", { class: "btn-small" }, ["Enable Gitea"]);
        enable.onclick = () =>
            devEnableGiteaDialog(view, () => renderDevGiteaTab(view, body));
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, [gstate.exists
                ? "Gitea is stopped. Enable it to open the web UI. "
                : "Gitea isn't enabled yet. Enable it under Management → " +
                  "Infrastructure, or here: "]),
            enable,
        ]));
        return;
    }
    let res;
    try {
        // /broker/-prefixed: the Management session cookie is Path=/broker.
        res = await fetch("/broker/dev/gitea-session", { method: "POST" });
    } catch (e) { return renderMgmtUnavailable(body); }
    if (res.status === 401) return renderMgmtLogin(view, renderDevelopmentInto);
    if (res.status === 403) return renderMgmtRejected(body);
    let data;
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok || !data.ok || !data.url) {
        // The running-gate above already passed, so this is a race (gitea
        // stopped mid-flight) or a session hiccup — offer Enable + a retry,
        // never the stale "add a repo first" remedy.
        body.innerHTML = "";
        const enable = el("button", { class: "btn-small" }, ["Enable Gitea"]);
        enable.onclick = () =>
            devEnableGiteaDialog(view, () => renderDevGiteaTab(view, body));
        const retry = el("button", { class: "btn-small" }, ["Retry"]);
        retry.onclick = () => renderDevGiteaTab(view, body);
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, ["Gitea isn't reachable right now. "]), enable, retry,
        ]));
        return;
    }
    body.innerHTML = "";
    // clipboard-write delegation is what lets Gitea's native code-block copy
    // button work inside the cross-origin frame (Permissions-Policy features
    // whose default allowlist is 'self' are silently revoked otherwise).
    const iframe = el("iframe", {
        class: "dev-gitea-frame",
        src: data.url,
        allow: "fullscreen; clipboard-read; clipboard-write",
    });
    iframe.setAttribute("allowfullscreen", "");
    body.appendChild(iframe);
}

function devCopyBtn(cmd) {
    const b = el("button", { class: "btn-small dev-copy", title: "Copy: " + cmd },
                 ["📋"]);
    b.onclick = async () => {
        try { await navigator.clipboard.writeText(cmd); } catch (e) { return; }
        b.textContent = "✓";
        setTimeout(() => { b.textContent = "📋"; }, 1200);
    };
    return b;
}

// One PR review on the broker's parallel detached lane (STAGE_DEV_GITEA S4):
// POST /broker/dev/review → {op_id} → stream the op via the build-tail modal
// (build_alive covers review ops). Advisory — the verdict lands in the host
// ledger and shows as a badge/panel after the Fetch-tab re-read on close.
async function devReviewDialog(view, body, repo, pr) {
    if (document.querySelector(".modal-backdrop")) return;
    const backdrop = el("div", { class: "modal-backdrop" });
    const card = el("div", { class: "card sw-build-card" }, [
        el("h2", {}, [`Review ${repo} #${pr}`]),
        el("div", { class: "mgmt-loading" }, ["Starting the sandboxed reviewer…"]),
    ]);
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    let res;
    try {
        res = await fetch("/broker/dev/review", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ repo: repo, pr: pr }),
        });
    } catch (e) {
        backdrop.remove();
        return renderMgmtUnavailable(body);
    }
    const redirect = mgmtStatusRedirect(view, res.status);
    if (redirect) { backdrop.remove(); return redirect(); }
    let b; try { b = await res.json(); } catch (e) { b = {}; }
    if (!b.ok || !b.op_id) {
        card.innerHTML = "";
        card.appendChild(el("h2", {}, [`Review ${repo} #${pr}`]));
        card.appendChild(el("div", { class: "error" },
                            ["Could not start the review: " + mgmtErrText(b)]));
        const close = el("button", { class: "btn btn-secondary" }, ["Close"]);
        close.onclick = () => backdrop.remove();
        card.appendChild(el("div", { class: "btn-row" }, [close]));
        return;
    }
    await mgmtTailBuildLog(view, backdrop, card, b.op_id,
                           `Review ${repo} #${pr}`,
                           () => renderDevFetchTab(view, body));
}

async function renderDevFetchTab(view, body) {
    body.innerHTML = "";
    body.appendChild(el("div", { class: "mgmt-loading" }, ["Loading dev repos…"]));
    let res;
    try {
        res = await fetch("/broker/dev");
    } catch (e) { return renderMgmtUnavailable(body); }
    if (res.status === 401) return renderMgmtLogin(view, renderDevelopmentInto);
    if (res.status === 403) return renderMgmtRejected(body);
    if (res.status === 503) return renderMgmtUnavailable(body);
    let data;
    try { data = await res.json(); } catch (e) { return renderMgmtUnavailable(body); }
    if (!res.ok || !data.ok || !data.result) {
        body.innerHTML = "";
        body.appendChild(el("div", { class: "mgmt-empty" },
                            [mgmtErrText(data) || "Could not load dev status."]));
        return;
    }
    renderDevFetchScreen(view, body, data.result);
}

function renderDevFetchScreen(view, body, result) {
    body.innerHTML = "";
    const gitea = result.gitea || {};
    const repos = Array.isArray(result.repos) ? result.repos : [];
    const attachments = Array.isArray(result.attachments) ? result.attachments : [];
    if (!gitea.exists) {
        const enable = el("button", { class: "btn-small" }, ["Enable Gitea"]);
        enable.onclick = () =>
            devEnableGiteaDialog(view, () => renderDevFetchTab(view, body));
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, ["The dev lane isn't enabled yet. "]), enable,
        ]));
        return;
    }
    if (!gitea.running) {
        const enable = el("button", { class: "btn-small" }, ["Enable Gitea"]);
        enable.onclick = () =>
            devEnableGiteaDialog(view, () => renderDevFetchTab(view, body));
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, ["Gitea is stopped. "]), enable,
        ]));
        return;
    }
    if (!repos.length) {
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            "No dev repos yet. A repo is mirrored here when you create a dev "
            + "project from a GitHub URL (Workflows → Dev), or add a dev box to "
            + "an existing project.",
        ]));
        return;
    }
    for (const r of repos) {
        const prs = Array.isArray(r.prs) ? r.prs : [];
        const branches = Array.isArray(r.branches) ? r.branches : [];
        const attached = attachments.filter((a) => a.repo === r.repo)
                                    .map((a) => a.project);
        const sync = el("button", { class: "btn-small" }, ["Sync"]);
        sync.onclick = async () => {
            sync.disabled = true;
            sync.textContent = "Syncing…";
            try {
                await fetch("/broker/dev/sync", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ repo: r.repo }),
                });
            } catch (e) { /* re-render reports the live state */ }
            renderDevFetchTab(view, body);
        };
        const meta = (r.private ? "private · " : "")
            + `${prs.length} open PR${prs.length === 1 ? "" : "s"} · `
            + `${branches.length} branch${branches.length === 1 ? "" : "es"}`
            + (r.mirror_synced_at ? ` · synced ${r.mirror_synced_at}` : "");
        // Active-fork control (per-consumer forks): a plain badge with one
        // live fork; a dropdown at ≥2 — the Management-steered GLOBAL
        // selector (PR list, rs-fetch and reviews all follow it).
        const forks = Array.isArray(r.forks) ? r.forks : [];
        const live = forks.filter((f) => f && !f.archived && f.user);
        let forkEl = null;
        if (live.length >= 2) {
            forkEl = el("select", { class: "dev-fork-select" },
                live.map((f) => {
                    const o = el("option", { value: f.user }, [f.user]);
                    if (f.user === r.active) o.selected = true;
                    return o;
                }));
            forkEl.onchange = async () => {
                forkEl.disabled = true;
                try {
                    await fetch("/broker/dev/active-fork", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ repo: r.repo, user: forkEl.value }),
                    });
                } catch (e) { /* re-render reports the live state */ }
                renderDevFetchTab(view, body);
            };
        } else if (r.active) {
            forkEl = el("span", { class: "dev-repo-meta" }, [`fork: ${r.active}`]);
        }
        // Remove is offered only for a FINISHED repo — no live fork, nobody
        // working it. Retiring a consumer (destroying its project, removing its
        // box) archives the fork, which is what releases the repo. This disable
        // is an affordance only: the broker's gate is the authority (it re-reads
        // the LIVE fork state and fails closed), so a stale page cannot delete.
        const blocker = live.length
            ? `still has a live agent fork (${live.map((f) => f.user).join(", ")})`
            : (attached.length
                ? `still worked by ${attached.join(", ")}`
                : "");
        const remove = el("button", {
            class: "btn-small btn-danger",
            title: blocker
                ? `Can't remove — ${blocker}. Delete the dev project or box first.`
                : "Delete this repo's mirror and its retired forks",
        }, ["Remove"]);
        remove.disabled = !!blocker;
        remove.onclick = () => devRepoRemoveDialog(view, body, r.repo);
        const rows = [el("div", { class: "dev-repo-head" }, [
            el("span", { class: "dev-repo-name" }, [r.repo]),
            el("span", { class: "dev-repo-meta" }, [meta]),
            ...(forkEl ? [forkEl] : []),
            sync,
            remove,
        ])];
        const reviews = (r.reviews && typeof r.reviews === "object") ? r.reviews : {};
        for (const p of prs) {
            const cells = [
                devCopyBtn(`rs-fetch ${r.repo} --pr ${p.number}`),
                el("span", { class: "dev-pr-id" }, [`#${p.number}`]),
                el("span", { class: "dev-pr-title" }, [p.title || ""]),
                el("span", { class: "dev-pr-meta" },
                   [`[${p.head || "?"}] ${p.updated_at || ""}`]),
            ];
            // Review verdicts (S4): ledger entries keyed by str(pr) — JS
            // property lookup coerces p.number across the int/str boundary.
            // All verdict strings are MODEL OUTPUT → text nodes only.
            const v = reviews[p.number];
            const reviewBtn = (label) => {
                const btn = el("button", { class: "btn-small dev-review-btn" },
                               [label]);
                btn.onclick = () => {
                    btn.disabled = true;
                    devReviewDialog(view, body, r.repo, p.number);
                };
                return btn;
            };
            let panel = null;
            if (!v) {
                cells.push(reviewBtn("Review"));
            } else if (v.status === "ok") {
                const stale = !!(v.head_sha && p.sha && v.head_sha !== p.sha);
                const badge = el("button", {
                    class: "btn-small dev-review-badge",
                    title: "show the review verdict",
                }, ["reviewed ✓" + (v.risk ? ` · ${v.risk}` : "")
                    + (stale ? " · stale" : "")]);
                panel = el("div", { class: "dev-verdict-panel" });
                panel.style.display = "none";
                panel.appendChild(el("div", { class: "dev-pr-meta" }, [
                    `reviewed ${v.reviewed_at || ""}`
                    + (stale ? " — the PR has new commits since this review" : ""),
                ]));
                if (v.summary) {
                    panel.appendChild(el("div", { class: "dev-verdict-summary" },
                                         [v.summary]));
                }
                const findings = Array.isArray(v.findings) ? v.findings : [];
                for (const f of findings) {
                    if (!f || typeof f !== "object") continue;
                    panel.appendChild(el("div", { class: "dev-verdict-finding" },
                        ["• " + (f.file ? f.file + ": " : "") + (f.note || "")]));
                }
                panel.appendChild(reviewBtn("Re-review"));
                badge.onclick = () => {
                    panel.style.display =
                        panel.style.display === "none" ? "" : "none";
                };
                cells.push(badge);
            } else {
                cells.push(el("span", { class: "dev-pr-meta dev-review-failed" },
                    ["review failed" + (v.reason ? `: ${v.reason}` : "")]));
                cells.push(reviewBtn("Retry"));
            }
            rows.push(el("div", { class: "dev-pr-row" }, cells));
            if (panel) rows.push(panel);
        }
        if (!prs.length) {
            rows.push(el("div", { class: "config-empty" }, ["No open PRs."]));
        }
        for (const b of branches) {
            rows.push(el("div", { class: "dev-branch-row" }, [
                devCopyBtn(`rs-fetch ${r.repo} --branch ${b.name}`),
                el("span", { class: "dev-pr-title" }, [b.name || ""]),
                el("span", { class: "dev-pr-meta" }, [b.committed_at || ""]),
            ]));
        }
        if (attached.length) {
            rows.push(el("div", { class: "dev-pr-meta dev-attached" },
                         ["worked by: " + attached.join(", ")]));
        }
        body.appendChild(el("div", { class: "card dev-repo-card" }, rows));
    }
    body.appendChild(el("div", { class: "hint" }, [
        "📋 copies the rs-fetch command — paste it in any project or box ",
        "terminal (every container carries rs-fetch + read-only fetch access).",
    ]));
}

// Delete a finished repo: its gitea mirror + every retired agent fork (history
// included) + their tokens + the host stamp. STEP-UP gated like destroy and
// box-remove — a stolen session cookie must not suffice. The broker re-verifies
// the fork state and refuses on a live fork (and fails closed if it cannot read
// it), so this dialog's own disable is only an affordance.
function devRepoRemoveDialog(view, body, repo) {
    const pwI = el("input", { type: "password", autocomplete: "current-password" });
    mgmtConfirmThenTail(view, {
        title: `Remove ${repo}`,
        tailTitle: `Removing ${repo}`,
        verb: "dev_repo_remove",
        confirmLabel: "Remove",
        danger: true,
        body: [
            el("p", {}, [
                `This deletes "${repo}" from Gitea: the mirror, every retired `
                + "agent fork (including its commit history), their tokens, and "
                + "the local record. It cannot be undone. The GitHub original is "
                + "untouched.",
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Re-enter your master password"]), pwI,
            ]),
        ],
        validate: () => (pwI.value ? null : "Re-enter your master password."),
        // Step-up: the retyped password is derived client-side and only the
        // proof rides the request; the broker verifies it async, so a wrong
        // password surfaces as a FAILED op (the destroy / box-remove semantics).
        request: async () => fetch("/broker/dev/repo-remove", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                repo: repo,
                proof: await deriveLoginProof(pwI.value),
            }),
        }),
        onDone: (ok) => { if (ok) renderDevFetchTab(view, body); },
        focus: () => pwI.focus(),
    });
}

// Management → Infrastructure (STAGE_DEV_GITEA S3): shared host services that
// aren't projects. Gitea only, for now — status + an explicit Start button (a
// page READ never starts it; this button is the deliberate action).
async function appendInfraSection(view) {
    const row = el("div", { class: "mgmt-row mgmt-infra-row" }, [
        el("span", {}, ["Gitea (dev lane)"]),
        el("span", { class: "mgmt-loading" }, ["…"]),
    ]);
    view.appendChild(el("div", { class: "mgmt-infra" }, [
        el("h3", { class: "mgmt-infra-title" }, ["Infrastructure"]),
        row,
    ]));
    let g = null;
    try {
        const res = await fetch("/broker/dev");
        if (res.ok) {
            const b = await res.json();
            if (b.ok && b.result) g = b.result.gitea || null;
        }
    } catch (e) { /* leave the unknown badge */ }
    row.innerHTML = "";
    row.appendChild(el("span", {}, ["Gitea (dev lane)"]));
    if (!g) {
        row.appendChild(el("span", { class: "type-badge" }, ["unknown"]));
        return;
    }
    const enableBtn = () => {
        const b = el("button", { class: "btn-small" }, ["Enable Gitea"]);
        b.onclick = () =>
            devEnableGiteaDialog(view, () => renderManagementInto(view));
        return b;
    };
    if (!g.exists) {
        row.appendChild(el("span", { class: "type-badge" }, ["not enabled"]));
        row.appendChild(enableBtn());
        return;
    }
    row.appendChild(el("span",
                       { class: g.running ? "state-running" : "state-stopped" },
                       [g.running ? "running" : "stopped"]));
    if (!g.running) {
        row.appendChild(enableBtn());
    }
    // Enabled (running or stopped — the verb resumes a stopped gitea itself).
    const pwBtn = el("button", { class: "btn-small" }, ["Set Gitea password"]);
    pwBtn.onclick = () =>
        devGiteaPasswdDialog(view, () => renderManagementInto(view));
    row.appendChild(pwBtn);
}

function mgmtAction(view, name, action) {
    // A stop/start recreates the supervisor (fresh container + re-deployed editor),
    // so drop the project's remembered tab — it should re-land on the editor default
    // rather than a now-dead terminal session from before the restart.
    delete state.projectLastService[name];
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
    // Keys are LOCKSTEP with the rscore box_* progress.step() calls.
    box_add: [
        { key: "validate", label: "checking the project" },
        { key: "create-box", label: "creating the box" },
        { key: "ready", label: "box ready" },
    ],
    box_remove: [
        { key: "validate", label: "checking the project" },
        { key: "discard", label: "discarding the box" },
    ],
    // Keys LOCKSTEP with rscore._provision_gitea's progress.step() calls.
    dev_gitea_start: [
        { key: "pull", label: "pulling the gitea image" },
        { key: "start", label: "starting gitea" },
        { key: "bootstrap", label: "creating accounts" },
    ],
    // Key LOCKSTEP with rscore.dev_passwd's progress.step() call.
    dev_passwd: [
        { key: "set", label: "setting the gitea password" },
    ],
};

// The deliberate "Enable Gitea" flow (STAGE_DEV_GITEA webui-first A): a tailed
// op (dev_gitea_start ∈ PROGRESS_VERBS) that CREATES gitea from nothing or
// resumes a stopped one. Shared by the Development Gitea tab, the Fetch tab's
// stopped branch, and Management → Infrastructure. onDone(ok) re-renders the
// caller's surface against the now-running gitea.
function devEnableGiteaDialog(view, onDone) {
    mgmtConfirmThenTail(view, {
        title: "Enable Gitea",
        tailTitle: "Enabling Gitea",
        verb: "dev_gitea_start",
        confirmLabel: "Enable",
        body: [el("p", {}, [
            "Starts the shared Gitea backend (a one-time image pull the first " +
            "time). Dev repos and dev boxes need it running.",
        ])],
        request: () => fetch("/broker/dev/gitea-start", { method: "POST" }),
        onDone: (ok) => { if (onDone) onDone(ok); },
    });
}

// Set the sandbox-admin gitea password (Management → Infrastructure). Step-up:
// retyped master password → client-side derivation, the proof rides the body
// (the destroy mold). The NEW gitea password is a SEPARATE secret by design —
// never the master password (gitea would become a second, weaker verifier of
// it: raw-password sign-ins, its own hash store, a container-reachable login
// endpoint).
function devGiteaPasswdDialog(view, onDone) {
    const pw1 = el("input", { type: "password", autocomplete: "new-password" });
    const pw2 = el("input", { type: "password", autocomplete: "new-password" });
    const masterI = el("input", { type: "password", autocomplete: "current-password" });
    mgmtConfirmThenTail(view, {
        title: "Set Gitea password",
        tailTitle: "Setting Gitea password",
        verb: "dev_passwd",
        confirmLabel: "Set password",
        body: [
            el("p", {}, [
                "Sets the sandbox-admin password — your interactive Gitea " +
                "sign-in (the Development page's Gitea tab, git over http). " +
                "A separate secret from your master password, on purpose.",
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["New Gitea password"]), pw1,
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Repeat it"]), pw2,
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Re-enter your master password"]), masterI,
            ]),
        ],
        validate: () => {
            // Floor mirrors broker_auth.MIN_PASSWORD_LENGTH (the renderSetup
            // vault-create floor — change together).
            if (pw1.value.length < 8) return "Password must be at least 8 characters.";
            if (pw1.value !== pw2.value) return "Passwords do not match.";
            if (!masterI.value) return "Re-enter your master password.";
            return null;
        },
        request: async () => fetch("/broker/dev/passwd", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                password: pw1.value,
                proof: await deriveLoginProof(masterI.value),
            }),
        }),
        onDone: (ok) => { if (onDone) onDone(ok); },
        focus: () => pw1.focus(),
    });
}

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
    const card = el("div", { class: cfg.cardClass ? "card " + cfg.cardClass : "card" }, [
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
        // Detached-lane ops (cfg.tailMode "buildlog", value or function) have
        // no OP_RUNS entry — GET /broker/op/<id> stays "unknown" by design —
        // so they tail terminal-first via the build-log modal (build_alive
        // covers the per-op lock lanes); everything else keeps the checklist
        // tail. The buildlog onDone fires on the Done click (the S4 review-
        // dialog semantics), so the ok flag is not meaningful there.
        const tailMode = typeof cfg.tailMode === "function" ? cfg.tailMode() : cfg.tailMode;
        if (tailMode === "buildlog") {
            await mgmtTailBuildLog(view, backdrop, card, body.op_id,
                                   cfg.tailTitle || cfg.title,
                                   () => { if (cfg.onDone) cfg.onDone(true); });
        } else {
            await mgmtTailOp(view, backdrop, card, body.op_id,
                             cfg.tailTitle || cfg.title, cfg.verb, cfg.onDone);
        }
    };
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    if (cfg.focus) setTimeout(() => cfg.focus(), 50);
    return backdrop;
}

// ---- software: build lane (pull / rebuild) ---------------------------------
// A build runs on the broker's DETACHED child (F2 Slice 2): the POST returns an
// op_id at once, then we stream the RAW build log from /fulllog (the authenticated
// relay of the host-only full log) into a scrolling <pre>, and read completion
// TERMINAL-FIRST from the view-log (/log). We deliberately do NOT consult
// GET /broker/op/<id> — it returns "unknown" for a build op by design (no OP_RUNS
// entry), which is not-failure; the child's op.progress.done()/fail() in the
// view-log is the completion signal.
async function mgmtTailBuildLog(view, backdrop, card, opId, title, onDone) {
    const phaseEl = el("div", { class: "op-phase" }, ["starting…"]);
    const pre = el("pre", { class: "sw-buildlog" }, [""]);
    const failEl = el("div", { class: "op-fail" });
    const doneBtn = el("button", { class: "btn", disabled: "" }, ["Working…"]);
    doneBtn.onclick = () => {
        if (doneBtn.disabled) return;
        backdrop.remove();
        // Default: the Software page (the original build-lane caller); the dev
        // review dialog passes its own Fetch-tab re-render.
        if (onDone) onDone(); else renderSoftwareInto(view);
    };
    card.innerHTML = "";
    card.appendChild(el("h2", {}, [title]));
    card.appendChild(phaseEl);
    card.appendChild(pre);
    card.appendChild(failEl);
    card.appendChild(el("div", { class: "btn-row" }, [doneBtn]));

    let logFrom = 0, viewFrom = 0, done = false, ok = false, sawTerminal = false, interrupted = false;
    // Liveness probe (via the build lock) — the terminal-first tail has no other
    // signal, so a hard-killed child (OOM / kill -9 / power-loss) that never wrote
    // a terminal would spin the modal forever. null on a transient error → assume
    // still alive and keep polling; only an explicit false ends the wait.
    const checkAlive = async () => {
        try {
            const r = await fetch(`/broker/op/${encodeURIComponent(opId)}/alive`);
            if (!r.ok) return null;
            const b = await r.json();
            return b.ok ? !!b.alive : null;
        } catch (e) { return null; }
    };
    const drainFull = async () => {
        const r = await fetch(`/broker/op/${encodeURIComponent(opId)}/fulllog?from=${logFrom}`);
        const redirect = mgmtStatusRedirect(view, r.status);
        if (redirect) return redirect;
        const b = await r.json();
        if (b.ok && b.exists && b.data) {
            logFrom = b.next;
            const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
            pre.textContent += b.data;
            if (atBottom) pre.scrollTop = pre.scrollHeight;   // follow the tail
        }
        return null;
    };
    const drainView = async () => {
        const r = await fetch(`/broker/op/${encodeURIComponent(opId)}/log?from=${viewFrom}`);
        const redirect = mgmtStatusRedirect(view, r.status);
        if (redirect) return redirect;
        const b = await r.json();
        if (b.started !== false && b.data) {
            viewFrom = b.next;
            for (const line of b.data.split("\n")) {
                if (!line.trim()) continue;
                let rec; try { rec = JSON.parse(line); } catch (e) { continue; }
                if (rec.status === "done") { sawTerminal = true; ok = true; }
                else if (rec.status === "failed") { sawTerminal = true; ok = false; }
                else if (rec.msg) phaseEl.textContent = rec.msg;
            }
        }
        return null;
    };

    while (!done) {
        try { const rd = await drainFull(); if (rd) { backdrop.remove(); return rd(); } }
        catch (e) { /* transient */ }
        try { const rd = await drainView(); if (rd) { backdrop.remove(); return rd(); } }
        catch (e) { /* transient */ }
        if (sawTerminal) { done = true; break; }   // view-log terminal = completion
        // No terminal yet — is the build still running? run_build writes the
        // terminal BEFORE releasing the lock, so alive===false means either the
        // terminal just landed (drain once more to catch it) or the child was
        // hard-killed without writing one (→ interrupted, don't spin forever).
        const alive = await checkAlive();
        if (alive === false) {
            try { await drainFull(); await drainView(); } catch (e) { /* transient */ }
            if (!sawTerminal) interrupted = true;
            done = true; break;
        }
        await opSleep(OP_POLL_INTERVAL_MS);
    }
    try { await drainFull(); } catch (e) { /* best-effort trailing bytes */ }
    if (interrupted) {
        phaseEl.textContent = "interrupted";
        failEl.textContent = "Interrupted — the process stopped without finishing. Check the log above.";
    } else if (ok) { phaseEl.textContent = "done"; }
    else { phaseEl.textContent = "failed"; failEl.textContent = "Failed — see the log above."; }
    doneBtn.textContent = "Done";
    doneBtn.disabled = false;
}

// Confirm → POST /broker/software/build → stream. cfg = {title, confirmLabel,
// body[], payload, tailTitle?}.
function mgmtBuildDialog(view, cfg) {
    if (document.querySelector(".modal-backdrop")) return null;
    const backdrop = el("div", { class: "modal-backdrop" });
    const errEl = el("div", { class: "error" });
    const cancel = el("button", { class: "btn btn-secondary" }, ["Cancel"]);
    cancel.onclick = () => backdrop.remove();
    const go = el("button", { class: "btn" }, [cfg.confirmLabel]);
    const card = el("div", { class: "card sw-build-card" }, [
        el("h2", {}, [cfg.title]),
        ...(cfg.body || []),
        el("div", { class: "btn-row" }, [cancel, go]),
        errEl,
    ]);
    go.onclick = async () => {
        errEl.textContent = "";
        go.disabled = true; cancel.disabled = true; go.textContent = "…";
        let res;
        try {
            res = await fetch("/broker/software/build", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(cfg.payload),
            });
        } catch (e) {
            go.disabled = false; cancel.disabled = false; go.textContent = cfg.confirmLabel;
            errEl.textContent = "Broker unreachable."; return;
        }
        const redirect = mgmtStatusRedirect(view, res.status);
        if (redirect) { backdrop.remove(); return redirect(); }
        let body; try { body = await res.json(); } catch (e) { body = {}; }
        if (!body.ok || !body.op_id) {
            go.disabled = false; cancel.disabled = false; go.textContent = cfg.confirmLabel;
            errEl.textContent = "Failed: " + mgmtErrText(body); return;
        }
        await mgmtTailBuildLog(view, backdrop, card, body.op_id, cfg.tailTitle || cfg.title);
    };
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    return backdrop;
}

// Two-step dist refresh: check the upstream version, then (only if newer) confirm
// the bump+rebuild and stream it. cfg = {title, distLabel, checkPayload,
// buildPayload, tailTitle}. The APPLY re-resolves child-side, so the previewed
// version is advisory — the confirm text says "at last check", never a guarantee.
async function mgmtRefreshDialog(view, cfg) {
    if (document.querySelector(".modal-backdrop")) return null;
    const backdrop = el("div", { class: "modal-backdrop" });
    const bodyEl = el("div", {});
    const card = el("div", { class: "card sw-build-card" }, [
        el("h2", {}, [cfg.title]),
        bodyEl,
    ]);
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);

    const closeRow = () => {
        const close = el("button", { class: "btn btn-secondary" }, ["Close"]);
        close.onclick = () => backdrop.remove();
        return el("div", { class: "btn-row" }, [close]);
    };
    const fail = (msg) => {
        bodyEl.innerHTML = "";
        bodyEl.appendChild(el("p", { class: "error" }, [msg]));
        bodyEl.appendChild(closeRow());
    };

    bodyEl.appendChild(el("p", { class: "mgmt-loading" }, ["Checking upstream…"]));
    let res;
    try {
        res = await fetch("/broker/software/refresh-check", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(cfg.checkPayload),
        });
    } catch (e) { fail("Broker unreachable."); return backdrop; }
    const redirect = mgmtStatusRedirect(view, res.status);
    if (redirect) { backdrop.remove(); return redirect(); }
    let body; try { body = await res.json(); } catch (e) { body = {}; }
    if (!body.ok || !body.result) {
        fail("Could not resolve upstream: " + mgmtErrText(body)); return backdrop;
    }
    const current = body.result.current || "(unset)";
    const latest = body.result.latest;
    bodyEl.innerHTML = "";
    // Up-to-date is equality on the RAW values ("" == "" for an unset pin at an
    // unresolved upstream would be odd, but the resolver returns a concrete
    // version); a stale cached dist at the same pin is covered by Pull.
    if (body.result.current === body.result.latest) {
        bodyEl.appendChild(el("p", {}, [
            cfg.distLabel + " is already at the upstream version ",
            el("span", { class: "sw-mono" }, [latest]),
            ". Use Pull to (re)build the dist if the cache is stale.",
        ]));
        bodyEl.appendChild(closeRow());
        return backdrop;
    }
    bodyEl.appendChild(el("p", {}, [
        cfg.distLabel + ": pin ",
        el("span", { class: "sw-mono" }, [current]),
        " → upstream ",
        el("span", { class: "sw-mono" }, [latest]),
        ".",
    ]));
    bodyEl.appendChild(el("p", {}, [
        "Bump to the current upstream (", el("span", { class: "sw-mono" }, [latest]),
        " at last check) in the local override (versions.local.env, untracked — ",
        "never committed) and re-pull the dist? A few minutes; the rest of the ",
        "webui stays responsive.",
    ]));
    const errEl = el("div", { class: "error" });
    const cancel = el("button", { class: "btn btn-secondary" }, ["Cancel"]);
    cancel.onclick = () => backdrop.remove();
    const go = el("button", { class: "btn" }, ["Bump + re-pull"]);
    go.onclick = async () => {
        errEl.textContent = "";
        go.disabled = true; cancel.disabled = true; go.textContent = "…";
        const reset = () => {
            go.disabled = false; cancel.disabled = false; go.textContent = "Bump + re-pull";
        };
        let r;
        try {
            r = await fetch("/broker/software/build", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(cfg.buildPayload),
            });
        } catch (e) { reset(); errEl.textContent = "Broker unreachable."; return; }
        const rd = mgmtStatusRedirect(view, r.status);
        if (rd) { backdrop.remove(); return rd(); }
        let b; try { b = await r.json(); } catch (e) { b = {}; }
        if (!b.ok || !b.op_id) { reset(); errEl.textContent = "Failed: " + mgmtErrText(b); return; }
        await mgmtTailBuildLog(view, backdrop, card, b.op_id, cfg.tailTitle || cfg.title);
    };
    bodyEl.appendChild(el("div", { class: "btn-row" }, [cancel, go]));
    bodyEl.appendChild(errEl);
    return backdrop;
}

// ---- create -----------------------------------------------------------------
// The create form for ONE chosen workflow (the workflows card is the picker —
// see renderWorkflowsScreen). The Workflows page fetched the catalog and passes the
// selected manifest + the agent enum; this builds the rest of the form. The
// broker's CREATE_WEBUI_FIELDS allow-list is the real input boundary (it silently
// DROPS any field not in the set), so every key the payload sends below is in that
// set — the two-file lockstep. The workflow is FIXED here; substrate /
// has_worker_layer / light-path presets all come from the manifest (substrate
// stays hidden, Q7). The --enable presets show only where they take effect
// (has_worker_layer — a research-flavor workflow); the agent + light-path group
// only on the docker substrate. github_pat is a SECRET: never preset, never
// logged/persisted browser-side (built in the request closure and POSTed once).

const MGMT_ENABLE_PRESETS = ["websearcher", "wrangler"];

function mgmtCreateDialog(view, manifest, agents) {
    manifest = manifest || {};
    agents = Array.isArray(agents) ? agents : [];
    const workflow = manifest.name || "research";
    const isDocker = manifest.substrate === "docker";
    const hasWorkerLayer = !!manifest.has_worker_layer;
    // sandbox-dind is the docker `sandbox` flavor + DIND (STAGE_SANDBOX_DIND_AGENT):
    // same agents + light-path group as the docker box, plus an opt-in box-harness
    // toggle. box_capable is manifest-derived (the broker workflows verb).
    const boxCapable = !!manifest.box_capable;
    // Dev-flagged workflow (dev:true — built-in `dev` or a BYO sibling): the
    // form is the URL-driven dev variant. It POSTs the detached dev-project
    // provision (mirror + create as one op), so the light-path clone group +
    // agents cards are hidden — the dev step owns the repo wiring, and the
    // dind fleet deploys the default agent dist regardless.
    const isDev = !!manifest.dev;
    const showInBox = !isDev && (isDocker || boxCapable);

    const nameI = el("input", { type: "text", autocomplete: "off" });
    const egressS = el("select", {}, [
        el("option", { value: "open" }, ["open"]),
        el("option", { value: "locked" }, ["locked"]),
    ]);
    // Dev preselects locked (matches the flavor's own default; PI decision).
    // Locked still allows 80/443/DNS — pip/apt/LLM work; gitea rides the
    // project bridge directly and is egress-mode-independent.
    if (isDev) egressS.value = "locked";
    // Editor (code-server) is universal + on by default (STAGE_BOX_EXT_UX C).
    // Unchecking sends disable:["code-server"]; checked sends nothing (default-on).
    const editorCb = el("input", { type: "checkbox" });
    editorCb.checked = true;
    // Editor as a bordered selectable card (mirrors the box window): editorCb stays
    // the detached value-holder, the card only drives + reflects it (.selected ↔ checked).
    const editorCard = el("div", { class: "box-opt-card selected" },
                          [el("span", { class: "box-opt-name" }, ["Editor (code-server)"])]);
    editorCard.onclick = () => {
        editorCb.checked = !editorCb.checked;
        editorCard.classList.toggle("selected", editorCb.checked);
    };
    // Reader (mobile artifact viewer, STAGE_READER) — dind-only + default OFF.
    // Checked → enable:["reader"] (merged with worker presets in the payload). The
    // card is offered ONLY on dind workflows (!isDocker): the backend rejects reader
    // on the docker substrate, so showing it there would be a dead-end control.
    const readerCb = el("input", { type: "checkbox" });
    readerCb.checked = false;
    const readerCard = el("div", { class: "box-opt-card" },
                          [el("span", { class: "box-opt-name" }, ["Reader (mobile viewer)"])]);
    readerCard.onclick = () => {
        readerCb.checked = !readerCb.checked;
        readerCard.classList.toggle("selected", readerCb.checked);
    };

    // Enable presets — only for a workflow that has a worker/sandbox layer
    // (research flavor). A bare box / sandbox host has none, so the backend would
    // silently drop these tokens; gating the group's visibility avoids that
    // no-op footgun. (CREATE_WEBUI_FIELDS still allows `enable`; we just don't
    // send it where it does nothing.)
    const checks = MGMT_ENABLE_PRESETS.map((p) => {
        const cb = el("input", { type: "checkbox", value: p });
        return { p, cb, label: el("label", { class: "mgmt-check" }, [cb, " " + p]) };
    });
    const enableField = el("div", { class: "field" }, [
        el("label", {}, ["Enable"]),
        el("div", { class: "mgmt-checks" }, checks.map((c) => c.label)),
    ]);
    if (!hasWorkerLayer) enableField.style.display = "none";

    // Docker-substrate-only agents (rendered as cards in the Settings region below).
    // Staged agents only — one independent on/off box each (STAGE_MULTI_AGENT),
    // default claude on. Un-staged KNOWN_AGENTS are omitted: the form never offers
    // an agent that isn't deployable yet (pull it under Management → Software), so
    // the POSTed set always validates in from_kwargs.
    const stagedAgents = agents
        .filter((a) => a && a.staged)
        .map((a) => a.name);
    // Multi-select (independent on/off set) — so each agent is its OWN toggle card
    // (cb stays the value-holder), NOT the box window's single-select radio agent.
    const agentChecks = stagedAgents.map((name) => {
        const cb = el("input", { type: "checkbox", value: name });
        if (name === "claude") cb.checked = true;   // default {claude} on
        const card = el("div", { class: "box-opt-card" + (cb.checked ? " selected" : "") },
                        [el("span", { class: "box-opt-name" }, [name])]);
        card.onclick = () => {
            cb.checked = !cb.checked;
            card.classList.toggle("selected", cb.checked);
        };
        return { name, cb, card };
    });
    const repoI = el("input", { type: "text", autocomplete: "off",
                                placeholder: "https://github.com/user/repo.git" });
    const refI = el("input", { type: "text", autocomplete: "off",
                               placeholder: "branch / tag / commit" });
    const setupT = el("textarea", { rows: "3", autocomplete: "off",
                                    placeholder: "setup commands run inside the box" });
    const patI = el("input", {
        type: "password", autocomplete: "off",
        title: "Optional. An in-box secret used only to clone a private repo over " +
               "https inside the locked box. Never logged or persisted — sent once " +
               "with this create and never stored.",
    });
    // Settings region — agent + editor as bordered cards inside a single bordered
    // box (mirrors the box window). Editor is universal; agents only showInBox
    // (docker box / sandbox-dind), preserving today's visibility split. No raw
    // <input> lives here (only cards), so the .field input{width:100%} bleed doesn't apply.
    const settingsRegion = el("div", { class: "field box-settings" }, [
        el("div", { class: "box-settings-label" }, ["Settings"]),
        ...(showInBox ? [el("div", { class: "box-opt-group" }, [
            el("div", { class: "box-opt-caption" }, ["Agents"]),
            agentChecks.length
                ? el("div", { class: "box-opt-cards" }, agentChecks.map((c) => c.card))
                : el("div", { class: "hint" }, [
                      "No agents available yet — pull one under Management → Software.",
                  ]),
        ])] : []),
        el("div", { class: "box-opt-group" }, [
            el("div", { class: "box-opt-caption" }, ["Extensions"]),
            el("div", { class: "box-opt-cards" },
               isDocker ? [editorCard] : [editorCard, readerCard]),
        ]),
    ]);

    // Clone-a-repo group: the light-path repo/ref/setup/PAT fields, shown only when
    // the operator opts in (and only showInBox). Agent moved out to Settings above.
    const cloneCb = el("input", { type: "checkbox" });
    const cloneToggle = el("label", { class: "mgmt-check" }, [cloneCb, " clone a git repo"]);
    const cloneGroup = el("div", { class: "mgmt-docker-group" }, [
        el("div", { class: "field" }, [el("label", {}, ["Repo (https)"]), repoI]),
        el("div", { class: "field" }, [el("label", {}, ["Ref"]), refI]),
        el("div", { class: "field" }, [el("label", {}, ["Setup"]), setupT]),
        el("div", { class: "field" }, [
            el("label", { title: patI.getAttribute("title") }, ["GitHub PAT (secret)"]),
            patI,
        ]),
        el("div", { class: "hint" }, [
            "Repo/setup run inside the box (docker sandbox or sandbox-dind).",
        ]),
    ]);
    // Prefill the light-path presets from the manifest (overridable; the server's
    // explicit-wins applies the preset if the field is left blank). PAT never preset.
    repoI.value = manifest.repo || "";
    refI.value = manifest.ref || "";
    setupT.value = manifest.setup || "";
    // Default the clone tickbox on iff the manifest carries a preset repo/ref/setup,
    // so a preset-repo workflow still POSTs those fields (request gates on cloneCb).
    const hasClonePreset = !!(manifest.repo || manifest.ref || manifest.setup);
    cloneCb.checked = hasClonePreset;
    cloneGroup.style.display = hasClonePreset ? "" : "none";
    cloneCb.onchange = () => { cloneGroup.style.display = cloneCb.checked ? "" : "none"; };

    // Dev variant inputs — its OWN url/PAT fields, NOT the light-path repoI/patI
    // (different semantics: this URL is mirrored into the shared Gitea and the
    // project works its consumer FORK; the PAT reaches only Gitea's migrate
    // config for a private source — never stored by RS, never logged).
    const devUrlI = el("input", { type: "text", autocomplete: "off",
                                  placeholder: "https://github.com/owner/repo" });
    const devPatI = el("input", {
        type: "password", autocomplete: "off",
        title: "Optional. Only needed for a private repo: used once by Gitea's " +
               "mirror config to pull the source. RS stores no copy — sent once " +
               "with this create and never logged or persisted.",
    });
    const devGroup = el("div", { class: "mgmt-docker-group" }, [
        el("div", { class: "field" }, [el("label", {}, ["GitHub repo (https)"]), devUrlI]),
        el("div", { class: "field" }, [
            el("label", { title: devPatI.getAttribute("title") }, ["GitHub PAT (secret)"]),
            devPatI,
        ]),
        el("div", { class: "hint" }, [
            "The repo is mirrored into the shared Gitea and the project's dev " +
            "agent works its own fork. A first mirror of a large repo can take " +
            "a couple of minutes.",
        ]),
    ]);

    mgmtConfirmThenTail(view, {
        title: "New project",
        tailTitle: "Creating project",
        verb: "create",
        confirmLabel: "Create",
        // The dev variant runs on the broker's DETACHED dev lane: no OP_RUNS
        // entry (GET /broker/op/<id> stays "unknown" by design), so it needs
        // the terminal-first build-log tail, not the checklist tail.
        tailMode: () => (isDev ? "buildlog" : undefined),
        body: [
            el("div", { class: "field" }, [
                el("label", {}, ["Workflow"]),
                el("div", { class: "mgmt-selected-workflow" }, [
                    workflow + (manifest.source === "byo" ? " (byo)" : ""),
                ]),
                manifest.description
                    ? el("div", { class: "hint" }, [manifest.description]) : null,
            ]),
            el("div", { class: "field" }, [el("label", {}, ["Project name"]), nameI]),
            ...(isDev ? [devGroup] : []),
            el("div", { class: "field" }, [el("label", {}, ["Egress"]), egressS]),
            enableField,
            settingsRegion,
            ...(showInBox ? [cloneToggle, cloneGroup] : []),
            el("div", { class: "hint" }, [
                "Creating stages container images and can take 10–30s (longer cold).",
            ]),
        ],
        validate: () => {
            const n = nameI.value.trim();
            if (!n) return "Project name is required.";
            // Mirror rscore's project-name regex: without this a bad name dies
            // broker-side as the opaque "a valid op_id is required" (the op_id
            // charset gate precedes from_kwargs on the detached lane).
            if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(n)) {
                return "Project name must start with a letter or digit and use " +
                       "only letters, digits, '-' or '_'.";
            }
            if (isDev && !devUrlI.value.trim().startsWith("https://github.com/")) {
                return "A dev project needs a GitHub repo URL (https://github.com/owner/repo).";
            }
            // Mirror from_kwargs: an in-box repo needs a ref (pin the clone).
            if (showInBox && cloneCb.checked && repoI.value.trim() && !refI.value.trim()) {
                return "A workflow repo requires a ref.";
            }
            return null;
        },
        request: () => {
            if (isDev) {
                // URL-driven dev-project provision (mirror + create as one
                // detached op) — a different route from /broker/project.
                // `workflow` threads the manifest this dialog was opened from
                // (a BYO dev workflow creates what ITS manifest says); `pat`
                // rides this POST body only (never logged, never OP_RUNS; the
                // broker re-filters + shape-validates pre-spawn). Lockstep:
                // every key here is in DEV_PROJECT_WEBUI_FIELDS.
                const payload = {
                    name: nameI.value.trim(),
                    workflow: workflow,
                    url: devUrlI.value.trim(),
                    egress: egressS.value,
                };
                const pat = devPatI.value.trim();
                if (pat) payload.pat = pat;
                if (!editorCb.checked) payload.disable = ["code-server"];
                if (readerCb.checked) payload.enable = ["reader"];
                return fetch("/broker/dev/project", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
            }
            // Lockstep: every key below is in CREATE_WEBUI_FIELDS. enable rides
            // only for a workflow with a worker layer; the in-box fields (agents,
            // repo/ref/setup/PAT) for a docker box OR sandbox-dind, and only when
            // non-empty — so a research create never puts them on the wire and
            // from_kwargs applies the manifest presets unshadowed.
            const payload = {
                name: nameI.value.trim(),
                workflow: workflow,
                egress: egressS.value,
            };
            // Editor on by default; unchecking disables the code-server service.
            if (!editorCb.checked) payload.disable = ["code-server"];
            // Build ONE merged enable array so the reader tickbox and the worker
            // presets don't clobber each other (the broker drops silently, so a
            // second assignment would just lose the other's tokens). Worker presets
            // ride only on a workflow with a worker layer; reader only on dind
            // (!isDocker), matching where each control is shown.
            const enableTokens = [];
            if (hasWorkerLayer) {
                enableTokens.push(...checks.filter((c) => c.cb.checked).map((c) => c.p));
            }
            if (!isDocker && readerCb.checked) enableTokens.push("reader");
            if (enableTokens.length) payload.enable = enableTokens;
            if (showInBox) {
                const sel = agentChecks.filter((c) => c.cb.checked)
                                       .map((c) => c.name);
                if (sel.length) payload.agents = sel;   // empty => clean box
                // Light-path fields ride only when the operator opts into a clone,
                // so an unchecked clone POSTs none of them and from_kwargs applies
                // the manifest presets unshadowed (same as the blank-field path).
                if (cloneCb.checked) {
                    const repo = repoI.value.trim();
                    const ref = refI.value.trim();
                    const setup = setupT.value.trim();
                    const pat = patI.value.trim();
                    if (repo) payload.repo = repo;
                    if (ref) payload.ref = ref;
                    if (setup) payload.setup = setup;
                    if (pat) payload.github_pat = pat;
                }
            }
            return fetch("/broker/project", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
        },
        // On success, surface the new (running) project in the sidebar — JIT-fetch
        // its creds the same way Attach does, then refresh the rail behind the box.
        onDone: async (ok) => {
            if (!ok) return;
            if (await attachIntoVault(nameI.value.trim())) refreshProjectRail();
        },
        focus: () => nameI.focus(),
    });
}

// ---- workflows screen (clickable workflow cards) ---------------------------
// The Workflows page is the create entry point: a grid of workflow cards.
// Clicking a card opens mgmtCreateDialog prefilled to that workflow. Gated
// behind the management session (the broker `workflows` verb is token-gated) — a
// 401 routes through the same login screen, returning here on success. Substrate
// is never shown (Q7): a card is a workflow, not a containment runtime.

function openWorkflows() {
    if (state.hostPage === "workflows") return leaveHostView();
    state.hostPage = "workflows";
    const view = enterHostView();
    if (!view) return;
    setActiveNavEntry("workflows-entry");
    renderWorkflowsInto(view);
}

async function renderWorkflowsInto(view) {
    view.innerHTML = "";
    view.appendChild(el("div", { class: "mgmt-loading" }, ["Loading workflows…"]));
    let res;
    try {
        res = await fetch("/broker/workflows");
    } catch (e) { return renderMgmtUnavailable(view); }
    if (res.status === 401) return renderMgmtLogin(view, renderWorkflowsInto);
    if (res.status === 403) return renderMgmtRejected(view);
    if (res.status === 503) return renderMgmtUnavailable(view);
    let body;
    try { body = await res.json(); } catch (e) { return renderMgmtUnavailable(view); }
    if (!res.ok || !body.ok || !body.result) {
        return renderMgmtVerbError(view, body, renderWorkflowsInto);
    }
    // Which workflows have a rendered Explain doc (baked static index — built-ins
    // only). Best-effort + cached: a missing index just means no Explain buttons.
    if (state.explainIndex === null) {
        try {
            const ir = await fetch("/static/explain/index.json");
            state.explainIndex = ir.ok ? await ir.json() : [];
        } catch (e) { state.explainIndex = []; }
    }
    renderWorkflowsScreen(view, body.result);
}

function renderWorkflowsScreen(view, result) {
    view.innerHTML = "";
    const workflows = Array.isArray(result.workflows) ? result.workflows : [];
    const agents = Array.isArray(result.agents) ? result.agents : [];
    const explain = new Set(state.explainIndex || []);
    // A card's section: its declared group, else Store — the catch-all for a
    // group-less manifest (e.g. a future BYO entry).
    const bucketOf = (m) =>
        (["research", "base", "store"].includes(m.group) ? m.group : "store");
    const buildCard = (m) => {
        // A built-in with a baked doc → Explain (the in-app explainer); any other
        // repo-bearing card → Explain opens the repo's GitHub README. Both
        // stopPropagation so they don't also trigger the card's create dialog.
        let action = null;
        if (explain.has(m.name)) {
            action = el("button", { class: "explain-btn", title: "Open the explainer" }, ["Explain"]);
            action.onclick = (ev) => { ev.stopPropagation(); openExplain(m.name); };
        } else if (m.repo) {
            const url = m.repo.replace(/\.git$/, "") + "#readme";
            action = el("a", { class: "explain-btn", href: url, target: "_blank",
                               rel: "noopener noreferrer", title: "Open the repo README" }, ["Explain"]);
            action.onclick = (ev) => { ev.stopPropagation(); };
        }
        const tags = Array.isArray(m.tags) ? m.tags : [];
        const tagsEl = el("div", { class: "workflow-tags" },
            tags.map((t) => el("span", { class: "workflow-tag tag-" + t }, [t])));
        const card = el("div", { class: "workflows-card group-" + bucketOf(m),
                                 title: m.description || "" }, [
            el("div", { class: "workflows-card-name" }, [
                m.title || m.name,
                m.source === "byo" ? el("span", { class: "workflows-byo" }, ["byo"]) : null,
            ]),
            el("div", { class: "workflows-card-desc" }, [m.description || ""]),
            el("div", { class: "workflows-card-actions" }, [tagsEl, action]),
        ]);
        card.onclick = () => mgmtCreateDialog(view, m, agents);
        return card;
    };
    // Partition into the three sections, then render Research → Base → Store —
    // each non-empty group under its own header.
    const buckets = { research: [], base: [], store: [] };
    for (const m of workflows) buckets[bucketOf(m)].push(buildCard(m));
    // Import an existing project — a card in the Store section (imported things);
    // was the rail's "+ Add project". Opens the SSH-coordinates modal.
    const importCard = el("div", { class: "workflows-card import-card",
                                   title: "Add an existing project by its SSH coordinates" }, [
        el("div", { class: "workflows-card-name" }, ["Import project"]),
        el("div", { class: "workflows-card-desc" },
           ["Add a box you run elsewhere, by its SSH coordinates."]),
        el("div", { class: "workflows-card-actions" }, [null]),
    ]);
    importCard.onclick = () => openAddProjectModal();
    buckets.store.push(importCard);
    const sections = [["research", "Research"], ["base", "Base"], ["store", "Store"]]
        .filter(([key]) => buckets[key].length)
        .map(([key, label]) => el("div", { class: "workflows-group" }, [
            el("div", { class: "workflows-group-title" }, [label]),
            el("div", { class: "workflows-grid" }, buckets[key]),
        ]));
    view.appendChild(el("div", { class: "workflows-screen" }, [
        el("h2", { class: "workflows-title" }, ["New Project"]),
        el("div", { class: "hint workflows-sub" },
           ["Pick a workflow to create a project, or import an existing one."]),
        ...sections,
    ]));
}

// ---- Explain floating popover (STAGE_WORKFLOW_EXPLAIN) ----------------------
// A workflow's Explain button opens its baked learning doc (static HTML with an
// inline interactive SVG) as a FLOATING box centered over the page, dismissed on
// an outside click or Escape — not a persistent host-page tab. Same outside-click
// discipline as the project config box (pointerdown capture + composedPath).

let explainBox = null;

function closeExplainBox() {
    if (!explainBox) return;
    explainBox.remove();
    explainBox = null;
    document.removeEventListener("pointerdown", onExplainOutsidePointer, true);
    document.removeEventListener("keydown", onExplainKeydown, true);
}

// Keep open when the click is inside the box OR on an Explain button (so the
// button's own click, which opens/replaces the box, isn't self-cancelled).
function onExplainOutsidePointer(ev) {
    if (!explainBox) return;
    const path = ev.composedPath ? ev.composedPath() : [];
    for (const node of path) {
        if (node === explainBox) return;
        if (node && node.classList && node.classList.contains("explain-btn")) return;
    }
    closeExplainBox();
}

function onExplainKeydown(ev) { if (ev.key === "Escape") closeExplainBox(); }

function explainErrorEl(name) {
    return el("div", { class: "mgmt-error explain-error" }, [
        el("h2", {}, ["Doc unavailable"]),
        el("p", {}, ["Couldn't load the “" + name + "” explainer. If you just added "
            + "it, rebuild the webui image so it's rendered into the bundle."]),
    ]);
}

async function openExplain(name) {
    closeExplainBox();   // one at a time
    const closeBtn = el("button", { class: "explain-popover-close", title: "Close" }, ["×"]);
    closeBtn.onclick = closeExplainBox;
    // The doc HTML is scoped under .explain-doc so the SVG's self-scoped <style>
    // applies (same as the prior host-page render).
    const doc = el("div", { class: "explain-doc" }, [
        el("div", { class: "mgmt-loading" }, ["Loading…"]),
    ]);
    const box = el("div", { class: "explain-popover" }, [closeBtn, doc]);
    document.body.appendChild(box);
    explainBox = box;
    document.addEventListener("pointerdown", onExplainOutsidePointer, true);
    document.addEventListener("keydown", onExplainKeydown, true);

    let res;
    try {
        res = await fetch("/static/explain/" + encodeURIComponent(name) + ".html");
    } catch (e) { if (explainBox === box) { doc.innerHTML = ""; doc.appendChild(explainErrorEl(name)); } return; }
    if (explainBox !== box) return;   // dismissed while loading
    if (!res.ok) { doc.innerHTML = ""; doc.appendChild(explainErrorEl(name)); return; }
    const html = await res.text();
    if (explainBox !== box) return;
    // Built-in, repo-controlled, script-free fragment (md→HTML + inline SVG at build).
    doc.innerHTML = html;
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
                el("label", {}, ["Re-enter your master password"]), pwI,
            ]),
        ],
        validate: () => {
            if (nameI.value.trim() !== name) return "Type the project name exactly to confirm.";
            if (!pwI.value) return "Re-enter your master password.";
            return null;
        },
        // Step-up: the retyped password is derived CLIENT-SIDE (unified login —
        // the raw password never transits) and the proof rides the request; the
        // broker re-verifies it async, so a wrong password surfaces as a FAILED
        // op in phase 2 ("Failed — Wrong password."), not an inline phase-1
        // error. Retype (vs reusing state.loginProof) is deliberate: it keeps
        // step-up meaningful against a stolen webui session cookie.
        request: async () => fetch(`/broker/project/${encodeURIComponent(name)}/destroy`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ proof: await deriveLoginProof(pwI.value) }),
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

// ---- sandbox boxes (the config box's Boxes section) ------------------------
// box add/remove reuse the management op-tail (mgmtConfirmThenTail). Those were
// built for the Management screen and take a `view` for the on-Done re-render +
// the 401/403/503 auth redirects. Box ops are launched from the rail config box,
// which has no such view, so we pass a detached throwaway element: the on-Done
// `renderManagementInto` renders into nothing (harmless — and it still refreshes
// the sidebar's running set as a side effect), and the real refresh runs in the
// op's onDone below. A mid-op auth-expiry redirect lands in the throwaway too,
// but the box op is short and the session was just used to open the config box.
function boxOpView() { return el("div"); }

// After a box add/remove: the supervisor updated extensions.json, so the project's
// tab set changed. Invalidate the cached services and, if the project is open,
// re-activate it to surface the new / removed pi-iso tab. The box list itself
// re-loads on the next config-box open (the config box is dismissed when the op
// modal takes a pointer-down).
async function refreshAfterBoxChange(project) {
    delete state.projectServices[project];
    if (state.activeProject === project) {
        try { await activateProject(project); } catch (e) { /* best-effort */ }
    }
}

// The box window (STAGE_BOX_EXT_UX Slice B). Fetches the box-preset catalog +
// the project's allowed MCPs FIRST (operator-extensible presets + the MCP picker
// need a read path — the box_presets verb), then opens a New-Project-style modal
// with preset radio-cards + agent/editor/MCP toggles + BYO clone fields + an
// Explain button. A non-ok / unreachable read surfaces the error and does NOT
// open the window — a box can't be added on a stopped/non-dind supervisor anyway,
// and a malformed box-registry ValidationError must be shown, not swallowed.
async function mgmtBoxAddDialog(project) {
    let presets, allowed;
    try {
        const res = await fetch(`/broker/project/${encodeURIComponent(project)}/box-presets`);
        let body; try { body = await res.json(); } catch (e) { body = {}; }
        if (!res.ok || !body.ok || !body.result) {
            alert("Can't add a box: " + (mgmtErrText(body)
                || "the project must be a running dind project."));
            return;
        }
        presets = body.result.presets || [];
        allowed = body.result.allowed_mcps || [];
    } catch (e) {
        alert("Can't add a box: broker unreachable.");
        return;
    }
    if (!presets.length) { alert("No box presets available for this project."); return; }

    const nameI = el("input", { type: "text", autocomplete: "off",
                                placeholder: "auto (box-N)" });

    // Preset cards — no visible control; the selected card is marked by an accent
    // border (markSelected toggles `.selected`). The radio is a hidden, label-
    // driven state holder so a click anywhere on the card selects it.
    let selectedPreset = presets[0];
    const cardEls = [];
    const cards = presets.map((p) => {
        const radio = el("input", { type: "radio", name: "box-preset", value: p.name });
        if (p === presets[0]) radio.checked = true;
        radio.onchange = () => { if (radio.checked) { selectedPreset = p; markSelected(); applyPreset(); } };
        // Per-box explain ⓘ — opens this preset's learning doc (box-<name>.html).
        // `explain-btn` whitelists it in onExplainOutsidePointer; stopPropagation
        // keeps the click off the (hidden) radio.
        const info = el("button", { type: "button", class: "explain-btn box-preset-explain",
                                    title: "Explain " + p.name }, ["ⓘ"]);
        info.onclick = (ev) => {
            ev.preventDefault(); ev.stopPropagation(); openExplain("box-" + p.name);
        };
        const card = el("div", { class: "box-preset-card" }, [
            el("label", { class: "box-preset-pick" }, [
                radio,
                el("span", { class: "box-preset-body" }, [
                    el("span", { class: "box-preset-name" }, [p.name]),
                    el("span", { class: "box-preset-desc" }, [p.description || ""]),
                ]),
            ]),
            info,
        ]);
        cardEls.push({ preset: p, card });
        return card;
    });
    function markSelected() {
        for (const { preset, card } of cardEls)
            card.classList.toggle("selected", preset === selectedPreset);
    }
    markSelected();

    // Agent + editor are bordered selectable cards (markAgent/editor toggle
    // `.selected`). `agentS` (detached <select>) and `editorCb` (detached
    // checkbox) stay the VALUE HOLDERS so the payload + MCP-coupling code below
    // are byte-unchanged — the cards only drive + reflect them. There is no
    // "preset default" choice: selecting a preset pre-selects its default agent
    // (applyPreset), which the operator can then override.
    const agentS = el("select", {}, [
        el("option", { value: "none" }, ["none (blank box)"]),
        el("option", { value: "claude" }, ["claude"]),
    ]);
    const agentSpecs = [
        { value: "none", label: "None" },
        { value: "claude", label: "Claude" },
    ];
    const agentCardEls = [];
    const agentCards = agentSpecs.map((s) => {
        const card = el("div", { class: "box-opt-card" },
                        [el("span", { class: "box-opt-name" }, [s.label])]);
        card.onclick = () => { if (!agentS.disabled) { agentS.value = s.value; markAgent(); } };
        agentCardEls.push({ value: s.value, card });
        return card;
    });
    const agentCardsWrap = el("div", { class: "box-opt-cards" }, agentCards);
    function markAgent() {
        for (const { value, card } of agentCardEls)
            card.classList.toggle("selected", value === agentS.value);
    }

    const editorCb = el("input", { type: "checkbox" });   // box-level toggle, default off
    const editorCard = el("div", { class: "box-opt-card" },
                          [el("span", { class: "box-opt-name" }, ["Editor"])]);
    editorCard.onclick = () => {
        editorCb.checked = !editorCb.checked;
        editorCard.classList.toggle("selected", editorCb.checked);
    };

    // MCP picker over the project's allowed MCPs. Checking ≥1 forces the agent on
    // (nothing else reaches an MCP) — reflect the backend coupling by forcing +
    // disabling the agent select.
    const mcpBoxes = allowed.map((m) => {
        const cb = el("input", { type: "checkbox" });
        cb.onchange = applyMcpCoupling;
        return { name: m, cb };
    });
    const mcpList = el("div", { class: "config-mcp-picker" },
        mcpBoxes.length
            ? mcpBoxes.map((b) => el("label", { class: "mgmt-check" }, [b.cb, " " + b.name]))
            : [el("div", { class: "config-empty" }, ["No MCPs allowed for this project yet."])]);

    // BYO clone fields — shown only for a clone preset.
    const repoI = el("input", { type: "text", autocomplete: "off",
                                placeholder: "https://github.com/user/repo.git" });
    const refI = el("input", { type: "text", autocomplete: "off",
                               placeholder: "branch / tag / commit" });
    const setupT = el("textarea", { rows: "2", autocomplete: "off",
                                    placeholder: "setup command run inside the box" });
    const byoGroup = el("div", { class: "mgmt-docker-group" }, [
        el("div", { class: "field" }, [el("label", {}, ["Repo (https)"]), repoI]),
        el("div", { class: "field" }, [el("label", {}, ["Ref"]), refI]),
        el("div", { class: "field" }, [el("label", {}, ["Setup"]), setupT]),
        el("div", { class: "hint" }, ["Cloned + setup-run inside the box at boot."]),
    ]);

    // Dev preset (webui-first B): the box is provisioned FROM a GitHub URL —
    // mirror+fork+attach+create run as ONE detached broker op, no prior
    // `dev attach` needed. The PAT is for private sources only and rides this
    // POST body alone: RS stores no PAT (gitea keeps it in its own per-repo
    // mirror config), so changing it later = remove the repo + re-create.
    const devUrlI = el("input", { type: "text", autocomplete: "off",
                                  placeholder: "https://github.com/owner/repo" });
    const devPatI = el("input", { type: "password", autocomplete: "off",
                                  placeholder: "private repos only" });
    const devGroup = el("div", { class: "mgmt-docker-group" }, [
        el("div", { class: "field" }, [el("label", {}, ["GitHub repo (https)"]), devUrlI]),
        el("div", { class: "field" }, [el("label", {}, ["GitHub PAT (optional)"]), devPatI]),
        el("div", { class: "hint" },
           ["Mirrored + forked into the shared Gitea; the agent's fork is " +
            "cloned at boot and work is delivered via PRs. The PAT (private " +
            "repos only, prefer read-only fine-grained) is kept by Gitea " +
            "per-repo — to change it, remove the repo and re-create the box."]),
    ]);

    // A dev box can't take MCPs (its dedicated bridge has no path to
    // mcp-proxy — both S2 gates reject them); the dialog must not offer a
    // guaranteed rejection, and a live picker would wedge the agent cards via
    // applyMcpCoupling. Disabling unchecks first so the coupling releases.
    function setMcpsDisabled(disabled) {
        for (const b of mcpBoxes) {
            if (disabled) b.cb.checked = false;
            b.cb.disabled = disabled;
        }
        mcpList.classList.toggle("disabled", disabled);
        applyMcpCoupling();
    }

    function applyPreset() {
        const isDev = !!selectedPreset.dev;
        // A dev preset locks the MCP picker (uncheck + disable) BEFORE the
        // agent pre-select below, so the coupling can't hold the cards locked.
        setMcpsDisabled(isDev);
        // Pre-select the preset's default agent (unless MCP coupling has forced
        // claude on + locked the cards); the operator can still override it.
        if (!agentS.disabled) agentS.value = selectedPreset.agent_default ? "claude" : "none";
        markAgent();
        // Pre-check the editor toggle from the preset's UI default (still un-checkable).
        editorCb.checked = !!selectedPreset.editor_default;
        editorCard.classList.toggle("selected", editorCb.checked);
        // Show the BYO clone fields only when the preset clones AND the repo isn't
        // baked into the preset (a baked-repo preset like paper-orchestra hides them);
        // a dev preset shows the attached-repo picker instead.
        byoGroup.style.display = (selectedPreset.clone && !selectedPreset.repo) ? "" : "none";
        devGroup.style.display = isDev ? "" : "none";
    }
    function applyMcpCoupling() {
        const any = mcpBoxes.some((b) => b.cb.checked);
        if (any) { agentS.value = "claude"; agentS.disabled = true; }
        else { agentS.disabled = false; }
        markAgent();
        agentCardsWrap.classList.toggle("disabled", agentS.disabled);
    }
    applyPreset();
    applyMcpCoupling();

    // The overview explainer (box.md), relegated to a small ⓘ beside the "Box
    // type" label. `explain-btn` whitelists it in onExplainOutsidePointer.
    const overviewBtn = el("button", { type: "button", class: "explain-btn box-overview-explain",
                                       title: "About boxes" }, ["ⓘ"]);
    overviewBtn.onclick = (ev) => { ev.preventDefault(); openExplain("box"); };

    mgmtConfirmThenTail(boxOpView(), {
        title: `Add a box to ${project}`,
        tailTitle: `Adding a box to ${project}`,
        verb: "box_add",
        confirmLabel: "Add box",
        cardClass: "box-add-card",
        body: [
            el("div", { class: "field" }, [el("label", {}, ["Name (optional)"]), nameI]),
            el("div", { class: "field" }, [
                el("div", { class: "box-type-label" }, ["Box type", overviewBtn]),
                el("div", { class: "box-preset-cards" }, cards),
            ]),
            el("div", { class: "field box-settings" }, [
                el("div", { class: "box-settings-label" }, ["Settings"]),
                el("div", { class: "box-opt-group" }, [
                    el("div", { class: "box-opt-caption" }, ["Agent"]),
                    agentCardsWrap,
                ]),
                el("div", { class: "box-opt-group" }, [
                    el("div", { class: "box-opt-caption" }, ["Extensions"]),
                    el("div", { class: "box-opt-cards" }, [editorCard]),
                ]),
                el("div", { class: "box-opt-group" }, [
                    el("div", { class: "box-opt-caption" }, ["MCP tools"]),
                    mcpList,
                ]),
            ]),
            byoGroup,
            devGroup,
        ],
        validate: () => {
            const n = nameI.value.trim();
            if (n && !/^[a-z][a-z0-9-]*$/.test(n)) {
                return "Box name must be lowercase, start with a letter, and use only letters, digits, or '-'.";
            }
            if (selectedPreset.clone && repoI.value.trim() && !refI.value.trim()) {
                return "A repo requires a ref (pin the clone).";
            }
            if (selectedPreset.dev
                    && !devUrlI.value.trim().startsWith("https://github.com/")) {
                return "A dev box needs a GitHub repo URL (https://github.com/owner/repo).";
            }
            return null;
        },
        // The dev preset runs on the broker's DETACHED dev-box lane: no
        // OP_RUNS entry (GET /broker/op/<id> stays "unknown" by design), so
        // it needs the terminal-first build-log tail, not the checklist tail.
        tailMode: () => (selectedPreset.dev ? "buildlog" : undefined),
        request: () => {
            if (selectedPreset.dev) {
                // URL-driven provision (mirror+fork+attach+create as one op) —
                // a different route from /box; `pat` rides this POST body only
                // (never logged, never OP_RUNS; broker re-filters + validates).
                const payload = {
                    name: nameI.value.trim() || null,
                    url: devUrlI.value.trim(),
                    pat: devPatI.value.trim(),
                    editor: editorCb.checked,
                };
                if (agentS.value) payload.agent = agentS.value;
                return fetch(`/broker/project/${encodeURIComponent(project)}/dev-box`, {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
            }
            const payload = {
                name: nameI.value.trim() || null,
                preset: selectedPreset.name,
                editor: editorCb.checked,
                mcps: mcpBoxes.filter((b) => b.cb.checked).map((b) => b.name),
            };
            // Agent is always explicit now (the preset default is pre-selected,
            // not a sentinel); no `browser`.
            if (agentS.value) payload.agent = agentS.value;
            if (selectedPreset.clone) {
                const repo = repoI.value.trim(), ref = refI.value.trim(),
                    setup = setupT.value.trim();
                if (repo) payload.repo = repo;
                if (ref) payload.ref = ref;
                if (setup) payload.setup = setup;
            }
            return fetch(`/broker/project/${encodeURIComponent(project)}/box`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
        },
        onDone: async (ok) => { if (ok) await refreshAfterBoxChange(project); },
        focus: () => nameI.focus(),
    });
}

function mgmtBoxRemoveDialog(project, box) {
    const pwI = el("input", { type: "password", autocomplete: "current-password" });
    const keepCb = el("input", { type: "checkbox" });
    const warn = el("p", {}, [
        `This discards box "${box}" in "${project}" — its container and ` +
        "workspace are wiped. This cannot be undone.",
    ]);
    // Toggle the warning copy: preserving artifacts is recoverable, wiping is not.
    keepCb.onchange = () => {
        warn.textContent = keepCb.checked
            ? `This removes box "${box}" in "${project}" — its container is torn ` +
              "down but its workspace artifacts stay on disk."
            : `This discards box "${box}" in "${project}" — its container and ` +
              "workspace are wiped. This cannot be undone.";
    };
    mgmtConfirmThenTail(boxOpView(), {
        title: `Remove box "${box}"`,
        tailTitle: `Removing ${box}`,
        verb: "box_remove",
        confirmLabel: "Remove",
        danger: true,
        body: [
            warn,
            el("label", { class: "mgmt-check" }, [
                keepCb, " preserve artifacts (keep workspace files on disk)",
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Re-enter your master password"]), pwI,
            ]),
        ],
        validate: () => (pwI.value ? null : "Re-enter your master password."),
        // Step-up: retyped password → client-side derivation; the proof rides
        // the request and the broker re-verifies async, so a wrong password
        // surfaces as a FAILED op in phase 2, like destroy.
        request: async () => fetch(
            `/broker/project/${encodeURIComponent(project)}/box/${encodeURIComponent(box)}/remove`,
            {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    proof: await deriveLoginProof(pwI.value),
                    keep_workspace: keepCb.checked,
                }),
            }),
        onDone: async (ok) => { if (ok) await refreshAfterBoxChange(project); },
        focus: () => pwI.focus(),
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

// Periodic re-sync of the ACTIVE project's service tabs. The tab set is
// server-side probe-gated — an exported port (or a box editor) shows only once
// it's actually LISTENING on the supervisor netns — so the documented flow
// "register the port, THEN start serving it" leaves the tab hidden until the
// listener comes up, and nothing re-fetches after that. Without this, a
// newly-listening surface only appears on a manual project re-select or a full
// reload (the same latency the box-editor stub-boot delay has). Runs on the
// rail's reachability cadence (PROBE_INTERVAL_MS) and only touches the strip
// when the id/label signature actually changed, so an unchanged poll is a
// no-op (no flicker, no focus theft).
function scheduleServicesRefresh() {
    if (state.servicesTimer) clearInterval(state.servicesTimer);
    state.servicesTimer = setInterval(refreshActiveServices, PROBE_INTERVAL_MS);
}

// Tab-visible fingerprint: the sorted service ids plus their labels. A newly-
// listening port adds an id; a re-add relabels; a stopped listener drops one —
// all move the signature, so the guard in refreshActiveServices fires only then.
function serviceTabsSignature(map) {
    return Object.keys(map || {}).sort()
        .map((id) => `${id}\x00${(map[id] && map[id].label) || ""}`)
        .join("\x01");
}

async function refreshActiveServices() {
    const name = state.activeProject;
    if (!name) return;
    let fresh;
    try {
        const res = await fetch(`/services/${encodeURIComponent(name)}`);
        if (!res.ok) return;
        fresh = await res.json();
    } catch (_) {
        return;   // transient; the next tick retries
    }
    // The active project (or the whole vault) may have changed while the fetch
    // was in flight — bail rather than render stale tabs onto a different view.
    if (state.activeProject !== name) return;
    const current = state.projectServices[name] || {};
    if (serviceTabsSignature(fresh) === serviceTabsSignature(current)) return;
    state.projectServices[name] = fresh;
    renderServiceTabs(name, fresh);
    // renderServiceTabs rebuilds the strip without the active underline; re-apply
    // it. activateService returns early on an existing, connected terminal, so no
    // open pane is torn down. If the active service vanished (a listener stopped),
    // fall back to the first visible tab — mirrors setServiceHidden.
    const visible = visibleServiceIds(name, fresh);
    if (state.activeService && visible.includes(state.activeService)) {
        activateService(state.activeService);
    } else if (visible.length > 0) {
        // Mobile: visible[0] is code-server whenever the editor is enabled
        // (SERVICES insertion order) — route through the mobile landing
        // preference instead. Desktop keeps the bare first-tab fallback.
        const next = mobileModeActive()
            ? mobileLandingService(name, visible, fresh) : visible[0];
        if (next) activateService(next);
        else { state.activeService = null; showWelcome(false); }
    }
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
    // "Projects list visible" differs by shell: desktop = rail pinned or
    // expanded; mobile = the full-screen Projects view is showing (the rail
    // flags stay untouched on mobile — driving railExpanded instead would
    // arm the outside-click auto-collapse handlers).
    const listVisible = mobileModeActive()
        ? !!document.querySelector(".dashboard.mobile-projects")
        : (state.railPinned || state.railExpanded);
    if (!listVisible) return;
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
    if (badge) setTypeBadge(badge, status.workflow);
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
    if (state.servicesTimer) { clearInterval(state.servicesTimer); state.servicesTimer = null; }
    for (const t of Object.values(state.terminals)) {
        try { if (t.ws) t.ws.close(); } catch (_) {}
        try { if (t.term) t.term.dispose(); } catch (_) {}
    }
    // Locked vault = logged out of Management too: revoke the webui session +
    // broker token (fire-and-forget — a down broker must not block locking).
    // A refresh/tab-close drops JS memory without running this, so that path
    // keeps today's behavior: the session cookie just ages out on its TTL.
    try {
        fetch("/broker/logout", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
        }).catch(() => {});
    } catch (e) { /* best-effort */ }
    state.derivedKey = null;
    state.loginProof = null;
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

// Layout mode selector (Settings): auto = follow the breakpoint; the manual
// values are the escape hatch in both directions. Changing it is a full
// shell re-render (rerenderShell), so this control also closes Settings.
function makeLayoutSelector() {
    const sel = document.createElement("select");
    sel.className = "theme-select";
    sel.title = "Layout";
    const current = localStorage.getItem(MOBILE_MODE_KEY) || "auto";
    for (const [id, label] of [["auto", "Auto (by screen width)"],
                               ["desktop", "Desktop"], ["mobile", "Mobile"]]) {
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = label;
        if (id === current) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.onchange = () => {
        if (sel.value === "auto") localStorage.removeItem(MOBILE_MODE_KEY);
        else localStorage.setItem(MOBILE_MODE_KEY, sel.value);
        rerenderShell();
    };
    return sel;
}

// ---- add / remove project --------------------------------------------------

function openAddProjectModal() {
    // No import-string paste box: its only filler was the output of a host CLI
    // command. A project on this sandbox attaches with one click (Management →
    // Attach, which pulls the SSH coordinates from the broker); an external box
    // is entered by hand below. Neither needs a shell.
    const nameI = el("input", { type: "text" });
    const hostI = el("input", { type: "text" });
    const portI = el("input", { type: "number", min: "1", max: "65535", value: "22" });
    const userI = el("input", { type: "text", value: "research" });
    const passI = el("input", { type: "password", autocomplete: "new-password" });
    const errEl = el("div", { class: "error" });

    const backdrop = el("div", { class: "modal-backdrop" });
    const cancel = el("button", { class: "btn btn-secondary" }, ["Cancel"]);
    cancel.onclick = () => backdrop.remove();

    const save = el("button", { class: "btn" }, ["Import"]);
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
        el("h2", {}, ["Import project"]),
        el("div", { class: "hint" }, [
            "For a project on this sandbox, use Attach under Management — it "
            + "fetches the coordinates for you. This form is for a box you run "
            + "elsewhere.",
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
    delete state.projectLastService[name];
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
    // Mobile twin of the collapse above: picking a project leaves the
    // full-screen Projects view.
    if (mobileModeActive()) setMobileProjectsView(false);

    state.activeProject = name;
    // Bust the per-project service cache on every activation so a service that
    // came up AFTER the first activation (a box editor's code-server stub takes a
    // few seconds to boot) surfaces on re-select — without a full page reload.
    // fetchProjectServices otherwise caches the first probe forever. Idiomatic:
    // mirrors the delete-then-reactivate busts already used on service toggles.
    delete state.projectServices[name];
    const enabled = await fetchProjectServices(name);

    // Load per-project pin state from the vault entry. Missing fields
    // decode cleanly to "no pin, default ratio" — vault schema stays v1.
    const project = state.vault.projects.find((p) => p.name === name);
    const desiredPin = project?.pinned_service || null;
    // Drop the pin if its service is no longer enabled — e.g. operator
    // disabled it between sessions — or if the user has hidden that tab.
    // Cheaper than a vault migration.
    const hiddenSet = new Set(project?.hidden_services || []);
    // No split panes on mobile: the pin (a side-pane concept) is forced off —
    // applySplitLayout then keeps/returns the unsplit shape.
    state.pinnedService = !mobileModeActive()
        && desiredPin && enabled[desiredPin] && !hiddenSet.has(desiredPin)
        ? desiredPin : null;
    const ratio = project?.split_ratio;
    state.splitRatio = typeof ratio === "number" ? ratio : SPLIT_RATIO_DEFAULT;

    renderServiceTabs(name, enabled);
    applySplitLayout();

    // Landing tab: a project reopens on whatever it last had open this session
    // (per-project memory). A project with no remembered tab — never opened, or
    // reset by a stop/start / destroy — defaults to the editor when enabled, then
    // the first always-on (CLI), then any. code-server is kind:http so it was never
    // the always_on default; preferring it here is what makes a fresh project (and
    // a freshly-restarted one) land on the editor instead of the CLI.
    const visible = visibleServiceIds(name, enabled);
    if (visible.length === 0) {
        state.activeService = null;
        showWelcome();
        return;
    }
    let next;
    if (mobileModeActive()) {
        // Mobile never lands on a visual surface except the reader (those
        // tabs are hidden; the editor is deliberately unreachable on mobile).
        next = mobileLandingService(name, visible, enabled);
        if (!next) {
            // Visible set is all-visual (e.g. every CLI tab hidden on
            // desktop) — nothing to land on; keep the strip populated.
            state.activeService = null;
            showWelcome(false);
            return;
        }
    } else {
        next = state.projectLastService[name];
        if (!next || !enabled[next] || !visible.includes(next) || next === state.pinnedService) {
            next = (visible.includes("code-server") && "code-server" !== state.pinnedService && "code-server")
                || visible.find((id) => id !== state.pinnedService && enabled[id].always_on)
                || visible.find((id) => id !== state.pinnedService)
                || visible[0];
        }
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
        // Ports render even with no tabs loaded — register before you start serving.
        appendExportedPortsSection(box, project);
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
    appendExportedPortsSection(box, project);
    appendEditorExtensionSection(box, project, enabled);
    return box;
}

// The editor "Extensions" surface (STAGE_BOX_EXT_UX C) — host-container webui
// surfaces, the unified "Add extension" sense (Editor now; Overleaf-style compose
// surfaces later). Client-rendered from the services map: `code-server` present ⇒
// on. The toggle goes through `update` enable/disable code-server, which RECREATES
// the container — supported on BOTH substrates now (dind via _recreate_supervisor,
// docker via _recreate_docker_substrate). Universal (renders on every flavor).
function appendEditorExtensionSection(box, project, enabled) {
    const section = el("div", { class: "config-section" });
    section.appendChild(el("div", { class: "config-section-label" }, ["Extensions"]));
    const editorOn = Object.prototype.hasOwnProperty.call(enabled, "code-server");
    const row = el("div", { class: "config-box-row" }, [
        el("span", { class: "config-box-name" }, ["Editor"]),
        el("span", { class: "config-box-meta" }, [editorOn ? "on" : "off"]),
    ]);
    // A bare docker box has no creds-stash, so the recreate resets its in-box
    // claude login (dind stashes creds across the supervisor recreate).
    const isDocker = !(enabled.supervisor && enabled.supervisor.box_harness);
    const btn = el("button", { class: "btn btn-secondary" },
                  [editorOn ? "Disable" : "Enable"]);
    btn.onclick = () => mgmtEditorToggle(project.name, editorOn, isDocker);
    row.appendChild(btn);
    section.appendChild(row);
    // Reader toggle (STAGE_READER) — dind-only (a docker box has no reader), so it
    // is omitted there rather than offering a control the broker refuses. The reader
    // deploys/removes LIVE on dind (no recreate), like the dind editor.
    if (!isDocker) {
        const readerOn = Object.prototype.hasOwnProperty.call(enabled, "reader");
        const rrow = el("div", { class: "config-box-row" }, [
            el("span", { class: "config-box-name" }, ["Reader"]),
            el("span", { class: "config-box-meta" }, [readerOn ? "on" : "off"]),
        ]);
        const rbtn = el("button", { class: "btn btn-secondary" },
                       [readerOn ? "Disable" : "Enable"]);
        rbtn.onclick = () => mgmtReaderToggle(project.name, readerOn);
        rrow.appendChild(rbtn);
        section.appendChild(rrow);
    }
    box.appendChild(section);
}

// The exported-ports surface (STAGE_EXPORTED_PORTS): register a port the PI is
// serving inside the supervisor (rs-project-<proj>:<port>) so it shows as an http
// tab. Renders REGARDLESS of whether services have loaded — you register a port,
// THEN start serving it. The list is the broker's port_list; the tab itself only
// appears once the port is actually listening (server-side probe-gated).
function appendExportedPortsSection(box, project) {
    const section = el("div", { class: "config-section" });
    section.appendChild(el("div", { class: "config-section-label" }, ["Ports"]));
    const listWrap = el("div", { class: "config-ports-list" });
    section.appendChild(listWrap);

    const portI = el("input", { type: "number", min: "1", max: "65535",
                                class: "config-port-num", placeholder: "port" });
    const labelI = el("input", { type: "text", class: "config-port-label",
                                 placeholder: "label" });
    const addBtn = el("button", { class: "btn btn-secondary" }, ["Add"]);

    async function loadPorts() {
        listWrap.innerHTML = "";
        let body;
        try {
            const res = await fetch(
                `/broker/project/${encodeURIComponent(project.name)}/ports`);
            try { body = await res.json(); } catch (e) { body = {}; }
            if (!res.ok || !body.ok || !body.result) {
                listWrap.appendChild(el("div", { class: "config-empty" },
                    [mgmtErrText(body) || "Couldn't load ports."]));
                return;
            }
        } catch (e) {
            listWrap.appendChild(el("div", { class: "config-empty" },
                ["Couldn't load ports."]));
            return;
        }
        const ports = body.result.ports || [];
        if (ports.length === 0) {
            listWrap.appendChild(el("div", { class: "config-empty" },
                ["No ports exported yet."]));
            return;
        }
        for (const e of ports) {
            const rm = el("button", { class: "close-tab-btn",
                                      title: `Remove port ${e.port}` }, ["✕"]);
            rm.onclick = () => removePort(e.port);
            listWrap.appendChild(el("div", { class: "config-box-row" }, [
                el("span", { class: "config-box-name" }, [`${e.port} — ${e.label}`]),
                rm,
            ]));
        }
    }

    async function addPort() {
        const port = parseInt(portI.value, 10);
        const label = labelI.value.trim();
        if (!Number.isInteger(port) || port < 1 || port > 65535) {
            alert("Enter a port between 1 and 65535."); return;
        }
        if (!label) { alert("Enter a label for the tab."); return; }
        addBtn.disabled = true;
        try {
            const res = await fetch(
                `/broker/project/${encodeURIComponent(project.name)}/port`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ port, label }),
                });
            let body; try { body = await res.json(); } catch (e) { body = {}; }
            if (!res.ok || !body.ok) {
                alert("Couldn't add port: " + (mgmtErrText(body) || res.status));
                return;
            }
            portI.value = ""; labelI.value = "";
            await loadPorts();
            await refreshAfterBoxChange(project.name);
        } finally {
            addBtn.disabled = false;
        }
    }

    async function removePort(port) {
        try {
            const res = await fetch(
                `/broker/project/${encodeURIComponent(project.name)}/port-remove`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ port }),
                });
            let body; try { body = await res.json(); } catch (e) { body = {}; }
            if (!res.ok || !body.ok) {
                alert("Couldn't remove port: " + (mgmtErrText(body) || res.status));
                return;
            }
            await loadPorts();
            await refreshAfterBoxChange(project.name);
        } catch (e) { /* best-effort */ }
    }

    addBtn.onclick = addPort;
    section.appendChild(el("div", { class: "config-box-row config-port-add" },
                           [portI, labelI, addBtn]));
    section.appendChild(el("div", { class: "config-hint" }, [
        "The tab appears once something is listening on that port inside the supervisor.",
    ]));
    box.appendChild(section);
    loadPorts();
}

function mgmtEditorToggle(name, on, isDocker) {
    const word = on ? "Disable" : "Enable";
    // dind deploys/removes the editor live (no recreate); docker must recreate the
    // runc container (its editor is a bind-mount, addable only at create).
    const body = [el("p", {}, [isDocker
        ? `${word} the code-server editor on "${name}". This recreates the ` +
          "project's container (fresh container); running work in it is interrupted."
        : `${word} the code-server editor on "${name}". This deploys it live ` +
          "(no recreate) — it takes effect on the next page load."])];
    if (isDocker) {
        body.push(el("p", { class: "hint" }, [
            "On a bare docker box this resets the in-box claude login — run " +
            "`claude` then /login again inside afterwards.",
        ]));
    }
    mgmtConfirmThenTail(boxOpView(), {
        title: `${word} the editor on ${name}`,
        tailTitle: `${on ? "Disabling" : "Enabling"} the editor on ${name}`,
        verb: "update",
        confirmLabel: word,
        body: body,
        request: () => fetch(`/broker/project/${encodeURIComponent(name)}/update`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(on ? { disable: ["code-server"] } : { enable: ["code-server"] }),
        }),
        onDone: async (ok) => { if (ok) await refreshAfterBoxChange(name); },
    });
}

// Reader toggle (STAGE_READER) — dind-only, so no docker-recreate branch: the
// reader always deploys/removes LIVE via the update reader-flip live-toggle path.
function mgmtReaderToggle(name, on) {
    const word = on ? "Disable" : "Enable";
    mgmtConfirmThenTail(boxOpView(), {
        title: `${word} the reader on ${name}`,
        tailTitle: `${on ? "Disabling" : "Enabling"} the reader on ${name}`,
        verb: "update",
        confirmLabel: word,
        body: [el("p", {}, [`${word} the mobile artifact reader on "${name}". ` +
            "This deploys it live (no recreate) — it takes effect on the next page load."])],
        request: () => fetch(`/broker/project/${encodeURIComponent(name)}/update`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(on ? { disable: ["reader"] } : { enable: ["reader"] }),
        }),
        onDone: async (ok) => { if (ok) await refreshAfterBoxChange(name); },
    });
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
        // Same mobile guard as the refreshActiveServices fallback: never
        // auto-open a hidden-on-mobile visual tab.
        const next = mobileModeActive()
            ? mobileLandingService(project.name, visible, enabled) : visible[0];
        if (next) activateService(next);
        else { state.activeService = null; showWelcome(false); }
    }
}

// "Visual vs CLI" is a product grouping, not a transport one, so it's an
// explicit (optional) `surface` field defaulting to kind-derived: http
// surfaces (code-server, future exported ports) are visual; ssh surfaces
// (Supervisor/Management/PI shells) are cli. An entry may override by
// declaring `surface` in services.py — it rides the whole-dict /services
// payload to here for free.
function surfaceOf(id, svc) {
    return svc.surface || (svc.kind === "http" ? "visual" : "cli");
}

// Per-tab icon name, default-derived: the editor gets the code glyph, any
// cli surface gets the terminal glyph, everything else the generic window.
function iconOf(id, svc) {
    if (svc.icon) return svc.icon;
    if (id === "code-server") return "editor";
    return surfaceOf(id, svc) === "cli" ? "terminal" : "generic";
}

// Hand-authored inline SVG glyphs (CSP-clean: inline markup, currentColor,
// no external refs). Built via innerHTML — the makePinButton() precedent —
// because el() routes through createElement, which makes an inert
// HTML-namespace <svg> that does not render.
const TAB_ICON_SVG = {
    editor: '<svg viewBox="0 0 16 16" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M5.5 3.4 1 8l4.5 4.6L7 11.1 3.9 8 7 4.9zM10.5 3.4 9 4.9 12.1 8 9 11.1l1.5 1.5L15 8z"/></svg>',
    terminal: '<svg viewBox="0 0 16 16" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M2 2.9 3.4 1.5 9.9 8l-6.5 6.5L2 13.1 7.1 8z"/><path d="M8 12h6v2H8z"/></svg>',
    generic: '<svg viewBox="0 0 16 16" fill="currentColor" fill-rule="evenodd" xmlns="http://www.w3.org/2000/svg"><path d="M1.5 3h13v10h-13V3zm1.5 1.5v7h10v-7H3z"/></svg>',
};

function iconSvg(name) {
    const span = el("span", { class: "tab-icon" });
    span.innerHTML = TAB_ICON_SVG[name] || TAB_ICON_SVG.generic;
    return span;
}

function renderServiceTabs(projectName, enabled) {
    const strip = document.getElementById("service-tabs");
    if (!strip) return;
    strip.innerHTML = "";
    strip.appendChild(makeProjectsTab());
    // Active-project label — a bordered chip (distinct from the service tabs),
    // followed by a separator (same rule as the visual/cli divider) so it reads as
    // an anchor for the strip rather than the first tab. On mobile the chip IS
    // the Projects-view toggle (the desktop Projects tab is CSS-hidden there);
    // on desktop the guard keeps it inert static text.
    const chip = el("div", { class: "active-project" }, [projectName]);
    chip.onclick = () => {
        if (!mobileModeActive()) return;
        mobileToggleProjects();
    };
    strip.appendChild(chip);
    strip.appendChild(el("div", { class: "tab-group-divider" }));
    const ids = visibleServiceIds(projectName, enabled);
    if (ids.length === 0) {
        strip.appendChild(el("div", { class: "empty" }, [
            "No services enabled for this project.",
        ]));
        return;
    }
    // Partition into Visual (editors / iframe surfaces) then CLI (terminals),
    // preserving SERVICES insertion order within each group. Visual leads —
    // the editor is the primary work surface. A thin vertical rule separates
    // the groups, omitted when either is empty so a CLI-only project (e.g. a
    // bare docker box with no live editor) shows no orphan divider.
    // The box harness is a standing dind utility (STAGE_DIND_UNIFY): the server
    // stamps `box_harness` on the always-present Supervisor tab spec for ANY dind
    // project (research + sandbox-dind), so the "+ Add box" control shows on both.
    // A pi-iso-* tab is a disposable BOX (gets the ✕) when box_kind === "sandbox".
    const boxHarness = !!(enabled["supervisor"] && enabled["supervisor"].box_harness);
    const visual = [], cli = [];
    for (const id of ids) {
        (surfaceOf(id, enabled[id]) === "visual" ? visual : cli).push(id);
    }
    const makeTab = (id) => {
        const svc = enabled[id];
        const isPinned = id === state.pinnedService;
        const pinBtn = el("button", {
            class: "pin-tab-btn",
            title: isPinned ? "Unpin from side" : "Pin to side",
        }, ["⇥"]);
        pinBtn.onclick = (ev) => { ev.stopPropagation(); togglePin(id); };
        const kids = [iconSvg(iconOf(id, svc)), el("span", {}, [svc.label || id]), pinBtn];
        // A real box tab (box_kind === "sandbox") carries a ✕ that discards the box
        // in place — box deletion lives on the tab, not in a sidebar settings panel.
        if (boxHarness && id.startsWith("pi-iso-") && svc.box_kind === "sandbox") {
            const boxName = id.slice("pi-iso-".length);
            const closeBtn = el("button", {
                class: "close-tab-btn", title: `Remove box "${boxName}"`,
            }, ["✕"]);
            closeBtn.onclick = (ev) => {
                ev.stopPropagation();
                mgmtBoxRemoveDialog(projectName, boxName);
            };
            kids.push(closeBtn);
        }
        return el("div", {
            class: isPinned ? "tab pinned" : "tab",
            "data-service": id,
            // Surface stamp for the mobile CSS (visual tabs are hidden there);
            // nothing desktop keys on it.
            "data-surface": surfaceOf(id, svc),
            onclick: () => activateService(id),
        }, kids);
    };
    for (const id of visual) strip.appendChild(makeTab(id));
    if (visual.length > 0 && cli.length > 0) {
        strip.appendChild(el("div", { class: "tab-group-divider" }));
    }
    for (const id of cli) strip.appendChild(makeTab(id));
    // + Add box lives on the strip for ANY dind project (research + sandbox-dind);
    // boxes are created where they appear, not in a sidebar settings panel. On
    // research the harness is staged lazily on the first box_add (STAGE_DIND_UNIFY).
    if (boxHarness) {
        const addBox = el("button", {
            class: "add-box-tab-btn", title: "Add a box",
        }, ["+"]);
        addBox.onclick = () => mgmtBoxAddDialog(projectName);
        strip.appendChild(addBox);
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
        // Remember this as the project's landing tab (main-pane only; the pinned
        // side-pane activation below is skipped by the !isPinned guard).
        state.projectLastService[state.activeProject] = serviceId;
    }
    // Key-bar visibility follows the resolved KIND of the active service, so
    // it must be derived here — BEFORE the existing-terminal fast path below
    // returns early — or a reader→CLI re-activation would skip it.
    updateMobileKeybar();

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

function welcomeText() {
    const none = !state.vault || state.vault.projects.length === 0;
    if (mobileModeActive()) {
        return none ? "No projects yet. Add one from the project list."
                    : "Tap the project name to switch projects.";
    }
    return none ? "No projects yet. Click the Projects tab to add one."
                : "Click the Projects tab to attach.";
}

// clearTabs=false is the mobile reader empty-state's path: it must keep the
// tab strip — nothing re-renders the strip afterward (activateService only
// class-toggles existing tabs; the services poll rebuilds only on a
// signature change). The sole desktop caller passes nothing → default true →
// byte-identical behavior.
function showWelcome(clearTabs = true) {
    for (const t of Object.values(state.terminals)) {
        if (t.container) t.container.classList.add("hidden");
    }
    if (clearTabs) {
        const strip = document.getElementById("service-tabs");
        if (strip) strip.innerHTML = "";
    }
    const welcome = document.getElementById("welcome");
    if (welcome) welcome.style.display = "";
    // Callers null state.activeService before showing the welcome — hide the
    // key bar to match (no-op on desktop / when the bar doesn't exist).
    updateMobileKeybar();
    // The clearTabs wipe above removes both openers — restore whichever this
    // layout uses, or a project-less strip is a softlock (mobile: the drawer
    // chip; desktop: the Projects tab of an unpinned, auto-collapsed rail).
    // Both are guarded + idempotent, so the showWelcome(false) callers — whose
    // strip still carries its openers — no-op through here.
    ensureMobileChip();
    ensureProjectsTab();
}

// ---- ssh-kind terminal -----------------------------------------------------

// ONE window-level refit for all terminals, installed once at bootstrap. It
// replaces the old per-openSshTerminal "resize" listeners (which leaked one
// per open and refit only the active terminal): this refits the active AND
// the pinned terminal — the only visible ones. It also maintains --vvh, the
// visual-viewport height var the mobile shell uses as its layout height: on
// iOS (no interactive-widget viewport support) the soft keyboard shrinks
// only the visual viewport, so --vvh is what keeps the terminal + bottom nav
// above the keyboard, and the refit resends rows/cols over the ws.
let globalTermRefitInstalled = false;
function installGlobalTermRefit() {
    if (globalTermRefitInstalled) return;
    globalTermRefitInstalled = true;
    const setVvh = () => {
        if (!window.visualViewport) return;
        document.documentElement.style.setProperty(
            "--vvh", `${Math.round(window.visualViewport.height)}px`);
    };
    let raf = 0;
    const onViewportChange = () => {
        if (raf) return;
        raf = requestAnimationFrame(() => {
            raf = 0;
            setVvh();
            if (!state.activeProject) return;
            const keys = new Set();
            if (state.activeService) keys.add(tkey(state.activeProject, state.activeService));
            if (state.pinnedService) keys.add(tkey(state.activeProject, state.pinnedService));
            for (const k of keys) {
                const t = state.terminals[k];
                if (t && t.fitAddon) { try { t.fitAddon.fit(); } catch (_) {} }
            }
        });
    };
    window.addEventListener("resize", onViewportChange);
    if (window.visualViewport) {
        window.visualViewport.addEventListener("resize", onViewportChange);
        setVvh();   // seed before the first resize event
    }
}

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
        // Mobile key bar: a latched one-shot Ctrl transforms the next typed
        // character (no-op when the latch is off, i.e. always on desktop).
        const data = consumeKeybarCtrl(d);
        if (ws.readyState === WebSocket.OPEN) {
            ws.send(new TextEncoder().encode(data));
        }
    });

    term.onResize(({ rows, cols }) => {
        if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "resize", rows, cols }));
        }
    });
    // Window-resize refit is handled by the single bootstrap-installed
    // installGlobalTermRefit listener (the old per-terminal listener here
    // leaked one registration per open).
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

    // Mount the iframe at the service's own browser origin (a dedicated
    // webui port per container — /services/<project> allocates it and hands
    // back origin_url). The session cookie minted above is host-keyed and
    // port-blind, so it reaches the origin port with no extra handshake.
    if (!svc || !svc.origin_url) {
        status.textContent = "No origin port available for this service.";
        return;
    }
    const iframe = el("iframe", {
        class: "http-iframe",
        src: svc.origin_url,
        // Only the absolute minimum sandbox the upstream needs. code-server
        // needs scripts, same-origin (cookies), forms, popups (its
        // command-palette opens windows for some commands), modals, and
        // clipboard. Drop top-navigation: prevents iframe-escape.
        sandbox: [
            "allow-scripts", "allow-same-origin", "allow-forms",
            "allow-popups", "allow-popups-to-escape-sandbox",
            "allow-modals", "allow-downloads",
        ].join(" "),
        // The frame is CROSS-origin now (its own origin port), so Permissions-
        // Policy features whose default allowlist is 'self' — fullscreen (F11 /
        // the editor's toggle-fullscreen), clipboard — are denied unless the
        // embedder delegates them explicitly. Same-origin got these for free
        // under the retired path proxy.
        allow: "fullscreen; clipboard-read; clipboard-write",
        allowfullscreen: "",
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
            `Click OK only if you intentionally recreated the supervisor (e.g. you changed its services from Management) — otherwise this could be a man-in-the-middle.`,
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
    // Mobile class before the first render — the unlock/setup cards are
    // already styled by the html.mobile scope.
    applyMobileClass();
    installMobileModeWatcher();
    installGlobalTermRefit();
    installMobileProjectsOutsideClose();
    installRailOutsideClickHandlers();
    if (loadStored()) renderUnlock();
    else renderSetup();
});
