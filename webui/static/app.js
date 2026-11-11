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
// Terminal palette, chosen apart from the interface theme. Absent = follow
// the interface theme's own terminal palette (the default, and the only
// behavior before the split).
const TERM_THEME_KEY = "rs-webui-term-theme";
const IFRAME_ZOOM_KEY = "rs-webui-iframe-zoom";
const IFRAME_ZOOMS = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2];
const IFRAME_ZOOM_DEFAULT = 0.9;
// Optional px cap on ssh (CLI) terminal panes, for line-length readability on
// wide non-split windows. Applied WINDOW-side (a max-width on .terminal-pad,
// which xterm-addon-fit measures) — never via xterm options, so the
// xterm↔agent wiring stays untouched. 0 = off (full width, the default).
// Values reasoned from the terminal font at its DEFAULT size (fontSize 13,
// monospace advance ≈ 0.6em ⇒ ~7.8px/col): 800 ≈ 100 cols, 1000 ≈ 125 cols,
// 1200 ≈ 150 cols. A changed CLI font size shifts the ~col estimates (the
// labels stay honest approximations); the px cap itself is exact regardless.
const CLI_WIDTH_KEY = "rs-webui-cli-max-width";
const CLI_MAX_WIDTHS = [0, 800, 1000, 1200];
// xterm font size for ssh (CLI) terminal panes, applied globally and LIVE to
// open terminals (the applyTheme options-proxy pattern + a refit, since cell
// metrics change). 13 is the previously hardcoded constructor value — keeping
// it as the default leaves unset behavior byte-identical. Integer px only
// (xterm cell metrics); steps widen upward because the relative change per px
// shrinks as sizes grow (the IFRAME_ZOOMS 6-option shape).
const TERM_FONT_SIZE_KEY = "rs-webui-term-font-size";
const TERM_FONT_SIZES = [11, 12, 13, 14, 16, 18];
const TERM_FONT_SIZE_DEFAULT = 13;
const RAIL_PINNED_KEY = "rs-webui-rail-pinned";
const RAIL_WIDTH_KEY = "rs-webui-rail-width";
const RAIL_WIDTH_DEFAULT = 200;
// Max prevents the rail from swallowing the terminal area on narrow
// viewports. Min is intentionally permissive — at 80px the footer
// dropdowns clip but project names + status dots stay legible, which
// is the only thing the rail actually has to show when shrunk to a
// status strip. Same posture as COLUMN_MIN_FRAC.
const RAIL_WIDTH_MIN = 80;
const RAIL_WIDTH_MAX = 480;
// The ⚙ project settings panel's width and height after the operator dragged
// its corner (one size for every project's panel; the defaults live in
// style.css). Kept apart, so a panel only ever dragged wider still follows
// its content's height.
const CONFIG_PANEL_WIDTH_KEY = "rs-webui-config-panel-width";
const CONFIG_PANEL_HEIGHT_KEY = "rs-webui-config-panel-height";
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
// The rail's host CPU/RAM bars refresh on their own cadence. Each tick is two
// /proc reads server-side and one tiny request, so cost does not set it; it
// is how fresh the RAM figure is and how often the CPU minute moves on (the
// CPU figure is a difference of two counter readings, exact at any cadence) —
// slower than ~10 s, memory you just claimed (a build you started) shows up
// late.
const HOST_STATS_INTERVAL_MS = 5000;
// The CPU bar is the average over this window, not the last tick: a per-tick
// figure swings with every short burst and reads as noise beside the rail.
const CPU_AVG_WINDOW_MS = 60000;
const BYTES_PER_GB = 1024 ** 3;   // GB as free/htop/docker print it (2^30)
// Column layout: up to three side-by-side groups, each a width fraction of
// the terminal area. The floor keeps every column usable — below ~15% of a
// typical desktop area neither a tab strip nor a terminal has workable
// width — and the splitter clamp derives from it (a drag can never crush a
// neighbor to nothing). Three columns is the ceiling the PI set: the daily
// case is editor + one or two terminal groups; beyond that, panes stop
// earning their width.
const MAX_TAB_COLUMNS = 3;
const COLUMN_MIN_FRAC = 0.15;
// Fraction of a column's width forming the left/right drop bands that split
// a new column out of it. 0.2 is wide enough to hit at drag speed while
// keeping the center (move-into) zone dominant.
const COLUMN_EDGE_BAND_FRAC = 0.2;
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
            "--pane-bg": "#000",
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
            "--pane-bg": "#ffffff",
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
            "--pane-bg": "#002b36",
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
            "--pane-bg": "#282a36",
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
            "--pane-bg": "#2e3440",
        },
    },
};

const DEFAULT_THEME = "nord";

// Terminal-only palettes (Settings → Terminal theme). The interface themes'
// own palettes are REFERENCED, never copied, so "Same as interface theme"
// and naming that same theme explicitly can never drift apart. `tone` only
// groups the dropdown. The added palettes follow each project's published
// terminal colors; Monokai Dimmed is the terminal panel of the default editor
// theme (code-server-settings.json) — the bundled theme gives its ANSI
// colors but no terminal foreground, cursor or selection, so those are
// code-server's own fallbacks (foreground/cursor #cccccc, selection = the
// editor selection #676b7180), and its ANSI black equals its background
// exactly as it does in the editor.
const TERM_THEMES = {
    dark: { label: "Dark", tone: "dark", xterm: THEMES.dark.xterm },
    "solarized-dark": { label: "Solarized Dark", tone: "dark", xterm: THEMES["solarized-dark"].xterm },
    dracula: { label: "Dracula", tone: "dark", xterm: THEMES.dracula.xterm },
    nord: { label: "Nord", tone: "dark", xterm: THEMES.nord.xterm },
    "gruvbox-dark": {
        label: "Gruvbox Dark", tone: "dark",
        xterm: {
            background: "#282828", foreground: "#ebdbb2",
            cursor: "#ebdbb2", cursorAccent: "#282828",
            selectionBackground: "rgba(146,131,116,0.4)",
            black: "#282828", red: "#cc241d", green: "#98971a", yellow: "#d79921",
            blue: "#458588", magenta: "#b16286", cyan: "#689d6a", white: "#a89984",
            brightBlack: "#928374", brightRed: "#fb4934", brightGreen: "#b8bb26",
            brightYellow: "#fabd2f", brightBlue: "#83a598", brightMagenta: "#d3869b",
            brightCyan: "#8ec07c", brightWhite: "#ebdbb2",
        },
    },
    "one-dark": {
        label: "One Dark", tone: "dark",
        xterm: {
            background: "#282c34", foreground: "#abb2bf",
            cursor: "#528bff", cursorAccent: "#282c34",
            selectionBackground: "rgba(103,118,150,0.4)",
            black: "#3f4451", red: "#e05561", green: "#8cc265", yellow: "#d18f52",
            blue: "#4aa5f0", magenta: "#c162de", cyan: "#42b3c2", white: "#e6e6e6",
            brightBlack: "#4f5666", brightRed: "#ff616e", brightGreen: "#a5e075",
            brightYellow: "#f0a45d", brightBlue: "#4dc4ff", brightMagenta: "#de73ff",
            brightCyan: "#4cd1e0", brightWhite: "#ffffff",
        },
    },
    "tokyo-night": {
        label: "Tokyo Night", tone: "dark",
        xterm: {
            background: "#1a1b26", foreground: "#c0caf5",
            cursor: "#c0caf5", cursorAccent: "#1a1b26",
            selectionBackground: "#283457",
            black: "#15161e", red: "#f7768e", green: "#9ece6a", yellow: "#e0af68",
            blue: "#7aa2f7", magenta: "#bb9af7", cyan: "#7dcfff", white: "#a9b1d6",
            brightBlack: "#414868", brightRed: "#f7768e", brightGreen: "#9ece6a",
            brightYellow: "#e0af68", brightBlue: "#7aa2f7", brightMagenta: "#bb9af7",
            brightCyan: "#7dcfff", brightWhite: "#c0caf5",
        },
    },
    "catppuccin-mocha": {
        label: "Catppuccin Mocha", tone: "dark",
        xterm: {
            background: "#1e1e2e", foreground: "#cdd6f4",
            cursor: "#f5e0dc", cursorAccent: "#1e1e2e",
            selectionBackground: "rgba(88,91,112,0.6)",
            black: "#45475a", red: "#f38ba8", green: "#a6e3a1", yellow: "#f9e2af",
            blue: "#89b4fa", magenta: "#f5c2e7", cyan: "#94e2d5", white: "#bac2de",
            brightBlack: "#585b70", brightRed: "#f38ba8", brightGreen: "#a6e3a1",
            brightYellow: "#f9e2af", brightBlue: "#89b4fa", brightMagenta: "#f5c2e7",
            brightCyan: "#94e2d5", brightWhite: "#a6adc8",
        },
    },
    "monokai-dimmed": {
        label: "Monokai Dimmed", tone: "dark",
        xterm: {
            background: "#1e1e1e", foreground: "#cccccc",
            cursor: "#cccccc", cursorAccent: "#1e1e1e",
            selectionBackground: "#676b7180",
            black: "#1e1e1e", red: "#c4265e", green: "#86b42b", yellow: "#b3b42b",
            blue: "#6a7ec8", magenta: "#8c6bc8", cyan: "#56adbc", white: "#e3e3dd",
            brightBlack: "#666666", brightRed: "#f92672", brightGreen: "#a6e22e",
            brightYellow: "#e2e22e", brightBlue: "#819aff", brightMagenta: "#ae81ff",
            brightCyan: "#66d9ef", brightWhite: "#f8f8f2",
        },
    },
    light: { label: "Light", tone: "light", xterm: THEMES.light.xterm },
    "solarized-light": {
        label: "Solarized Light", tone: "light",
        xterm: {
            background: "#fdf6e3", foreground: "#657b83",
            cursor: "#586e75", cursorAccent: "#fdf6e3",
            selectionBackground: "rgba(7,54,66,0.15)",
            black: "#073642", red: "#dc322f", green: "#859900", yellow: "#b58900",
            blue: "#268bd2", magenta: "#d33682", cyan: "#2aa198", white: "#eee8d5",
            brightBlack: "#002b36", brightRed: "#cb4b16", brightGreen: "#586e75",
            brightYellow: "#657b83", brightBlue: "#839496", brightMagenta: "#6c71c4",
            brightCyan: "#93a1a1", brightWhite: "#fdf6e3",
        },
    },
    "catppuccin-latte": {
        label: "Catppuccin Latte", tone: "light",
        xterm: {
            background: "#eff1f5", foreground: "#4c4f69",
            cursor: "#dc8a78", cursorAccent: "#eff1f5",
            selectionBackground: "rgba(172,176,190,0.6)",
            black: "#5c5f77", red: "#d20f39", green: "#40a02b", yellow: "#df8e1d",
            blue: "#1e66f5", magenta: "#ea76cb", cyan: "#179299", white: "#acb0be",
            brightBlack: "#6c6f85", brightRed: "#d20f39", brightGreen: "#40a02b",
            brightYellow: "#df8e1d", brightBlue: "#1e66f5", brightMagenta: "#ea76cb",
            brightCyan: "#179299", brightWhite: "#bcc0cc",
        },
    },
    "github-light": {
        label: "GitHub Light", tone: "light",
        xterm: {
            background: "#ffffff", foreground: "#1f2328",
            cursor: "#0969da", cursorAccent: "#ffffff",
            selectionBackground: "rgba(9,105,218,0.2)",
            black: "#24292f", red: "#cf222e", green: "#116329", yellow: "#4d2d00",
            blue: "#0969da", magenta: "#8250df", cyan: "#1b7c83", white: "#6e7781",
            brightBlack: "#57606a", brightRed: "#a40e26", brightGreen: "#1a7f37",
            brightYellow: "#633c01", brightBlue: "#218bff", brightMagenta: "#a475f9",
            brightCyan: "#3192aa", brightWhite: "#8c959f",
        },
    },
};

function loadStoredTheme() {
    const id = localStorage.getItem(THEME_KEY);
    return THEMES[id] ? id : DEFAULT_THEME;
}

// null = follow the interface theme (the stored key is absent).
function loadStoredTermTheme() {
    const id = localStorage.getItem(TERM_THEME_KEY);
    return TERM_THEMES[id] ? id : null;
}

function applyTheme(id) {
    // Unknown ids resolve to the default here, not at the reader: the
    // terminal repaint below reads state.theme back through
    // currentXtermTheme().
    const valid = THEMES[id] ? id : DEFAULT_THEME;
    for (const [k, v] of Object.entries(THEMES[valid].css)) {
        document.documentElement.style.setProperty(k, v);
    }
    state.theme = valid;
    localStorage.setItem(THEME_KEY, valid);
    repaintTerminals();
}

function applyTermTheme(id) {
    state.termTheme = TERM_THEMES[id] ? id : null;
    if (state.termTheme) localStorage.setItem(TERM_THEME_KEY, state.termTheme);
    else localStorage.removeItem(TERM_THEME_KEY);
    repaintTerminals();
}

function currentXtermTheme() {
    const pinned = state.termTheme && TERM_THEMES[state.termTheme];
    return pinned ? pinned.xterm : THEMES[state.theme || DEFAULT_THEME].xterm;
}

// Live recolor of every open terminal, plus the --xterm-bg var that paints
// each ssh pane's own frame (style.css .ssh-instance) so the pad's inset and
// row-rounding strip match the palette rather than the interface. Colors
// never touch cell metrics, so this is safe on hidden panes (unlike font
// size — see syncTermFontSize).
function repaintTerminals() {
    const x = currentXtermTheme();
    document.documentElement.style.setProperty("--xterm-bg", x.background);
    for (const t of Object.values(state.terminals)) {
        if (t.term) t.term.options.theme = x;
    }
}

const state = {
    derivedKey: null,        // CryptoKey | null
    salt: null,              // Uint8Array | null
    loginProof: null,        // string | null — broker login derivation; memory-only
                             // sibling of derivedKey (set at setup/post-decrypt
                             // unlock, cleared on lock, NEVER persisted)
    vault: null,             // { version, projects, settings } | null
    activeProject: null,     // string | null
    hostPage: null,          // "development" | "sandboxes" | null
    explainIndex: null,      // string[] of workflows with a rendered Explain doc (/static/explain/index.json); null = unfetched
    workingModes: null,      // [{id, label, description, subagents}] from /broker/working-modes; null = unfetched or failed (workingModesError says why)
    workingModesError: "",
    modelCatalog: null,      // { efforts, models, defaults, types } from /broker/models; null = unfetched. The webui image has no `models/` (it sits outside the ./webui build context), so this relay is the only source — and every picker pre-selects from its `defaults`, the same ones the verbs fall back to, so browser and CLI cannot diverge.
    activeService: null,     // string | null
    serviceRegistry: null,   // { [serviceId]: spec } from /services
    projectServices: {},     // { [projectName]: { [serviceId]: spec } }
    // Detected web apps (STAGE_AUTO_PORTS). detectPaused: a gated tab fetch
    // answered 401, so tabs come from plain /services until the next
    // successful Management sign-in (loginGen counts those, so a stale 401
    // landing after a fresh sign-in cannot re-pause). detectInFlight /
    // detectPending: one detect=1 per project at a time, one queued behind it.
    detectPaused: false,
    lastDetection: {},       // { [projectName]: the marker of its last tab fetch }
    loginGen: 0,
    detectInFlight: {},
    detectPending: {},
    projectLastService: {},  // { [projectName]: serviceId } — per-project landing memory (in-memory; reload/restart resets to the editor default)
    terminals: {},           // "${project}:${service}" -> { term, fitAddon, ws, container, project, service }
    probeTimer: null,
    servicesTimer: null,
    hostStatsTimer: null,
    hostStats: null,         // last painted {cpu: 0..1 | null, memUsed, memTotal}; null = no data, bars hidden
    hostCpuSamples: [],      // raw {t, busy, total} jiffies samples of the last CPU_AVG_WINDOW_MS
    hostStatsInFlight: false,  // one request at a time: a stalled webui must not pile them up
    brokerStale: false,      // the broker runs older code than is on the host (from /broker/host-stats)
    theme: null,
    termTheme: null,         // persisted: TERM_THEMES id, or null = follow the interface theme
    railPinned: false,       // persisted: keep rail in flex flow (push layout)
    railExpanded: false,     // in-memory: rail visible (overlay when unpinned)
    railWidth: RAIL_WIDTH_DEFAULT,  // persisted: rail width in px
    columnActive: [],        // per-column shown service id for the active project (session-only)
    tabLayout: null,         // last engine-reconciled {columns, ratios} (drag/splitter working copy)
    iframeZoom: IFRAME_ZOOM_DEFAULT,  // CSS transform scale applied to http-kind iframes
    cliMaxWidth: 0,          // px cap on ssh terminal panes (0 = off / full width)
    termFontSize: TERM_FONT_SIZE_DEFAULT,  // xterm font size for ssh panes (live-applied)
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

// ---- per-project UI state (vault settings.project_ui) ----------------------
// Per-project UI prefs (tab layout + hidden tabs) live in a name-keyed map
// under vault.settings, NOT on the sidebar rows: broker-derived rows are
// _jit transients that persistVault strips wholesale (the SSH-creds
// guarantee), so row fields never persist. Writers mirror row -> map on
// each user action; the broker mint sites rehydrate map -> row. Both
// directions deep-copy so a map entry never aliases a live row's arrays.
// Non-_jit bookmark rows (manual Import) are mirrored into the map too —
// harmless and self-consistent: row and map stay in step, and the row wins
// for bookmarks since Import never rehydrates. Not duplication to "fix" in
// either direction.
const PROJECT_UI_FIELDS = ["hidden_services", "tab_columns", "column_ratios"];

function vaultSettings() {
    if (!state.vault.settings || typeof state.vault.settings !== "object") {
        state.vault.settings = {};
    }
    return state.vault.settings;
}

function projectUiMap() {
    const s = vaultSettings();
    if (!s.project_ui || typeof s.project_ui !== "object") s.project_ui = {};
    return s.project_ui;
}

function syncProjectUiState(project) {
    const entry = {};
    for (const f of PROJECT_UI_FIELDS) {
        if (project[f] !== undefined) entry[f] = JSON.parse(JSON.stringify(project[f]));
    }
    const map = projectUiMap();
    if (Object.keys(entry).length > 0) map[project.name] = entry;
    else delete map[project.name];
}

function rehydrateProjectUiState(row) {
    const entry = projectUiMap()[row.name];
    if (!entry || typeof entry !== "object") return;
    for (const f of PROJECT_UI_FIELDS) {
        if (entry[f] !== undefined) row[f] = JSON.parse(JSON.stringify(entry[f]));
    }
}

// ---- rs-fetch auto-commit tick (vault settings.fetch_auto_commit) -----------
// The repos whose card-head auto-commit tick is ON, kept as a plain list of
// repo NAMES beside project_ui and project_order — the third per-user (per
// browser, in the encrypted vault) preference, and the set-level shape: one
// boolean per repo, so "ticked" and "present in the list" are the same test.
// Same three invariants as the rail order: the read is a tolerant NON-creating
// probe (it runs on every card render, and a render must never touch saved
// state); exactly ONE function assigns the list (called by the tick's change
// handler and by the mirror-Remove prune, nowhere else); and a removed mirror
// is pruned, so a repo mirrored again later starts unticked (the reused-name
// rule the project map follows).
function readFetchAutoCommit() {
    const s = state.vault && state.vault.settings;
    const raw = s && s.fetch_auto_commit;
    if (!Array.isArray(raw)) return [];
    const out = [];
    for (const n of raw) {
        if (typeof n === "string" && n && !out.includes(n)) out.push(n);
    }
    return out;
}

// The ONE writer. Persists at once (best-effort, like the destroy prune) and
// flips every OTHER card's box for the same repo that is on screen right now:
// a repo can be on screen twice (the Development page and a project's Fetch
// pane), each card copies from its own box, and two visible boxes must never
// disagree about one saved preference. JS string compare over the stamped
// attribute, never a CSS attribute-value selector (the data-rkey precedent).
async function setFetchAutoCommit(repo, on) {
    const list = readFetchAutoCommit().filter((n) => n !== repo);
    if (on) list.push(repo);
    if (list.length) vaultSettings().fetch_auto_commit = list;
    else delete vaultSettings().fetch_auto_commit;
    for (const box of document.querySelectorAll(".dev-ac-check[data-ac-repo]")) {
        if (box.getAttribute("data-ac-repo") !== repo) continue;
        box.checked = on;
    }
    try { await persistVault(); } catch (e) { /* best-effort */ }
}

// ---- rail order (vault settings.project_order) ------------------------------
// The order the operator dragged the sidebar rows into, kept as a list of
// project NAMES beside the per-project map above — deliberately NOT a saved
// reshuffle of state.vault.projects. Most rows are _jit transients that
// persistVault strips: they are re-minted from the broker on every load, each
// pushed as its own fetch lands (syncSidebarFromBroker's Promise.all), so a
// saved array order would say nothing about them. Names survive that.

// The rail's two group separators ride in the same saved list, as names no
// project can have (project names are alnum-first; the NUL is the same
// never-a-name convention devReviewsInFlight keys on). One sits under the
// last project by default, the other above the first; "top" and "bottom"
// name only those defaults — once dragged, either line can be anywhere.
// Sharing the list means a stopped project keeps its place after the item it
// last followed: writeProjectOrder re-inserts an absent name after its saved
// predecessor, a line included — so it changes group only when that
// predecessor is dragged across a line, or a line is dragged past that
// predecessor.
const RAIL_SEPARATOR = "\u0000separator";
const RAIL_SEPARATOR_TOP = "\u0000separator-top";
const RAIL_SEPARATORS = [RAIL_SEPARATOR_TOP, RAIL_SEPARATOR];

// Tolerant, NON-CREATING probe: applyProjectOrder runs on every rail render,
// and a render must never touch saved state (the tab engine's reconciliation
// follows the same purity rule). A non-array, or entries that aren't strings,
// read as "nothing saved" rather than throwing — the vault is decoded from a
// blob older builds wrote.
function readProjectOrder() {
    const s = state.vault && state.vault.settings;
    const raw = s && s.project_order;
    if (!Array.isArray(raw)) return [];
    const out = [];
    for (const n of raw) {
        if (typeof n === "string" && n && !out.includes(n)) out.push(n);
    }
    return out;
}

// The saved order as the rail reads it: a line that was never saved sits at
// its default end — the top line before everything, the other after
// everything. The ONE place those defaults live: the sort, the line
// placement and the writer's re-insertion all walk this, so a vault saved
// before a line existed reads the same way in all three.
function railVirtualOrder() {
    const order = readProjectOrder();
    if (!order.includes(RAIL_SEPARATOR_TOP)) order.unshift(RAIL_SEPARATOR_TOP);
    if (!order.includes(RAIL_SEPARATOR)) order.push(RAIL_SEPARATOR);
    return order;
}

// Rank of every saved name (projects and lines) in the virtual order, plus the
// rank a never-arranged project takes: just above the visually LOWEST line
// (the largest index), i.e. the end of the group above it.
function railRanks() {
    const order = railVirtualOrder();
    const rank = new Map(order.map((n, i) => [n, i]));
    const lowest = Math.max(...RAIL_SEPARATORS.map((n) => rank.get(n)));
    return { rank, unarranged: lowest - 0.5 };
}

// Sort the live project list into the saved order, IN PLACE (the destroy path
// reassigns state.vault.projects instead — both are fine, nothing holds an
// index into or an alias of this array across a render). The sort itself never
// writes: a later unrelated persist does carry the sorted array, which is
// harmless because the order is re-derived from the saved names on every
// render anyway. Saved names in saved order;
// every project the operator has never arranged goes, BY NAME, just above the
// lowest line (railRanks) — with nothing arranged, that is the middle group
// between the two default lines. Never broker-response order, which differs
// run to run and is exactly the shuffle this feature exists to remove.
function applyProjectOrder() {
    const { rank, unarranged } = railRanks();
    state.vault.projects.sort((a, b) => {
        const ra = rank.has(a.name) ? rank.get(a.name) : unarranged;
        const rb = rank.has(b.name) ? rank.get(b.name) : unarranged;
        if (ra !== rb) return ra - rb;
        if (rank.has(a.name)) return 0;         // both saved: ranks are unique
        // localeCompare, not `<`: the relational operators compare UTF-16 code
        // units, which puts every capitalised name above every lowercase one
        // ("Zoo" before "alpha") — a tail that reads as unsorted.
        return a.name.localeCompare(b.name);
    });
}

// The rail's full item order — project names and the two lines (call after
// applyProjectOrder). The lines are MERGED into the already-sorted project
// list by rank rather than re-sorting it, so the projects keep exactly the
// order applyProjectOrder gave them.
function railItemNames() {
    const { rank, unarranged } = railRanks();
    const rankOf = (n) => (rank.has(n) ? rank.get(n) : unarranged);
    const lines = RAIL_SEPARATORS.slice().sort((a, b) => rank.get(a) - rank.get(b));
    const out = [];
    for (const p of state.vault.projects) {
        while (lines.length && rank.get(lines[0]) < rankOf(p.name)) out.push(lines.shift());
        out.push(p.name);
    }
    return out.concat(lines);
}

// Persist the rail's current order — the ONLY writer, called from the drop.
// Saved names that are NOT on screen right now (a stopped project has no row
// until it runs again) are re-inserted after whichever saved predecessor
// survived, so an absent project keeps its slot instead of falling to the end
// when it comes back.
async function writeProjectOrder(names) {
    // The VIRTUAL order: a line never saved still has a predecessor role at
    // its default end, so a stopped project saved before the top line existed
    // re-inserts below that line, where it was drawn, not above it.
    const prev = railVirtualOrder();
    const present = new Set(names);
    const out = names.slice();
    let cursor = -1;                            // insertion point in `out`
    for (const n of prev) {
        if (present.has(n)) { cursor = out.indexOf(n); continue; }
        if (out.includes(n)) continue;          // never duplicate a name
        cursor += 1;
        out.splice(cursor, 0, n);
    }
    vaultSettings().project_order = out;
    await persistVault();
}

// Forget a destroyed project's slot — the twin of the project_ui prune it sits
// beside, and for the same reason: a later project reusing the name lands
// where never-arranged projects go (just above the lowest line) rather than
// inheriting a stranger's position. A project removed any
// other way leaves its name behind; a saved name with no row is skipped by
// applyProjectOrder, so the residue is invisible.
function pruneProjectOrder(name) {
    const s = state.vault && state.vault.settings;
    if (!s || !Array.isArray(s.project_order)) return;
    s.project_order = s.project_order.filter((n) => n !== name);
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

// `carry` (a box change on the open project): the previous map, whose detected
// tabs are kept while this fetch cannot speak for them (withheld etc.), so a
// box change never makes an on-screen app column vanish for one refresh. A
// project switch passes none: a returning project starts from a fresh
// detection rather than from tabs seen earlier.
async function fetchProjectServices(projectName, carry, onScreen) {
    if (state.projectServices[projectName]) return state.projectServices[projectName];
    const r = await fetchProjectTabs(projectName, false);
    state.lastDetection[projectName] = r ? r.detection : "fallback";
    state.projectServices[projectName] = !r ? {}
        : (carry ? mergeDetectedTabs(r.services, carry, onScreen || onScreenServiceIds(),
                                     r.detection, r.opened)
                 : r.services);
    // The gated route serves a snapshot only to the session that just saw it;
    // otherwise the detected tabs join one detection later — fire it now.
    if (r && r.detection === "withheld") {
        setTimeout(() => {
            if (state.activeProject === projectName) refreshActiveServices();
        }, 0);
    }
    return state.projectServices[projectName];
}

// A project's tabs: through the session-gated route (which adds detected web
// apps) when the layout is not the phone's and detection is not paused,
// otherwise the plain /services/<project>. Returns {services, detection,
// opened} or null on a transport failure. `detection` is the server's marker
// ("" = authoritative; busy/unavailable/withheld/off), or "fallback" when the
// tabs came from /services. A 401 pauses detection (no sign-in attempt of its
// own: it waits for the next unlock or Management sign-in); any other
// non-200 falls back for this call only.
async function fetchProjectTabs(projectName, detect) {
    const name = encodeURIComponent(projectName);
    if (!mobileModeActive() && !state.detectPaused) {
        const gen = state.loginGen;
        let res = null;
        try {
            res = await fetch(`/broker/project/${name}/services?detect=${detect ? 1 : 0}`);
        } catch (_) { res = null; }
        if (res && res.status === 200) {
            let b = null;
            try { b = await res.json(); } catch (_) { b = null; }
            if (b && b.ok && b.services) {
                return { services: b.services, detection: b.detection || "",
                         opened: b.opened || [] };
            }
        } else if (res && res.status === 401 && gen === state.loginGen) {
            state.detectPaused = true;
        }
    }
    try {
        const res = await fetch(`/services/${name}`);
        if (!res.ok) return null;
        return { services: await res.json(), detection: "fallback", opened: [] };
    } catch (_) {
        return null;
    }
}

async function renderDashboard(opts = {}) {
    // clearBody() detaches every terminal container, so no terminal may
    // survive this rebuild: tear them all down first (see the helper below).
    // Every caller inherits it — a mid-session re-render (Management attach,
    // project import, layout flip) must not leave a blank terminal area.
    teardownAllTerminals();
    // A rebuilt dashboard has no host page open (clearBody drops its view), and
    // this must happen BEFORE makeProjectRail: its bottom nav derives the
    // highlighted entry from state.hostPage. A stale value would also make the
    // next tap on that entry hit the toggle-closed branch and look dead.
    state.hostPage = null;
    closeFloatBox();   // clearBody would drop its markup but leave its listeners
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
    paintProjectBadges();
    scheduleServicesRefresh();
    scheduleHostStats();

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
    // the Sandboxes page surfaces the mismatch card when opened).
    // skipBrokerLogin: shell re-renders (mobile/desktop mode flip, Settings
    // Layout change) reuse the existing session cookie — re-firing the login
    // per flip would break the once-per-unlock contract below and, on a
    // vault↔broker password mismatch, burn the global login limiter on
    // every resize across the breakpoint. syncSidebarFromBroker is
    // 401-silent, so a dead session just skips the sync.
    if (opts.skipBrokerLogin) {
        syncSidebarFromBroker();
        seedLifecycleFromBroker();
        seedSoftwareFromBroker();
    } else {
        tryBrokerLogin().then(() => {
            syncSidebarFromBroker();
            // The timer's first sample ran before the session existed (401);
            // sample again now (restarting the tick, so the next sample is a
            // full interval later) so the bars appear without waiting — but
            // only then: with figures cached (a reload kept the session
            // cookie) there is nothing to fix.
            if (!state.hostStats) scheduleHostStats();
            // Start/stop/update and software builds keep running on the
            // broker across a reload; re-learn them so their labels and
            // completion notices come back.
            seedLifecycleFromBroker();
            seedSoftwareFromBroker();
        });
    }
}

// Tear down EVERY terminal (all projects) ahead of a full shell re-render;
// renderDashboard calls it before clearBody(), so every re-render owns it.
// clearBody() detaches the containers; a surviving state.terminals entry
// would then hit activateService's fast-path, un-hide a detached container,
// and leave the terminal area blank until the ws happens to drop. Byobu
// sessions persist server-side, so closed terminals reconnect on the next
// activation (scrollback is lost — accepted for a re-render).
// The containers are not removed here: clearBody() wipes the whole body
// right after. onclose is nulled before close() (refreshService's idiom):
// the close event fires asynchronously, after the re-render has re-opened
// the same keys, and the old handler would mark the FRESH entry
// disconnected — its next activation would then needlessly reconnect.
// Deliberately keeps state.activeProject: the re-render re-activates it.
function teardownAllTerminals() {
    for (const k of Object.keys(state.terminals)) {
        const t = state.terminals[k];
        try { if (t.ws) { t.ws.onclose = null; t.ws.close(); } } catch (_) {}
        try { if (t.term) t.term.dispose(); } catch (_) {}
        delete state.terminals[k];
    }
}

// Full shell re-render on a layout-mode change (breakpoint crossing in auto
// mode, or the Settings Layout selector). hostPage is cleared because the
// rebuilt DOM has no open host page — a stale value would make the next
// host-nav tap hit the toggle-closed branch and appear dead. The terminal
// teardown happens inside renderDashboard.
async function rerenderShell() {
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
        if (res.status === 200) noteBrokerLogin();
        return res.status === 200;
    } catch (e) {
        return false;   // broker/webui unreachable — Management is opt-in
    }
}

// A Management sign-in succeeded: detected tabs may be fetched again.
function noteBrokerLogin() {
    state.loginGen += 1;
    state.detectPaused = false;
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
// the terminal-area splitter so the drag survives moving the cursor
// across iframes / xterm canvases. A sidebar rebuild (an op finishing,
// the broker sync) replaces this splitter mid-drag: capture is dropped
// and the release lands elsewhere, so lostpointercapture is the cancel
// path. Engines deliver it at the document or at the detached element,
// so both are listened to; the release handler removes them FIRST.
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
    const onLost = (ev) => { if (ev.pointerId === pointerId) onUp(); };
    const onUp = () => {
        if (!dragging) return;
        dragging = false;
        splitter.removeEventListener("lostpointercapture", onLost);
        document.removeEventListener("lostpointercapture", onLost);
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
        splitter.addEventListener("lostpointercapture", onLost);
        document.addEventListener("lostpointercapture", onLost);
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
    // The rail is overflow-y: auto, so its scroll offset lives on the element
    // being discarded — carry it across, or every rebuild yanks a long list
    // back to the top (most visible on a drop, which rebuilds per gesture).
    const scrolled = old.scrollTop;
    const fresh = makeProjectRail();
    old.replaceWith(fresh);
    fresh.scrollTop = scrolled;
    applyRailState();
    schedulePolling();
    paintProjectBadges();
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
        // Only a transient (_jit) row takes the keyring's coordinates. A saved
        // bookmark (manual Import) keeps the ones the operator entered: copying
        // the keyring password onto it would let persistVault write it into the
        // encrypted vault, and would overwrite what the operator typed.
        if (existing._jit) {
            existing.host = info.host; existing.port = info.port;
            existing.username = info.username; existing.password = info.password;
        }
    } else {
        const row = {
            name: info.name, host: info.host, port: info.port,
            username: info.username, password: info.password, _jit: true,
        };
        rehydrateProjectUiState(row);
        state.vault.projects.push(row);
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
    // The single choke point every rail render passes through (initial render,
    // refresh-after-op, mobile drawer) — so the saved order is applied here
    // rather than at each of the three sites that push a row.
    applyProjectOrder();
    const rail = el("aside", { class: "project-rail" });
    const splitter = el("div", { class: "rail-splitter", title: "Drag to resize" });
    installRailSplitterDrag(splitter);
    rail.appendChild(splitter);
    const header = el("div", { class: "rail-header" }, [
        el("span", {}, ["Projects"]),
        makePinButton(),
    ]);
    rail.appendChild(header);
    // Rows and the two group lines, in railItemNames order; the lines are
    // drawn only when there is a project to separate. Rows are taken in
    // sorted order, which railItemNames preserves.
    if (state.vault.projects.length) {
        let next = 0;
        for (const n of railItemNames()) {
            rail.appendChild(RAIL_SEPARATORS.includes(n)
                ? makeRailSeparator(n)
                : makeProjectRow(state.vault.projects[next++]));
        }
    }
    // (Import-an-existing-project moved into the New Project page as a box —
    // openAddProjectModal is reached from there now, not a rail button.)
    rail.appendChild(el("div", { class: "rail-spacer" }));
    // Host CPU/RAM bars, directly above the nav group's divider — painted from
    // the cached sample so a rail rebuild never blanks them.
    const hostStats = makeHostStats();
    paintHostStats(hostStats);
    rail.appendChild(hostStats);
    // New Project + Development + Sandboxes + Settings sit together at the BOTTOM of the rail —
    // the host-side create + lifecycle + UI surfaces, distinct from the vault
    // bookmarks above.
    rail.appendChild(makeBottomNav());

    // Theme + Editor-zoom moved to the Settings box; the footer keeps Lock vault.
    const footer = el("div", { class: "rail-footer" }, [
        el("button", { class: "lock-btn", onclick: lockVault }, [navIcon("lock"), "Lock vault"]),
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

// The bottom rail nav: New Project / Development / Sandboxes / Settings, each
// on its OWN line (the .rail-nav-group stacks them vertically). The entry of
// the open host page carries .active, derived HERE from state.hostPage: every
// rail rebuild (create, destroy, a drag) re-creates this group, so a highlight
// set once at open would vanish with the first rebuild.
function makeBottomNav() {
    const mk = (cls, icon, label, title, open) => {
        const on = !!state.hostPage && cls === state.hostPage + "-entry";
        const e = el("div", { class: "nav-entry " + cls + (on ? " active" : ""), title },
                     [navIcon(icon), el("span", {}, [label])]);
        e.onclick = (ev) => { ev.stopPropagation(); open(); };
        return e;
    };
    // Explain docs are ephemeral floating popovers now (dismissed on outside
    // click), not persistent rail tabs — so the bottom nav has no doc entries.
    return el("div", { class: "rail-nav-group" }, [
        mk("workflows-entry", "newProject", "New Project",
           "New Project — pick a workflow, or import an existing project", openWorkflows),
        mk("development-entry", "development", "Development",
           "Development — dev repos, PRs, fetch commands (shared Gitea)", openDevelopment),
        mk("sandboxes-entry", "management", "Sandboxes",
           "Sandboxes — projects, infrastructure, software (broker)", openSandboxes),
        mk("settings-entry", "settings", "Settings", "Interface settings", openSettings),
    ]);
}

// Line icons for the bottom nav and the Lock vault button: one stroke style,
// coloured by currentColor so they follow every theme and the hover/active
// states (style.css .nav-icon). Settings is sliders, not a gear, so it cannot
// be mistaken for a project row's ⚙.
//
// Icon geometry from Lucide (https://lucide.dev): folder-plus, git-branch,
// server, sliders-horizontal, lock.
//   ISC License. Copyright (c) for portions of Lucide are held by Cole Bemis
//   2013-2022 as part of Feather (MIT). All other copyright (c) for Lucide are
//   held by Lucide Contributors 2022.
//   Permission to use, copy, modify, and/or distribute this software for any
//   purpose with or without fee is hereby granted, provided that the above
//   copyright notice and this permission notice appear in all copies.
//   THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
//   WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
//   MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
//   SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
//   WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
//   ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR
//   IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
//   Portions derived from Feather: The MIT License (MIT), Copyright (c)
//   2013-2022 Cole Bemis. Permission is hereby granted, free of charge, to any
//   person obtaining a copy of this software and associated documentation files
//   (the "Software"), to deal in the Software without restriction, including
//   without limitation the rights to use, copy, modify, merge, publish,
//   distribute, sublicense, and/or sell copies of the Software, and to permit
//   persons to whom the Software is furnished to do so, subject to the
//   following conditions: The above copyright notice and this permission notice
//   shall be included in all copies or substantial portions of the Software.
//   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
//   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
//   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
//   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
//   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
//   FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
//   DEALINGS IN THE SOFTWARE.
const NAV_ICON_SVG = (() => {
    const svg = (body) =>
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"' +
        ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"' +
        ' xmlns="http://www.w3.org/2000/svg">' + body + '</svg>';
    return {
        newProject: svg(
            '<path d="M12 10v6"/><path d="M9 13h6"/>' +
            '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>'),
        development: svg(
            '<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/>' +
            '<circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>'),
        management: svg(
            '<rect width="20" height="8" x="2" y="2" rx="2" ry="2"/>' +
            '<rect width="20" height="8" x="2" y="14" rx="2" ry="2"/>' +
            '<line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/>'),
        settings: svg(
            '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/>' +
            '<line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/>' +
            '<line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/>' +
            '<line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/>' +
            '<line x1="16" x2="16" y1="18" y2="22"/>'),
        lock: svg(
            '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/>' +
            '<path d="M7 11V7a5 5 0 0 1 10 0v4"/>'),
    };
})();

// An icon box holding one NAV_ICON_SVG entry. innerHTML, never el("svg"):
// createElement makes an HTML-namespace <svg> that renders nothing.
function navIcon(key) {
    const span = el("span", { class: "nav-icon" });
    span.innerHTML = NAV_ICON_SVG[key];
    return span;
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

// The ONLY mutator of the .mobile-projects class.
function setMobileProjectsView(on) {
    const dashboard = document.querySelector(".dashboard");
    if (!dashboard) return;
    dashboard.classList.toggle("mobile-projects", on);
    updateMobileKeybar();
    // Close transition: #terminal-area was display:none while the view was
    // open, so any viewport change during it (rotation, soft keyboard) made
    // the global refit a no-op on a 0x0 container — refit the now-visible
    // active terminal. Deferred so the reflow lands first (the applyRailState
    // idiom); harmless duplicate on the activateProject close path.
    if (!on) {
        const t = activeTerminal();
        if (t && t.fitAddon) {
            // Sync-then-fit: the chip's leave-Settings route (closeSandboxes →
            // drawer → close-without-switching) restores the area through
            // NEITHER applyColumnLayout NOR activateService, so this is the
            // third font-size application site — without it a font changed in
            // Settings silently doesn't take until the next tab tap.
            setTimeout(() => {
                syncTermFontSize(t);
                try { t.fitAddon.fit(); } catch (_) {}
            }, 0);
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
    // The host page's chip (.host-chip) is NOT a project chip: it leaves with
    // the page, so it must never stand in for the placeholder this guards.
    if (!strip || strip.querySelector(".active-project:not(.host-chip)")) return;
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

// ---- host pages: the non-project pages that take over the main area ---------
// A host page hides the terminal area and reuses the single #management-view
// pane; closeManagement (called by activateProject) reverses it. Two kinds:
//
//  * TABBED pages (HOST_PAGES: Development, Sandboxes) own the top strip while
//    open: #service-tabs gets .host-mode, which hides every project-strip child
//    except the Projects toggle (CSS), and mountHostStrip appends a .host-strip
//    — a chip naming the page plus one .host-tab per section. The project's
//    strip is HIDDEN, never destroyed, so its tabs and panes come back as they
//    were. .host-tab is deliberately not .tab: the service-tab code (global
//    un-highlight, drag, the mobile whitelist) keys on .tab and must never
//    touch these.
//  * LEGACY pages keep the old look: the project's tabs are hidden inline and
//    the project chip stays (on a phone it is the only drawer opener). No page
//    is legacy today — New Project and Settings are floating boxes over the
//    page (openFloatBox) — but the mode stays the strip's safe default for a
//    page with no HOST_PAGES entry.
//
// Every render goes into a FRESH .host-body inside the view (renderHostPage):
// a slow fetch that answers after the operator switched tab or page then
// writes into a detached body instead of over what is on screen now. The body
// carries __rsRender = renderHostPage, which is how a dialog's Done or a
// sign-in landing re-renders "the page I am on" (rerenderView, renderMgmtLogin).
let sandboxesActiveTab = "management";

const HOST_PAGES = {
    development: {
        title: "Development",
        tabs: [["gitea", "Gitea"], ["fetch", "Fetch"], ["repos", "Repos"],
               ["reviews", "Reviews"], ["backup", "Backup"]],
        get: () => devActiveTab,
        set: (k) => { devActiveTab = k; },
    },
    sandboxes: {
        title: "Sandboxes",
        tabs: [["management", "Management"], ["infrastructure", "Infrastructure"],
               ["software", "Software"], ["mcp", "MCP"]],
        get: () => sandboxesActiveTab,
        set: (k) => { sandboxesActiveTab = k; },
    },
};

function hostPageRenderer(page) {
    if (page === "development") return renderDevelopmentInto;
    if (page === "sandboxes") return renderSandboxesInto;
    return null;
}

// The project-strip members the LEGACY pages hide inline (the divider is
// neither a .tab nor data-service-bearing, so without it a floating rule
// would strand after the chip).
const LEGACY_HIDE_SELECTOR =
    "#service-tabs .tab[data-service], #service-tabs .tab-group-divider, #service-tabs .column-tab-group";

// Puts the strip in exactly ONE mode for the open page (hidden=true) or back
// to the project's own strip (hidden=false; closeManagement nulls hostPage
// first). Also the remount for every strip rebuild that happens under a page
// (renderServiceTabs, showWelcome): it only repaints the strip, it never
// re-renders the page's content.
function setServiceTabsHidden(hidden) {
    const strip = document.getElementById("service-tabs");
    const tabbed = hidden && !!HOST_PAGES[state.hostPage];
    const legacy = hidden && !tabbed;
    if (strip) strip.classList.toggle("host-mode", tabbed);
    mountHostStrip();
    document.querySelectorAll(LEGACY_HIDE_SELECTOR)
        .forEach((t) => { t.style.display = legacy ? "none" : ""; });
}

// (Re)builds the open tabbed page's chip + tabs; removes them otherwise.
// Idempotent. The chip carries .active-project so the mobile chip styling and
// the drawer's outside-tap exclusion apply to it; .host-chip tells the
// softlock guard (ensureMobileChip) it is not a project chip.
function mountHostStrip() {
    const strip = document.getElementById("service-tabs");
    if (!strip) return;
    const old = strip.querySelector(".host-strip");
    if (old) old.remove();
    const page = HOST_PAGES[state.hostPage];
    if (!page) return;
    const chip = el("div", { class: "active-project host-chip" }, [page.title]);
    chip.onclick = () => {
        if (!mobileModeActive()) return;
        mobileToggleProjects();
    };
    const tabs = page.tabs.map(([key, label]) => {
        const tab = el("div", {
            class: "host-tab" + (page.get() === key ? " active" : ""),
            "data-host-tab": key,
        }, [label]);
        tab.onclick = (ev) => {
            ev.stopPropagation();
            if (page.get() === key) return;
            page.set(key);
            renderHostPage();
        };
        return tab;
    });
    strip.appendChild(el("div", { class: "host-strip" },
        [chip, el("div", { class: "tab-group-divider" }), ...tabs]));
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

// Renders the open page into a fresh body (see the block comment above).
// Stands as a view's __rsRender and as a sign-in landing; does nothing once no
// page is open, or when handed a body that is no longer on screen.
function renderHostPage(view) {
    // Called as a stamp with the body it was stamped on: a STALE body (the
    // operator moved on while, say, a sign-in was answering) must not redraw
    // the page that is on screen now. Called bare (a tab click, a page open):
    // render the open page.
    if (view && !view.isConnected) return undefined;
    const render = hostPageRenderer(state.hostPage);
    const pane = document.getElementById("management-view");
    if (!render || !pane) return undefined;
    const body = el("div", { class: "host-body" });
    body.__rsRender = renderHostPage;
    pane.replaceChildren(body);
    return render(body);
}

// The open page's current body, or null.
function hostBody() {
    const body = document.querySelector("#management-view > .host-body");
    return body && body.isConnected ? body : null;
}

// "Re-render the page this view belongs to" — a dialog's Done, a section's
// own refresh. A host-page body re-renders the open page (and does nothing
// when it is a stale, detached body); an unstamped view (boxOpView's detached
// div, …) keeps the historical Management re-render exactly.
function rerenderView(view) {
    if (view && view.__rsRender) {
        return view.isConnected ? view.__rsRender(view) : undefined;
    }
    return renderManagementInto(view);
}

// Clicking the already-open page's rail entry leaves it (temporary-tab feel).
function openHostPage(page) {
    if (state.hostPage === page) return leaveHostView();
    state.hostPage = page;
    const view = enterHostView();
    if (!view) return;
    setActiveNavEntry(page + "-entry");
    renderHostPage();
}

function openSandboxes() { openHostPage("sandboxes"); }

// ---- floating boxes: New Project and Settings --------------------------------
// A panel centred over whatever is open, on a dimmed backdrop; opening one
// changes no page and no project. Closed by its ✕, Escape, or a click on the
// dim. The backdrop is .float-backdrop, NOT .modal-backdrop, on purpose: every
// dialog's one-modal guard (`if (document.querySelector(".modal-backdrop"))`)
// must still let the create / import dialogs open ABOVE the New Project box,
// and the background re-renders that wait for "no open dialog" must not wait
// on a box that can stay open indefinitely. Its z-index sits below the
// dialogs, the corner notices and Explain, above everything on the page.
//
// Escape and the dim click both yield to what is above the box: Escape is
// ignored while Explain or a dialog is open (they own it), and a press that
// started while Explain was open only closes Explain. Both listeners are
// document CAPTURE listeners registered at open, so they run before Explain's
// (which re-registers at every Explain open) and see Explain still open.
let floatBox = null;   // {kind, backdrop, onKey, onDown, explainAtDown, downOnBackdrop}

function closeFloatBox() {
    if (!floatBox) return;
    const fb = floatBox;
    floatBox = null;
    fb.backdrop.remove();
    document.removeEventListener("keydown", fb.onKey, true);
    document.removeEventListener("pointerdown", fb.onDown, true);
}

function openFloatBox(kind, title, render) {
    closeFloatBox();   // one at a time
    if (mobileModeActive()) setMobileProjectsView(false);
    const body = el("div", { class: "float-body" });
    const close = el("button", { class: "float-close", title: "Close" }, ["✕"]);
    close.onclick = () => closeFloatBox();
    const card = el("div", { class: "card float-box float-" + kind }, [
        el("div", { class: "float-head" }, [el("h2", {}, [title]), close]),
        body,
    ]);
    const backdrop = el("div", { class: "float-backdrop" }, [card]);
    const fb = { kind, backdrop, explainAtDown: false, downOnBackdrop: false };
    fb.onDown = (ev) => {
        fb.explainAtDown = !!explainBox;
        fb.downOnBackdrop = ev.target === backdrop;
    };
    fb.onKey = (ev) => {
        if (ev.key !== "Escape" || floatBox !== fb) return;
        if (explainBox || document.querySelector(".modal-backdrop")) return;
        // A terminal focused behind the Settings box must never see this ESC.
        ev.stopPropagation();
        ev.preventDefault();
        closeFloatBox();
    };
    // Close only when the press ALSO began on the dim (a text selection
    // dragged out of the box ends on it), and not when it closed Explain.
    backdrop.onclick = (ev) => {
        if (ev.target !== backdrop || !fb.downOnBackdrop || fb.explainAtDown) return;
        closeFloatBox();
    };
    document.addEventListener("pointerdown", fb.onDown, true);
    document.addEventListener("keydown", fb.onKey, true);
    floatBox = fb;
    document.body.appendChild(backdrop);
    // The body is the view its content renders into: a sign-in landing or a
    // dialog's Done re-renders it through its stamp (rerenderView).
    body.__rsRender = render;
    render(body);
    return body;
}

// New Project: the workflow cards (renderWorkflowsInto).
function openWorkflows() { openFloatBox("workflows", "New Project", renderWorkflowsInto); }
// Settings: the interface settings.
function openSettings() { openFloatBox("settings", "Settings", renderSettingsInto); }

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

// Sandboxes: the projects table (Management), the shared host state that is
// not a project (Infrastructure: pass-through ports, Gitea, the reviewer), and
// the software fleet (Software: dists, images, pins). The Management table's
// read also syncs the rail and re-seeds the background-action watchers; the
// other two tabs do not read the project list for themselves, so they run the
// same session hooks here — once per entry, tab switch or sign-in landing.
function renderSandboxesInto(view) {
    if (state.hostPage === "sandboxes") mountHostStrip();
    const tab = sandboxesActiveTab;
    if (tab === "infrastructure" || tab === "software") {
        // Infrastructure syncs the rail from the list it reads anyway.
        if (tab === "software") syncSidebarFromBroker();
        seedLifecycleFromBroker();
        if (tab === "infrastructure") seedSoftwareFromBroker();   // Software seeds itself
        return tab === "infrastructure" ? renderInfraInto(view) : renderSoftwareInto(view);
    }
    if (tab === "mcp") {
        // Reads no project list for itself: the same session hooks as above.
        syncSidebarFromBroker();
        seedLifecycleFromBroker();
        seedSoftwareFromBroker();
        return renderMcpInto(view);
    }
    return renderManagementInto(view);
}

// Settings (a floating box): the local UI settings (interface + terminal
// theme, layout, editor zoom, CLI width + font size — all client-side). They
// apply at once against the page behind the box; the font size goes through
// the deferred syncTermFontSize like every other site.
function renderSettingsInto(view) {
    view.innerHTML = "";
    view.appendChild(el("div", { class: "settings-screen" }, [
        el("div", { class: "field" }, [
            el("label", {}, ["Interface theme"]),
            makeThemeSelector(),
        ]),
        el("div", { class: "field" }, [
            el("label", {}, ["Terminal theme"]),
            makeTermThemeSelector(),
        ]),
        el("div", { class: "field" }, [
            el("label", {}, ["Layout"]),
            makeLayoutSelector(),
        ]),
        el("div", { class: "field" }, [
            el("label", {}, ["Editor zoom"]),
            makeIframeZoomSelector(),
        ]),
        el("div", { class: "field" }, [
            el("label", {}, ["CLI width"]),
            makeCliWidthSelector(),
        ]),
        el("div", { class: "field" }, [
            el("label", {}, ["CLI font size"]),
            makeTermFontSizeSelector(),
        ]),
    ]));
}

// Sandboxes → MCP: the shared MCP gateway and its server list. A server is an
// address plus optional secret request headers; the page shows a secret's
// NAME only and never receives its value back. Wiring a server to a project
// or a box is not on this page yet. Every read is this page's own render or
// one of its buttons — nothing here runs on a timer.
const MCP_GATEWAY_START_TITLE = "Starting the MCP gateway";
const MCP_GATEWAY_TEXT = {
    running: "running",
    stopped: "not running",
    absent: "not running",
    image_missing: "not running (its image is built when you start it)",
};
const MCP_OUTCOME_TEXT = {
    mcp: "Answers as an MCP server",
    refused: "Reachable, but it refuses the credentials",
    not_mcp: "Reachable, but it does not speak MCP at that address",
    unreachable: "Not reachable",
    unknown: "Not on the list any more",
    gateway_down: "Could not be asked",
    gateway_restarting: "Could not be asked yet",
};
const MCP_TEST_RETRY_TEXT = "Could not ask, try again";

async function renderMcpInto(view) {
    view.innerHTML = "";
    view.appendChild(el("div", { class: "mgmt-loading" }, ["Loading the MCP servers…"]));
    let res;
    try {
        res = await fetch("/broker/mcp/servers");
    } catch (e) { return renderMgmtUnavailable(view); }
    if (res.status === 401) return renderMgmtLogin(view, view.__rsRender || renderMcpInto);
    if (res.status === 403) return renderMgmtRejected(view);
    if (res.status === 503) return renderMgmtUnavailable(view);
    let body;
    try { body = await res.json(); } catch (e) { return renderMgmtUnavailable(view); }
    if (!res.ok || !body.ok || !body.result) {
        return renderMgmtVerbError(view, body, renderMcpInto);
    }
    // The wiring is a second read. If it cannot be had (a broker from before
    // wiring existed, a passing failure) the servers still show, with a line
    // where the Wiring section would be — never a page-wide error.
    let wiring = null;
    try {
        const wres = await fetch("/broker/mcp/wiring");
        if (wres.status === 401) return renderMgmtLogin(view, view.__rsRender || renderMcpInto);
        if (wres.ok) {
            const wbody = await wres.json();
            if (wbody && wbody.ok && wbody.result) wiring = wbody.result;
        }
    } catch (e) { /* the line below */ }
    renderMcpScreen(view, body.result, wiring);
}

// POST one MCP page action. Returns {status, body}; status 0 = the request
// itself failed. Never routes anywhere: each caller decides what a failure
// paints, because a row's Test must not replace the page.
async function mcpPost(path, payload) {
    let res;
    try {
        res = await fetch(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload || {}),
        });
    } catch (e) { return { status: 0, body: {} }; }
    let body; try { body = await res.json(); } catch (e) { body = {}; }
    return { status: res.status, body };
}

function mcpTestText(result) {
    const base = MCP_OUTCOME_TEXT[result.outcome] || MCP_OUTCOME_TEXT.unreachable;
    return result.detail ? base + ": " + result.detail : base;
}

// Test one row and paint its cell in place. A session that ended lands on the
// sign-in; every other failure (a busy or slow broker included) is a line on
// this row — never the page-wide outage card.
async function mcpTestRow(view, name, cell) {
    cell.className = "mcp-test";
    cell.textContent = "Asking…";
    const { status, body } = await mcpPost("/broker/mcp/servers/test", { name });
    if (status === 401) return renderMgmtLogin(view, view.__rsRender || renderMcpInto);
    if (status !== 200 || !body.ok || !body.result) {
        cell.className = "mcp-test mcp-test-bad";
        cell.textContent = status === 200 && body.error
            ? mgmtErrText(body) : MCP_TEST_RETRY_TEXT;
        return;
    }
    cell.className = "mcp-test " + (body.result.outcome === "mcp" ? "mcp-test-ok" : "mcp-test-bad");
    cell.textContent = mcpTestText(body.result);
}

// One after another: each is one bounded broker call, and the broker serves
// one at a time anyway.
async function mcpTestAll(view, rows, btn) {
    btn.disabled = true;
    try {
        for (const r of rows) {
            // A sign-in card (or any re-render) replaced the rows: stop, so a
            // failed sign-in is never retried once per remaining row.
            if (!view.isConnected || !r.cell.isConnected) return;
            await mcpTestRow(view, r.name, r.cell);
        }
    } finally { btn.disabled = false; }
}

// The Add payload: only what was typed (the broker refuses bad shapes itself).
function mcpAddPayload(f) {
    const payload = { name: f.name.trim(), url: f.url.trim() };
    if (f.description.trim()) payload.description = f.description.trim();
    const headers = {};
    for (const h of f.headers) {
        if (h.name.trim()) headers[h.name.trim()] = h.value;
    }
    if (Object.keys(headers).length) payload.headers = headers;
    return payload;
}

// The Edit payload: the address and description as they now read; a secret
// header is sent only when a new value was typed, and named for removal when
// its Remove box is ticked. An untouched secret is not sent at all.
function mcpEditPayload(name, f) {
    const payload = { name, url: f.url.trim(), description: f.description.trim() };
    const set = {};
    for (const h of f.headers) {
        if (h.name.trim() && h.value) set[h.name.trim()] = h.value;
    }
    if (Object.keys(set).length) payload.headers_set = set;
    if (f.remove.length) payload.headers_remove = f.remove.slice();
    return payload;
}

// The Add / Edit dialog. `existing` is the list row being edited, or null.
// Secret values are password inputs that start EMPTY in both modes: the page
// never holds a saved secret, so there is nothing to refill them with.
function mcpServerDialog(view, existing) {
    if (document.querySelector(".modal-backdrop")) return null;
    const backdrop = el("div", { class: "modal-backdrop" });
    const errEl = el("div", { class: "error" });
    const nameI = el("input", { type: "text", autocomplete: "off",
                                placeholder: "zotero" });
    const urlI = el("input", { type: "text", autocomplete: "off",
                               placeholder: "https://example.org/mcp" });
    const descI = el("input", { type: "text", autocomplete: "off",
                                placeholder: "what this server is for" });
    if (existing) {
        nameI.value = existing.name; nameI.disabled = true;
        urlI.value = existing.url;
        descI.value = existing.description || "";
    }
    const headerRows = [];            // new or replaced secrets: {nameI, valueI}
    const removeBoxes = [];           // saved secrets: {name, cb}
    const headersWrap = el("div", { class: "mcp-headers" });
    const addHeaderRow = (fixedName) => {
        const hn = el("input", { type: "text", autocomplete: "off",
                                 class: "mcp-header-name", placeholder: "Authorization" });
        const hv = el("input", { type: "password", autocomplete: "new-password",
                                 class: "mcp-header-value",
                                 placeholder: fixedName ? "new value (empty keeps it)" : "value" });
        if (fixedName) { hn.value = fixedName; hn.disabled = true; }
        const line = el("div", { class: "mcp-header-row" }, [hn, hv]);
        if (fixedName) {
            const cb = el("input", { type: "checkbox" });
            removeBoxes.push({ name: fixedName, cb });
            line.appendChild(el("span", { class: "mcp-header-remove" }, [cb, " remove"]));
        }
        headerRows.push({ nameI: hn, valueI: hv });
        headersWrap.appendChild(line);
    };
    for (const hname of (existing ? existing.headers : [])) addHeaderRow(hname);
    const moreBtn = el("button", { type: "button", class: "btn-small" }, ["Add a secret header"]);
    moreBtn.onclick = () => addHeaderRow(null);
    const cancel = el("button", { class: "btn btn-secondary" }, ["Cancel"]);
    cancel.onclick = () => backdrop.remove();
    const go = el("button", { class: "btn" }, [existing ? "Save" : "Add server"]);
    const read = () => ({
        name: nameI.value, url: urlI.value, description: descI.value,
        headers: headerRows.map((h) => ({ name: h.nameI.value, value: h.valueI.value })),
        remove: removeBoxes.filter((b) => b.cb.checked).map((b) => b.name),
    });
    go.onclick = async () => {
        errEl.textContent = "";
        const f = read();
        const payload = existing ? mcpEditPayload(existing.name, f) : mcpAddPayload(f);
        go.disabled = true; cancel.disabled = true;
        const { status, body } = await mcpPost(
            "/broker/mcp/servers/" + (existing ? "edit" : "add"), payload);
        const redirect = mgmtStatusRedirect(view, status, view.__rsRender || renderMcpInto);
        if (redirect) { backdrop.remove(); return redirect(); }
        if (status !== 200 || !body.ok) {
            go.disabled = false; cancel.disabled = false;
            errEl.textContent = status === 200 ? mgmtErrText(body) : "Connection failed.";
            return;
        }
        backdrop.remove();
        rerenderView(view);
    };
    const card = el("div", { class: "card mcp-server-card" }, [
        el("h2", {}, [existing ? "Edit MCP server" : "Add an MCP server"]),
        el("div", { class: "field" }, [el("label", {}, ["Name"]), nameI]),
        el("div", { class: "field" }, [el("label", {}, ["Address"]), urlI]),
        el("div", { class: "hint" }, [
            "An http:// or https:// address the gateway can reach: the internet, "
            + "this machine (host.docker.internal) or your network. A key does "
            + "not go in the address; add it as a secret header."]),
        el("div", { class: "field" }, [el("label", {}, ["Description"]), descI]),
        el("div", { class: "mcp-headers-title" }, ["Secret headers"]),
        el("div", { class: "hint" }, [
            "Sent to the server with every request, for example Authorization. "
            + "A saved value is never shown again; it can be replaced or removed."]),
        headersWrap,
        el("div", { class: "mcp-headers-more" }, [moreBtn]),
        el("div", { class: "btn-row" }, [cancel, go]),
        errEl,
    ]);
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    return backdrop;
}

// A plain confirm for an action that answers at once (Remove, Start an empty
// list): it POSTs, shows a refusal in place, and re-renders the page on success.
function mcpConfirmDialog(view, cfg) {
    if (document.querySelector(".modal-backdrop")) return null;
    const backdrop = el("div", { class: "modal-backdrop" });
    const errEl = el("div", { class: "error" });
    const cancel = el("button", { class: "btn btn-secondary" }, ["Cancel"]);
    cancel.onclick = () => backdrop.remove();
    const go = el("button", { class: "btn" }, [cfg.confirmLabel]);
    go.onclick = async () => {
        errEl.textContent = "";
        go.disabled = true; cancel.disabled = true;
        const { status, body } = await mcpPost(cfg.path, cfg.payload);
        const redirect = mgmtStatusRedirect(view, status, view.__rsRender || renderMcpInto);
        if (redirect) { backdrop.remove(); return redirect(); }
        if (status !== 200 || !body.ok) {
            go.disabled = false; cancel.disabled = false;
            errEl.textContent = status === 200 ? mgmtErrText(body) : "Connection failed.";
            return;
        }
        backdrop.remove();
        rerenderView(view);
    };
    backdrop.appendChild(el("div", { class: "card" }, [
        el("h2", {}, [cfg.title]),
        el("p", {}, [cfg.text]),
        el("div", { class: "btn-row" }, [cancel, go]),
        errEl,
    ]));
    document.body.appendChild(backdrop);
    return backdrop;
}

// Start the gateway: a background build-lane action (it builds the gateway's
// small image first when that is missing), tailed here until it ends.
async function mcpStartGateway(view, btn, errEl) {
    if (document.querySelector(".modal-backdrop")) return;
    errEl.textContent = "";
    btn.disabled = true;
    const { status, body } = await mcpPost("/broker/software/build",
                                           { verb: "mcp_gateway_start" });
    const redirect = mgmtStatusRedirect(view, status, view.__rsRender || renderMcpInto);
    if (redirect) return redirect();
    if (status !== 200 || !body.ok || !body.op_id) {
        btn.disabled = false;
        errEl.textContent = status === 200 ? mgmtErrText(body) : "Connection failed.";
        return;
    }
    const backdrop = el("div", { class: "modal-backdrop" });
    const card = el("div", { class: "card sw-build-card" }, [
        el("h2", {}, [MCP_GATEWAY_START_TITLE])]);
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    await mgmtTailBuildLog(view, backdrop, card, body.op_id, MCP_GATEWAY_START_TITLE,
                           () => rerenderView(view), view.__rsRender || renderMcpInto);
}

// What the last Save reported, shown once by the render that follows it (the
// page re-reads itself after a Save so the server rows' counts are right).
let mcpWiringFlash = null;

const MCP_APPLIED_TEXT = {
    added: "added; available from the agent’s next session",
    kept: "already there",
    removed: "removed",
    conflict: "NOT added: the agent already has a server of that name",
    failed: "could not be changed",
};
const MCP_APPLY_STATE_TEXT = {
    pending: "Saved. It is applied when it next starts.",
    applying: "Saved. It is still being applied in the container.",
    "no agent": "Saved. No agent is deployed there yet.",
    failed: "Saved, but it could not be applied in the container.",
};

// The lines for one consumer's last apply: one per agent and server, or the
// one sentence of a state that is not "applied".
function mcpAppliedLines(applied) {
    if (!applied) return ["Not applied yet."];
    if (applied.state !== "applied") {
        return [MCP_APPLY_STATE_TEXT[applied.state] || "Not applied yet."];
    }
    const lines = [];
    const agents = applied.agents || {};
    for (const agent of Object.keys(agents).sort()) {
        for (const name of Object.keys(agents[agent]).sort()) {
            lines.push(agent + " / " + name + ": "
                       + (MCP_APPLIED_TEXT[agents[agent][name]] || agents[agent][name]));
        }
    }
    return lines.length ? lines : ["Nothing to change."];
}

// The Wiring section: pick a project, pick who in it, tick servers, Save.
// `wiring` is the page's second read ({table, error, projects, targets}).
function mcpWiringSection(view, servers, wiring) {
    const section = el("div", { class: "mgmt-infra" }, [
        el("h3", { class: "mgmt-infra-title" }, ["Wiring"])]);
    if (!wiring) {
        section.appendChild(el("div", { class: "mgmt-settings-hint" }, [
            "The wiring could not be read. Refresh to try again."]));
        return section;
    }
    if (wiring.error) {
        const warn = el("div", { class: "mcp-list-error" }, [
            el("div", { class: "error" }, [wiring.error])]);
        if (wiring.table === "damaged") {
            const reset = el("button", { class: "btn-small" }, ["Start with no wiring"]);
            reset.onclick = () => mcpConfirmDialog(view, {
                title: "Start with no wiring",
                text: "The wiring table on the host cannot be read, so it is replaced "
                    + "by an empty one. Every project’s and box’s wiring is "
                    + "discarded; the gateway refuses them all until you wire them again.",
                confirmLabel: "Discard and start empty",
                path: "/broker/mcp/wiring-reset",
                payload: {},
            });
            warn.appendChild(reset);
        }
        section.appendChild(warn);
        return section;
    }
    const targets = Array.isArray(wiring.targets) ? wiring.targets : [];
    section.appendChild(el("div", { class: "mgmt-settings-hint" }, [
        "Give a project’s agent, or one of its boxes, the servers it may use. "
        + "Nothing restarts; an agent sees a change in its next session."]));
    if (!targets.length || !servers.length) {
        section.appendChild(el("div", { class: "mgmt-settings-hint" }, [
            !servers.length ? "Add a server first." : "There is no project to wire yet."]));
        return section;
    }
    const projectSel = el("select", { class: "config-port-box mcp-wire-project" },
        targets.map((t) => el("option", { value: t.project }, [t.project])));
    const whoSel = el("select", { class: "config-port-box mcp-wire-who" });
    const cards = el("div", { class: "box-opt-cards mcp-wire-cards" });
    const note = el("div", { class: "mgmt-settings-hint" });
    const status = el("div", { class: "mcp-wire-status" });
    const save = el("button", { class: "btn btn-secondary" }, ["Save"]);
    const ticked = new Set();
    const target = () => targets.find((t) => t.project === projectSel.value) || targets[0];
    const consumerKey = () => (whoSel.value === "" ? "agent" : "box:" + whoSel.value);
    const current = () => {
        const row = (wiring.projects || {})[projectSel.value] || {};
        return row[consumerKey()] || null;
    };
    const drawCards = () => {
        const cur = current();
        ticked.clear();
        for (const n of (cur ? cur.servers : [])) ticked.add(n);
        cards.replaceChildren(...servers.map((srv) => {
            const card = el("div", { class: "box-opt-card", title: srv.url }, [
                el("span", { class: "box-opt-name" }, [srv.name])]);
            card.classList.toggle("selected", ticked.has(srv.name));
            card.onclick = () => {
                if (ticked.has(srv.name)) ticked.delete(srv.name); else ticked.add(srv.name);
                card.classList.toggle("selected", ticked.has(srv.name));
            };
            return card;
        }));
        status.replaceChildren(...(cur && cur.servers.length
            ? mcpAppliedLines(cur.applied).map((l) => el("div", {}, [l])) : []));
    };
    const drawWho = () => {
        const t = target();
        const options = [];
        if (t.agent) options.push(el("option", { value: "" }, ["The project’s agent"]));
        for (const b of t.boxes) options.push(el("option", { value: b }, ["Box: " + b]));
        whoSel.replaceChildren(...options);
        const lines = [];
        if (!t.agent) {
            lines.push("A research project’s own agent plans and delegates; "
                       + "its boxes can be wired here.");
        }
        if (t.boxes.length && !t.boxes_wirable) {
            lines.push("This project was made before boxes could reach the MCP gateway: "
                       + "Stop and Start it (after Rebuild all under Sandboxes → Software) "
                       + "before wiring its boxes.");
        }
        if (!options.length) lines.push("Nothing in this project can be wired.");
        note.replaceChildren(...lines.map((l) => el("div", {}, [l])));
        save.disabled = !options.length;
        if (options.length) drawCards(); else { cards.replaceChildren(); status.replaceChildren(); }
    };
    projectSel.onchange = drawWho;
    whoSel.onchange = drawCards;
    save.onclick = async () => {
        const payload = { project: projectSel.value, servers: Array.from(ticked).sort() };
        if (whoSel.value !== "") payload.box = whoSel.value;
        save.disabled = true;
        status.replaceChildren(el("div", {}, ["Saving…"]));
        const { status: code, body } = await mcpPost("/broker/mcp/wire", payload);
        if (code === 401) return renderMgmtLogin(view, view.__rsRender || renderMcpInto);
        save.disabled = false;
        if (code !== 200 || !body.ok || !body.result) {
            status.replaceChildren(el("div", { class: "mcp-test-bad" }, [
                code === 200 ? "Couldn’t save: " + mgmtErrText(body)
                             : "Couldn’t save: connection failed."]));
            return;
        }
        // Re-read the page (the server rows count who is wired) and show what
        // this Save reported where the same selection is drawn again.
        mcpWiringFlash = { project: payload.project, who: whoSel.value,
                           lines: mcpAppliedLines(body.result.applied) };
        rerenderView(view);
    };
    section.append(
        el("div", { class: "config-box-row mcp-wire-row" }, [projectSel, whoSel, save]),
        note, cards, status);
    // Land on what the last Save touched, with its report.
    const flash = mcpWiringFlash;
    mcpWiringFlash = null;
    if (flash && targets.some((t) => t.project === flash.project)) {
        projectSel.value = flash.project;
        drawWho();
        whoSel.value = flash.who;
        drawCards();
        status.replaceChildren(...flash.lines.map((l) => el("div", {}, [l])));
    } else {
        drawWho();
    }
    return section;
}

function renderMcpScreen(view, result, wiring) {
    view.innerHTML = "";
    const servers = Array.isArray(result.servers) ? result.servers : [];
    const listOk = result.list === "ok";
    const rows = [];

    const refresh = el("button", { class: "btn-small" }, ["Refresh"]);
    refresh.onclick = () => rerenderView(view);
    const addBtn = el("button", { class: "btn-small" }, ["Add server"]);
    addBtn.disabled = !listOk;
    addBtn.onclick = () => mcpServerDialog(view, null);
    const testAll = el("button", { class: "btn-small" }, ["Test all"]);
    testAll.disabled = !servers.length;
    testAll.onclick = () => mcpTestAll(view, rows, testAll);
    view.appendChild(el("div", { class: "mgmt-header" }, [
        el("div", { class: "mgmt-toolbar" }, [refresh, addBtn, testAll]),
    ]));

    // The gateway line: its state, and Start when it is not running.
    const gwErr = el("span", { class: "mcp-test mcp-test-bad" });
    const gwRow = el("div", { class: "mgmt-row mgmt-infra-row mcp-server-row" }, [
        el("span", {}, ["MCP gateway"]),
        el("span", { class: "type-badge" }, [MCP_GATEWAY_TEXT[result.gateway] || "unknown"]),
    ]);
    if (result.gateway !== "running") {
        const start = el("button", { class: "btn-small" }, ["Start"]);
        start.onclick = () => mcpStartGateway(view, start, gwErr);
        gwRow.append(start, gwErr);
    }
    const section = el("div", { class: "mgmt-infra" }, [
        el("h3", { class: "mgmt-infra-title" }, ["Gateway"]),
        gwRow,
        el("div", { class: "mgmt-settings-hint" }, [
            "One shared container that holds every server's address and secrets. "
            + "Projects and boxes reach MCP servers only through it, and only the "
            + "ones they are wired to below."]),
    ]);
    view.appendChild(section);

    if (result.error) {
        const warn = el("div", { class: "mcp-list-error" }, [
            el("div", { class: "error" }, [result.error])]);
        if (result.list === "damaged") {
            const reset = el("button", { class: "btn-small" }, ["Start an empty list"]);
            reset.onclick = () => mcpConfirmDialog(view, {
                title: "Start an empty list",
                text: "The server list on the host cannot be read, so it is replaced "
                    + "by an empty one. Every server and every secret on it is "
                    + "discarded; you add the servers again afterwards.",
                confirmLabel: "Discard and start empty",
                path: "/broker/mcp/servers/reset",
                payload: {},
            });
            warn.appendChild(reset);
        }
        view.appendChild(warn);
    }

    const list = el("div", { class: "mgmt-infra" }, [
        el("h3", { class: "mgmt-infra-title" }, ["Servers"])]);
    if (!servers.length && listOk) {
        list.appendChild(el("div", { class: "mgmt-settings-hint" }, [
            "No servers yet. Add one with its address."]));
    }
    for (const srv of servers) {
        const cell = el("span", { class: "mcp-test" });
        const test = el("button", { class: "btn-small" }, ["Test"]);
        test.onclick = () => mcpTestRow(view, srv.name, cell);
        const edit = el("button", { class: "btn-small" }, ["Edit"]);
        edit.onclick = () => mcpServerDialog(view, srv);
        const remove = el("button", { class: "btn-small" }, ["Remove"]);
        remove.onclick = () => mcpConfirmDialog(view, {
            title: "Remove " + srv.name,
            text: "The server and its secret headers are taken off the list."
                + (wired.length
                    ? " It is wired to " + wiredNames.join(", ") + ": the gateway "
                      + "refuses it for them at once, and each agent’s entry for it "
                      + "goes the next time that project or box starts or is re-wired."
                    : ""),
            confirmLabel: "Remove",
            path: "/broker/mcp/servers/remove",
            payload: { name: srv.name },
        });
        const secrets = (srv.headers || []).length
            ? "Secret headers: " + srv.headers.join(", ") : "";
        const wired = Array.isArray(srv.wired) ? srv.wired : [];
        const wiredNames = wired.map((w) => w[0] + (w[1] === "agent" ? "" : " / " + w[1].slice(4)));
        const wiredText = wired.length ? "Wired to: " + wiredNames.join(", ") : "";
        const meta = [srv.description, secrets, wiredText].filter(Boolean).join(" · ");
        list.appendChild(el("div", { class: "mgmt-row mgmt-infra-row mcp-server-row",
                                     "data-mcp-server": srv.name }, [
            el("span", { class: "mcp-server-name" }, [srv.name]),
            el("span", { class: "mcp-server-url" }, [srv.url]),
            el("span", { class: "mcp-server-actions" }, [test, edit, remove]),
            el("div", { class: "mcp-server-meta" }, [meta]),
            cell,
        ]));
        rows.push({ name: srv.name, cell });
    }
    view.appendChild(list);
    view.appendChild(mcpWiringSection(view, servers, wiring));
}

// Sandboxes → Software — the host's agent/editor/reader/node dists, the built
// image fleet and the effective version pins (base + local override), via the
// login-gated broker `software_status` read, with the pull/refresh/rebuild
// lane. A 401 lands on the sign-in card, which comes back to this tab.
async function renderSoftwareInto(view) {
    view.innerHTML = "";
    view.appendChild(el("div", { class: "mgmt-loading" }, ["Loading software status…"]));
    let res;
    try {
        res = await fetch("/broker/software");
    } catch (e) { return renderMgmtUnavailable(view); }
    if (res.status === 401) return renderMgmtLogin(view, view.__rsRender || renderSoftwareInto);
    if (res.status === 403) return renderMgmtRejected(view);
    if (res.status === 503) return renderMgmtUnavailable(view);
    let body;
    try { body = await res.json(); } catch (e) { return renderMgmtUnavailable(view); }
    if (!res.ok || !body.ok || !body.result) {
        return renderMgmtVerbError(view, body, renderSoftwareInto);
    }
    renderSoftwareScreen(view, body.result);
    paintSoftwareLabels();
    seedSoftwareFromBroker();
}

function renderSoftwareScreen(view, result) {
    view.innerHTML = "";
    const agents = Array.isArray(result.agents) ? result.agents : [];
    const editor = result.editor || {};
    const reader = result.reader || {};
    const node = result.node || {};
    const images = Array.isArray(result.images) ? result.images : [];
    const pins = Array.isArray(result.pins) ? result.pins : [];
    const dockerOk = !!result.docker_ok;

    const refresh = el("button", { class: "btn-small" }, ["Refresh"]);
    refresh.onclick = () => renderSoftwareInto(view);
    const rebuildBtn = el("button", { class: "btn-small sw-fleet-btn" }, ["Rebuild all"]);
    rebuildBtn.onclick = () => softwareInFlight.has("fleet") ? null : mgmtBuildDialog(view, {
        title: "Rebuild the image fleet",
        tailTitle: "Rebuilding the image fleet",
        confirmLabel: "Rebuild all",
        payload: { verb: "rebuild" },
        body: [el("p", {}, ["Rebuild every image at the current effective pins — a long operation (often 10–15 min), one image at a time on the host. You can send it to the background and keep working; a notice reports when it ends."])],
    });
    // The tab's name (Sandboxes → Software) is the strip; only the toolbar here.
    view.appendChild(el("div", { class: "mgmt-header" }, [
        el("div", { class: "mgmt-toolbar" }, [refresh, rebuildBtn]),
    ]));
    // The fleet rebuild has no row of its own: its in-flight line sits here.
    view.appendChild(el("div", { class: "sw-fleet-line", "data-swtarget": "fleet" }, [
        el("span", { class: "sw-status" }, []),
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
        b.onclick = () => softwareInFlight.has(swTarget(payload.verb, payload.agent)) ? null : mgmtBuildDialog(view, {
            title: "Pull dist",
            tailTitle: softwareTitle(payload.verb, swTarget(payload.verb, payload.agent)),
            confirmLabel: "Pull",
            payload: payload, body: [el("p", {}, [tip])],
        });
        return b;
    };
    // Refresh = preview upstream, then (only if newer) bump the untracked override
    // pin + re-pull via the build lane. distLabel names the dist in the dialog.
    const refreshBtn = (distLabel, checkPayload, buildPayload) => {
        const b = el("button", { class: "btn-small sw-refresh-btn" }, ["Refresh"]);
        const target = swTarget(buildPayload.verb, buildPayload.agent);
        b.onclick = () => (softwareInFlight.has(target) || softwareChecking.has(target)) ? null : mgmtRefreshDialog(view, {
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
        distRows.push(el("div", { class: "sw-row", "data-swtarget": swTarget("agent_pull", a.agent) }, [
            el("span", { class: "sw-name" }, ["agent: " + a.agent]),
            cell(a.present ? "yes" : "no"),
            cell(a.cached_version, "sw-mono"),
            cell(a.effective_pin, "sw-mono"),
            el("span", { class: "sw-status" }, [distStatus(a)]),
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
        // The companion editor extension as a CONTINUATION sub-row (same six grid
        // columns: name, Present, Cached, Effective pin, Status, actions — the two
        // version cells must land under the parent's headings, hence the empty
        // Present cell). Gated on has_ext, which is the SAME field software_status
        // uses to decide whether the extension participates in matches_pin — so a
        // "stale — re-pull" can never render without the row that explains it.
        if (a.has_ext) {
            distRows.push(el("div", { class: "sw-row sw-subrow" }, [
                el("span", { class: "sw-subname" }, ["└ extension"]),
                el("span", {}, []),
                cell(a.cached_ext_version, "sw-mono"),
                cell(a.ext_pin, "sw-mono"),
                el("span", {}, []),
                el("span", {}, []),
            ]));
        }
        // Extra pins an agent's dist bundles (pi: the RS rules extension, the
        // private node) — the same continuation shape, one sub-row per pin, named
        // by the versions.env key; they participate in matches_pin exactly like
        // the extension does, so the row explains itself when one moved.
        for (const x of (Array.isArray(a.extra_pins) ? a.extra_pins : [])) {
            distRows.push(el("div", { class: "sw-row sw-subrow" }, [
                el("span", { class: "sw-subname", title: x.pin }, ["└ " + (x.label || x.pin)]),
                el("span", {}, []),
                cell(x.cached, "sw-mono"),
                cell(x.effective, "sw-mono"),
                el("span", {}, []),
                el("span", {}, []),
            ]));
        }
    }
    const nExt = Object.keys(editor.extensions || {}).length;
    distRows.push(el("div", { class: "sw-row", "data-swtarget": "editor" }, [
        el("span", { class: "sw-name" }, ["editor: code-server" + (nExt ? ` (+${nExt} ext)` : "")]),
        cell(editor.present ? "yes" : "no"),
        cell(editor.cached_version, "sw-mono"),
        cell(editor.effective_pin, "sw-mono"),
        el("span", { class: "sw-status" }, [distStatus(editor)]),
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
    distRows.push(el("div", { class: "sw-row", "data-swtarget": "reader" }, [
        el("span", { class: "sw-name" }, ["reader: nbconvert"
            + (reader.markdown_version ? ` (+md ${reader.markdown_version})` : "")]),
        cell(reader.present ? "yes" : "no"),
        cell(reader.cached_version, "sw-mono"),
        cell(reader.effective_pin, "sw-mono"),
        el("span", { class: "sw-status" }, [distStatus(reader)]),
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
    // Node seed (STAGE_NODE_SEED): Pull ONLY — node is a seed, not a managed dist,
    // so there is no Refresh (bump NODE_VERSION in versions.env by hand, then Pull).
    // The npm version rides in the name for parity with the reader's markdown note.
    distRows.push(el("div", { class: "sw-row", "data-swtarget": "node" }, [
        el("span", { class: "sw-name" }, ["node"
            + (node.npm_version ? ` (npm ${node.npm_version})` : "")]),
        cell(node.present ? "yes" : "no"),
        cell(node.cached_version, "sw-mono"),
        cell(node.effective_pin, "sw-mono"),
        el("span", { class: "sw-status" }, [distStatus(node)]),
        el("span", { class: "sw-act" }, [
            pullBtn(
                { verb: "node_pull" },
                "Fetch the node runtime seed at the effective pin, in a throwaway build container (under a minute). Node is a seed: it is copied into a box once and then detached — RS does not track or update it after."),
        ]),
    ]));
    view.appendChild(el("div", { class: "sw-section" }, [
        el("h3", {}, ["Dists"]),
        el("div", { class: "sw-table sw-dists" }, distRows),
        el("div", { class: "hint" }, [
            "Running projects keep the agent versions they have until Update is " +
            "pressed in their Config box (Agents); new projects get these.",
        ]),
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
    // A "local" row whose key this installation ships gets "Use repo version":
    // it removes that one key from the local override (the pin_reset verb). The
    // server decides which rows qualify (`resettable`, the same predicate the
    // verb validates with), so the button is never offered for a refused key.
    const pinRows = [el("div", { class: "sw-row sw-row-head" }, [
        el("span", {}, ["Pin"]), el("span", {}, ["Value"]), el("span", {}, ["Source"]),
        el("span", {}, [""]),
    ])];
    for (const p of pins) {
        const errSpan = el("span", { class: "sw-pin-err sw-warn" }, []);
        const act = [];
        if (p.resettable) {
            const btn = el("button", {
                class: "btn-small",
                title: "Put back " + p.base + ", the version this installation ships (removes the local override)",
            }, ["Use repo version"]);
            btn.onclick = () => pinResetClick(view, p.key, btn, errSpan);
            act.push(btn);
        }
        pinRows.push(el("div", { class: "sw-row" }, [
            el("span", { class: "sw-mono" }, [p.key]),
            el("span", { class: "sw-mono" }, [p.value]),
            p.override
                ? el("span", {
                    class: "sw-badge override",
                    title: p.base != null
                        ? "from versions.local.env — this installation ships " + p.base
                        : "from versions.local.env",
                }, ["local"])
                : el("span", { class: "sw-badge base", title: "from versions.env" }, ["base"]),
            el("span", { class: "sw-act" }, act),
            errSpan,
        ]));
    }
    view.appendChild(el("div", { class: "sw-section" }, [
        el("h3", {}, ["Version pins"]),
        el("div", { class: "sw-table sw-pins" }, pinRows),
        el("p", { class: "sw-pins-hint" }, [
            "Use repo version changes the pin only. To apply it, Pull the download that bundles that pin, or use Rebuild all for an image pin (new projects pick it up, and a project's boxes move to a new box image at its next start).",
        ]),
    ]));
}

// "Use repo version" on one pin row: POST the key, then re-read the whole page
// (the row flips to "base"). Transport failures and 401/403/503 are the page's
// (renderMgmtUnavailable / mgmtStatusRedirect); a broker refusal (busy, or a key
// the verb refuses) is shown inline in the row and the button comes back.
async function pinResetClick(view, key, btn, errSpan) {
    if (btn.disabled) return;
    btn.disabled = true;
    errSpan.textContent = "";
    let res;
    try {
        res = await fetch("/broker/software/pin-reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key: key }),
        });
    } catch (e) { return renderMgmtUnavailable(view); }
    const redirect = mgmtStatusRedirect(view, res.status, renderSoftwareInto);
    if (redirect) return redirect();
    let body;
    try { body = await res.json(); } catch (e) { return renderMgmtUnavailable(view); }
    if (!res.ok || !body || !body.ok) {
        // Reachable only on 200-with-parseable-JSON: the broker answered and the
        // verb refused — show its words in the row, never the unreachable card.
        errSpan.textContent = mgmtErrText(body);
        btn.disabled = false;
        return;
    }
    renderSoftwareInto(view);
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
    // Pick up actions started elsewhere (another tab, before a reload) and
    // restart any watcher a session expiry stalled — a 401 lands here after
    // the login card, so software builds' watchers restart here too.
    seedLifecycleFromBroker();
    seedSoftwareFromBroker();
}

// Sandboxes → Infrastructure: pass-through ports, then the shared services
// (Gitea, the reviewer). The pass-through section names the projects a claim
// can go to, so this tab reads the project list too — with the same
// transport-vs-verb split as every host page.
async function renderInfraInto(view) {
    view.innerHTML = "";
    view.appendChild(el("div", { class: "mgmt-loading" }, ["Loading…"]));
    let res;
    try {
        res = await fetch("/broker/projects");
    } catch (e) {
        return renderMgmtUnavailable(view);
    }
    if (res.status === 401) return renderMgmtLogin(view, view.__rsRender || renderInfraInto);
    if (res.status === 403) return renderMgmtRejected(view);
    if (res.status === 503) return renderMgmtUnavailable(view);
    let body;
    try { body = await res.json(); } catch (e) { return renderMgmtUnavailable(view); }
    if (!res.ok || !body.ok) {
        return renderMgmtVerbError(view, body, renderInfraInto);
    }
    view.innerHTML = "";
    const refresh = el("button", { class: "btn-small" }, ["Refresh"]);
    refresh.onclick = () => renderInfraInto(view);
    view.appendChild(el("div", { class: "mgmt-header" }, [
        el("div", { class: "mgmt-toolbar" }, [refresh]),
    ]));
    // Pass-through ports ABOVE the shared services (PI decision). Both are
    // async fire-and-forget with their static DOM appended synchronously, so
    // the section order is deterministic regardless of fetch timing.
    appendPassthroughSection(view, body.result || []);
    appendInfraSection(view);
    appendSettingsSection(view);
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
    // No explicit landing: re-render the page this view belongs to (a host-page
    // body is stamped with its renderer), else the historical Management table.
    const route = onSuccess || (view && view.__rsRender) || renderManagementInto;
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
    if (res && res.status === 200) { noteBrokerLogin(); return route(view); }
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

// The broker `list` state of a project whose container is gone while
// something of it (workspace, network, DIND volume) is still on the host.
// LOCKSTEP with rscore.ORPHANED_STATE.
const ORPHANED_STATE = "orphaned";
const ORPHANED_TITLE = "Its container is gone (for example after the host's Docker " +
    "was reset). Destroy removes the workspace and everything else left of it.";

// ---- the Management table: a real, sortable table ---------------------------
// Click a column name to sort by it, again to reverse. Unknown values (a
// workflow not read yet or absent, an SSH address the broker did not give, a
// size not measured) sort LAST in both directions — "the biggest first" must
// not open on a page of "—". Ties fall back to the project name A→Z. The
// choice is kept in this browser like the other interface settings.
const MGMT_SORT_STORE = "rs-webui-mgmt-sort";
const MGMT_SORT_COLUMNS = [["name", "Project"], ["workflow", "Workflow"], ["state", "State"],
                           ["ssh", "SSH"], ["size", "Size"]];
const MGMT_SORT_DEFAULT = { key: "name", dir: "asc" };

// A stored sort, validated; anything else is the default.
function decodeMgmtSort(raw) {
    let v = null;
    try { v = JSON.parse(raw); } catch (_) { v = null; }
    if (!v || typeof v !== "object") return { ...MGMT_SORT_DEFAULT };
    const keyOk = MGMT_SORT_COLUMNS.some(([k]) => k === v.key);
    if (!keyOk || (v.dir !== "asc" && v.dir !== "desc")) return { ...MGMT_SORT_DEFAULT };
    return { key: v.key, dir: v.dir };
}

function loadMgmtSort() {
    let raw = null;
    try { raw = localStorage.getItem(MGMT_SORT_STORE); } catch (_) { raw = null; }
    return decodeMgmtSort(raw);
}

function saveMgmtSort(sort) {
    try { localStorage.setItem(MGMT_SORT_STORE, JSON.stringify(sort)); } catch (_) { /* per-browser convenience */ }
}

// Row models: {name, workflow, state, ssh, size} — size a number or null.
function mgmtCompare(a, b, key, dir) {
    const va = a[key], vb = b[key];
    const missA = va === null || va === undefined || va === "";
    const missB = vb === null || vb === undefined || vb === "";
    if (missA !== missB) return missA ? 1 : -1;
    if (!missA) {
        const c = (typeof va === "number" && typeof vb === "number")
            ? va - vb : String(va).localeCompare(String(vb));
        if (c !== 0) return (dir === "desc" ? -1 : 1) * (c < 0 ? -1 : 1);
    }
    return String(a.name).localeCompare(String(b.name));
}

function renderMgmtTable(view, projects) {
    view.innerHTML = "";
    // The New Project box is the create entry point (the workflow picker is
    // its card grid); this button just opens it. mgmtCreateDialog is only
    // ever opened from a workflows card, with a chosen workflow manifest.
    const create = el("button", { class: "btn-small" }, ["+ New project"]);
    create.onclick = () => openWorkflows();
    const refresh = el("button", { class: "btn-small" }, ["Refresh"]);
    refresh.onclick = () => renderManagementInto(view);
    // Disk sizes are a USER-TRIGGERED measurement: summing a workspace means
    // walking every file in it (seconds and more than a core on a large dev
    // tree), so nothing computes it on open or on a timer — the column starts
    // empty and this button fills it for every listed project.
    const measure = el("button", { class: "btn-small" }, ["Measure sizes"]);
    // No Log-out control: under unified login the session model is "locked
    // vault = logged out" — Lock vault (rail footer) revokes the broker
    // session; a separate Management logout would just auto-re-login.
    // The page's name is the strip chip (Sandboxes) and this tab's (Management),
    // so the header row carries only the toolbar.
    view.appendChild(el("div", { class: "mgmt-header" }, [
        el("div", { class: "mgmt-toolbar" }, [create, refresh, measure]),
    ]));
    // Above both branches, so a host with no projects still sees it.
    appendOriginNotice(view);   // async fill, placeholder appended now
    if (projects.length === 0) {
        view.appendChild(el("div", { class: "mgmt-empty" }, ["No projects on this host."]));
        return;
    }
    const sort = loadMgmtSort();
    const models = [];
    const tbody = el("tbody", {});
    // Rows are built ONCE and re-ordered in place, so their buttons, busy
    // labels and late-filled cells survive every re-sort.
    const applySort = () => {
        models.sort((a, b) => mgmtCompare(a, b, sort.key, sort.dir));
        for (const m of models) tbody.appendChild(m.tr);
        for (const th of heads) {
            const on = th.getAttribute("data-sort") === sort.key;
            if (on) th.setAttribute("aria-sort", sort.dir === "desc" ? "descending" : "ascending");
            else th.removeAttribute("aria-sort");
        }
        sortSelect.value = sort.key;
        sortDir.textContent = sort.dir === "desc" ? "↓" : "↑";
        sortDir.title = sort.dir === "desc" ? "Descending — click to reverse" : "Ascending — click to reverse";
    };
    const setSort = (key, dir) => {
        sort.key = key;
        sort.dir = dir;
        saveMgmtSort(sort);
        applySort();
    };
    const heads = MGMT_SORT_COLUMNS.map(([key, label]) => {
        const th = el("th", { scope: "col", class: "mgmt-sortable", "data-sort": key,
                              title: "Sort by " + label.toLowerCase() }, [label]);
        th.onclick = () => setSort(key, sort.key === key && sort.dir === "asc" ? "desc" : "asc");
        return th;
    });
    // The phone layout has no column headers (each row is a card): the same
    // sort, as a selector and a direction button above the cards.
    const sortSelect = el("select", { class: "mgmt-sort-select", title: "Sort by" },
        MGMT_SORT_COLUMNS.map(([key, label]) => el("option", { value: key }, [label])));
    sortSelect.onchange = () => setSort(sortSelect.value, sort.dir);
    const sortDir = el("button", { class: "btn-small mgmt-sort-dir" }, ["↑"]);
    sortDir.onclick = () => setSort(sort.key, sort.dir === "asc" ? "desc" : "asc");
    const fill = {};   // project name → {badge, size} elements to populate async
    for (const p of projects) {
        const running = p.state === "running";
        const orphaned = p.state === ORPHANED_STATE;
        const badge = el("span", { class: "type-badge" });          // filled by mgmtFillStatus
        const sizeEl = el("span", { class: "mgmt-size" }, ["—"]);   // filled by Measure sizes
        fill[p.project] = { badge, size: sizeEl };
        // Attach + Update need a live supervisor (bridge endpoint / recreate);
        // only offered while running. Destroy is always available. An
        // orphaned project (its container is gone) offers ONLY Destroy: there
        // is no container to start or stop.
        const actions = [];
        if (!orphaned) {
            const power = el("button", { class: "btn-small" }, [running ? "Stop" : "Start"]);
            power.onclick = () => mgmtAction(view, p.project, running ? "stop" : "start");
            actions.push(power);
        }
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
        const tr = el("tr", { class: "mgmt-prow", "data-project": p.project }, [
            el("td", { class: "mgmt-pname" }, [el("span", { class: "mgmt-name-text" }, [p.project])]),
            el("td", { class: "mgmt-workflow" }, [badge]),
            el("td", Object.assign(
                { class: "mgmt-state " + (running ? "state-running" : "state-stopped"),
                  "data-state": p.state },
                orphaned ? { title: ORPHANED_TITLE } : {}), [p.state]),
            el("td", { class: "mgmt-ssh" }, [p.ssh || "—"]),
            el("td", { class: "mgmt-size-cell" }, [sizeEl]),
            el("td", { class: "mgmt-actions-cell" }, [el("div", { class: "mgmt-actions" }, actions)]),
        ]);
        models.push({ name: p.project, workflow: "", state: p.state || "", ssh: p.ssh || "",
                      size: null, tr });
    }
    const byName = {};
    for (const m of models) byName[m.name] = m;
    // Late data (the workflow badges, measured sizes) lands in the models and
    // re-sorts when it is the column the table is sorted on.
    const onData = (column, values) => {
        for (const [name, v] of Object.entries(values)) {
            if (byName[name]) byName[name][column] = v;
        }
        if (sort.key === column) applySort();
    };
    view.appendChild(el("div", { class: "mgmt-sortbar" }, [
        el("span", { class: "mgmt-sortbar-label" }, ["Sort by"]), sortSelect, sortDir,
    ]));
    view.appendChild(el("div", { class: "mgmt-ptable-wrap" }, [
        el("table", { class: "mgmt-ptable" }, [
            el("thead", {}, [el("tr", {}, [...heads, el("th", { scope: "col" }, ["Actions"])])]),
            tbody,
        ]),
    ]));
    applySort();
    paintLifecycleLabels();   // rows with an action in flight: label + disabled buttons
    mgmtFillStatus(fill, onData);
    measure.onclick = () => mgmtMeasureSizes(fill, measure, onData);
}

// The broker `list` carries name/state/ssh; the project flavour (the badge)
// comes off the same marker-only /projects/status call the rail uses (read
// from the /projects:ro mount, joined here by name). Disk size is NOT read
// here — see mgmtMeasureSizes.
// `onData(column, {name: value})`, when given, receives what was filled (the
// table keeps its sort models with it).
async function mgmtFillStatus(fill, onData) {
    const names = Object.keys(fill);
    if (names.length === 0) return;
    let data;
    try {
        const res = await fetch(`/projects/status?names=${encodeURIComponent(names.join(","))}`);
        if (!res.ok) return;
        data = await res.json();
    } catch (e) { return; }
    const got = {};
    for (const [name, st] of Object.entries(data)) {
        const ref = fill[name];
        if (!ref) continue;
        const label = st && st.error !== "not_found" ? st.workflow : null;
        setTypeBadge(ref.badge, label);
        got[name] = label || "";
    }
    if (onData) onData("workflow", got);
}

// "Measure sizes": the one place the webui asks the server to walk project
// trees (/projects/disk). Every listed project at once; the cells show "…"
// while the walk runs, the button is disabled for the duration so a second
// click cannot stack a second walk, and a failed fetch leaves "?" rather than
// a stale number. A Refresh mid-flight replaces the table — the late fill then
// writes into detached nodes, which is harmless — and hands the user a fresh,
// enabled button, so a second click during a first walk starts a second walk:
// accepted, the cost is bounded by clicks and the later answer is the fresher.
async function mgmtMeasureSizes(fill, btn, onData) {
    const names = Object.keys(fill);
    if (names.length === 0) return;
    btn.disabled = true;
    for (const ref of Object.values(fill)) ref.size.textContent = "…";
    // Every listed name gets a value: a byte count, or null (unknown) when the
    // walk named none for it or failed.
    const got = {};
    for (const name of names) got[name] = null;
    try {
        const res = await fetch(`/projects/disk?names=${encodeURIComponent(names.join(","))}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        for (const [name, st] of Object.entries(data)) {
            const ref = fill[name];
            if (!ref) continue;
            const bytes = st && typeof st.disk_bytes === "number" ? st.disk_bytes : null;
            ref.size.textContent = bytes !== null ? formatBytes(bytes) : "—";
            got[name] = bytes;
        }
    } catch (e) {
        for (const ref of Object.values(fill)) ref.size.textContent = "?";
    } finally {
        if (onData) onData("size", got);
        btn.disabled = false;
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
// Three sub-tabs: "Gitea" (the gitea web UI on its own origin port, session
// minted via the Management-anchored /broker/dev/gitea-session), "Fetch" (the
// RS-rendered open-PR/branch list with click-to-copy rs-fetch commands, fed by
// one /broker/dev read per open — Q6, no poller), "Repos" (the dev-lane
// inventory: per-repo activity counts and the rs-fetch visibility switches that
// decide which repos the Fetch list shows, off the SAME /broker/dev read) and
// "Reviews" (every verdict ever recorded, off its own ledger read). The Fetch
// tab is the guaranteed copy surface; the agent's final-comment code block
// inside Gitea (with Gitea's own copy button) is the convenience layer.
//
// Reviews is deliberately NOT a section of the Fetch tab's repo cards: a repo
// card exists only while gitea runs AND its mirror is still listed, so a
// history living there would vanish exactly when the rows it outlives do. Its
// read touches no gitea at all, so the tab renders with gitea stopped and after
// the last mirror is gone. Repos is the opposite case and belongs where it is:
// it enumerates exactly those live mirrors, so it shares Fetch's read and its
// gitea-running gate.

let devActiveTab = "gitea";

function openDevelopment() { openHostPage("development"); }

// The Development sections, keyed like HOST_PAGES.development.tabs (the strip
// tabs ARE the section switch). Gitea stays the landing section: the inventory
// is a management surface, not the thing you open the page to do. In-page
// jumps (`devActiveTab = "repos"; renderDevelopmentInto(view)`) and the 401
// landings call this directly, so it also repaints the strip's highlight.
const DEV_TAB_RENDER = {
    gitea: renderDevGiteaTab,
    fetch: renderDevFetchTab,
    repos: renderDevReposTab,
    reviews: renderDevReviewsTab,
    backup: renderDevBackupTab,
};

function renderDevelopmentInto(view) {
    if (state.hostPage === "development") mountHostStrip();
    view.innerHTML = "";
    const body = el("div", { class: "dev-body" });
    view.appendChild(body);
    const render = DEV_TAB_RENDER[devActiveTab] || renderDevGiteaTab;
    render(view, body);
}

// The Backup tab: take and discard an encrypted copy of the whole
// dev Gitea. The board — issues, pull requests and their discussion — is the
// human<->agent channel and exists in no git object, while an agent owns its
// fork and in gitea ownership implies destruction. This is the surface that
// makes that survivable.
//
// The panel shows the EXPORT'S OWN facts and nothing else. It deliberately does
// not summarise what is inside: the Development page's repo counters filter to
// the mirrors (not the consumer forks, which is where boards live) and count
// only OPEN items, so any headline drawn from them would state a number
// materially smaller than what the file actually holds.
function devBackupBytes(n) {
    if (typeof n !== "number") return "";
    const mb = n / (1024 * 1024);
    return mb >= 1 ? mb.toFixed(1) + " MiB" : Math.max(1, Math.round(n / 1024)) + " KiB";
}

async function renderDevBackupTab(view, body) {
    body.innerHTML = "";
    body.appendChild(el("div", { class: "mgmt-loading" }, ["Loading backup…"]));
    // Two independent reads: whether gitea is up (a backup needs it running),
    // and whether a backup is already resident (its host path, size and age, from
    // the host — the page cannot compute a path inside a container).
    let devRes, artRes;
    try {
        [devRes, artRes] = await Promise.all([
            fetch("/broker/dev"), fetch("/broker/dev/board-export"),
        ]);
    } catch (e) { return renderMgmtUnavailable(body); }
    for (const res of [devRes, artRes]) {
        // The transport-vs-verb split: _relay reports its OWN failures as
        // 401/403/503 and a verb's refusal as HTTP 200 with {ok:false}. They must
        // not collapse into one error card — a 503 means the broker is
        // unreachable, which is a different remedy from a refused verb.
        if (res.status === 401) return renderMgmtLogin(view, renderDevelopmentInto);
        if (res.status === 403) return renderMgmtRejected(body);
        if (res.status === 503) return renderMgmtUnavailable(body);
    }
    let dev = {}, art = {};
    try { dev = await devRes.json(); } catch (e) { dev = {}; }
    try { art = await artRes.json(); } catch (e) { art = {}; }
    body.innerHTML = "";
    const rerender = () => renderDevBackupTab(view, body);

    // A VERB refusal (HTTP 200 carrying {ok:false}) or an unparseable body must
    // render the REAL error — never collapse into the "Gitea isn't running"
    // card below. That collapse is B38: it sends the operator to press Enable on
    // a Gitea that is already up, hiding whatever actually failed. The sibling
    // tabs carry the same arm for exactly this reason.
    if (!devRes.ok || !dev.ok || !dev.result) {
        const retryE = el("button", { class: "btn-small" }, ["Retry"]);
        retryE.onclick = rerender;
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, [(mgmtErrText(dev)
                             || "Could not read the dev state.") + " "]),
            retryE,
        ]));
        return;
    }
    // `running` is nested under result.gitea, not at the top of result — the
    // same read the Repos and Fetch tabs do. One level too high is SILENT:
    // undefined is falsy, so the tab reports gitea down while it is up.
    const gstate = dev.result.gitea || {};
    const running = !!gstate.running;
    // The artifact read is allowed to be absent without failing the page: it is
    // a relay like the one beside it, so an empty reading means "no backup"
    // rather than "broken"; the dev-state arm above is what catches a real
    // failure, and this default only covers the ordinary absent-file case.
    const info = (art && art.ok && art.result) ? art.result : {};

    if (!running) {
        // Only in place of Enable: a held Gitea with a backup already on disk
        // still shows where that file is.
        if (devGiteaHeldNotice(body, gstate)) return;
        const enable = el("button", { class: "btn-small" }, ["Enable Gitea"]);
        enable.onclick = () => devEnableGiteaDialog(view, rerender);
        const retry = el("button", { class: "btn-small" }, ["Retry"]);
        retry.onclick = rerender;
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, ["Gitea isn't running, so there is nothing to back "
                            + "up yet. "]), enable, retry,
        ]));
        return;
    }

    const head = el("div", { class: "dev-repo-head" }, [
        el("span", { class: "dev-repo-name" }, ["Backup"]),
    ]);
    const kids = [head];

    if (info.present) {
        const when = info.created_at
            ? new Date(info.created_at).toLocaleString() : "unknown";
        kids.push(el("p", {}, [
            "One backup is held, taken ", el("strong", {}, [when]),
            " (", devBackupBytes(info.size), "). It stays here until you take "
            + "another or discard it. Copy it somewhere safe — it is on the "
            + "machine running this, at:",
        ]));
        kids.push(el("p", { class: "dev-backup-path" },
                 [info.path || "(the host could not be asked where backups live)"]));
    } else {
        kids.push(el("p", {}, [
            "No backup is held yet. When you take one it is written to:",
        ]));
        kids.push(el("p", { class: "dev-backup-path" },
                 [info.path || "(the host could not be asked where backups live)"]));
    }

    kids.push(el("p", { class: "dev-repo-meta" }, [
        "A backup is the ", el("strong", {}, ["whole Gitea"]),
        " — every repository and every project's board, not one of them. "
        + "It is encrypted with a passphrase you choose, which is not your "
        + "master password and cannot be recovered if you lose it. Treat the "
        + "file as the server itself: it carries the server's own keys as well "
        + "as its contents.",
    ]));
    kids.push(el("p", { class: "dev-repo-meta" }, [
        "What it does not contain: the host's record of which project works "
        + "which repository, and the agents' own credentials. Those live "
        + "outside Gitea, so anything set up after a backup will need setting "
        + "up again if you ever restore from it.",
    ]));
    // THE WARNING (maintainer ruling, 2026-09-14). A backup can only ever be
    // restored into the Gitea that made it — the instance identity in its config
    // is what a restore checks, and a re-created Gitea has a new one. Saying so
    // here is the difference between a backup that protects what the operator
    // thinks it protects and one that quietly does not.
    kids.push(el("p", { class: "dev-repo-meta" }, [
        el("strong", {}, ["A backup protects against things being deleted "
                          + "inside Gitea, not against losing Gitea itself. "]),
        "It can only be restored into the same Gitea that made it. If this "
        + "Gitea is ever destroyed and set up again, its old backups cannot be "
        + "read — the new one is a different server, and a restore refuses "
        + "rather than appearing to work and silently losing everything that "
        + "was encrypted.",
    ]));
    // The FOURTH sanctioned exemption to the no-CLI-mention directive, granted
    // for exactly this and deliberately not taken until the command existed.
    kids.push(el("p", { class: "dev-repo-meta" }, [
        "Restoring is done on the machine running this, not from here — it "
        + "stops Gitea and replaces every repository and every board, so it "
        + "asks for confirmation at the terminal: ",
        el("code", {}, ["research dev board import <file>"]),
    ]));

    const take = el("button", { class: "btn-small" },
                   [info.present ? "Take a new backup" : "Take a backup"]);
    take.onclick = () => devBoardExportDialog(view, rerender);
    const actions = [take];
    if (info.present) {
        const discard = el("button", { class: "btn-small btn-danger" },
                           ["Discard"]);
        discard.onclick = () => devBoardDiscardDialog(view, rerender);
        actions.push(discard);
    }
    kids.push(el("div", { class: "btn-row left" }, actions));
    body.appendChild(el("div", { class: "card dev-repo-card" }, kids));
}

function devBoardExportDialog(view, onDone) {
    const pp1 = el("input", { type: "password", autocomplete: "new-password" });
    const pp2 = el("input", { type: "password", autocomplete: "new-password" });
    const masterI = el("input", { type: "password", autocomplete: "current-password" });
    mgmtConfirmThenTail(view, {
        title: "Take a backup",
        tailTitle: "Backing up Gitea",
        verb: "dev_board_export",
        confirmLabel: "Take backup",
        body: [
            el("p", {}, [
                "Encrypts the whole Gitea — every repository and every board — "
                + "into one file on the machine running this, which you "
                + "can copy and keep.",
            ]),
            el("p", {}, [
                "Choose a passphrase for the file. It is not your master "
                + "password, and there is no way to recover it: lose it and the "
                + "backup cannot be opened by anyone, including you.",
            ]),
            el("p", {}, [
                "This can take a while on a large Gitea, and other management "
                + "actions wait until it finishes.",
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Backup passphrase"]), pp1,
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Repeat it"]), pp2,
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Re-enter your master password"]), masterI,
            ]),
        ],
        // Mirrors ALL of the server's refusals, not just the length floor: the
        // server also rejects a whitespace-edged value, and a password input
        // holds a pasted trailing space perfectly well. Without the check that
        // lands as a mid-operation refusal on a backup the operator already
        // committed to. The 8 mirrors broker_auth.MIN_PASSWORD_LENGTH (the
        // renderSetup / devGiteaPasswdDialog floor — change together).
        validate: () => {
            if (pp1.value.length < 8) return "Passphrase must be at least 8 characters.";
            if (pp1.value !== pp1.value.trim()) {
                return "Passphrase must not start or end with a space.";
            }
            if (pp1.value !== pp2.value) return "Passphrases do not match.";
            if (!masterI.value) return "Re-enter your master password.";
            return null;
        },
        request: async () => fetch("/broker/dev/board-export", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                passphrase: pp1.value,
                proof: await deriveLoginProof(masterI.value),
            }),
        }),
        onDone: (ok) => { if (onDone) onDone(ok); },
        focus: () => pp1.focus(),
    });
}

function devBoardDiscardDialog(view, onDone) {
    const masterI = el("input", { type: "password", autocomplete: "current-password" });
    mgmtConfirmThenTail(view, {
        title: "Discard this backup",
        tailTitle: "Discarding backup",
        verb: "dev_board_discard",
        confirmLabel: "Discard",
        danger: true,
        body: [
            el("p", {}, [
                "Deletes the backup held here. If you have not copied it "
                + "elsewhere, it is gone. You can take another while Gitea is "
                + "healthy — but if this backup is the only copy of a board "
                + "something already destroyed, there is nothing to take "
                + "another from.",
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Re-enter your master password"]), masterI,
            ]),
        ],
        validate: () => masterI.value ? null : "Re-enter your master password.",
        request: async () => fetch("/broker/dev/board-discard", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ proof: await deriveLoginProof(masterI.value) }),
        }),
        onDone: (ok) => { if (onDone) onDone(ok); },
        focus: () => masterI.focus(),
    });
}

// The review HISTORY: every verdict on the host, newest first, off
// /broker/dev/reviews. A badge on a PR row is a decoration on something live —
// it dies with the row when the PR lands, the fork is purged or a collection
// rewrites the shas. These rows are the durable record, which is why each one
// names what was reviewed (title / branch / commit subject / fork) rather than
// leaning on a neighbouring row for context: the row it came from is usually
// gone by the time anyone reads this.
//
// Rendered from the entry alone — no gitea state is consulted, so a stopped
// gitea shows the same list rather than the enable card the other two tabs owe
// their read to.
async function renderDevReviewsTab(view, body) {
    body.innerHTML = "";
    body.appendChild(el("div", { class: "mgmt-loading" }, ["Loading reviews…"]));
    let res;
    try {
        res = await fetch("/broker/dev/reviews");
    } catch (e) { return renderMgmtUnavailable(body); }
    if (res.status === 401) return renderMgmtLogin(view, renderDevelopmentInto);
    if (res.status === 403) return renderMgmtRejected(body);
    // _relay's 503 body is PARSEABLE JSON ({ok:false, broker_unavailable}) and
    // carries no message — peel it explicitly or the error card below renders
    // the bare token at the operator.
    if (res.status === 503) return renderMgmtUnavailable(body);
    let data;
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok || !data.ok || !data.result) {
        body.innerHTML = "";
        const retry = el("button", { class: "btn-small" }, ["Retry"]);
        retry.onclick = () => renderDevReviewsTab(view, body);
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, [(mgmtErrText(data)
                             || "Could not read the reviews.") + " "]),
            retry,
        ]));
        return;
    }
    const rows = Array.isArray(data.result.reviews) ? data.result.reviews : [];
    body.innerHTML = "";
    if (!rows.length) {
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, ["No reviews yet. Review a pull request or a "
                            + "commit from the Fetch tab and it is kept here."]),
        ]));
        return;
    }
    const card = el("div", { class: "card dev-repo-card" }, [
        el("div", { class: "dev-repo-head" }, [
            el("span", { class: "dev-repo-name" }, ["Reviews"]),
            el("span", { class: "dev-repo-meta" },
               [`${rows.length} recorded`]),
        ]),
    ]);
    for (const v of rows) card.appendChild(devReviewHistoryRow(v));
    body.appendChild(card);
}

// ONE history row + its lazily-toggled verdict panel, in a wrapper so the panel
// stays with its row (the list has no stable anchor to re-attach to).
function devReviewHistoryRow(v) {
    const pr = v.kind === "pr";
    const what = pr ? `#${v.pr}`
                    : `@${String(v.commit || "").slice(0, 9)}`;
    // The title/subject is only on entries written since verdicts started
    // carrying it; an older one degrades to its identifier alone.
    const label = (pr ? v.title : v.subject) || "";
    const ok = v.status === "ok";
    const badge = el("button", {
        // Outcome-only colour keying, exactly as the row badges do it — never
        // the free-text risk label, which the reviewer's prompt owns.
        class: "btn-small " + (ok ? "dev-review-badge" : "dev-review-failed")
            + (v.outcome === "pass" ? " dev-outcome-pass"
               : v.outcome === "fail" ? " dev-outcome-fail" : ""),
        title: ok ? "show the review verdict" : "show what the attempt recorded",
    }, [ok ? "reviewed ✓" + (v.risk ? ` · ${v.risk}` : "")
           : "review failed" + (v.reason ? `: ${v.reason}` : "")]);
    const panel = el("div", { class: "dev-verdict-panel" });
    panel.style.display = "none";
    for (const n of devVerdictBody(v, v.fork ? ` · fork ${v.fork}` : ""))
        panel.appendChild(n);
    badge.onclick = () => {
        panel.style.display = panel.style.display === "none" ? "" : "none";
    };
    return el("div", { class: "dev-review-history-row" }, [
        el("div", { class: "dev-pr-row" }, [
            el("span", { class: "dev-pr-meta" }, [devStamp(v.reviewed_at)]),
            el("span", { class: "dev-pr-id" }, [v.repo || ""]),
            el("span", { class: "dev-pr-meta" }, [what]),
            el("span", { class: "dev-pr-title" },
               [label + (pr && v.head ? `  [${v.head}]` : "")]),
            badge,
        ]),
        panel,
    ]);
}

async function renderDevGiteaTab(view, body) {
    body.innerHTML = "";
    body.appendChild(el("div", { class: "mgmt-loading" }, ["Opening Gitea…"]));
    // Gate on gitea RUNNING before minting a session / loading the iframe — a
    // stopped gitea would otherwise proxy the iframe into an unreachable
    // upstream (the _proxy_http 502 is the backstop; this is the graceful path).
    let devRes;
    try {
        devRes = await fetch("/broker/dev");
    } catch (e) { return renderMgmtUnavailable(body); }
    if (devRes.status === 401) return renderMgmtLogin(view, renderDevelopmentInto);
    if (devRes.status === 403) return renderMgmtRejected(body);
    // _relay's 503 body is PARSEABLE JSON ({ok:false, broker_unavailable}) —
    // without this peel a broker outage would fall through to the verb-error
    // arm below, inverting the split on the ONE surface allowed to name
    // `research broker start`.
    if (devRes.status === 503) return renderMgmtUnavailable(body);
    let dev;
    try { dev = await devRes.json(); } catch (e) { return renderMgmtUnavailable(body); }
    if (!devRes.ok || !dev.ok || !dev.result) {
        // Reachable ONLY on 200-with-parseable-JSON (the ladder above peeled
        // every relay-own failure): the broker ANSWERED and the verb refused
        // — render the REAL error, never the "isn't enabled" card (B38; the
        // renderDevFetchTab shape).
        body.innerHTML = "";
        const retry = el("button", { class: "btn-small" }, ["Retry"]);
        retry.onclick = () => renderDevGiteaTab(view, body);
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, [
                (mgmtErrText(dev) || "Could not read the dev status.") + " "]),
            retry,
        ]));
        return;
    }
    const gstate = dev.result.gitea || {};
    if (gstate.upgrading || (gstate.upgrade && gstate.upgrade.phase !== "committed")) {
        body.innerHTML = "";               // the "Opening Gitea…" line
        devGiteaHeldNotice(body, gstate);
        return;
    }
    if (!gstate.running) {
        body.innerHTML = "";
        const enable = el("button", { class: "btn-small" }, ["Enable Gitea"]);
        enable.onclick = () =>
            devEnableGiteaDialog(view, () => renderDevGiteaTab(view, body));
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, [gstate.exists
                ? "Gitea is stopped. Enable it to open the web UI. "
                : "Gitea isn't enabled yet. Enable it under Sandboxes → " +
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

// cmd is a STRING (fixed command) or a FUNCTION (computed AT CLICK TIME —
// the card's auto-commit tick box changes what a copy produces, so provider
// buttons read the live state; their hover title stays generic because a
// tooltip carrying a stale command would lie about the tick).
function devCopyBtn(cmd) {
    const isFn = typeof cmd === "function";
    const b = el("button", { class: "btn-small dev-copy",
                             title: isFn ? "Copy the rs-fetch command"
                                         : "Copy: " + cmd }, ["📋"]);
    b.onclick = async () => {
        const text = isFn ? cmd() : cmd;
        try { await navigator.clipboard.writeText(text); } catch (e) { return; }
        b.textContent = "✓";
        setTimeout(() => { b.textContent = "📋"; }, 1200);
    };
    return b;
}

// ---- In-flight review registry + background watcher -------------------------
// Reviews are DETACHED broker ops (the parallel review lane): once started,
// nothing requires a modal to stay open. The registry keeps "reviewing…"
// state across panel re-renders within this page session; the watcher polls
// the op to its terminal and re-renders the stamped cells in place. Key:
// repo + NUL + loc, where loc is a FULL commit sha or "#"+pr — NUL is illegal
// in repo names, shas and PR numbers, so keys cannot collide. NOTE the key
// also lands in a data-rkey DOM attribute; matching is a JS string compare
// over [data-rkey] elements, never a CSS attribute-value selector (CSS.escape
// maps NUL to U+FFFD, which would never match the stored value). Value:
// {opId, watching, stalled}. Deliberately NOT persisted: after a page reload
// an in-flight review shows a plain Review button again — re-clicking runs a
// duplicate review and the last ledger write wins (harmless, accepted).
const devReviewsInFlight = new Map();

function devReviewKey(repo, loc) { return repo + "\u0000" + loc; }

// Apply `fill` to every CONNECTED element stamped with this key (JS-compare
// matching — see the NUL note above); detached leftovers are skipped.
function devUpdateReviewCells(key, fill) {
    for (const cellEl of document.querySelectorAll("[data-rkey]")) {
        if (cellEl.getAttribute("data-rkey") !== key) continue;
        if (!cellEl.isConnected) continue;
        fill(cellEl);
    }
}

function devMarkReviewingCells(key) {
    devUpdateReviewCells(key, (cellEl) => {
        cellEl.innerHTML = "";
        cellEl.appendChild(el("span", { class: "dev-pr-meta dev-reviewing" },
                              ["reviewing…"]));
    });
}

// The verdict BODY — the meta line, the summary and the findings — shared by
// the PR badge panel, the commit badge panel and the Reviews tab. Only the
// body is shared: each caller keeps its own panel element, its own anchoring
// (the PR panel re-anchors past an open commits panel) and its own buttons,
// because those differ and quietly regressing the ordering is the trap here.
// All verdict strings are MODEL OUTPUT → text nodes only, never innerHTML.
// Verdict timestamps are stored as full ISO instants in UTC (microseconds and
// a +00:00 offset); a reader wants the minute it happened, so both the history
// rows and the panels render them through here. The zone marker is kept — the
// truncation is the only thing that would have dropped it, and a bare
// wall-clock time silently reads as local.
function devStamp(iso) {
    const s = String(iso || "");
    return s ? s.slice(0, 16).replace("T", " ") + " UTC" : "";
}

function devVerdictBody(v, extraMeta) {
    const nodes = [el("div", { class: "dev-pr-meta" }, [
        `reviewed ${devStamp(v.reviewed_at)}`
        + (v.model ? ` (model: ${v.model})` : "") + (extraMeta || ""),
    ])];
    if (v.summary)
        nodes.push(el("div", { class: "dev-verdict-summary" }, [v.summary]));
    for (const f of (Array.isArray(v.findings) ? v.findings : [])) {
        if (!f || typeof f !== "object") continue;
        nodes.push(el("div", { class: "dev-verdict-finding" },
            ["• " + (f.file ? f.file + ": " : "") + (f.note || "")]));
    }
    return nodes;
}

// Render ONE PR row's review cell — shared by buildDevRepoCard's initial
// render and devResolvePrReviewCell's in-place completion resolve (the PI
// revision of the never-auto-re-render decision: the PAGE is still never
// re-rendered under the operator, but the CELL resolves in place). ctx =
// {view, body, rerender, onLogin}, the devRefreshCommitCells shape. The
// verdict panel is LAZY: built on the first badge click and inserted after
// the row's commits panel when one follows (preserving the shipped
// row → commits-panel → verdict-panel order), toggled thereafter via the
// local ref, and stamped data-rkey-panel so a cell re-render can clear THIS
// row's stale panel. The removal is scoped to the row's own sibling run,
// never a document-wide query — the Development page and a per-project pane
// showing the same repo carry colliding keys, and one surface must never
// reach into the other's DOM; the initial build path (cell not yet in a row,
// closest → null) needs no removal at all, the card wipe detaches old panels
// wholesale.
function devRenderPrReviewCell(cellEl, repo, p, v, ctx) {
    const rkey = devReviewKey(repo, "#" + String(p.number));
    cellEl.innerHTML = "";
    const row = cellEl.closest(".dev-pr-row");
    if (row) {
        let sib = row.nextElementSibling;
        while (sib && !sib.classList.contains("dev-pr-row")) {
            const next = sib.nextElementSibling;
            if (sib.getAttribute("data-rkey-panel") === rkey) sib.remove();
            sib = next;
        }
    }
    const reviewBtn = (label) => {
        const btn = el("button", { class: "btn-small dev-review-btn" },
                       [label]);
        btn.onclick = () => {
            btn.disabled = true;
            devReviewDialog(ctx.view, ctx.body, repo, { pr: p.number },
                            ctx.rerender, ctx.onLogin);
        };
        return btn;
    };
    const flight = devReviewsInFlight.get(rkey);
    if (flight) {
        cellEl.appendChild(el("span",
            { class: "dev-pr-meta dev-reviewing" }, ["reviewing…"]));
        if (!flight.watching)   // stalled: restart with THIS render's context
            devWatchReview(rkey, flight.opId,
                           () => devResolvePrReviewCell(rkey, repo, p.number,
                                                        ctx));
        return;
    }
    if (!v) { cellEl.appendChild(reviewBtn("Review")); return; }
    if (v.status === "ok") {
        const stale = !!(v.head_sha && p.sha && v.head_sha !== p.sha);
        const badge = el("button", {
            // Color keys on the STRICT outcome field only — never on the
            // free-text risk label, which the prompt owns.
            class: "btn-small dev-review-badge"
                + (v.outcome === "pass" ? " dev-outcome-pass"
                   : v.outcome === "fail" ? " dev-outcome-fail" : ""),
            title: "show the review verdict",
        }, ["reviewed ✓" + (v.risk ? ` · ${v.risk}` : "")
            + (stale ? " · stale" : "")]);
        let panel = null;
        badge.onclick = () => {
            if (panel) {
                panel.style.display =
                    panel.style.display === "none" ? "" : "none";
                return;
            }
            const prow = cellEl.closest(".dev-pr-row");
            if (!prow) return;   // detached leftover — nothing to anchor on
            panel = el("div", { class: "dev-verdict-panel" });
            panel.setAttribute("data-rkey-panel", rkey);
            for (const n of devVerdictBody(v, stale
                    ? " — the PR has new commits since this review" : ""))
                panel.appendChild(n);
            panel.appendChild(reviewBtn("Re-review"));
            // D1 anchor: keep the shipped row → commits-panel → verdict-panel
            // order when the commits expander sits after this row.
            let anchor = prow;
            if (anchor.nextElementSibling && anchor.nextElementSibling
                    .classList.contains("dev-commits-panel"))
                anchor = anchor.nextElementSibling;
            anchor.after(panel);
        };
        cellEl.appendChild(badge);
        return;
    }
    cellEl.appendChild(el("span",
        { class: "dev-pr-meta dev-review-failed" },
        ["review failed" + (v.reason ? `: ${v.reason}` : "")]));
    cellEl.appendChild(reviewBtn("Retry"));
}

// The PR twin of devRefreshCommitCells: on a review's terminal, fetch the
// repo's status and repaint the PR row's cell IN PLACE — fresh verdict, fresh
// head sha (the stale calc). Every degraded arm (transport failure,
// unparseable body, missing row/PR — incl. the B37 {repo,error} row, a
// stopped gitea's empty row, and a since-closed PR — and 401/403) falls back
// to the shipped "done — refresh" button: a BACKGROUND resolve must never
// yank the page to a login card or re-render it under the operator (the
// devWatchReview stall posture).
async function devResolvePrReviewCell(key, repo, prNumber, ctx) {
    let p = null, v = null;
    try {
        const res = await fetch(
            `/broker/dev/repo-status?repo=${encodeURIComponent(repo)}`);
        if (res.ok) {
            const data = await res.json();
            const row = (data.ok && data.result && data.result.repo
                         && typeof data.result.repo === "object")
                ? data.result.repo : null;
            if (row && Array.isArray(row.prs)) {
                p = row.prs.find((x) => x && x.number === prNumber) || null;
                if (row.reviews && typeof row.reviews === "object")
                    v = row.reviews[String(prNumber)] || null;
            }
        }
    } catch (e) { /* degraded arm below */ }
    devUpdateReviewCells(key, (cellEl) => {
        if (p && v) return devRenderPrReviewCell(cellEl, repo, p, v, ctx);
        cellEl.innerHTML = "";
        const btn = el("button", { class: "btn-small dev-review-btn" },
                       ["done — refresh"]);
        btn.onclick = () => ctx.rerender();
        cellEl.appendChild(btn);
    });
}

// Background completion watcher for one detached review op — the
// mgmtTailBuildLog completion contract minus the DOM tail: drain the view log
// for a terminal record, with the alive probe as the hard-killed-child escape
// (run_review writes the terminal BEFORE releasing its lock, so alive:false
// trails an already-written terminal on every in-process path; absent one,
// the child was hard-killed and the ledger read simply shows whatever
// landed). Cadence: OP_POLL_INTERVAL_MS — each round is two byte-cursor reads
// + a lock-file stat server-side, trivial at any plausible fan-out, and
// reviews run minutes, so a bespoke slower cadence would only delay the badge
// while adding a constant.
// AUTH POSTURE: a 401/403 STOPS the watcher (watching=false, stalled=true)
// and KEEPS the registry entry — the rows keep reading "reviewing…", and the
// first registry-aware render after re-login restarts the watcher (cursor
// from 0 is safe: this drain only scans for terminal records). Transient
// failures (transport, 503, non-JSON) keep polling, like the modal loop.
// The double-start guard is `watching`, set SYNCHRONOUSLY before the first
// await — two renders restarting the same stalled entry cannot both pass it.
async function devWatchReview(key, opId, onTerminal) {
    const entry = devReviewsInFlight.get(key);
    if (!entry || entry.watching) return;
    entry.watching = true;
    entry.stalled = false;
    let viewFrom = 0, sawTerminal = false;
    const stall = () => { entry.watching = false; entry.stalled = true; };
    while (true) {
        try {
            const r = await fetch(
                `/broker/op/${encodeURIComponent(opId)}/log?from=${viewFrom}`);
            if (r.status === 401 || r.status === 403) return stall();
            const b = await r.json();
            if (b.started !== false && b.data) {
                viewFrom = b.next;
                for (const line of b.data.split("\n")) {
                    if (!line.trim()) continue;
                    let rec;
                    try { rec = JSON.parse(line); } catch (e) { continue; }
                    if (rec.status === "done" || rec.status === "failed")
                        sawTerminal = true;
                }
            }
        } catch (e) { /* transient — keep polling */ }
        if (sawTerminal) break;
        let alive = null;
        try {
            const r = await fetch(
                `/broker/op/${encodeURIComponent(opId)}/alive`);
            if (r.status === 401 || r.status === 403) return stall();
            if (r.ok) {
                const b = await r.json();
                alive = b.ok ? !!b.alive : null;
            }
        } catch (e) { /* transient */ }
        if (alive === false) break;   // hard-killed, or the terminal just landed
        await opSleep(OP_POLL_INTERVAL_MS);
    }
    devReviewsInFlight.delete(key);
    try { onTerminal(); } catch (e) { /* cell refresh is best-effort */ }
}

// Render ONE commit's review cell from a ledger entry (v; null = never
// reviewed) into its stamped cell. Module-level — the expander, the dialog's
// dismissal path and the watcher all render through this, so a cell they
// don't own still gets the full shapes. The ok-verdict panel is the row's
// IMMEDIATE next sibling (the render adjacency), replaced wholesale per fill.
// All verdict strings are MODEL OUTPUT → text nodes only. ctx threads the
// locator + landing closures for the buttons ({view, body, sel, rerender,
// onLogin}); ANY of the repo's locators is valid for the terminal refresh —
// the ledger map read is repo-wide, locator-independent.
function devRenderCommitReviewCell(cell, repo, sha, v, ctx) {
    const key = devReviewKey(repo, sha);
    const row = cell.closest(".dev-commit-row");
    const sib = row ? row.nextElementSibling : null;
    if (sib && sib.classList.contains("dev-verdict-panel")) sib.remove();
    cell.innerHTML = "";
    const flight = devReviewsInFlight.get(key);
    if (flight) {
        cell.appendChild(el("span", { class: "dev-pr-meta dev-reviewing" },
                            ["reviewing…"]));
        if (!flight.watching)   // stalled (post-relogin render): restart with
            devWatchReview(key, flight.opId,   // THIS render's context
                           () => devRefreshCommitCells(repo, sha, ctx));
        return;
    }
    const reviewBtn = (label) => {
        const rbtn = el("button", { class: "btn-small dev-review-btn" },
                        [label]);
        rbtn.onclick = () => {
            rbtn.disabled = true;
            devReviewDialog(ctx.view, ctx.body, repo,
                            { commit: sha, pr: ctx.sel.pr,
                              branch: ctx.sel.branch },
                            ctx.rerender, ctx.onLogin);
        };
        return rbtn;
    };
    if (!v) {
        cell.appendChild(reviewBtn("Review"));
    } else if (v.status === "ok") {
        const badge = el("button", {
            // Same outcome-only color keying as the PR badge above.
            class: "btn-small dev-review-badge"
                + (v.outcome === "pass" ? " dev-outcome-pass"
                   : v.outcome === "fail" ? " dev-outcome-fail" : ""),
            title: "show the review verdict",
        }, ["reviewed ✓" + (v.risk ? ` · ${v.risk}` : "")]);
        const vpanel = el("div", { class: "dev-verdict-panel" });
        vpanel.style.display = "none";
        for (const n of devVerdictBody(v, "")) vpanel.appendChild(n);
        vpanel.appendChild(reviewBtn("Re-review"));
        badge.onclick = () => {
            vpanel.style.display =
                vpanel.style.display === "none" ? "" : "none";
        };
        cell.appendChild(badge);
        if (row) row.after(vpanel);
    } else {
        cell.appendChild(el("span", { class: "dev-pr-meta dev-review-failed" },
            ["review failed" + (v.reason ? `: ${v.reason}` : "")]));
        cell.appendChild(reviewBtn("Retry"));
    }
}

// Terminal refresh for a commit review: ONE commits-page read purely for its
// repo-wide sha-keyed ledger map (dev_commits returns the FULL map with any
// page), then re-render every stamped cell. On a failed read: a small
// "refresh" retry button — never leave "reviewing…" behind once the registry
// entry is gone. Read-ok-but-absent (a hard-killed child that never wrote a
// ledger entry) renders the plain Review button — honest: no verdict exists.
async function devRefreshCommitCells(repo, sha, ctx) {
    const key = devReviewKey(repo, sha);
    let v = null, got = false;
    const locator = ctx.sel.pr != null
        ? `pr=${encodeURIComponent(ctx.sel.pr)}`
        : `branch=${encodeURIComponent(ctx.sel.branch)}`;
    try {
        const res = await fetch(
            `/broker/dev/commits?repo=${encodeURIComponent(repo)}`
            + `&${locator}&page=1`);
        const data = await res.json();
        if (res.ok && data.ok && data.result
                && data.result.reviews
                && typeof data.result.reviews === "object") {
            v = data.result.reviews[sha] || null;
            got = true;
        }
    } catch (e) { /* fall through to the retry button */ }
    devUpdateReviewCells(key, (cellEl) => {
        if (got) return devRenderCommitReviewCell(cellEl, repo, sha, v, ctx);
        cellEl.innerHTML = "";
        const retry = el("button", { class: "btn-small" }, ["refresh"]);
        retry.onclick = () => devRefreshCommitCells(repo, sha, ctx);
        cellEl.appendChild(retry);
    });
}

// Lazy per-row commit dropdown (the unified commit page): ▸ expands one PAGE
// of a PR's or a branch's commits, NEWEST-FIRST — newest at the top, oldest at
// the bottom, so the rs-fetch WALK runs BOTTOM-UP — each with a copy button for
// `rs-fetch <repo> <locator> --commit <sha>` and per-commit review affordances
// (Review / reviewed-badge / Retry, fed by the dev_commits result's sha-keyed
// ledger entries). view/body are threaded in for devReviewDialog
// (status-redirect routing + the "done — refresh" fallback's re-render);
// `rerender`/`onLogin` (optional) ride through to it — the clicked-refresh
// landing and the view-rooted 401 landing respectively (absent ⇒ the
// Fetch-tab default / the Management landing; see devReviewDialog for why
// they differ).
//
// The broker read stays click-triggered by design — the page read must never
// fan out per-row — but it is now ONE PAGE PER EXPLICIT CLICK, not once per
// expander: the first expand loads page 1 and "Show older commits" appends the
// next page below. Four PERSISTENT regions, created once and never rebuilt:
// a bulk-select toolbar (Select / Review all / Copy all — revealed with the
// first rows; the checkbox column is CSS-toggled, never a row rebuild), rows
// (append-only — a failed load must never destroy loaded rows), a status
// foot (the button / message, cleared per load), and the walk note (shown once
// when the first rows land, so it cannot stack per page).
//
// Commit subjects are AGENT OUTPUT → text nodes only (the verdict-string
// rule). THREE failure texts, never collapsed: transport
// (unreachable/503/unparseable), verb refusal (200 + ok:false), and gitea-down
// (200 + ok:true + running:false — an empty list from a stopped gitea is
// otherwise indistinguishable from the end of the history). All three are
// RECOVERABLE: they leave the rows alone and offer a Retry for the same page.
function devCommitsExpander(view, body, repo, sel, rerender, onLogin,
                            autoCommit) {
    const btn = el("button", {
        class: "btn-small dev-commits-btn",
        title: "show commits (fetchable one by one)",
    }, ["▸"]);
    const panel = el("div", { class: "dev-commits-panel" });
    panel.style.display = "none";
    const rowsEl = el("div", { class: "dev-commits-rows" });
    const footEl = el("div", { class: "dev-commits-foot" });
    const noteEl = el("div", { class: "dev-pr-meta dev-commits-note" }, [
        "Newest first — the oldest commit is at the bottom. Fetching bottom-up "
        + "stages one commit (+ its message) per step; skipping ahead stages "
        + "everything below it as one squash with the messages concatenated.",
    ]);
    noteEl.style.display = "none";
    // The mirror-boundary note, shown only when the mirror HAS this branch but
    // its head is not among the loaded rows. A fifth PERSISTENT region, not a
    // foot message: foot() wipes itself on every load (including every failure
    // arm), and not a row either — rows are never rebuilt or reordered (the
    // one insertion above an existing row is the mirror-head divider, when a
    // later page's probe names a row already on screen).
    const divergeEl = el("div", { class: "dev-pr-meta dev-commits-diverge" });
    divergeEl.style.display = "none";
    const toolsEl = el("div", { class: "dev-commits-tools" });
    toolsEl.style.display = "none";
    panel.appendChild(toolsEl);
    panel.appendChild(rowsEl);
    panel.appendChild(divergeEl);
    panel.appendChild(footEl);
    panel.appendChild(noteEl);

    // `page` is the highest page RENDERED (0 = nothing yet) and doubles as the
    // first-load guard: a failed page-N>1 load must not make a re-expand
    // re-fetch page 1 and duplicate the rows already on screen. Recovery from
    // a failed page comes from the Retry button instead.
    let page = 0;
    let open = false;
    let inFlight = false;
    let selecting = false;
    // The mirror's head sha for THIS branch ("" = no counterpart upstream, a
    // PR locator, or an unverifiable probe — the server collapses all three,
    // deliberately: a probe failure must never be renderable as a claim), and
    // whether we have drawn its divider on any loaded page. The divider is
    // drawn either when the head's row arrives in a new page, or — when a
    // page's probe failed and a later one succeeds — over the row already on
    // screen (placeDividerOnLoaded), so one failed read cannot leave a false
    // "doesn't contain the mirror's head" at the end of the walk. headSeen is
    // never reset: if the mirror advances mid-session the divider stays where
    // it is, which under-reports what has landed — conservative, and it costs
    // at most an over-fetch that rs-fetch's patch-id walk skips. It clears
    // when the card re-renders and builds a fresh expander, not on a
    // re-expand (which does not reload).
    let mirrorHead = "";
    let headSeen = false;
    // How many rows sat ABOVE the divider when it was drawn — the set the
    // Select-ahead button ticks, captured at DRAW time and never recomputed
    // from the head sha: mirrorHead is re-read on every page and the divider
    // is never redrawn, so a click-time lookup could disagree with the line
    // on screen (or find nothing) after the mirror advances mid-session.
    // Complete by construction: rows load newest-first, so every row above
    // the head's is already rendered when the head's row lands — and when the
    // line is placed over a row already on screen, the rows above it are
    // exactly the ones before it in loadedOrder.
    let aheadCount = 0;
    // The history was walked to its end (an empty page, or a last page) —
    // distinguishes "head not loaded yet" from "head not in this branch".
    let ended = false;
    const selected = new Set();          // full shas ticked in select mode
    const loadedOrder = [];              // display order (newest-first)
    const checks = new Map();            // full sha → its row's checkbox
    const rowEls = new Map();            // full sha → its row element
    // Context for the module-level review-cell renderer + watcher restarts:
    // the buttons and the terminal refresh need the locator and the landing
    // closures this expander was threaded.
    const ctx = { view, body, sel, rerender, onLogin };

    // The card's auto-commit tick rides every copied command; consulted at
    // CALL time (per-row copies pass a provider; Copy all builds at click).
    const cmdFor = (sha) => (sel.pr != null
        ? `rs-fetch ${repo} --pr ${sel.pr} --commit ${sha}`
        : `rs-fetch ${repo} --branch ${sel.branch} --commit ${sha}`)
        + (autoCommit && autoCommit() ? " --auto-commit" : "");

    const foot = (nodes) => {
        footEl.innerHTML = "";
        for (const n of nodes) footEl.appendChild(n);
        // An empty foot is display:none so the panel's inter-region gap does
        // not leave a stray band under the last row.
        footEl.style.display = nodes.length ? "" : "none";
    };
    const meta = (text) => el("div", { class: "dev-pr-meta" }, [text]);
    // Every failure arm lands here: rows are NEVER touched (a transport blip on
    // the page-4 click must not erase pages 1-3), and the same page stays
    // retryable — gitea coming back up, or the broker restarting, is an
    // ordinary transient, not a terminal state for this panel.
    const fail = (text, next) => {
        const retry = el("button", { class: "btn-small" }, ["Retry"]);
        retry.onclick = () => load(next);
        foot([meta(text), retry]);
    };
    const moreBtn = (next) => {
        // No count in the label: the page size lives in gitea.COMMITS_PAGE_SIZE
        // and a number here would be a silent lockstep to drift.
        const b = el("button", { class: "btn-small dev-commits-more" },
                     ["Show older commits"]);
        b.onclick = () => load(next);
        return b;
    };

    // Bulk-select toolbar: Select reveals the checkbox column (class toggle
    // only — the append-only rows invariant holds), Review all fires one
    // detached review per selected commit (no modal — the rows resolve via
    // the background watcher), Copy all puts one rs-fetch command per
    // selected commit on the clipboard OLDEST-FIRST (the walk order; display
    // order is newest-first, so the copy list is its reverse). Selection
    // spans loaded pages and reviews exactly what is ticked — re-reviewing an
    // already-✓ commit is deliberate (the badge is visible at the checkbox).
    const selectBtn = el("button", { class: "btn-small dev-select-btn" },
                         ["Select"]);
    // Select ahead: tick every row above the mirror-head divider — the
    // commits the human's repo lacks — in one click. Always shown beside
    // Select; ENABLED only once the divider has been drawn among the loaded
    // rows with at least one row above it. Disabled, the title says which
    // unknown state applies. Additive (the tick-all convention): ticks
    // already present, above or below the line, stay the operator's.
    const aheadBtn = el("button", { class: "btn-small dev-ahead-btn" },
                        ["Select ahead"]);
    const reviewAllBtn = el("button", { class: "btn-small" }, ["Review all"]);
    const copyAllBtn = el("button", { class: "btn-small" }, ["Copy all"]);
    toolsEl.appendChild(selectBtn);
    toolsEl.appendChild(aheadBtn);
    toolsEl.appendChild(reviewAllBtn);
    toolsEl.appendChild(copyAllBtn);
    // The divider's own caveat, carried onto the button: the boundary assumes
    // a linear branch history, and on a merged one it UNDER-reports — a commit
    // below the line may not be in the mirror yet, so the button would miss
    // it. (Over-selecting, the other direction, only arises from a mirror that
    // advanced mid-session; it is harmless — rs-fetch skips what the repo has.)
    const AHEAD_LINEAR = " Assumes a linear branch history: on a merged one a "
        + "commit below the line may not be in the mirror yet — check the "
        + "rows below.";
    // Why the button is blocked, from the same state the boundary note reads.
    // The DRAWN divider is tested first, like updateDiverge does: the head
    // string is re-read per page and a later page's failed probe empties it,
    // and an enabled button must never carry the "isn't known" title over a
    // line that is on screen. The empty-head arm must NOT claim "no
    // counterpart": the server reports one "" for a fork-only branch, a PR
    // locator AND a failed probe, so the title lists the three readings
    // rather than asserting one.
    const aheadTitle = () => {
        if (headSeen) {
            if (!aheadCount) {
                return "Nothing ahead of the mirror — the mirror's head is "
                    + "the newest commit here.";
            }
            return `Tick the ${aheadCount} commit${aheadCount === 1 ? "" : "s"} `
                + "above the mirror head — everything your repo lacks."
                + AHEAD_LINEAR;
        }
        if (!mirrorHead) {
            return "The mirror's head for this branch isn't known — a PR, a "
                + "branch the mirror doesn't have, or a head that couldn't be "
                + "read — so there is no boundary to select up to.";
        }
        return ended
            ? "This branch doesn't contain the mirror's head, so there is "
              + "no boundary to select up to."
            : "The mirror's head isn't among the commits loaded so far — "
              + "show older commits until it appears.";
    };
    const updateTools = () => {
        selectBtn.classList.toggle("active", selecting);
        const n = selected.size;
        reviewAllBtn.textContent = `Review all (${n})`;
        copyAllBtn.textContent = `Copy all (${n})`;
        reviewAllBtn.style.display = selecting ? "" : "none";
        copyAllBtn.style.display = selecting ? "" : "none";
        reviewAllBtn.disabled = !n;
        copyAllBtn.disabled = !n;
        const ahead = headSeen && aheadCount > 0;
        aheadBtn.disabled = !ahead;
        aheadBtn.textContent = ahead ? `Select ahead (${aheadCount})`
                                     : "Select ahead";
        aheadBtn.title = aheadTitle();
    };
    selectBtn.onclick = () => {
        selecting = !selecting;
        panel.classList.toggle("selecting", selecting);
        updateTools();
    };
    aheadBtn.onclick = () => {
        if (!(headSeen && aheadCount > 0)) return;
        if (!selecting) {
            selecting = true;
            panel.classList.add("selecting");
        }
        // A programmatic `checked` fires no change event, so the selection
        // set is updated here alongside the box.
        for (const sha of loadedOrder.slice(0, aheadCount)) {
            selected.add(sha);
            const check = checks.get(sha);
            if (check) check.checked = true;
        }
        updateTools();
    };
    const selectedOldestFirst = () =>
        loadedOrder.slice().reverse().filter((s) => selected.has(s));
    copyAllBtn.onclick = async () => {
        const shas = selectedOldestFirst();
        if (!shas.length) return;
        const text = shas.map(cmdFor).join("\n") + "\n";
        try { await navigator.clipboard.writeText(text); } catch (e) { return; }
        copyAllBtn.textContent = `✓ ${shas.length} copied`;
        setTimeout(updateTools, 1200);
    };
    // One POST per selected commit, awaited sequentially (each returns in ms
    // — the broker only spawns the detached child; the review lane runs them
    // in parallel, N-at-a-time by design, no client cap). 401 routes through
    // the threaded onLogin landing; 403/503/transport stop the loop with a
    // foot message (the transport-vs-verb split); a per-commit verb refusal
    // marks THAT cell failed (view-side; a re-render restores ledger truth)
    // and keeps going.
    reviewAllBtn.onclick = async () => {
        const shas = selectedOldestFirst();
        if (!shas.length) return;
        reviewAllBtn.disabled = true;
        for (const sha of shas) {
            const key = devReviewKey(repo, sha);
            if (devReviewsInFlight.has(key)) continue;   // already running
            let res;
            try {
                res = await fetch("/broker/dev/review", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ repo: repo, commit: sha }),
                });
            } catch (e) {
                foot([meta("The broker isn't reachable — is it running?")]);
                break;
            }
            if (res.status === 401) {
                const redirect = mgmtStatusRedirect(view, res.status, onLogin);
                if (redirect) return redirect();
            }
            if (res.status === 403 || res.status === 503) {
                foot([meta(res.status === 403
                    ? "Origin rejected — reload the page."
                    : "The broker isn't reachable — is it running?")]);
                break;
            }
            let b; try { b = await res.json(); } catch (e) { b = {}; }
            if (!b.ok || !b.op_id) {
                devUpdateReviewCells(key, (cellEl) =>
                    devRenderCommitReviewCell(cellEl, repo, sha,
                        { status: "failed", reason: mgmtErrText(b) }, ctx));
                continue;
            }
            devReviewsInFlight.set(key, { opId: b.op_id, watching: false,
                                          stalled: false });
            devMarkReviewingCells(key);
            devWatchReview(key, b.op_id,
                           () => devRefreshCommitCells(repo, sha, ctx));
        }
        updateTools();
    };
    updateTools();

    // The mirror-boundary note. Called ONLY after a successful render, so a
    // failed page leaves the last honest state on screen. Gated on a non-empty
    // mirrorHead — i.e. on having positively confirmed the mirror carries this
    // branch — so a probe failure (which the server reports as "") can only
    // ever produce silence here, never a claim about the fork's state.
    // `ended` distinguishes "not among what is loaded so far" (more pages
    // remain) from "not in this branch's history at all" (we walked off the
    // end); only the second is a statement about the branch.
    const updateDiverge = (ended) => {
        if (!mirrorHead || headSeen) {
            divergeEl.style.display = "none";
            return;
        }
        divergeEl.textContent = ended
            ? "This branch doesn't contain the mirror's head."
            : "The mirror's head isn't among the commits loaded so far.";
        divergeEl.style.display = "";
    };

    // The boundary marker. One builder for both places it is drawn, so the
    // two cannot drift.
    const mirrorDivider = () => el("div", { class: "dev-commit-divider" }, [
        el("span", { class: "dev-pr-meta" }, ["Mirror head"]),
    ]);
    // A page's probe can name a row that an EARLIER page already put on
    // screen: page 1's probe failed (the server reports "" for that), and a
    // later page's succeeded. renderRows only compares the new page's rows,
    // so without this the walk would end claiming the branch lacks a head
    // that is right there. Called on every page right after the head is
    // re-read; a no-op on page 1 (nothing loaded) and once a line is drawn
    // (drawn ONCE — the flap and the advanced-mirror cases keep the first
    // line). The Select-ahead set is still captured here, at draw time: the
    // rows above the line are the ones before the head in display order.
    const placeDividerOnLoaded = () => {
        if (!mirrorHead || headSeen) return;
        const at = loadedOrder.indexOf(mirrorHead);
        if (at < 0) return;
        headSeen = true;
        aheadCount = at;
        rowsEl.insertBefore(mirrorDivider(), rowEls.get(mirrorHead));
    };

    // Append one page's rows. Per-commit review verdicts: ledger entries keyed
    // by FULL sha. All verdict strings are MODEL OUTPUT → text nodes only. No
    // stale marker — a commit is immutable. The review UI lives in a stamped
    // cell (data-rkey) rendered by the module-level helper, so the background
    // watcher and the dialog's dismissal path can re-render it without owning
    // this closure; the checkbox column is CSS-revealed by select mode (rows
    // are never rebuilt).
    const renderRows = (commits, reviews) => {
        for (const c of commits) {
            if (!c || typeof c !== "object" || !c.sha) continue;
            // The boundary marker, laid down ABOVE the commit it names: that
            // commit and everything below it are already in the mirror, so
            // everything above the line is what the human's repo lacks. AFTER
            // the malformed-row guard (a null c must not reach .sha) and
            // before the row it precedes. FULL-sha compare — the row only
            // DISPLAYS the first 9 characters.
            // Drawn ONCE (!headSeen): the head sha is re-read per page, so a
            // mirror that advanced between clicks must not draw a second line
            // — the first one stays, which is the conservative posture above.
            // A head that names a row from an EARLIER page is placed by
            // placeDividerOnLoaded before this page renders.
            if (mirrorHead && !headSeen && c.sha === mirrorHead) {
                headSeen = true;
                aheadCount = loadedOrder.length;
                rowsEl.appendChild(mirrorDivider());
            }
            loadedOrder.push(c.sha);
            const check = el("input", { type: "checkbox",
                                        class: "dev-commit-check" });
            checks.set(c.sha, check);
            check.onchange = () => {
                if (check.checked) selected.add(c.sha);
                else selected.delete(c.sha);
                updateTools();
            };
            const cell = el("span", { class: "dev-review-cell" });
            cell.setAttribute("data-rkey", devReviewKey(repo, c.sha));
            const row = el("div", { class: "dev-commit-row" }, [
                check,
                devCopyBtn(() => cmdFor(c.sha)),
                el("span", { class: "dev-pr-id" }, [c.sha.slice(0, 9)]),
                el("span", { class: "dev-pr-title" }, [c.subject || ""]),
                el("span", { class: "dev-pr-meta" }, [c.date || ""]),
                cell,
            ]);
            rowEls.set(c.sha, row);
            rowsEl.appendChild(row);
            devRenderCommitReviewCell(cell, repo, c.sha,
                                      reviews[c.sha] || null, ctx);
        }
    };

    async function load(next) {
        if (inFlight) return;
        inFlight = true;
        foot([meta("Loading commits…")]);
        const locator = sel.pr != null
            ? `pr=${encodeURIComponent(sel.pr)}`
            : `branch=${encodeURIComponent(sel.branch)}`;
        let res, data;
        try {
            res = await fetch(`/broker/dev/commits?repo=${encodeURIComponent(repo)}`
                              + `&${locator}&page=${encodeURIComponent(next)}`);
            data = await res.json();
        } catch (e) {
            inFlight = false;
            fail("The broker isn't reachable — is it running?", next);
            return;
        }
        inFlight = false;
        if (res.status === 503) {
            fail("The broker isn't reachable — is it running?", next);
            return;
        }
        if (!res.ok || !data.ok || !data.result) {
            // The broker ANSWERED and the verb refused — show the real reason.
            fail(mgmtErrText(data) || "Could not load commits.", next);
            return;
        }
        // Only past that guard is data.result guaranteed non-null — reading
        // `running` above it would throw on exactly the verb-refusal path.
        if (data.result.running === false) {
            fail("Gitea isn't running — start it from Sandboxes → "
                 + "Infrastructure, then retry.", next);
            return;
        }
        // The cursor echo is a GUARD, not decoration: a broker running older
        // code filters the unknown `page` field out silently (no error at any
        // layer) and serves page 1 for every click — appending that would
        // duplicate rows already on screen, with matching shas and no way to
        // tell. Refuse to append anything we did not ask for.
        if (data.result.page !== next) {
            fail("The broker served a different page than requested — it looks "
                 + "out of date. Nothing was added.", next);
            return;
        }
        const commits = Array.isArray(data.result.commits)
            ? data.result.commits : [];
        const reviews = (data.result.reviews
                         && typeof data.result.reviews === "object")
            ? data.result.reviews : {};
        // Past the cursor guard, so a refused page cannot change the marker,
        // and above the empty-page return, so both updateDiverge call sites
        // read the same fresh value — and see a line placed over a row an
        // earlier page loaded.
        mirrorHead = typeof data.result.mirror_head === "string"
            ? data.result.mirror_head : "";
        placeDividerOnLoaded();
        if (!commits.length) {
            // has_more is "a FULL page came back", so a history that is an
            // exact multiple of the page size offers one button that fetches
            // an empty page. That is ordinary, not an error — and on page 1 it
            // means the fork genuinely has no commits.
            foot([meta(next === 1 ? "No commits." : "No older commits.")]);
            // An empty page past the first IS the end of the history — the
            // other half of the has_more split below. On page 1 nothing is
            // loaded, so there is nothing to say about a boundary.
            if (next > 1) {
                ended = true;
                updateDiverge(true);
                updateTools();
            }
            return;
        }
        renderRows(commits, reviews);
        page = next;
        noteEl.style.display = "";
        toolsEl.style.display = "";
        ended = !data.result.has_more;
        updateDiverge(ended);
        // The Select-ahead gate changes with every page (the head may have
        // just been paged in, or the walk may have ended without it).
        updateTools();
        foot(data.result.has_more ? [moreBtn(next + 1)] : []);
    }

    btn.onclick = () => {
        open = !open;
        btn.textContent = open ? "▾" : "▸";
        panel.style.display = open ? "" : "none";
        if (open && page === 0) load(1);
    };
    return { btn, panel };
}

// One review on the broker's parallel detached lane (STAGE_DEV_GITEA S4):
// POST /broker/dev/review → {op_id} → stream the op via the build-tail modal
// (build_alive covers review ops). `sel` is the locator: {commit} reviews a
// single commit (full sha; sel may ALSO carry the pr/branch it was listed
// under — threaded so a dismissal's watcher can read the ledger map back
// through /broker/dev/commits), {pr} alone reviews the whole PR. The POST
// body carries exactly ONE locator (commit wins — it is only ever set for a
// commit review). Advisory — the verdict lands in the host ledger.
// The box is DISMISSABLE: a backdrop click closes it while the review keeps
// running detached — the row flips to "reviewing…" and resolves IN PLACE via
// the background watcher (commit cells via devRefreshCommitCells, PR cells
// via devResolvePrReviewCell; "done — refresh" survives only as the degraded
// fallback). With the box left OPEN, opts.onTerminal resolves the cells the
// moment the terminal lands. A review already running for this subject no-ops up
// front: the modal guard only stops concurrent DIALOGS, not a duplicate of a
// detached review a stale row still offers.
// `rerender` (optional) is the landing for the click-driven "done — refresh"
// fallback and for dialogs opened from repainted cells (fires with view/body
// intact): the Development page passes its Fetch-tab re-render, the
// per-project Fetch pane passes itself. Done itself never re-renders the page
// — that would collapse every open commit list and drop its paging — it only
// closes the box, resolving the cells first when a hard-killed op left them
// unresolved. `onLogin` (optional) is the 401 POST-LOGIN landing and
// must be VIEW-rooted — renderMgmtLogin wipes `view` (mgmtCard), detaching
// any body-scoped target — so the scoped pane (where view === the pane
// container) passes its own renderer and Development passes NOTHING (the
// pre-slice Management landing). The two are deliberately distinct values.
async function devReviewDialog(view, body, repo, sel, rerender, onLogin) {
    const loc = sel.commit ? sel.commit : "#" + String(sel.pr);
    const key = devReviewKey(repo, loc);
    if (devReviewsInFlight.has(key)) return;
    if (document.querySelector(".modal-backdrop")) return;
    const onDone = rerender || (() => renderDevFetchTab(view, body));
    const title = sel.commit
        ? `Review ${repo} @${sel.commit.slice(0, 9)}`
        : `Review ${repo} #${sel.pr}`;
    const backdrop = el("div", { class: "modal-backdrop" });
    const card = el("div", { class: "card sw-build-card" }, [
        el("h2", {}, [title]),
        el("div", { class: "mgmt-loading" }, ["Starting the sandboxed reviewer…"]),
    ]);
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    let opId = null;
    let dismissed = false;
    let terminalSeen = false;
    const cctx = { view, body, sel, rerender: onDone, onLogin };
    // In-place completion resolve — fired by the OPEN modal's tail the moment
    // the terminal record lands (opts.onTerminal), and by the background
    // watcher on the dismissal path. Either way the page is never re-rendered
    // under the operator; only the stamped cells repaint.
    const resolveCells = () => {
        terminalSeen = true;
        if (sel.commit) devRefreshCommitCells(repo, sel.commit, cctx);
        else devResolvePrReviewCell(key, repo, sel.pr, cctx);
    };
    // Done's landing: close the box and leave the page alone — a full card
    // rebuild would collapse every open commit list and drop its paging.
    // After a real terminal the cells are already repainted; only a
    // hard-killed op (no terminal record) still needs its cells resolved.
    const closeLeavesPage = () => { if (!terminalSeen) resolveCells(); };
    // Hand the running op to the background watcher (the dismissal path).
    const registerDetached = () => {
        devReviewsInFlight.set(key, { opId: opId, watching: false,
                                      stalled: false });
        devMarkReviewingCells(key);
        if (sel.commit) {
            devWatchReview(key, opId, () => devRefreshCommitCells(
                repo, sel.commit, cctx));
        } else {
            devWatchReview(key, opId,
                           () => devResolvePrReviewCell(key, repo, sel.pr,
                                                        cctx));
        }
    };
    const dismiss = () => {
        if (dismissed) return;
        dismissed = true;
        backdrop.remove();
        // Pre-op dismissal (the POST is still pending): the continuation
        // below registers a successfully started op, or silently drops a
        // refusal — the row then still offers Review, the honest state.
        // Post-terminal dismissal: the cells already resolved via onTerminal —
        // re-registering would flash "reviewing…" and mint a watcher whose
        // registry entry lies until its first poll.
        if (opId && !terminalSeen) registerDetached();
    };
    backdrop.onclick = (e) => { if (e.target === backdrop) dismiss(); };
    let res;
    try {
        res = await fetch("/broker/dev/review", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                repo: repo,
                ...(sel.commit ? { commit: sel.commit } : { pr: sel.pr }),
            }),
        });
    } catch (e) {
        if (dismissed) return;
        backdrop.remove();
        return renderMgmtUnavailable(body);
    }
    if (dismissed) {
        // The operator already clicked away — never resurrect any UI; just
        // hand a successfully started op to the watcher.
        let b2; try { b2 = await res.json(); } catch (e) { b2 = {}; }
        if (b2.ok && b2.op_id) { opId = b2.op_id; registerDetached(); }
        return;
    }
    const redirect = mgmtStatusRedirect(view, res.status, onLogin);
    if (redirect) { backdrop.remove(); return redirect(); }
    let b; try { b = await res.json(); } catch (e) { b = {}; }
    if (!b.ok || !b.op_id) {
        card.innerHTML = "";
        card.appendChild(el("h2", {}, [title]));
        card.appendChild(el("div", { class: "error" },
                            ["Could not start the review: " + mgmtErrText(b)]));
        const close = el("button", { class: "btn btn-secondary" }, ["Close"]);
        close.onclick = () => backdrop.remove();
        card.appendChild(el("div", { class: "btn-row" }, [close]));
        return;
    }
    opId = b.op_id;
    await mgmtTailBuildLog(view, backdrop, card, b.op_id, title,
                           closeLeavesPage, onLogin,
                           { onDismiss: dismiss, onTerminal: resolveCells });
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

// The reviewer's token state on the Fetch surfaces — the sandboxed reviewer
// authenticates from a long-lived setup-token stored host-side as one-time
// setup (it never rotates; nothing credential-shaped ever returns from the
// reviewer container). Absent FIELD (an older broker) → null, render nothing
// (silent degradation); token present → a quiet hint line; token missing →
// a warning that reviews will fail, carrying the two-step bootstrap remedy —
// a SANCTIONED CLI mention (PI ruling: the token store is initial-setup
// bootstrap, the broker-passwd tier; the mint itself runs in any project
// terminal, which is already a browser surface).
function devReviewerLine(reviewer) {
    if (!reviewer || typeof reviewer !== "object") return null;
    if (reviewer.present) {
        return el("div", { class: "hint dev-reviewer-line" },
                  ["reviewer token set"
                   + (reviewer.set_at ? ` · ${reviewer.set_at}` : "")]);
    }
    return el("div", { class: "dev-reviewer-line dev-reviewer-warn" }, [
        "reviewer token not set — reviews will fail. One-time setup: run "
        + "claude setup-token in any project terminal (logged into the "
        + "dedicated reviewer account), then store it on the host with "
        + "research dev reviewer-token.",
    ]);
}

function renderDevFetchScreen(view, body, result) {
    body.innerHTML = "";
    const gitea = result.gitea || {};
    const repos = Array.isArray(result.repos) ? result.repos : [];
    const attachments = Array.isArray(result.attachments) ? result.attachments : [];
    // Before the not-enabled check: mid-upgrade the container is briefly
    // renamed aside, and "isn't enabled" would be a lie.
    if (devGiteaHeldNotice(body, gitea)) return;
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
            + "project from a GitHub URL (New Project → Dev), or add a dev box to "
            + "an existing project.",
        ]));
        return;
    }
    // Reviewer-token state, always within the repos-present path (the Review
    // affordances live on the cards below; the empty states have none).
    const revLine = devReviewerLine(result.reviewer);
    if (revLine) body.appendChild(revLine);
    // ⚠ The visibility filter runs HERE, strictly AFTER the !repos.length gate
    // above. Filtering first would tell an operator who has merely hidden every
    // repo that they have none and should go mirror one — false, and it points
    // them at the wrong page. All-hidden falls through to the hidden-count line
    // at the bottom instead.
    const visible = devFetchVisible(repos);
    const hidden = repos.length - visible.length;
    for (const r of visible) {
        const attached = attachments.filter((a) => a.repo === r.repo)
                                    .map((a) => a.project);
        body.appendChild(buildDevRepoCard(view, body, r, attached, {
            scoped: false,
            rerender: () => renderDevFetchTab(view, body),
            // Every mirror's fork list, so the purge dialog can name the OTHER
            // repos a retired identity owns a fork of (purging is user-scoped,
            // this view is repo-scoped). Free — already fetched. The scoped
            // pane passes none: it holds one repo, and it has no purge control.
            //
            // ⚠ UNFILTERED, deliberately — `repos`, never `visible`. This feeds
            // devForksOf, which tells the step-up-gated purge dialog what is
            // about to be irreversibly deleted; that list already under-reports
            // on degraded rows by construction, and a display preference must
            // never become a second way to under-report it. The filter governs
            // which cards render and nothing else.
            allRepos: repos,
        }));
    }
    if (hidden) {
        body.appendChild(devHiddenLine(hidden, "Repos", () => {
            devActiveTab = "repos";
            renderDevelopmentInto(view);
        }));
    }
    body.appendChild(devFetchLegend());
}

// The 📋/▸/auto-commit legend — ONE copy shared by the Development page and
// the project-wide (fetch-:all) pane so the two cannot drift; the per-repo
// pane omits it deliberately (one card needs no legend).
function devFetchLegend() {
    return el("div", { class: "hint" }, [
        "📋 copies the rs-fetch command — paste it in a fetch-enabled project ",
        "or box terminal. rs-fetch stages the work with the agent's message ",
        "prefilled and never commits unless the card's auto-commit box is ",
        "ticked (then each fetched commit lands locally with the agent's ",
        "original timestamps); ▸ lists a row's commits to fetch them one by ",
        "one.",
    ]);
}

// ---- rs-fetch visibility (the Repos tab's toggle, and the lists it filters) --
//
// A repo the operator has switched OFF is dropped from the rs-fetch LISTS — the
// Development page's Fetch tab and a project's project-wide fetch pane. It is a
// display setting and nothing more: the repo stays mirrored, its own per-repo
// fetch tab still renders, and `rs-fetch <repo>` typed in a terminal is
// unaffected (nothing in a container enumerates repos).
//
// TOLERANT on purpose: a row from a broker that predates the field carries no
// `fetch_enabled` at all, and must stay visible. Only an explicit `false` hides.
function devFetchVisible(repos) {
    return repos.filter((r) => !(r && r.fetch_enabled === false));
}

// The "N repos hidden" footer. Never let a filtered list end silently — the
// whole failure this guards against is a repo the operator cannot find and
// cannot remember hiding. `onOpen` is the way back, and it differs per surface:
// on the Development page it switches to the Repos tab (saying "Development →
// Repos" there would point at the page you are standing on); on a project's
// pane it opens Development.
function devHiddenLine(count, label, onOpen) {
    const open = el("button", { class: "btn-small" }, [label]);
    open.onclick = onOpen;
    return el("div", { class: "hint dev-hidden-line" }, [
        el("span", {}, [`${count} repo${count === 1 ? "" : "s"} hidden from `
                        + "this list. "]),
        open,
    ]);
}

// ---- The Repos tab: the dev-lane inventory + the rs-fetch switches ----------
//
// Reads the SAME /broker/dev payload the Fetch tab reads — one verb, no second
// read path — through the same transport-vs-verb ladder: a thrown fetch, a 503
// or an unparseable body mean the broker is unreachable; a 200 carrying
// {ok:false} means the broker ANSWERED and the verb refused, which is a
// different thing and must not be reported as an outage.
async function renderDevReposTab(view, body) {
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
        const retry = el("button", { class: "btn-small" }, ["Retry"]);
        retry.onclick = () => renderDevReposTab(view, body);
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, [(mgmtErrText(data) || "Could not load dev status.")
                            + " "]),
            retry,
        ]));
        return;
    }
    renderDevReposScreen(view, body, data.result);
}

// One inventory row's plain-language state, or "" when there is nothing to say.
//
// ⚠ `empty` is the ACTIVE FORK's emptiness, never the mirror's — so a repo
// nobody has ever worked reads {active:"", empty:false} while a worked one whose
// agent has not pushed reads {active:<user>, empty:true}. A bare "empty" badge
// would therefore mark the SECOND and not the first, which is the inverse of
// what an inventory reader infers. Spell both out instead.
function devRepoStateText(r) {
    if (!r.active) return "no agent yet";
    if (r.empty) return "no pushes yet";
    return "";
}

// A count cell: a number, or an em-dash when the figure is genuinely unknown.
// 0 and "unknown" are DIFFERENT answers here — a repo with no consumer fork has
// no counters at all, and printing 0 would claim it is quiet rather than
// unworked, which is exactly the distinction this table exists to draw.
function devCountCell(n) {
    return el("td", { class: "dev-repos-num" },
              [typeof n === "number" ? String(n) : "—"]);
}

function renderDevReposScreen(view, body, result) {
    body.innerHTML = "";
    const gitea = result.gitea || {};
    const repos = Array.isArray(result.repos) ? result.repos : [];
    const attachments = Array.isArray(result.attachments) ? result.attachments : [];
    // The list itself comes from gitea's search API, so a stopped gitea has
    // nothing to enumerate and nothing to toggle — the same Enable prompt as
    // the Fetch tab, not a special case.
    if (devGiteaHeldNotice(body, gitea)) return;
    if (!gitea.exists || !gitea.running) {
        const enable = el("button", { class: "btn-small" }, ["Enable Gitea"]);
        enable.onclick = () =>
            devEnableGiteaDialog(view, () => renderDevReposTab(view, body));
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, [gitea.exists ? "Gitea is stopped. "
                                         : "The dev lane isn't enabled yet. "]),
            enable,
        ]));
        return;
    }
    if (!repos.length) {
        body.appendChild(el("div", { class: "mgmt-empty" }, [
            "No dev repos yet. A repo is mirrored here when you create a dev "
            + "project from a GitHub URL (New Project → Dev), or add a dev box to "
            + "an existing project.",
        ]));
        return;
    }
    // The active fork lives in the repo cell's sub-line rather than its own
    // column: it is the longest value in the table, and on a phone a sixth
    // column pushed the rs-fetch switch — the one control here — off-screen.
    // The two optional columns are CSS-hidden at narrow widths (see style.css),
    // which leaves repo + counts + switch, the set that is worth a phone.
    const head = el("tr", {}, [
        el("th", {}, ["Repo"]),
        el("th", { class: "dev-repos-num" }, ["Issues"]),
        el("th", { class: "dev-repos-num" }, ["PRs"]),
        el("th", { class: "dev-repos-opt" }, ["Synced"]),
        el("th", { class: "dev-repos-opt" }, ["Worked by"]),
        el("th", {}, ["rs-fetch"]),
    ]);
    const rows = [];
    for (const r of repos) {
        // Deduplicated PROJECT names (devWorkedByProjects says why). An EMPTY
        // list does NOT mean nobody is working the repo — provision_consumer
        // mints a fork with no ledger row at all — so this column is rendered
        // plainly, never as a warning state.
        const worked = devWorkedByProjects(
            attachments.filter((a) => a && a.repo === r.repo).map((a) => a.project));
        rows.push(devRepoInventoryRow(view, body, r, worked));
    }
    body.appendChild(el("div", { class: "dev-repos-wrap" }, [
        el("table", { class: "dev-repos-table" }, [
            el("thead", {}, [head]), el("tbody", {}, rows),
        ]),
    ]));
    body.appendChild(el("div", { class: "hint" }, [
        "Issues and PRs are counted on each repo's active agent fork — the "
        + "fork is where the agent files work and where you file tasks for it. "
        + "Switching rs-fetch off hides a repo from the Fetch tab and from "
        + "every project's rs-fetch tab; it never deletes anything, and "
        + "rs-fetch still works on it by name in a terminal.",
    ]));
}

// ONE inventory row. The degraded arm is not optional: dev_status hands back a
// {repo, error} row when a repo's status read raises, and that row carries NONE
// of the keys the data columns read — so an unqualified render would print
// "undefined" across it and, worse, would report "no agent yet" (the `active`
// key is merely absent) about a repo whose fork state is unknown. The toggle
// stays live there on purpose: a sick repo is exactly the one you reach for the
// hide button on, and the preference is a local file read that works regardless.
// The one exception, on either arm, is a repo this host has not added (gitea
// holds it, this host's dev-lane records do not): the switch is disabled and the
// row says why, since every write on it would be refused.
function devRepoInventoryRow(view, body, r, worked) {
    const toggle = devFetchToggle(view, body, r);
    const notAdded = devRepoNotAdded(r);
    if (r.error) {
        const errCell = [
            el("div", { class: "dev-repo-name" }, [r.repo || ""]),
            el("div", { class: "dev-pr-meta dev-review-failed" },
               [String(r.error)]),
        ];
        if (notAdded) errCell.push(devRepoNotAddedNote());
        return el("tr", { class: "dev-repos-degraded" }, [
            el("td", {}, errCell),
            el("td", { class: "dev-repos-num" }, ["—"]),
            el("td", { class: "dev-repos-num" }, ["—"]),
            el("td", { class: "dev-repos-opt" }, ["—"]),
            el("td", { class: "dev-repos-opt" }, ["—"]),
            el("td", {}, [toggle]),
        ]);
    }
    // Sub-line: what this row's numbers belong to. The fork NAME is the
    // attribution — simultaneous consumers on one repo are legal, so a bare
    // count would read as the repo's total when it is one fork's — and the two
    // plain-language states stand in its place when there is no fork to name.
    const nameCell = [el("div", { class: "dev-repo-name" }, [r.repo || ""])];
    const sub = [];
    if (r.private) sub.push(el("span", {}, ["private"]));
    if (r.active) sub.push(el("span", { class: "dev-repos-fork" }, [r.active]));
    const state = devRepoStateText(r);
    if (state) sub.push(el("span", {}, [state]));
    if (sub.length) {
        const line = el("div", { class: "dev-pr-meta dev-repos-sub" }, []);
        sub.forEach((s, i) => {
            if (i) line.appendChild(el("span", {}, [" · "]));
            line.appendChild(s);
        });
        nameCell.push(line);
    }
    if (notAdded) nameCell.push(devRepoNotAddedNote());
    return el("tr", {}, [
        el("td", {}, nameCell),
        devCountCell(r.open_issues),
        devCountCell(r.open_prs),
        el("td", { class: "dev-pr-meta dev-repos-opt" },
           [r.mirror_synced_at || "—"]),
        el("td", { class: "dev-pr-meta dev-repos-opt" },
           [worked.length ? worked.join(", ") : "—"]),
        el("td", {}, [toggle]),
    ]);
}

// A repo gitea holds but this host has not added: the dev-lane writes gate on
// this host's own record of the repo, and dev_status reports that record as
// `added`. Tolerant exactly like the rs-fetch flag: only an explicit `false`
// counts, so a row from a broker that predates the key stays fully live.
function devRepoNotAdded(r) {
    return !!r && r.added === false;
}

// What the operator reads on a not-added row. Plain text, naming no CLI
// command: re-entering the repo's GitHub URL in a dev project or dev box create
// adds it on this host (on an existing mirror that only syncs it).
function devRepoNotAddedNote() {
    return el("div", { class: "dev-pr-meta dev-repo-not-added" }, [
        "Not added on this host: Gitea holds this mirror, but this host’s "
        + "dev-lane records don’t (another RS install sharing this Gitea added "
        + "it, or these records were reset). Its rs-fetch switch works once it "
        + "is added here — creating a dev project or a dev box from its GitHub "
        + "URL adds it.",
    ]);
}

// The rs-fetch switch. Optimism is deliberately absent: the button disables
// itself, posts, and the tab re-renders from what the SERVER says — never from
// what the page believed. `enabled` is sent as a real boolean and is named
// explicitly at every hop, because a field the broker does not recognise is
// dropped silently (the verb's validator refuses a missing one rather than
// reading it as "disable").
function devFetchToggle(view, body, r) {
    const on = r.fetch_enabled !== false;
    const btn = el("button", {
        class: "btn-small dev-fetch-toggle" + (on ? "" : " off"),
        title: on
            ? "Shown on the Fetch tab and on every project's rs-fetch tab. "
              + "Click to hide it there."
            : "Hidden from the rs-fetch lists. The repo, its forks and its "
              + "history are untouched, and rs-fetch still works on it by "
              + "name. Click to show it again.",
    }, [on ? "Enabled" : "Disabled"]);
    // A refusal is REPORTED, not swallowed. The verb's gate is reachable in
    // normal use — the repo was removed in another tab, or this host's record
    // of it went away after the tab rendered — and without this the only
    // feedback is the button silently snapping back, which reads as the page
    // being broken. The message is server text (already written to be
    // browser-safe, naming no CLI command) and goes in as a TEXT node. A repo
    // the read already reports as not added never gets a click handler: the
    // switch shows its state, disabled, and the row carries the note.
    const err = el("span", { class: "dev-pr-meta dev-review-failed" }, []);
    err.hidden = true;
    if (devRepoNotAdded(r)) {
        btn.disabled = true;
        btn.title = "This repo isn’t added on this host, so its rs-fetch "
            + "switch can’t change yet. Creating a dev project or a dev box "
            + "from its GitHub URL adds it here.";
        return el("span", { class: "dev-fetch-cell" }, [btn, err]);
    }
    btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = "…";
        err.hidden = true;
        let msg = "";
        try {
            const res = await fetch("/broker/dev/fetch-enabled", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ repo: r.repo, enabled: !on }),
            });
            // The transport-vs-verb split: 401/403/503 are the broker being
            // unreachable or refusing the webui, not this verb refusing — and
            // they are THREE different remedies, so they get three messages.
            // Collapsing them into one that names only the session cause sends
            // an operator whose broker is down to re-login, which cannot help.
            if (res.status === 401) {
                msg = "Management session expired — reload the page.";
            } else if (res.status === 403) {
                msg = "The broker rejected the webui.";
            } else if (res.status === 503) {
                msg = "The broker isn’t reachable.";
            } else {
                const data = await res.json();
                if (!res.ok || !data.ok) msg = mgmtErrText(data) || "Refused.";
            }
        } catch (e) {
            msg = "The broker isn’t reachable.";
        }
        if (msg) {
            // Leave the row as it was and say why, rather than re-rendering
            // into a state the click did not achieve.
            err.textContent = msg;
            err.hidden = false;
            btn.disabled = false;
            btn.textContent = on ? "Enabled" : "Disabled";
            return;
        }
        renderDevReposTab(view, body);
    };
    return el("span", { class: "dev-fetch-cell" }, [btn, err]);
}

// The card's sync sentence: where the agent's recorded base branch stands
// against the same branch on the mirror, from repo_status's `upstream`
// ({branch, state, behind, ahead}, or null). "" for anything not known — the
// key absent (a degraded row), null, an unrecognised state, or a count that is
// not a non-negative integer — so a malformed reply can only produce silence,
// never a claim. "Not on upstream yet" rather than "not collected": a count is
// an exact set difference, and collection re-creates commits under new ids.
function devUpstreamText(u) {
    if (!u || typeof u !== "object") return "";
    const count = (v) => Number.isInteger(v) && v >= 0;
    const commits = (k) => `${k} commit${k === 1 ? "" : "s"}`;
    if (u.state === "in_sync") return "In sync with upstream.";
    if (u.state === "same_content") {
        return "Same content as upstream, but the agent hasn't rebased onto "
            + "it yet.";
    }
    if (u.state === "diverged") {
        return "Out of sync with upstream; the agent needs to rebase onto it.";
    }
    if (u.state !== "counted" || !count(u.behind) || !count(u.ahead)
        || (!u.behind && !u.ahead)) {
        return "";
    }
    if (!u.ahead) return `${commits(u.behind)} behind upstream.`;
    if (!u.behind) return `${commits(u.ahead)} not on upstream yet.`;
    return `${commits(u.behind)} behind upstream; ${u.ahead} not on `
        + "upstream yet.";
}

// The note under a card's PR or branch list when it is shorter than the fork's
// real list: repo_status pages both lists to the end, but a slow gitea can run
// out of the per-repo time budget (or fail a later page) first. `total` is
// gitea's count (an integer), null when the read stopped early without one, or
// undefined on a reply that predates the field — which, like a complete list,
// says nothing. Worded for every stop reason, not only the clock.
function devListShortfall(listed, total, one, many) {
    const tail = " — the rest could not be loaded. Reload to try again.";
    if (total === null) {
        return `Showing the first ${listed} ${listed === 1 ? one : many}${tail}`;
    }
    if (Number.isInteger(total) && total > listed) {
        return `Showing ${listed} of ${total} ${total === 1 ? one : many}${tail}`;
    }
    return "";
}

// Project names for a repo's "worked by" — deduplicated, first-seen order.
// The read verbs project the attachment ledger without its `box` field, so a
// project whose own agent works a repo AND hosts dev boxes on it arrives as
// one identical entry per consumer; the line names PROJECTS, and a project
// is either working the repo or not.
function devWorkedByProjects(names) {
    return [...new Set((Array.isArray(names) ? names : []).filter(Boolean))];
}

// Build ONE dev repo's card — shared by the Development Fetch tab (unscoped)
// and the per-project Fetch pane (scoped). view/body host the review dialog's
// redirects and error cards (the host page, or the pane container — both work:
// every render helper takes a bare element). opts:
//   scoped   — hide the GLOBAL affordances: Remove (destructive, Development-
//              only) and the ≥2-fork dropdown (the Management-steered GLOBAL
//              selector; steering from a pane would silently re-steer every
//              consumer's rs-fetch/review reads) — the plain `fork:` badge
//              shows instead.
//   rerender — the re-render target after Sync / fork change / a clicked
//              "done — refresh" (a finished review's Done only closes its
//              box; the 401 post-login landing is the distinct view-rooted
//              onLogin derived below).
//
// Open PRs past DEV_PR_ROWS_SHOWN collapse behind a "Show more" toggle, so the
// branch rows below stay near the top of a busy card. Ten rows (about 300 px)
// keep the branch rows within the first screen of a laptop with room to spare:
// at 5 the operator expands for any moderately busy fork; at 20+ the branches
// are back below the fold, which is the thing this exists to stop. A card with
// no more PRs than this renders exactly as before.
const DEV_PR_ROWS_SHOWN = 10;
// Repos whose PR list the operator expanded. Session memory only (never the
// vault): it keeps the list open across card re-renders (Sync, a fork change, a
// review landing) until the page reloads. Keyed by repo — the Development page
// and a project's pane showing the same repo share it.
const devPrListExpanded = new Set();
function buildDevRepoCard(view, body, r, attached, opts) {
    const scoped = !!(opts && opts.scoped);
    const workedBy = devWorkedByProjects(attached);
    const rerender = (opts && opts.rerender)
        || (() => renderDevFetchTab(view, body));
    // The 401 post-login landing must be VIEW-rooted: renderMgmtLogin wipes
    // `view` (mgmtCard), which on Development DETACHES `body` — so a
    // body-scoped rerender there would render into a dead node. The scoped
    // pane is its own view (view === body === the pane container), so its
    // rerender is safe; unscoped, undefined keeps the Management landing.
    const onLogin = scoped ? rerender : undefined;
    const allRepos = (opts && Array.isArray(opts.allRepos)) ? opts.allRepos : [];
    const prs = Array.isArray(r.prs) ? r.prs : [];
    const branches = Array.isArray(r.branches) ? r.branches : [];
    // agent/<name> session branches (rs-wt's prefix): minted one per session,
    // never deleted, so the fork's branch list is mostly this clutter — the
    // hide tick below (default ON) filters them out of the branch rows.
    const isAgentBranch = (b) => ((b && b.name) || "").startsWith("agent/");
    const agentBranches = branches.filter(isAgentBranch);
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
        rerender();
    };
    // Degraded row (B37): repo_status failed for THIS repo — render its
    // error inline (a text node: server text, house style) and keep the
    // healthy rows around it. Sync stays (the plausible heal for a
    // half-migrated/sick repo); the other affordances need fork state the
    // degraded row doesn't carry.
    if (r.error) {
        return el("div", { class: "card dev-repo-card" }, [
            el("div", { class: "dev-repo-head" }, [
                el("span", { class: "dev-repo-name" }, [r.repo || ""]),
                el("span", { class: "dev-pr-meta dev-review-failed" },
                   [String(r.error)]),
                sync,
            ]),
        ]);
    }
    // The counts are gitea's totals when the reply carries them, so a list
    // cut short (the note under it) still shows the fork's real size; never
    // fewer than the rows listed (a PR closed mid-read can lower the count).
    const realCount = (listed, total) =>
        (Number.isInteger(total) ? Math.max(listed, total) : listed);
    const prCount = realCount(prs.length, r.prs_total);
    const branchCount = realCount(branches.length, r.branches_total);
    const meta = (r.private ? "private · " : "")
        + (r.empty
            // The quiet empty state (B37): a zero-commit source, or a
            // fresh consumer fork before the agent's first push.
            ? "empty — the agent hasn't pushed yet"
            : `${prCount} open PR${prCount === 1 ? "" : "s"} · `
              + `${branchCount} branch${branchCount === 1 ? "" : "es"}`)
        + (r.mirror_synced_at ? ` · synced ${r.mirror_synced_at}` : "");
    // Active-fork control (per-consumer forks): a plain badge with one
    // live fork; a dropdown at ≥2 — the Management-steered GLOBAL
    // selector (PR list, rs-fetch and reviews all follow it). Scoped mode
    // never shows the dropdown (badge only): a pane must not re-steer the
    // global selector.
    const forks = Array.isArray(r.forks) ? r.forks : [];
    const live = forks.filter((f) => f && !f.archived && f.user);
    // Retired (archived) consumer forks. Until now these were filtered out
    // entirely, which is why a retired identity was invisible while still
    // blocking a same-named project or box: destroy keeps the gitea user (it
    // owns the archived fork, and deleting a user purges its repos), so the
    // name stays taken with nothing on screen to explain it. Each row offers
    // the purge that frees the name.
    const retired = forks.filter((f) => f && f.archived && f.user);
    let forkEl = null;
    if (!scoped && live.length >= 2) {
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
            rerender();
        };
    } else if (r.active) {
        forkEl = el("span", { class: "dev-repo-meta" }, [`fork: ${r.active}`]);
    }
    // Auto-commit tick (card-level, default OFF), REMEMBERED PER REPO in the
    // vault's settings.fetch_auto_commit list (readFetchAutoCommit /
    // setFetchAutoCommit): it initialises from the list on every render and
    // the change handler is the list's one writer. A LIVE getter consulted at
    // click time by every copy surface on this card (per-commit copies, Copy
    // all, the PR/branch row copies) — never baked into a button at render,
    // so a copy can never contradict the tick. The box is stamped with its
    // repo so the writer can keep a second card for the same repo (the
    // Development page and a project's pane) showing the same state.
    const acBox = el("input", { type: "checkbox", class: "dev-ac-check" });
    acBox.setAttribute("data-ac-repo", r.repo);
    acBox.checked = readFetchAutoCommit().includes(r.repo);
    acBox.onchange = () => { setFetchAutoCommit(r.repo, acBox.checked); };
    const acWrap = el("label", {
        class: "dev-ac",
        title: "Copied rs-fetch commands include --auto-commit: each fetched "
             + "commit is committed locally with the agent's original "
             + "timestamps — your identity, hooks disabled, unsigned. "
             + "Unticked: commands stage only (the default). Remembered for "
             + "this repo in this browser.",
    }, [acBox, "auto-commit"]);
    const autoCommit = () => acBox.checked;
    // Hide-agent/ tick (card-level, default ON). Ephemeral, unlike the
    // auto-commit tick above — it is display, not consent — so it resets to
    // hidden on every card re-render and is never persisted. Rendered only
    // when the fork actually HAS agent/
    // branches (no inert control otherwise); the label's count is what
    // explains an all-hidden branch list against the meta line's TOTAL.
    // Toggling repaints ONLY the branch rows (paintBranches below) — no
    // refetch, no card re-render, open PR expanders untouched.
    // Shares the auto-commit box's class for its look, but carries NO
    // data-ac-repo stamp — that absence is load-bearing: the auto-commit
    // writer's cross-card flip selects on the stamp, and must never reach this.
    const hideBox = el("input", { type: "checkbox", class: "dev-ac-check" });
    hideBox.checked = true;
    const hideWrap = el("label", {
        class: "dev-ac dev-hide-agent",
        title: "Hide the agent/<name> session branches (one per agent "
             + "session, never deleted — they accumulate by design). "
             + "Untick to list them.",
    }, [hideBox, `hide agent/ (${agentBranches.length})`]);
    const head = [
        el("span", { class: "dev-repo-name" }, [r.repo]),
        el("span", { class: "dev-repo-meta" }, [meta]),
        ...(forkEl ? [forkEl] : []),
        acWrap,
        ...(agentBranches.length ? [hideWrap] : []),
        sync,
    ];
    if (!scoped) {
        // Remove is offered only for a FINISHED repo — no live fork, nobody
        // working it. Retiring a consumer (destroying its project, removing its
        // box) archives the fork, which is what releases the repo. This disable
        // is an affordance only: the broker's gate is the authority (it re-reads
        // the LIVE fork state and fails closed), so a stale page cannot delete.
        const blocker = live.length
            ? `still has a live agent fork (${live.map((f) => f.user).join(", ")})`
            : (workedBy.length
                ? `still worked by ${workedBy.join(", ")}`
                : "");
        const remove = el("button", {
            class: "btn-small btn-danger",
            title: blocker
                ? `Can't remove — ${blocker}. Delete the dev project or box first.`
                : "Delete this repo's mirror, its retired forks and their identities",
        }, ["Remove"]);
        remove.disabled = !!blocker;
        remove.onclick = () => devRepoRemoveDialog(view, body, r.repo);
        head.push(remove);
    }
    const rows = [el("div", { class: "dev-repo-head" }, head)];
    const reviews = (r.reviews && typeof r.reviews === "object") ? r.reviews : {};
    // PR rows past DEV_PR_ROWS_SHOWN go into one wrapper that collapses as a
    // whole: each row's commits panel and its lazily-inserted verdict panel are
    // the row's SIBLINGS, so they land in the wrapper with it and hide with it.
    // Collapsed is display:none, never detached, so the review watcher still
    // finds the stamped cells inside (it only checks isConnected). Built only
    // when there is overflow: a shorter list renders exactly as before.
    const prMore = prs.length > DEV_PR_ROWS_SHOWN
        ? el("div", { class: "dev-pr-more" }) : null;
    let prIndex = 0;
    for (const p of prs) {
        const into = (prMore && prIndex >= DEV_PR_ROWS_SHOWN) ? prMore : null;
        prIndex += 1;
        const commitsUi = devCommitsExpander(view, body, r.repo,
                                             { pr: p.number }, rerender,
                                             onLogin, autoCommit);
        const cells = [
            devCopyBtn(() => `rs-fetch ${r.repo} --pr ${p.number}`
                             + (autoCommit() ? " --auto-commit" : "")),
            commitsUi.btn,
            el("span", { class: "dev-pr-id" }, [`#${p.number}`]),
            el("span", { class: "dev-pr-title" }, [p.title || ""]),
            el("span", { class: "dev-pr-meta" },
               [`[${p.head || "?"}] ${p.updated_at || ""}`]),
        ];
        // Review verdicts (S4): ledger entries keyed by str(pr) — JS
        // property lookup coerces p.number across the int/str boundary.
        // All verdict strings are MODEL OUTPUT → text nodes only. The review
        // UI lives in a stamped cell (data-rkey, str-coerced pr) rendered by
        // the shared devRenderPrReviewCell, so a dismissed review's watcher
        // resolves it IN PLACE across card re-renders, and a mid-flight
        // re-render shows "reviewing…" instead of a duplicate-inviting
        // Review button. The verdict panel is lazy (first badge click) —
        // nothing to push after the commits panel here anymore.
        const v = reviews[p.number];
        const rkey = devReviewKey(r.repo, "#" + String(p.number));
        const revCell = el("span", { class: "dev-review-cell" });
        revCell.setAttribute("data-rkey", rkey);
        devRenderPrReviewCell(revCell, r.repo, p, v,
                              { view, body, rerender, onLogin });
        cells.push(revCell);
        const prRow = el("div", { class: "dev-pr-row" }, cells);
        if (into) {
            into.append(prRow, commitsUi.panel);
        } else {
            rows.push(prRow);
            rows.push(commitsUi.panel);
        }
    }
    if (prMore) {
        // The button sits ABOVE the hidden rows, so expanding reveals them
        // below it and "Show fewer" stays where it was clicked (no jump).
        const moreBtn = el("button", { class: "btn-small dev-pr-more-btn" });
        const paintMore = (open) => {
            prMore.classList.toggle("collapsed", !open);
            moreBtn.textContent = open
                ? "Show fewer"
                : `Show more (${prs.length} total open PRs)`;
        };
        // The direction comes from THIS card's wrapper, never from the Set: a
        // card built before another surface expanded the same repo is still
        // collapsed, and reading the Set would make its first click a no-op.
        moreBtn.onclick = () => {
            const open = prMore.classList.contains("collapsed");
            if (open) devPrListExpanded.add(r.repo);
            else devPrListExpanded.delete(r.repo);
            paintMore(open);
        };
        paintMore(devPrListExpanded.has(r.repo));
        rows.push(moreBtn);
        rows.push(prMore);
    }
    const prShort = devListShortfall(prs.length, r.prs_total,
                                     "open PR", "open PRs");
    if (prShort) rows.push(el("div", { class: "dev-pr-meta" }, [prShort]));
    // The sync sentence joins "No open PRs." when there are none, and is its
    // own faint line under the PR rows otherwise. The tooltip names the branch
    // pair (the sentence compares ONE branch) and the freshness caveat:
    // upstream here is the mirror, as of its last sync.
    const upText = devUpstreamText(r.upstream);
    const upAttrs = (cls) => {
        const a = { class: cls };
        const b = upText && r.upstream.branch;
        if (typeof b === "string" && b) {
            // Named by fork: the card follows the ACTIVE fork, which on a
            // project's pane can be another consumer's.
            const who = r.active ? `${r.active}'s` : "The agent's";
            a.title = `${who} ${b} compared with upstream's ${b}, as of the `
                + "mirror's last sync.";
        }
        return a;
    };
    if (!prs.length && !r.empty) {
        rows.push(el("div", upAttrs("config-empty"),
                     [upText ? `No open PRs. ${upText}` : "No open PRs."]));
    } else if (upText) {
        rows.push(el("div", upAttrs("dev-pr-meta dev-upstream"), [upText]));
    }
    // Branch rows live in their own wrapper so the hide-agent/ tick can
    // repaint them locally (no refetch, no card re-render — an open branch
    // expander is discarded with its row, accepted). .dev-branch-wrap
    // re-applies the card's flex column + 6px gap (the .dev-commits-rows
    // precedent: rows inside a plain div lose .dev-repo-card's gap and
    // render tighter), and its :empty rule display:none's the wrapper so a
    // branch-less or all-hidden card doesn't carry a phantom gap slot.
    const branchWrap = el("div", { class: "dev-branch-wrap" });
    const paintBranches = () => {
        branchWrap.innerHTML = "";
        const visible = hideBox.checked
            ? branches.filter((b) => !isAgentBranch(b))
            : branches;
        for (const b of visible) {
            const commitsUi = devCommitsExpander(view, body, r.repo,
                                                 { branch: b.name }, rerender,
                                                 onLogin, autoCommit);
            branchWrap.append(el("div", { class: "dev-branch-row" }, [
                devCopyBtn(() => `rs-fetch ${r.repo} --branch ${b.name}`
                                 + (autoCommit() ? " --auto-commit" : "")),
                commitsUi.btn,
                el("span", { class: "dev-pr-title" }, [b.name || ""]),
                el("span", { class: "dev-pr-meta" }, [b.committed_at || ""]),
            ]));
            branchWrap.append(commitsUi.panel);
        }
    };
    hideBox.onchange = paintBranches;
    paintBranches();
    rows.push(branchWrap);
    const branchShort = devListShortfall(branches.length, r.branches_total,
                                         "branch", "branches");
    if (branchShort) {
        rows.push(el("div", { class: "dev-pr-meta" }, [branchShort]));
    }
    if (workedBy.length) {
        rows.push(el("div", { class: "dev-pr-meta dev-attached" },
                     ["worked by: " + workedBy.join(", ")]));
    }
    // Retired identities, with the purge that frees their names. !scoped for
    // the same reason as Remove and the fork dropdown: a per-project pane must
    // not delete another project's identity. The button is an affordance only —
    // the broker re-derives the ledger and live-fork state and fails closed.
    if (!scoped && retired.length) {
        for (const f of retired) {
            const del = el("button", { class: "btn-small btn-danger" }, ["Purge"]);
            del.onclick = () => devPurgeIdentityDialog(
                view, body, f.user, devForksOf(f.user, allRepos, r));
            rows.push(el("div", { class: "dev-branch-row dev-retired-row" }, [
                el("span", { class: "dev-pr-meta" }, ["retired"]),
                el("span", { class: "dev-pr-title" }, [f.user]),
                del,
            ]));
        }
    }
    return el("div", { class: "card dev-repo-card" }, rows);
}

// ---- Per-project Fetch panes (the `fetch-<repo>` and `fetch-:all` tabs) ----
// The Development Fetch page's repo cards, rendered as a service pane off the
// token-gated /broker/dev* relays. Two modes, ONE renderer:
//   repo given  — the `fetch-<repo>` tab: that repo alone, off /broker/dev/
//                 repo-status. Active-fork steered, so content is identical to
//                 the Development page's card for it.
//   repo null   — the `fetch-:all` tab of an rs-fetch-enabled project: EVERY
//                 mirrored repo, off /broker/dev. All-mirrors because an
//                 rs-fetch consumer can pull any mirror, and the mirror list is
//                 host-side (not on the webui's /projects:ro mount), so the repo
//                 set can only be resolved here, at open time.
// Sharing the renderer is safe because the ROW SHAPE is identical in both
// modes: dev_status and dev_repo_status each build a row from the same
// gitea.repo_status(...) + row["reviews"] = select_pr_verdicts(
// load_repo_pr_verdicts(...), ...) pair
// (cli/rscore.py dev_status / dev_repo_status), so buildDevRepoCard needs no
// per-mode branching. The ATTACHMENTS are the one thing that genuinely differs
// — see the filter below.
//
// Both modes are SCOPED: the global affordances (Remove, the ≥2-fork dropdown,
// Enable Gitea's tailed op) stay on the Development page — steering the active
// fork from a project pane would silently re-steer every other consumer's
// rs-fetch and review reads — and the pane links there instead. The
// transport-vs-verb ladder mirrors renderDevFetchTab, rendering into the pane
// container (every mgmt render helper takes a bare element), and the 401 arm
// routes back to THIS renderer so a session expiry never lands a host page
// inside the pane.
//
// The pane is a SNAPSHOT, not a live view: activateService unhides an existing
// instance without re-fetching, so a repo mirrored elsewhere after this pane
// first rendered appears on the next Sync, "done — refresh" click or page
// reload.
async function renderProjectFetchPane(container, repo) {
    const all = !repo;
    container.innerHTML = "";
    container.appendChild(el("div", { class: "mgmt-loading" },
                             [all ? "Loading dev repos…" : `Loading ${repo}…`]));
    let res;
    try {
        res = await fetch(all
            ? "/broker/dev"
            : `/broker/dev/repo-status?repo=${encodeURIComponent(repo)}`);
    } catch (e) { return renderMgmtUnavailable(container); }
    if (res.status === 401)
        return renderMgmtLogin(container,
                               () => renderProjectFetchPane(container, repo));
    if (res.status === 403) return renderMgmtRejected(container);
    // _relay's 503 body is PARSEABLE JSON — peel it explicitly or a broker
    // outage would render as a verb error (the transport-vs-verb split).
    if (res.status === 503) return renderMgmtUnavailable(container);
    let data;
    try { data = await res.json(); } catch (e) { return renderMgmtUnavailable(container); }
    // The broker ANSWERED and the verb refused (200 + {ok:false}) — distinct
    // from the transport arms peeled above. Genuinely live in all-repos mode:
    // dev_status keeps its mirror ENUMERATION page-fatal (a raising list_repos
    // becomes a ValidationError), so a healthy broker with a sick gitea lands
    // here. Do not "simplify" this into the gitea empty state below — that
    // would report an erroring gitea as a stopped one.
    if (!res.ok || !data.ok || !data.result) {
        container.innerHTML = "";
        const retry = el("button", { class: "btn-small" }, ["Retry"]);
        retry.onclick = () => renderProjectFetchPane(container, repo);
        container.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, [(mgmtErrText(data) || (all
                ? "Could not load dev status."
                : "Could not load the repo status.")) + " "]),
            retry,
        ]));
        return;
    }
    container.innerHTML = "";
    const result = data.result;
    const gitea = result.gitea || {};
    if (devGiteaHeldNotice(container, gitea)) return;
    if (!gitea.exists || !gitea.running) {
        const openDev = el("button", { class: "btn-small" },
                           ["Open Development"]);
        openDev.onclick = () => openDevelopment();
        container.appendChild(el("div", { class: "mgmt-empty" }, [
            el("span", {}, [gitea.exists
                ? "Gitea is stopped. Enable it from the Development page. "
                : "The dev lane isn't enabled yet. Enable it from the "
                  + "Development page. "]),
            openDev,
        ]));
        return;
    }
    const rows = all
        ? (Array.isArray(result.repos) ? result.repos : [])
        : [(result.repo && typeof result.repo === "object") ? result.repo : {}];
    const atts = Array.isArray(result.attachments) ? result.attachments : [];
    if (all && !rows.length) {
        container.appendChild(el("div", { class: "mgmt-empty" }, [
            "No dev repos yet. A repo is mirrored here when you create a dev "
            + "project from a GitHub URL (New Project → Dev), or add a dev box to "
            + "an existing project.",
        ]));
        return;
    }
    if (!all && !rows[0].repo) {
        container.appendChild(el("div", { class: "mgmt-empty" },
            [`No status for ${repo} — was the repo removed?`]));
        return;
    }
    // Same ordering rule as the Development page: filter AFTER the empty gate
    // above, and only in all-repos mode — the per-repo `fetch-<repo>` tab was
    // opened by name, so it never hides its one repo.
    const visible = all ? devFetchVisible(rows) : rows;
    const hiddenCount = all ? rows.length - visible.length : 0;
    for (const r of visible) {
        // ⚠ The two verbs return DIFFERENT attachment shapes, so these must not
        // be unified: dev_status emits [{project, repo}] for EVERY repo (filter,
        // or every card claims every project), while dev_repo_status emits
        // [{project}] already scoped server-side to this repo — it has no `repo`
        // key at all, so filtering there yields [] and silently drops the card's
        // "worked by" line. See dev_status / dev_repo_status in cli/rscore.py.
        const attached = (all ? atts.filter((a) => a && a.repo === r.repo) : atts)
            .map((a) => a && a.project).filter(Boolean);
        container.appendChild(buildDevRepoCard(container, container, r, attached, {
            scoped: true,
            rerender: () => renderProjectFetchPane(container, repo),
            // No allRepos: it feeds the purge dialog, which is a global
            // affordance a scoped card never renders.
        }));
    }
    // Reviewer-token state beside the cards' Review affordances (both Fetch
    // surfaces; same silent degradation on a missing field). Placed AFTER the
    // cards so the two project panes read identically; the Development page
    // keeps its own placement above them.
    const revLine = devReviewerLine(result.reviewer);
    if (revLine) container.appendChild(revLine);
    if (hiddenCount) {
        container.appendChild(devHiddenLine(hiddenCount, "Open Development",
                                            () => openDevelopment()));
    }
    // The shared legend — worth carrying only where there are many cards and
    // the 📋/▸/auto-commit affordances need naming (the per-repo pane omits it).
    if (all) container.appendChild(devFetchLegend());
}

// Which repos does <user> own a fork of, across every mirror the page loaded?
// Purging is USER-scoped while this view is repo-scoped, so the dialog has to
// say what else goes. `here` is the card's own repo, kept even when the page
// data is thin so the list is never empty on the row you clicked.
//
// ⚠ This is a FLOOR, never an exhaustive list: dev_status degrades a sick repo
// to {repo, error} with NO forks key (B37), so a partly-sick gitea silently
// under-reports — which is exactly when someone is most likely to be cleaning
// up. The dialog copy must read as "at least these".
function devForksOf(user, allRepos, here) {
    const out = [];
    for (const rr of allRepos) {
        if (!rr || !Array.isArray(rr.forks)) continue;
        if (rr.forks.some((f) => f && f.user === user) && rr.repo)
            out.push(rr.repo);
    }
    if (here && here.repo && !out.includes(here.repo)) out.push(here.repo);
    return out.sort();
}

// Purge a RETIRED agent identity: its gitea user and EVERY fork it owns. This
// is what frees the name — retirement archives the fork but keeps the user
// (deleting one purges its repos), and the surviving user's access token cannot
// be reissued, so a same-named project or box is blocked until this runs.
// STEP-UP gated like destroy and repo-remove. The broker re-derives both gates
// (the attachment ledger, and whether any fork is still live) and fails closed,
// so the row's presence is an affordance, never the authority.
// No onLogin: mgmtConfirmThenTail calls mgmtStatusRedirect(view, status) with
// no landing callback, and this control is !scoped-only — so the Management
// landing is already the right one, exactly as devRepoRemoveDialog relies on.
// Threading a callback here would be dead code that reads as if it weren't.
function devPurgeIdentityDialog(view, body, user, repos) {
    const pwI = el("input", { type: "password", autocomplete: "current-password" });
    const scope = repos.length
        ? `This deletes its fork${repos.length === 1 ? "" : "s"} of `
          + `${repos.join(", ")}, including all commit history on `
          + `${repos.length === 1 ? "it" : "them"}. `
        : "This deletes every fork it owns, including all commit history. ";
    mgmtConfirmThenTail(view, {
        title: `Purge ${user}`,
        tailTitle: `Purging ${user}`,
        verb: "dev_purge_consumer",
        confirmLabel: "Purge",
        danger: true,
        body: [
            el("p", {}, [
                `"${user}" is a retired agent identity. ` + scope
                + "Its Gitea user is deleted too, which frees the project or "
                + "box name for reuse. It cannot be undone, and the GitHub "
                + "original is untouched.",
            ]),
            el("p", { class: "dev-pr-meta" }, [
                "If this identity also has forks on repos not shown here, "
                + "those are deleted as well.",
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Re-enter your master password"]), pwI,
            ]),
        ],
        validate: () => (pwI.value ? null : "Re-enter your master password."),
        // Step-up: the retyped password is derived client-side and only the
        // proof rides the request; the broker verifies it async, so a wrong
        // password surfaces as a FAILED op (the destroy / repo-remove
        // semantics), not an inline phase-1 error.
        request: async () => fetch("/broker/dev/purge-consumer", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                user: user,
                proof: await deriveLoginProof(pwI.value),
            }),
        }),
        onDone: (ok) => { if (ok) renderDevFetchTab(view, body); },
        focus: () => pwI.focus(),
    });
}

// Delete a finished repo: its gitea mirror + every retired agent fork (history
// included) + the retired identities that own them (their gitea users — the
// purge that frees a project/box name; an identity another project still
// records, or that owns something else, is kept with a warning) + the tokens of
// identities nothing else uses + the host stamp. STEP-UP gated like destroy and
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
                + "agent fork (including its commit history), the retired agent "
                + "identities that own them (which frees their project or box "
                + "names for reuse), the tokens of identities nothing else uses, "
                + "and the local record. It cannot be undone. The GitHub original "
                + "is untouched.",
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
        // The prune: a repo mirrored again under this name starts with the
        // auto-commit tick OFF (the reused-name rule the project map follows).
        // Awaited (the op tail awaits onDone), best-effort inside, before the
        // tab re-renders.
        onDone: async (ok) => {
            if (!ok) return;
            await setFetchAutoCommit(repo, false);
            renderDevFetchTab(view, body);
        },
        focus: () => pwI.focus(),
    });
}

// Sandboxes → Infrastructure: shared host state that isn't a project, in two
// sub-groups — "Development" (the shared gitea dev lane) and "Reviewer" (the
// sandboxed reviewer's model/effort default). ALL static DOM (both sub-headers
// and both rows) is built synchronously BEFORE any await: the gitea fill below
// early-returns on unknown/not-enabled, and the reviewer control is
// gitea-independent — building it before the ladder is what keeps a gitea
// hiccup from vanishing the reviewer row (DOM order stays gitea-first; the
// reviewer row fills through its own async path).
async function appendInfraSection(view) {
    const row = el("div", { class: "mgmt-row mgmt-infra-row" }, [
        el("span", {}, ["Gitea (dev lane)"]),
        el("span", { class: "mgmt-loading" }, ["…"]),
    ]);
    const reviewerRow = el("div", { class: "mgmt-row mgmt-infra-row" }, [
        el("span", { class: "mgmt-loading" }, ["…"]),
    ]);
    view.appendChild(el("div", { class: "mgmt-infra" }, [
        el("h3", { class: "mgmt-infra-title" }, ["Shared services"]),
        el("div", { class: "mgmt-infra-sub" }, ["Development"]),
        row,
        el("div", { class: "mgmt-infra-sub" }, ["Reviewer"]),
        reviewerRow,
    ]));
    appendReviewerModelRow(view, reviewerRow);   // async, own fetch path
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
            devEnableGiteaDialog(view, () => rerenderView(view));
        return b;
    };
    const rerender = () => rerenderView(view);
    const up = g.upgrade || null;
    // While an upgrade or a restore holds Gitea, nothing here can act on it:
    // say so instead of offering buttons that would only be refused.
    if (g.upgrading) {
        row.appendChild(el("span", { class: "type-badge" },
                           ["being upgraded, put back or restored…"]));
        return;
    }
    // An upgrade cut off before the new version proved itself, or going back
    // cut off part way: Enable finishes either by putting the previous version
    // back, so the button says so.
    if (up && up.phase !== "committed") {
        row.appendChild(el("span", { class: "type-badge" },
                           [up.phase === "damaged"
                               ? "upgrade record damaged"
                               : up.phase === "rolling-back"
                                   ? `going back to ${up.from_version} interrupted`
                                   : `upgrade to ${up.to_version} interrupted`]));
        if (up.phase === "rolling-back") {
            const rec = el("button", { class: "btn-small" }, ["Recover"]);
            rec.onclick = () => devGiteaRecoverRollbackDialog(view, up, rerender);
            row.appendChild(rec);
        } else if (up.phase !== "damaged") {
            const rec = el("button", { class: "btn-small" }, ["Recover"]);
            rec.onclick = () => devEnableGiteaDialog(view, rerender);
            row.appendChild(rec);
        }
        return;
    }
    if (!g.exists) {
        row.appendChild(el("span", { class: "type-badge" }, ["not enabled"]));
        row.appendChild(enableBtn());
        return;
    }
    row.appendChild(el("span",
                       { class: g.running ? "state-running" : "state-stopped" },
                       [g.running ? "running" : "stopped"]));
    if (g.version) row.appendChild(el("span", { class: "type-badge" }, [g.version]));
    if (!g.running) {
        row.appendChild(enableBtn());
    }
    // Enabled (running or stopped — the verb resumes a stopped gitea itself).
    const pwBtn = el("button", { class: "btn-small" }, ["Set Gitea password"]);
    pwBtn.onclick = () => devGiteaPasswdDialog(view, rerender);
    row.appendChild(pwBtn);
    if (up && up.phase === "committed") {
        row.appendChild(el("span", { class: "type-badge" },
                           [`previous version ${up.from_version} kept`]));
        const back = el("button", { class: "btn-small" }, [`Go back to ${up.from_version}`]);
        back.onclick = () => devGiteaRollbackDialog(view, g, rerender);
        row.appendChild(back);
        const discard = el("button", { class: "btn-small" }, ["Discard previous version"]);
        discard.onclick = () => devGiteaDiscardPreviousDialog(view, g, rerender);
        row.appendChild(discard);
    } else if (g.running && g.version && g.target
               && devVersionOlder(g.version, g.target)) {
        const upBtn = el("button", { class: "btn-small" }, [`Upgrade to ${g.target}`]);
        upBtn.onclick = () => devGiteaUpgradeDialog(view, g, rerender);
        row.appendChild(upBtn);
    }
}

// True when dotted-number version a is older than b ("1.25.1" < "28.2.0").
// Display only: the broker decides for real and refuses when it disagrees.
function devVersionOlder(a, b) {
    const pa = String(a).split(".").map((n) => parseInt(n, 10));
    const pb = String(b).split(".").map((n) => parseInt(n, 10));
    if (pa.some(isNaN) || pb.some(isNaN)) return false;
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const x = pa[i] || 0, y = pb[i] || 0;
        if (x !== y) return x < y;
    }
    return false;
}

// Sandboxes → Infrastructure → Settings: host-wide choices that are not a
// service — today the task cap every project container gets (threads and
// processes together). The section's static DOM is appended synchronously,
// like the sections above it, so the page order never depends on fetch
// timing; the row then fills from GET /broker/settings. A read failure is a
// quiet badge in the row (the page's Refresh retries), never a page card.
const SETTINGS_PIDS_LABEL = "Task limit per project container";

async function appendSettingsSection(view) {
    const row = el("div", { class: "mgmt-row mgmt-infra-row mgmt-settings-row" }, [
        el("span", {}, [SETTINGS_PIDS_LABEL]),
        el("span", { class: "mgmt-loading" }, ["…"]),
    ]);
    view.appendChild(el("div", { class: "mgmt-infra" }, [
        el("h3", { class: "mgmt-infra-title" }, ["Settings"]),
        row,
        el("div", { class: "mgmt-settings-hint" }, [
            "Threads and processes together, per project. Saving changes "
            + "no running project: each one picks the value up the next "
            + "time it starts."]),
    ]));
    let body = null;
    try {
        const res = await fetch("/broker/settings");
        if (res.ok) body = await res.json();
    } catch (e) { /* the quiet badge below */ }
    if (!body) {
        row.replaceChildren(el("span", {}, [SETTINGS_PIDS_LABEL]),
                            el("span", { class: "type-badge" }, ["unavailable"]));
        return;
    }
    if (!body.ok) {
        // A broker started before this setting existed does not know the
        // verb: the same quiet badge as an unreachable read. Any other refusal
        // is the broker's own message, beside the label.
        const unknown = body.error && body.error.kind === "unknown_verb";
        row.replaceChildren(el("span", {}, [SETTINGS_PIDS_LABEL]),
                            unknown
                                ? el("span", { class: "type-badge" }, ["unavailable"])
                                : el("span", { class: "mgmt-reviewer-err" }, [mgmtErrText(body)]));
        return;
    }
    fillSettingsRow(view, row, body.result || {}, "");
}

function fillSettingsRow(view, row, settings, notice) {
    const p = settings.pids_limit || {};
    const lo = p.min || 1;
    const hi = p.max || 0;
    const input = el("input", { type: "number", class: "mgmt-settings-num",
                                min: String(lo), max: String(hi), step: "1" });
    input.value = p.value !== null && p.value !== undefined ? String(p.value) : "";
    const errEl = el("span", { class: "mgmt-reviewer-err" }, []);
    if (settings.error) {
        errEl.textContent = settings.error + "; Back to default replaces it.";
    }
    const noteEl = el("span", { class: "mgmt-settings-note" }, [notice || ""]);
    let busy = false;
    const post = async (payload, done) => {
        // One request at a time: Enter held down or a double click must not
        // stack saves.
        if (busy) return;
        busy = true;
        try {
            await postOnce(payload, done);
        } finally {
            busy = false;
        }
    };
    const postOnce = async (payload, done) => {
        errEl.textContent = "";
        noteEl.textContent = "";
        let res, body = null;
        try {
            res = await fetch("/broker/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
        } catch (e) { return renderMgmtUnavailable(view); }
        const redirect = mgmtStatusRedirect(view, res.status);
        if (redirect) return redirect();
        try { body = await res.json(); } catch (e) { return renderMgmtUnavailable(view); }
        if (!res.ok || !body.ok) {
            // Reachable only on 200-with-parseable-JSON (the relay peels
            // 401/403/503 off above): the broker ANSWERED and the verb
            // refused — its message beside the controls, never a page card.
            errEl.textContent = mgmtErrText(body);
            return;
        }
        fillSettingsRow(view, row, body.result || {}, done);
    };
    const save = el("button", { class: "btn-small" }, ["Save"]);
    save.onclick = () => {
        const raw = input.value.trim();
        const n = /^[0-9]+$/.test(raw) ? Number(raw) : NaN;
        if (!Number.isSafeInteger(n) || n < lo || (hi && n > hi)) {
            errEl.textContent = `Enter a whole number from ${lo} to ${hi}.`;
            return;
        }
        post({ pids_limit: n },
             "Saved. Each project uses it from its next start.");
    };
    input.onkeydown = (ev) => { if (ev.key === "Enter") save.onclick(); };
    const children = [el("span", {}, [SETTINGS_PIDS_LABEL]), input, save];
    if (!settings.error) {
        children.push(el("span", { class: "type-badge" },
                         [p.saved ? `saved · default ${p.default}` : "default"]));
    }
    if (p.saved || settings.error) {
        const reset = el("button", { class: "btn-small" }, ["Back to default"]);
        reset.onclick = () => post({ reset: true },
                                   "Back to the default. Each project uses it from its next start.");
        children.push(reset);
    }
    children.push(noteEl, errEl);
    row.replaceChildren(...children);
}

// The reviewer's model/effort DEFAULT (the fifth model_catalog type). Unlike
// the four project types there is no per-invocation knob: review_pr resolves
// this live at each run, so Apply takes effect on the very next review — no
// recreate, no restart. Apply writes the pair into the operator's untracked
// override via the token-gated model_default_set verb; Reset clears the
// override key back to the tracked base. `view` is the Sandboxes page root
// (the 401/403/503 ladders land there); `rowEl` is the pre-built row this
// fills (created before appendInfraSection's gitea await — see the D-placement
// note there).
async function appendReviewerModelRow(view, rowEl) {
    const cat = await ensureModelCatalog();
    rowEl.innerHTML = "";
    rowEl.appendChild(el("span", {}, ["Review model"]));
    if (!cat) {
        // ensureModelCatalog swallows transport AND verb failures into null;
        // this row is secondary surface, so a quiet badge (not a page-level
        // card) is the honest degradation — Retry = the page's Refresh.
        rowEl.appendChild(el("span", { class: "type-badge" },
                             ["model catalog unavailable"]));
        return;
    }
    const overridden = (cat.overridden || []).includes("reviewer");
    const picker = modelPicker(cat, "reviewer", "", "", {});
    const errEl = el("span", { class: "mgmt-reviewer-err" }, []);
    const post = async (payload) => {
        errEl.textContent = "";
        let res, body = null;
        try {
            res = await fetch("/broker/models/default", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
        } catch (e) { return renderMgmtUnavailable(view); }
        const redirect = mgmtStatusRedirect(view, res.status);
        if (redirect) return redirect();
        try { body = await res.json(); } catch (e) { return renderMgmtUnavailable(view); }
        if (!res.ok || !body.ok) {
            // Reachable only on 200-with-parseable-JSON (the relay peels
            // 401/403/503 off above): the broker ANSWERED and the verb
            // refused — show its message beside the controls, never the
            // unreachable card.
            errEl.textContent = mgmtErrText(body);
            return;
        }
        // Every picker in the app pre-selects from the cached /broker/models
        // payload — bust it so create dialogs / the box window seed the fresh
        // defaults too, then re-render the page (badge + picker re-seed).
        state.modelCatalog = null;
        rerenderView(view);
    };
    rowEl.appendChild(picker.row);
    const apply = el("button", { class: "btn-small" }, ["Apply"]);
    apply.onclick = () => {
        const v = picker.value();
        post({ type: "reviewer", model: v.model, effort: v.effort });
    };
    rowEl.appendChild(apply);
    if (overridden) {
        rowEl.appendChild(el("span", { class: "type-badge" }, ["overridden"]));
        const reset = el("button", { class: "btn-small" }, ["Reset to default"]);
        reset.onclick = () => post({ type: "reviewer", clear: true });
        rowEl.appendChild(reset);
    }
    rowEl.appendChild(errEl);
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
        project: name,
        confirmLabel: action === "start" ? "Start" : "Stop",
        body: [el("p", {}, [desc])],
        request: () => fetch(
            `/broker/project/${encodeURIComponent(name)}/${action}`,
            { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }),
    });
}

// An auth/availability status the caller should defer to (re-render the right
// card), or null if the response carries a real verb result to handle.
// `onLogin` (optional) is the post-login landing for the 401 arm — a caller
// hosted OUTSIDE the Management host page (the per-project Fetch pane) passes
// its own re-render so a session expiry never lands the Management table
// inside a service pane. Absent ⇒ renderMgmtLogin's default landing, exactly
// today's behavior.
function mgmtStatusRedirect(view, status, onLogin) {
    if (status === 401) return () => renderMgmtLogin(view, onLogin);
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
// stage never leaves a stuck pending row. A listed stage that a successful op
// did not need on its path (start on the docker substrate, a project already
// running, a live editor toggle) is removed when the op ends (dropPending).
// Keyed by verb, plus `create_docker`: the docker substrate's create, chosen by
// the create dialog. It lists only the stages every docker create emits BEFORE
// any optional one (git credentials, clone, setup, history line, ready), so
// those append ticked in the order they happen.
const OP_CHECKLISTS = {
    create: [
        { key: "validate", label: "checking prerequisites" },
        { key: "network", label: "creating project network" },
        { key: "create-container", label: "creating supervisor container" },
        { key: "stage-images", label: "staging inner images" },
        { key: "wire", label: "enabling workers and sandboxes" },
    ],
    create_docker: [
        { key: "validate", label: "checking prerequisites" },
        { key: "network", label: "creating project network" },
        { key: "create-container", label: "creating project container" },
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
    // Update's progress lines are its REPORT (each box refreshed or not, the
    // editor extension): they all share one step key, so the tail renders each
    // line's message as its own row (opChecklist's markMsg) instead of one "agents" row.
    agents_update: [],
    // Keys are LOCKSTEP with the rscore box_* progress.step() calls.
    box_add: [
        { key: "validate", label: "checking the project" },
        { key: "create-box", label: "creating the box" },
        { key: "boot", label: "starting the box" },
        { key: "ready", label: "box ready" },
    ],
    box_remove: [
        { key: "validate", label: "checking the project" },
        { key: "discard", label: "discarding the box" },
    ],
    // Keys LOCKSTEP with rscore._provision_gitea's progress.step() calls. The
    // Recover arm's steps (recover / rollback / restored) are deliberately NOT
    // listed: they happen only when an interrupted upgrade is being finished,
    // and unlisted keys are appended as they arrive.
    dev_gitea_start: [
        { key: "pull", label: "pulling the gitea image" },
        { key: "start", label: "starting gitea" },
        { key: "bootstrap", label: "creating accounts" },
    ],
    // Key LOCKSTEP with rscore.dev_passwd's progress.step() call.
    dev_passwd: [
        { key: "set", label: "setting the gitea password" },
    ],
    // Keys are LOCKSTEP with rscore `dev_board_export`'s progress.step() calls.
    dev_board_export: [
        { key: "gitea", label: "making sure gitea is running" },
        { key: "image", label: "resolving the gitea image" },
        { key: "dump", label: "exporting, encrypting and verifying" },
    ],
    // Keys LOCKSTEP with rscore `dev_board_discard`'s progress.step() call.
    dev_board_discard: [
        { key: "discard", label: "removing the backup" },
    ],
    // Keys LOCKSTEP with rscore `dev_gitea_discard_previous`'s progress.step() calls.
    dev_gitea_discard_previous: [
        { key: "container", label: "removing the previous Gitea container" },
        { key: "volume", label: "removing the previous Gitea's data" },
    ],
};

// The deliberate "Enable Gitea" flow (STAGE_DEV_GITEA webui-first A): a tailed
// op (dev_gitea_start ∈ PROGRESS_VERBS) that CREATES gitea from nothing or
// resumes a stopped one. Shared by the Development Gitea tab, the Fetch tab's
// stopped branch, and Sandboxes → Infrastructure. onDone(ok) re-renders the
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

// Upgrade the shared Gitea to the release this software ships (Sandboxes →
// Infrastructure). A detached broker op (the dev lane), so it tails the build
// log terminal-first like a dev box. Step-up: the master password is retyped
// and derived client-side; the backup passphrase rides the body to the broker
// and on to the child's stdin, never a log.
function devGiteaUpgradeDialog(view, g, onDone) {
    const pp1 = el("input", { type: "password", autocomplete: "new-password" });
    const pp2 = el("input", { type: "password", autocomplete: "new-password" });
    const masterI = el("input", { type: "password", autocomplete: "current-password" });
    mgmtConfirmThenTail(view, {
        title: `Upgrade Gitea to ${g.target}`,
        tailTitle: `Upgrading Gitea to ${g.target}`,
        verb: "dev_gitea_upgrade",
        tailMode: "buildlog",
        confirmLabel: "Upgrade",
        body: [
            el("p", {}, [
                `Gitea ${g.version} is upgraded to ${g.target}. First the whole `
                + "instance is backed up; then Gitea stops, its data is copied, "
                + `and ${g.target} starts on the copy. If ${g.target} does not `
                + `start, ${g.version} is put back on its untouched data.`,
            ]),
            el("p", {}, [
                "While it runs (usually a minute or two) Gitea is unavailable, "
                + "and other actions that use it wait. Afterwards a running dev "
                + "box may be restarted once to pick up Gitea's new address — "
                + "an agent session running inside it at that moment is "
                + "interrupted.",
            ]),
            el("p", {}, [
                `${g.version} and its data stay on this machine, taking as `
                + "much disk space as all your repositories, until you discard "
                + `them. Until then you can go back to ${g.version}, but `
                + `everything written to ${g.target} in the meantime is lost `
                + "when you do.",
            ]),
            el("p", {}, [
                "Choose a passphrase for the backup. It is not your master "
                + "password, and there is no way to recover it.",
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Backup passphrase"]), pp1,
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Repeat it"]), pp2,
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Re-enter your master password"]), masterI,
            ]),
        ],
        // The board export's checks, for the same reasons (devBoardExportDialog).
        validate: () => {
            if (pp1.value.length < 8) return "Passphrase must be at least 8 characters.";
            if (pp1.value !== pp1.value.trim()) {
                return "Passphrase must not start or end with a space.";
            }
            if (pp1.value !== pp2.value) return "Passphrases do not match.";
            if (!masterI.value) return "Re-enter your master password.";
            return null;
        },
        request: async () => fetch("/broker/dev/gitea-upgrade", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                passphrase: pp1.value,
                proof: await deriveLoginProof(masterI.value),
            }),
        }),
        onDone: (ok) => { if (onDone) onDone(ok); },
        focus: () => pp1.focus(),
    });
}

// Delete the Gitea release kept after an upgrade. Step-up: it removes the only
// way back.
function devGiteaDiscardPreviousDialog(view, g, onDone) {
    const up = g.upgrade || {};
    const masterI = el("input", { type: "password", autocomplete: "current-password" });
    mgmtConfirmThenTail(view, {
        title: `Discard Gitea ${up.from_version}`,
        tailTitle: `Discarding Gitea ${up.from_version}`,
        verb: "dev_gitea_discard_previous",
        confirmLabel: "Discard",
        danger: true,
        body: [
            el("p", {}, [
                `Deletes Gitea ${up.from_version}, its copy of the data, and the `
                + "backup taken just before the upgrade. After this there is no "
                + `way back to ${up.from_version}. Do it once ${up.to_version} has `
                + "worked for you.",
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Re-enter your master password"]), masterI,
            ]),
        ],
        validate: () => masterI.value ? null : "Re-enter your master password.",
        request: async () => fetch("/broker/dev/gitea-discard-previous", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ proof: await deriveLoginProof(masterI.value) }),
        }),
        onDone: (ok) => { if (onDone) onDone(ok); },
        focus: () => masterI.focus(),
    });
}

// Go back from a successful upgrade to the release kept beside it. A detached
// broker op (the dev lane), tailed like the upgrade. Step-up: it deletes
// everything written to the new release.
function devGiteaRollbackDialog(view, g, onDone) {
    const up = g.upgrade || {};
    const masterI = el("input", { type: "password", autocomplete: "current-password" });
    mgmtConfirmThenTail(view, {
        title: `Go back to Gitea ${up.from_version}`,
        tailTitle: `Going back to Gitea ${up.from_version}`,
        verb: "dev_gitea_rollback",
        tailMode: "buildlog",
        confirmLabel: "Go back",
        danger: true,
        body: [
            el("p", {}, [
                `Gitea ${up.to_version} is replaced by ${up.from_version}, on its `
                + "data exactly as it was at the moment of the upgrade. "
                + `Everything written to Gitea since then is deleted with ${up.to_version}'s `
                + "data: issues, comments, pull requests, reviews, board changes "
                + "and pushes. Agents keep the commits in their own clones and "
                + "can push them again.",
            ]),
            el("p", {}, [
                "Dev projects, dev boxes and mirrors set up since the upgrade "
                + "lose their Gitea side, so their agents cannot push. Ones "
                + "removed since then come back on the Gitea side, and their "
                + "names cannot be reused. A Gitea password set since the "
                + `upgrade goes back to the earlier one, and a Gitea backup taken `
                + `on ${up.to_version} cannot be restored into ${up.from_version}.`,
            ]),
            el("p", {}, [
                "While it runs Gitea is unavailable, and a running dev box may "
                + "be restarted once afterwards. You can upgrade again later.",
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Re-enter your master password"]), masterI,
            ]),
        ],
        validate: () => masterI.value ? null : "Re-enter your master password.",
        request: async () => fetch("/broker/dev/gitea-rollback", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ proof: await deriveLoginProof(masterI.value) }),
        }),
        onDone: (ok) => { if (onDone) onDone(ok); },
        focus: () => masterI.focus(),
    });
}

// Recover for a go-back that was cut off: Enable finishes it, which deletes
// the work done on the new release, so this dialog says so before it runs.
function devGiteaRecoverRollbackDialog(view, up, onDone) {
    mgmtConfirmThenTail(view, {
        title: `Finish going back to Gitea ${up.from_version}`,
        tailTitle: `Going back to Gitea ${up.from_version}`,
        verb: "dev_gitea_start",
        confirmLabel: "Recover",
        danger: true,
        body: [el("p", {}, [
            `Going back to ${up.from_version} did not finish. Recover puts `
            + `${up.from_version} back on its data from the moment of the upgrade `
            + `and then deletes ${up.to_version}'s data, with everything written `
            + "to it since the upgrade.",
        ])],
        request: () => fetch("/broker/dev/gitea-start", { method: "POST" }),
        onDone: (ok) => { if (onDone) onDone(ok); },
    });
}

// The one-line notice every Gitea-backed page shows while an upgrade, a
// go-back or a restore holds Gitea, instead of a "stopped" card whose Enable
// would only be refused — or, after one was cut off, would finish it without
// the warning Management's Recover gives. Returns true when it rendered.
function devGiteaHeldNotice(container, gitea) {
    if (!gitea) return false;
    const up = gitea.upgrade;
    let text = null;
    if (gitea.upgrading) {
        text = "Gitea is being upgraded, put back or restored. This page works "
            + "again when that finishes.";
    } else if (up && up.phase === "damaged") {
        text = "Gitea's upgrade record is damaged, so it is not safe to tell which "
            + "copy of Gitea's data is current. This page works again once the "
            + "record is repaired.";
    } else if (up && up.phase !== "committed") {
        text = "An earlier Gitea upgrade or going back did not finish. Settle it "
            + "under Sandboxes → Infrastructure first.";
    }
    if (!text) return false;
    container.appendChild(el("div", { class: "mgmt-empty" }, [el("span", {}, [text])]));
    return true;
}

// Set the sandbox-admin gitea password (Sandboxes → Infrastructure). Step-up:
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
// The expected-stage checklist for one op box: every OP_CHECKLISTS row up front
// (pending), each flipped to ✓ by markDone as its milestone lands.
function opChecklist(verb) {
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
    const markDone = (key, msg) => {
        // A conditional stage not pre-listed (e.g. update enable/disable) appends
        // already-checked as it arrives, labelled by its message when it has one
        // (a raw step key reads as jargon: "recover", "rollback").
        const ref = (items[key] && !items[key].dropped) ? items[key] : addRow(key, msg || key);
        ref.row.classList.remove("pending");
        ref.row.classList.add("ok");
        ref.icon.textContent = "✓";
    };
    // One row per progress MESSAGE (a verb whose lines are a report): a line that
    // says something was not done is marked failed, so a partial outcome never
    // reads as all green.
    const markMsg = (msg) => {
        // "NOT refreshed" / "could not", or a summary counting at least one box not refreshed
        const bad = /NOT refreshed|could not|[1-9]\d* not refreshed/.test(msg);
        const ref = addRow("msg-" + Object.keys(items).length, msg);
        ref.row.classList.remove("pending");
        ref.row.classList.add(bad ? "failed" : "ok");
        ref.icon.textContent = bad ? "✗" : "✓";
    };
    // One row per `warning` step (e.g. a box that started without one of its
    // read-only folders): its message, marked ⚠ — the op still succeeded.
    // A unique row key each, so two warnings never merge into one row.
    const markWarn = (msg) => {
        const ref = addRow("warn-" + Object.keys(items).length, msg);
        // Above any stage still pending, so it reads in the order it happened.
        const pending = listEl.querySelector(".op-check.pending:not(:last-child)");
        if (pending && pending !== ref.row) listEl.insertBefore(ref.row, pending);
        ref.row.classList.remove("pending");
        ref.row.classList.add("warn");
        ref.icon.textContent = "⚠";
    };
    // A `note` step: an informational outcome line (e.g. "History: on …"),
    // its message as a ✓ row of its own.
    const markNote = (msg) => {
        const ref = addRow("note-" + Object.keys(items).length, msg);
        const pending = listEl.querySelector(".op-check.pending:not(:last-child)");
        if (pending && pending !== ref.row) listEl.insertBefore(ref.row, pending);
        ref.row.classList.remove("pending");
        ref.row.classList.add("ok");
        ref.icon.textContent = "✓";
    };
    // Called once a SUCCESSFUL op's terminal record has been read (every
    // milestone before it is drained by then): a listed stage still pending was
    // not needed on this path, so its row is not part of what happened. A failed
    // or interrupted op keeps them — there they mean "stopped before this". The
    // entries stay in `items` (marked dropped) so the row-key counters never
    // shrink; a later markDone on a dropped key would append a fresh row.
    const dropPending = () => {
        for (const ref of Object.values(items)) {
            if (ref.row.classList.contains("pending")) { ref.row.remove(); ref.dropped = true; }
        }
    };
    return { listEl, markDone, markMsg, markWarn, markNote, dropPending };
}

async function mgmtTailOp(view, backdrop, card, opId, title, verb, onDone) {
    const { listEl, markDone, markWarn, markNote, dropPending } = opChecklist(verb);

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
        rerenderView(view);
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
                else if (rec.step === "warning" && rec.msg) markWarn(rec.msg);
                else if (rec.step === "note" && rec.msg) markNote(rec.msg);
                else markDone(rec.step, rec.msg);
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
    // Only once the log's own terminal was read: status alone can say ok while
    // the last drain failed, and a stage that ran must not be swept unread.
    if (ok && sawTerminal) dropPending();
    if (!ok) failEl.textContent = "Failed — " + mgmtOpFailMsg(result);
    doneBtn.textContent = "Done";
    doneBtn.disabled = false;
    if (onDone) { try { await onDone(ok, result); } catch (e) { /* best-effort */ } }
}

// ---- lifecycle lane: start / stop / update in the background ----------------
// The broker runs a project start/stop/update in a detached child and answers at
// once, one action at a time host-wide (a later one waits its turn — the child
// writes a `queue` milestone while it waits). The op lives only in the broker's
// view log: completion is TERMINAL-FIRST from that log, with /alive as the
// escape for a child killed before it wrote one (the mgmtTailBuildLog
// contract). The op box can be sent to the background; a watcher then carries
// the op to its end, and a corner notice reports the outcome.
const LIFECYCLE_LANE_VERBS = new Set(["start", "stop", "update", "agents_update"]);
const LIFECYCLE_LABELS = { start: "starting…", stop: "stopping…", update: "updating…",
                           agents_update: "updating agents…" };
const LIFECYCLE_TITLES = { start: "Starting", stop: "Stopping", update: "Updating",
                           agents_update: "Updating agents" };

// In-flight registry, keyed by project (the broker refuses a second action on a
// project that already has one, so one entry per project is exact). Value:
// {opId, verb, queued, modal, watching, stalled, onDone}. `modal` = an open op
// box is tailing it; `watching` = a background watcher is. A 401/403 stalls the
// watcher (entry kept, `stalled`), and the next seed restarts it.
const lifecycleInFlight = new Map();

function lifecycleLabel(entry) {
    return entry.queued ? "queued…" : (LIFECYCLE_LABELS[entry.verb] || "working…");
}

function lifecycleRegister(project, opId, verb, onDone) {
    const entry = { opId, verb, queued: false, modal: false, watching: false,
                    stalled: false, onDone: onDone || null };
    lifecycleInFlight.set(project, entry);
    paintLifecycleLabels();
    return entry;
}

function lifecycleSetQueued(project, opId, queued) {
    const entry = lifecycleInFlight.get(project);
    if (!entry || entry.opId !== opId || entry.queued === queued) return;
    entry.queued = queued;
    paintLifecycleLabels();
}

// Repaint every surface that shows an in-flight action: the rail row's tag and
// the Management row (state cell + its action buttons, disabled meanwhile — the
// broker would refuse them anyway). A Management row whose action ended while
// the table could not be re-rendered (a dialog was open) says so and keeps its
// buttons off: its render-time buttons describe the project BEFORE the action.
function paintLifecycleLabels() {
    for (const row of document.querySelectorAll(".project-rail .project[data-name]")) {
        const line = row.querySelector(".project-status-line");
        if (!line) continue;
        const entry = lifecycleInFlight.get(row.getAttribute("data-name"));
        let tag = line.querySelector(".project-inflight");
        if (!entry) { if (tag) tag.remove(); continue; }
        if (!tag) {
            tag = el("span", { class: "project-inflight" });
            const badge = line.querySelector(".type-badge");
            if (badge) badge.after(tag); else line.prepend(tag);
        }
        tag.textContent = lifecycleLabel(entry);
    }
    for (const row of document.querySelectorAll(".mgmt-prow[data-project]")) {
        const stateEl = row.querySelector(".mgmt-state");
        if (!stateEl) continue;
        const entry = lifecycleInFlight.get(row.getAttribute("data-project"));
        const buttons = row.querySelectorAll(".mgmt-actions button");
        if (entry) {
            stateEl.textContent = lifecycleLabel(entry);
            stateEl.classList.add("state-inflight");
            buttons.forEach((b) => { b.disabled = true; });
            row.setAttribute("data-inflight", "1");
        } else if (row.getAttribute("data-inflight")) {
            stateEl.textContent = "changed — Refresh";
            row.removeAttribute("data-inflight");
        }
    }
}

// Read one lane op's new view-log records from st.from. `onRecord` gets every
// non-terminal milestone; a terminal sets st.terminal = {ok, msg}. Returns the
// HTTP status on 401/403 (the caller decides what an expired session means),
// else 0 — transient failures are 0 too, the caller just polls again.
async function lifecycleDrain(st, onRecord) {
    let r;
    try {
        r = await fetch(`/broker/op/${encodeURIComponent(st.opId)}/log?from=${st.from}`);
    } catch (e) { return 0; }
    if (r.status === 401 || r.status === 403) return r.status;
    let b; try { b = await r.json(); } catch (e) { return 0; }
    if (!b || b.started === false || !b.data) return 0;
    st.from = b.next;
    for (const line of b.data.split("\n")) {
        if (!line.trim()) continue;
        let rec; try { rec = JSON.parse(line); } catch (e) { continue; }
        if (rec.status === "done") st.terminal = { ok: true, msg: rec.msg || "" };
        else if (rec.status === "failed") st.terminal = { ok: false, msg: rec.msg || "" };
        else onRecord(rec);
    }
    return 0;
}

// true / false from the broker's liveness probe; null when it could not answer
// (transient, or a session problem the next drain will surface).
async function lifecycleAlive(opId) {
    try {
        const r = await fetch(`/broker/op/${encodeURIComponent(opId)}/alive`);
        if (!r.ok) return null;
        const b = await r.json();
        return b.ok ? !!b.alive : null;
    } catch (e) { return null; }
}

// Poll one op to its end: drain, then — no terminal yet — ask /alive; the child
// writes its terminal BEFORE it drops its lock, so alive:false means either the
// terminal just landed (drain once more) or the child died without one
// (interrupted). Returns {ok, msg, interrupted} or {auth: <status>} or null when
// `stop()` says to give up (the op box was dismissed / the entry superseded).
async function lifecyclePoll(st, onRecord, stop) {
    while (true) {
        if (stop()) return null;
        const auth = await lifecycleDrain(st, onRecord);
        if (stop()) return null;
        if (auth) return { auth };
        if (st.terminal) break;
        const alive = await lifecycleAlive(st.opId);
        if (stop()) return null;
        if (alive === false) {
            await lifecycleDrain(st, onRecord);
            if (stop()) return null;
            break;
        }
        await opSleep(OP_POLL_INTERVAL_MS);
    }
    if (!st.terminal) return { ok: false, msg: "", interrupted: true };
    return { ok: st.terminal.ok, msg: st.terminal.msg, interrupted: false };
}

function lifecycleFailText(outcome) {
    if (outcome.interrupted) return "Interrupted — the action stopped without finishing.";
    return "Failed — " + (outcome.msg || "operation failed");
}

// The one completion path (op box or watcher): drop the entry, repaint, run the
// caller's hook, re-sync the sidebar (a started project joins it). A watcher's
// completion also posts the corner notice and refreshes Management when it is
// the open page — but never under an open dialog, and never while the page
// holds the operator's own work (focus or typed text in one of its fields, e.g.
// a half-filled pass-through claim): a re-render starts the page from scratch.
// Skipped, the finished row reads "changed — Refresh" (paintLifecycleLabels).
function lifecycleFinish(project, opId, outcome, background) {
    const entry = lifecycleInFlight.get(project);
    if (!entry || entry.opId !== opId) return;
    lifecycleInFlight.delete(project);
    paintLifecycleLabels();
    if (background) showLifecycleNotice(project, entry.verb, opId, outcome);
    if (entry.onDone) {
        try { Promise.resolve(entry.onDone(outcome.ok)).catch(() => {}); }
        catch (e) { /* the caller's hook is best-effort */ }
    }
    syncSidebarFromBroker();
    if (background && state.hostPage === "sandboxes" && sandboxesActiveTab === "management"
            && !document.querySelector(".modal-backdrop")) {
        const view = hostBody();
        if (view && !mgmtViewHoldsInput(view)) renderManagementInto(view);
    }
}

function mgmtViewHoldsInput(view) {
    const active = document.activeElement;
    // The table's own Sort by selector (phone) keeps focus after a pick; it is
    // a view control, not the operator's typing, so it never holds a refresh.
    if (active && active !== document.body && view.contains(active)
            && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)
            && !active.classList.contains("mgmt-sort-select")) return true;
    return Array.from(view.querySelectorAll("input:not([type=checkbox]):not([type=radio]), textarea"))
        .some((i) => i.value !== "");
}

// Background watcher for one in-flight entry (a dismissed op box, or an op this
// page learned from GET /broker/lifecycle). The double-start guard is
// `watching`, set before the first await.
async function lifecycleWatch(project) {
    const entry = lifecycleInFlight.get(project);
    if (!entry || entry.watching || entry.modal) return;
    entry.watching = true;
    entry.stalled = false;
    const opId = entry.opId;
    const st = { opId, from: 0, terminal: null };
    const res = await lifecyclePoll(
        st,
        (rec) => lifecycleSetQueued(project, opId, rec.step === "queue"),
        () => lifecycleInFlight.get(project) !== entry);
    if (res === null) return;                   // superseded
    if (res.auth) { entry.watching = false; entry.stalled = true; return; }
    lifecycleFinish(project, opId, res, true);
}

// Learn every action in flight on the broker (after the broker login, and on
// each Management render). Entries this page already tracks keep their state;
// stalled watchers restart. 401/503 → nothing (Management is opt-in).
async function seedLifecycleFromBroker() {
    let res;
    try { res = await fetch("/broker/lifecycle"); } catch (e) { return; }
    if (!res.ok) return;
    let body; try { body = await res.json(); } catch (e) { return; }
    const ops = (body && body.ok && body.result && Array.isArray(body.result.ops))
        ? body.result.ops : [];
    for (const op of ops) {
        if (!op || typeof op.project !== "string" || typeof op.op_id !== "string"
                || !LIFECYCLE_LANE_VERBS.has(op.verb)) continue;
        const cur = lifecycleInFlight.get(op.project);
        if (cur) continue;               // tracked already (or another op of ours)
        const entry = lifecycleRegister(op.project, op.op_id, op.verb, null);
        entry.queued = op.state === "queued";
        lifecycleWatch(op.project);
    }
    for (const [project, entry] of lifecycleInFlight) {
        if (entry.stalled && !entry.watching && !entry.modal) lifecycleWatch(project);
    }
    paintLifecycleLabels();
}

// Corner notice for an action that finished in the background. It stays until
// dismissed (an outcome nobody saw is the failure this exists to prevent);
// clicking it runs `onOpen` (reopen the op box, already complete, with the
// reason) — unless another dialog is open.
function showOpNotice(text, ok, onOpen) {
    let host = document.getElementById("op-notices");
    if (!host) {
        host = el("div", { id: "op-notices", class: "op-notices" });
        document.body.appendChild(host);
    }
    const body = el("button", { class: "op-notice-body", title: "Show the details" }, [text]);
    const close = el("button", { class: "op-notice-close", title: "Dismiss",
                                 "aria-label": "Dismiss" }, ["×"]);
    const notice = el("div", { class: "op-notice" + (ok ? "" : " failed"),
                               role: "status" }, [body, close]);
    close.onclick = () => notice.remove();
    body.onclick = () => {
        if (document.querySelector(".modal-backdrop")) return;
        notice.remove();
        onOpen();
    };
    host.appendChild(notice);
}

function showLifecycleNotice(project, verb, opId, outcome) {
    const past = { start: "started", stop: "stopped", update: "updated",
                   agents_update: "agents updated" }[verb] || "finished";
    const noun = { agents_update: "agent update" }[verb] || verb;
    const failed = verb === "agents_update" ? `${project}: the agent update failed`
                                             : `${project} failed to ${verb}`;
    const text = outcome.interrupted
        ? `${project}: the ${noun} was interrupted`
        : outcome.ok ? `${project} ${past}` : failed;
    showOpNotice(text, outcome.ok, () => {
        const backdrop = el("div", { class: "modal-backdrop" });
        const card = el("div", { class: "card" });
        backdrop.appendChild(card);
        document.body.appendChild(backdrop);
        lifecycleTail(null, backdrop, card, opId,
                      `${LIFECYCLE_TITLES[verb] || "Working on"} ${project}`,
                      verb, project, null, { replay: true });
    });
}

// What ticking "Share files" means — shown under the card in the box window
// while it is ticked. A box that does not share sees no shared/ at all. A
// mailbox folder that already exists under the name is adopted (kept from a
// sharing box of that name removed earlier).
const BOX_SHARE_HINT =
    "The box gets a mailbox, /workspace/shared/<name>/, which the project " +
    "agent, the workers and the other boxes that share files read; it is " +
    "kept when the box is removed, and a box added later under the same name " +
    "writes into it again. The box reads the project agent's mailbox and the " +
    "other sharing boxes' mailboxes, never the workers' (tick shared as a " +
    "read-only folder for that). Set when the box is created; to change it, " +
    "remove the box and add it again.";
// The ⚙ panel's line naming the boxes that share files.
function boxShareLine(rows) {
    const names = (rows || []).filter((x) => x && x.share === true).map((x) => x.name);
    return names.length
        ? "Shares files with the project: " + names.join(", ") + "."
        : "No box here shares files with the project.";
}

// The ⚙ Project folders line under its field (the long form rides the ⓘ).
const PROJECT_FOLDERS_SHORT =
    "Paths in RS's data folder, comma-separated. Read-only in the project " +
    "at /workspace/mnt/<name>/.";

// The one line shown under a box's folder field (box window and ⚙ Box
// folders); the full explanation below rides the section title's ⓘ.
const BOX_FOLDERS_SHORT =
    "Paths in the project, comma-separated. Read-only in the box at /mnt/<path>.";

// What a box's read-only folder means for the operator — the ⓘ text beside
// the folder section's title in the box window and the ⚙ Box folders section.
const BOX_FOLDERS_HINT =
    "Comma-separated paths relative to the project's workspace, for example " +
    "data, results/run3. Each appears in the box under /mnt/<path>, " +
    "read-only. Everything in it can be read inside the box, including any " +
    "logins or tokens it holds. Give shared to show the box every mailbox " +
    "and the workers' output. Inside boxes, workers, shared, .role-mcps and " +
    ".orchestrator (other agents write there) and inside a folder under mnt " +
    "(a host folder), only the whole folder can be given.";

// A small ⓘ beside a section title. Its text shows on mouseover (the title
// attribute) and, since a touch screen has no hover, a tap or Enter unfolds it
// in `detail`, which the caller places under the title. Returns both nodes.
function infoIcon(text) {
    const detail = el("div", { class: "info-detail" }, [text]);
    detail.style.display = "none";
    const toggle = (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        detail.style.display = detail.style.display === "none" ? "" : "none";
    };
    const icon = el("span", { class: "info-icon", title: text, tabindex: "0",
                              role: "button", "aria-label": "More information",
                              onclick: toggle,
                              onkeydown: (ev) => {
                                  if (ev.key === "Enter" || ev.key === " ") toggle(ev);
                              } }, ["ⓘ"]);
    return { icon, detail };
}

// A bordered box (the box window's Settings look, or `baseClass`'s) whose title
// is a button that folds its contents away; it opens collapsed. Deliberately
// NOT a `.field`: its label/input rules would restyle the tickboxes inside (the
// `.field` bleed). Collapsing only hides: every control keeps its value and is
// still sent.
function collapsibleBox(title, children, extraClass, baseClass) {
    const caret = el("span", { class: "box-collapse-caret" }, ["▸"]);
    const head = el("button", { type: "button", class: "box-collapse-head",
                                "aria-expanded": "false" }, [caret, title]);
    const box = el("div", { class: (baseClass || "box-settings") + " box-collapsible" +
                                   (extraClass ? " " + extraClass : "") }, [
        head,
        el("div", { class: "box-collapse-body" }, children),
    ]);
    head.onclick = () => setCollapsibleOpen(box, !box.classList.contains("open"));
    return box;
}

// Open or fold a collapsibleBox, keeping its caret and aria-expanded in step.
function setCollapsibleOpen(box, open) {
    if (box.classList.contains("open") !== open) box.classList.toggle("open");
    const head = box.children[0];
    head.children[0].textContent = open ? "▾" : "▸";
    head.setAttribute("aria-expanded", open ? "true" : "false");
}

// A comma-separated folder field → its trimmed, non-empty entries. The server
// is the gate (it normalises and refuses); this only splits.
function parseFolderList(text) {
    return String(text || "").split(",").map((t) => t.trim()).filter((t) => t);
}

// A courtesy check before the round trip: a folder path is relative. Returns
// the operator line for the first absolute entry, or null.
function folderListProblem(paths) {
    const bad = paths.find((p) => p.startsWith("/") || p.startsWith("~"));
    return bad ? "Folder paths are relative to the project's workspace (got: " +
                 bad + ")." : null;
}

// The project's top-level folders as an example line under a folder field
// (the server lists them; an unusable name is left out).
function folderChoicesLine(choices, error) {
    if (error) return error;
    const names = (choices || []).filter((c) => c.usable).map((c) => c.name);
    return names.length ? "Folders at the top of this project: " + names.join(", ")
                        : "This project has no folders yet.";
}

// Phase 2 of a start/stop/update op box. Same checklist as mgmtTailOp, plus a
// "Continue in background" button (and a backdrop click) that closes the box
// while the action carries on under a watcher. `opts.replay` re-shows a FINISHED
// op from its log (the notice's click): no registry, no hooks, just the record.
async function lifecycleTail(view, backdrop, card, opId, title, verb, project,
                             onDone, opts) {
    const replay = !!(opts && opts.replay);
    const { listEl, markDone, markMsg, markWarn, dropPending } = opChecklist(verb);
    const waitEl = el("div", { class: "op-waiting" });
    const failEl = el("div", { class: "op-fail" });
    const bgBtn = el("button", { class: "btn btn-secondary" }, ["Continue in background"]);
    const doneBtn = el("button", { class: "btn", disabled: "" }, [replay ? "…" : "Working…"]);
    const entry = (!replay && project) ? lifecycleRegister(project, opId, verb, onDone) : null;
    if (entry) entry.modal = true;
    let finished = false, dismissed = false;
    const dismiss = () => {
        if (finished || dismissed) return;
        dismissed = true;
        backdrop.remove();
        if (entry) { entry.modal = false; lifecycleWatch(project); }
    };
    bgBtn.onclick = dismiss;
    backdrop.onclick = (e) => { if (e.target === backdrop) dismiss(); };
    doneBtn.onclick = () => {
        if (doneBtn.disabled) return;
        backdrop.remove();
        if (!replay && view) rerenderView(view);
    };
    card.innerHTML = "";
    card.appendChild(el("h2", {}, [title]));
    card.appendChild(listEl);
    card.appendChild(waitEl);
    card.appendChild(failEl);
    card.appendChild(el("div", { class: "btn-row" }, replay ? [doneBtn] : [bgBtn, doneBtn]));

    const st = { opId, from: 0, terminal: null };
    const res = await lifecyclePoll(st, (rec) => {
        if (rec.step === "queue") {
            waitEl.textContent = rec.msg || "waiting for another project action to finish";
            if (entry) lifecycleSetQueued(project, opId, true);
            return;
        }
        waitEl.textContent = "";
        if (entry) lifecycleSetQueued(project, opId, false);
        if (rec.step === "warning" && rec.msg) { markWarn(rec.msg); return; }
        if (verb === "agents_update" && rec.msg) markMsg(rec.msg);
        else markDone(rec.step, rec.msg);
    }, () => dismissed);
    if (res === null) return;                   // sent to the background
    if (res.auth) {
        // Session expired mid-tail: the action carries on at the broker. Keep
        // the entry (stalled) so the next seed after re-login picks it back up.
        backdrop.remove();
        if (entry) { entry.modal = false; entry.stalled = true; }
        if (replay || !view) return;
        const redirect = mgmtStatusRedirect(view, res.auth);
        return redirect ? redirect() : undefined;
    }
    finished = true;
    backdrop.onclick = null;
    waitEl.textContent = "";
    bgBtn.remove();
    // res.ok comes only from a read terminal `done`, so every stage is drained.
    if (res.ok) dropPending();
    if (!res.ok) failEl.textContent = lifecycleFailText(res);
    doneBtn.textContent = "Done";
    doneBtn.disabled = false;
    if (entry) { entry.modal = false; lifecycleFinish(project, opId, res, false); }
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
        if (LIFECYCLE_LANE_VERBS.has(cfg.verb)) {
            // start/stop/update run on the broker's lifecycle lane: no OP_RUNS
            // entry, terminal-first tail, and the box can be sent to the
            // background while the action carries on.
            await lifecycleTail(view, backdrop, card, body.op_id,
                                cfg.tailTitle || cfg.title, cfg.verb,
                                cfg.project || null, cfg.onDone);
        } else if (tailMode === "buildlog") {
            await mgmtTailBuildLog(view, backdrop, card, body.op_id,
                                   cfg.tailTitle || cfg.title,
                                   () => { if (cfg.onDone) cfg.onDone(true); });
        } else {
            await mgmtTailOp(view, backdrop, card, body.op_id,
                             cfg.tailTitle || cfg.title, cfg.checklist || cfg.verb,
                             cfg.onDone);
        }
    };
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    if (cfg.focus) setTimeout(() => cfg.focus(), 50);
    return backdrop;
}

// ---- software: builds in flight -------------------------------------------
// The broker runs one software build at a time; a later one waits its turn
// (its view log carries a `queue` milestone while it waits), and a second build
// of the SAME target is refused. The build box can be sent to the background;
// a watcher then carries the op to its end and a corner notice reports it. The
// registry is keyed by target — the unit the broker's refusal keys on, so one
// entry per target is exact. Value: {opId, verb, agent, queued, modal,
// watching, stalled}; `modal` = an open box is tailing it, `watching` = a
// background watcher is. A 401/403 stalls the watcher (entry kept) and the next
// seed restarts it.
const softwareInFlight = new Map();
// Op ids this page saw finish: a seed racing a finishing child (holder gone,
// per-op lock not yet) must not revive one and post a second notice.
const softwareFinished = new Set();
// Targets whose Refresh check was closed before it answered: their Refresh
// stays off until it does, so checks cannot pile up on the broker.
const softwareChecking = new Set();

// Mirrors the broker's _build_target (same default agent as the verb).
function swTarget(verb, agent) {
    if (verb === "agent_pull" || verb === "agent_refresh") return "agent:" + (agent || "claude");
    if (verb === "editor_pull" || verb === "editor_refresh") return "editor";
    if (verb === "reader_pull" || verb === "reader_refresh") return "reader";
    if (verb === "node_pull") return "node";
    if (verb === "rebuild") return "fleet";
    if (verb === "mcp_gateway_start") return "mcp-gateway";
    return verb;
}

function swTargetName(target) {
    if (target.startsWith("agent:")) return target.slice("agent:".length) + " agent";
    return { editor: "editor", reader: "reader", node: "node", fleet: "image fleet",
             "mcp-gateway": "MCP gateway" }[target] || target;
}

function swLabel(entry) {
    if (entry.queued) return "queued…";
    if (entry.verb === "rebuild") return "rebuilding the image fleet…";
    if (entry.verb === "mcp_gateway_start") return "starting…";
    return /_refresh$/.test(entry.verb) ? "refreshing…" : "pulling…";
}

function swRegister(target, opId, verb, agent) {
    const entry = { opId, verb, agent: agent || null, queued: false, modal: false,
                    watching: false, stalled: false };
    softwareInFlight.set(target, entry);
    paintSoftwareLabels();
    return entry;
}

function swSetQueued(target, opId, queued) {
    const entry = softwareInFlight.get(target);
    if (!entry || entry.opId !== opId || entry.queued === queued) return;
    entry.queued = queued;
    paintSoftwareLabels();
}

// Repaint every Software surface stamped with a target: a dist row (its Status
// cell + its Pull/Refresh, off while that target is in flight — the broker
// would refuse them) and the fleet line under the header (+ Rebuild all). A
// row whose build ended while the page could not re-render says so and keeps
// its buttons off: its render-time status describes the dist BEFORE the build.
function paintSoftwareLabels() {
    for (const row of document.querySelectorAll("[data-swtarget]")) {
        const target = row.getAttribute("data-swtarget");
        const stateEl = row.querySelector(".sw-status");
        const buttons = target === "fleet"
            ? document.querySelectorAll(".sw-fleet-btn")
            : row.querySelectorAll(".sw-act button");
        const entry = softwareInFlight.get(target);
        if (entry) {
            if (stateEl) stateEl.textContent = swLabel(entry);
            buttons.forEach((b) => {
                b.disabled = true;
                b.title = entry.queued ? "Waiting for another build to finish"
                                       : "This is being built right now";
            });
            row.setAttribute("data-inflight", "1");
            row.removeAttribute("data-swfinished");
        } else if (row.getAttribute("data-inflight")) {
            // Not "Refresh" alone: each row has a Refresh button of its own,
            // which starts a version check — the page's is the one meant here.
            if (stateEl && target === "fleet") {
                stateEl.textContent = "The image fleet rebuild finished — press Refresh at the top of the page to see it.";
            } else if (stateEl) {
                stateEl.textContent = "finished";
                stateEl.title = "Press Refresh at the top of the page to see the new version";
            }
            row.removeAttribute("data-inflight");
            row.setAttribute("data-swfinished", "1");
        }
        if (target !== "fleet") {
            const refresh = row.querySelector(".sw-refresh-btn");
            if (refresh && softwareChecking.has(target)) {
                refresh.disabled = true;
                refresh.title = "Still checking upstream";
            }
        }
    }
}

// The Software tab's body, when it is on screen (Sandboxes → Software).
function softwareView() {
    if (state.hostPage !== "sandboxes" || sandboxesActiveTab !== "software") return null;
    return hostBody();
}

// Re-show a finished build from its logs (the notice's click): read-only, no
// registry, Done just closes it.
function softwareReplay(opId, title) {
    if (document.querySelector(".modal-backdrop")) return;
    const backdrop = el("div", { class: "modal-backdrop" });
    const card = el("div", { class: "card sw-build-card" });
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    const view = softwareView();
    mgmtTailBuildLog(view, backdrop, card, opId, title, () => {},
                     view ? renderSoftwareInto : undefined);
}

function softwareTitle(verb, target) {
    if (verb === "rebuild") return "Rebuilding the image fleet";
    if (verb === "mcp_gateway_start") return MCP_GATEWAY_START_TITLE;
    return (/_refresh$/.test(verb) ? "Refreshing " : "Pulling ") + swTargetName(target);
}

function softwareNoticeText(entry, target, outcome) {
    const name = swTargetName(target);
    const what = entry.verb === "rebuild" ? "rebuild" : /_refresh$/.test(entry.verb) ? "refresh" : "pull";
    if (entry.verb === "rebuild") {
        if (outcome.interrupted) return "The image fleet rebuild was interrupted";
        return outcome.ok ? "Image fleet rebuilt" : "The image fleet rebuild failed";
    }
    if (entry.verb === "mcp_gateway_start") {
        if (outcome.interrupted) return "Starting the MCP gateway was interrupted";
        return outcome.ok ? "MCP gateway started" : "The MCP gateway could not be started";
    }
    if (outcome.interrupted) return `${name}: the ${what} was interrupted`;
    return outcome.ok ? `${name} ${what === "refresh" ? "refreshed" : "pulled"}`
                      : `${name}: the ${what} failed`;
}

// The one completion path (open box or watcher): drop the entry, repaint. A
// watcher's completion also posts the corner notice and refreshes the Software
// section when it is on screen — but never under an open dialog, and never
// while the section holds the operator's own input.
function softwareFinish(target, opId, outcome, background) {
    const entry = softwareInFlight.get(target);
    if (!entry || entry.opId !== opId) return;
    softwareInFlight.delete(target);
    softwareFinished.add(opId);
    paintSoftwareLabels();
    if (!background) return;
    showOpNotice(softwareNoticeText(entry, target, outcome), outcome.ok,
                 () => softwareReplay(opId, softwareTitle(entry.verb, target)));
    const view = softwareView();
    if (view && !document.querySelector(".modal-backdrop") && !mgmtViewHoldsInput(view)) {
        renderSoftwareInto(view);
    }
}

// Background watcher for one in-flight build (a dismissed box, or a build this
// page learned from GET /broker/software/active). The double-start guard is
// `watching`, set before the first await.
async function softwareWatch(target) {
    const entry = softwareInFlight.get(target);
    if (!entry || entry.watching || entry.modal) return;
    entry.watching = true;
    entry.stalled = false;
    const opId = entry.opId;
    const st = { opId, from: 0, terminal: null };
    const res = await lifecyclePoll(
        st,
        (rec) => swSetQueued(target, opId, rec.step === "queue"),
        () => softwareInFlight.get(target) !== entry);
    if (res === null) return;                   // superseded
    if (res.auth) { entry.watching = false; entry.stalled = true; return; }
    softwareFinish(target, opId, res, true);
}

// Learn every build in flight on the broker (after the broker login, and on
// each Software render). Entries this page already tracks keep their state;
// stalled watchers restart. 401/503 → nothing (Management is opt-in).
async function seedSoftwareFromBroker() {
    let res;
    try { res = await fetch("/broker/software/active"); } catch (e) { return; }
    if (!res.ok) return;
    let body; try { body = await res.json(); } catch (e) { return; }
    const ops = (body && body.ok && body.result && Array.isArray(body.result.ops))
        ? body.result.ops : [];
    for (const op of ops) {
        if (!op || typeof op.target !== "string" || typeof op.op_id !== "string"
                || typeof op.verb !== "string" || softwareFinished.has(op.op_id)) continue;
        if (softwareInFlight.get(op.target)) continue;   // tracked already
        const entry = swRegister(op.target, op.op_id, op.verb, op.agent);
        entry.queued = op.state === "queued";
        softwareWatch(op.target);
    }
    for (const [target, entry] of softwareInFlight) {
        if (entry.stalled && !entry.watching && !entry.modal) softwareWatch(target);
    }
    paintSoftwareLabels();
}

// Phase 2 of a build box: the streaming log tail, closable. Registers the op
// so the page shows it in flight; dismissing hands it to a watcher; an end
// reached while the box is open drops it (no notice — the operator saw it).
async function softwareTail(view, backdrop, card, opId, title, target, payload) {
    const entry = swRegister(target, opId, payload.verb, payload.agent);
    entry.modal = true;
    let ended = false;
    const dismiss = () => {
        backdrop.remove();
        if (ended) {
            // Closed after the end by a click outside: land where Done lands,
            // or the finished row keeps its stale version and its buttons off.
            if (view && view.isConnected) renderSoftwareInto(view);
            return;
        }
        if (!entry.modal) return;
        entry.modal = false;
        softwareWatch(target);
    };
    await mgmtTailBuildLog(view, backdrop, card, opId, title, undefined,
                           renderSoftwareInto, {
        onDismiss: dismiss,
        onRecord: (rec) => swSetQueued(target, opId, rec.step === "queue"),
        onEnd: (outcome) => {
            ended = true;
            entry.modal = false;
            softwareFinish(target, opId, outcome, false);
        },
    });
    // Session expired mid-tail: the build carries on at the broker. Keep the
    // entry (stalled) so the next seed after re-login picks it back up.
    if (!ended && entry.modal) { entry.modal = false; entry.stalled = true; }
}

// ---- software: build lane (pull / rebuild) ---------------------------------
// A build runs on the broker's DETACHED child (F2 Slice 2): the POST returns an
// op_id at once, then we stream the RAW build log from /fulllog (the authenticated
// relay of the host-only full log) into a scrolling <pre>, and read completion
// TERMINAL-FIRST from the view-log (/log). We deliberately do NOT consult
// GET /broker/op/<id> — it returns "unknown" for a build op by design (no OP_RUNS
// entry), which is not-failure; the child's op.progress.done()/fail() in the
// view-log is the completion signal.
async function mgmtTailBuildLog(view, backdrop, card, opId, title, onDone,
                                onLogin, opts) {
    opts = opts || {};
    const phaseEl = el("div", { class: "op-phase" }, ["starting…"]);
    const pre = el("pre", { class: "sw-buildlog" }, [""]);
    const failEl = el("div", { class: "op-fail" });
    const doneBtn = el("button", { class: "btn", disabled: "" }, ["Working…"]);
    doneBtn.onclick = () => {
        if (doneBtn.disabled) return;
        backdrop.remove();
        // Default: the Sandboxes → Software tab (the original build-lane caller); the dev
        // review dialog passes a close that leaves the page as it is.
        if (onDone) onDone(); else renderSoftwareInto(view);
    };
    // Dismissable mode (opts.onDismiss — the review dialog and the Software
    // page's builds): a "Continue in background" button and a backdrop click
    // close the modal and hand the op to the caller's background watcher;
    // `cancelled` exits the loop without touching onDone or the DOM. The
    // button goes at the end (nothing left to continue); the backdrop click
    // stays the caller's, whose handler knows whether the op already ended.
    // Dev-box / repo-remove tails pass no opts and keep modal-until-done.
    let cancelled = false;
    const bgBtn = opts.onDismiss
        ? el("button", { class: "btn btn-secondary" }, ["Continue in background"]) : null;
    if (opts.onDismiss) {
        bgBtn.onclick = () => { cancelled = true; opts.onDismiss(); };
        backdrop.onclick = (e) => {
            if (e.target !== backdrop) return;
            cancelled = true;
            opts.onDismiss();
        };
    }
    card.innerHTML = "";
    card.appendChild(el("h2", {}, [title]));
    card.appendChild(phaseEl);
    card.appendChild(pre);
    card.appendChild(failEl);
    card.appendChild(el("div", { class: "btn-row" }, bgBtn ? [bgBtn, doneBtn] : [doneBtn]));

    let logFrom = 0, viewFrom = 0, done = false, ok = false, sawTerminal = false, interrupted = false;
    let failMsg = "";
    // A replay from a corner notice may have no page behind it (view null):
    // an expired session then just closes the box — there is no view to land
    // a login card in.
    const statusRedirect = (status) => {
        if (view) return mgmtStatusRedirect(view, status, onLogin);
        return (status === 401 || status === 403 || status === 503) ? () => undefined : null;
    };
    // Liveness probe (the op's per-op lock, via build_alive) — the
    // terminal-first tail has no other signal, so a hard-killed child (OOM /
    // kill -9 / power-loss) that never wrote a terminal would spin the modal
    // forever. null on a transient error → assume still alive and keep
    // polling; only an explicit false ends the wait.
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
        const redirect = statusRedirect(r.status);
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
        const redirect = statusRedirect(r.status);
        if (redirect) return redirect;
        const b = await r.json();
        if (b.started !== false && b.data) {
            viewFrom = b.next;
            for (const line of b.data.split("\n")) {
                if (!line.trim()) continue;
                let rec; try { rec = JSON.parse(line); } catch (e) { continue; }
                if (rec.status === "done") { sawTerminal = true; ok = true; }
                else if (rec.status === "failed") {
                    sawTerminal = true; ok = false;
                    // The broker's failure text, when the lane writes one for
                    // the operator (the Gitea upgrade says whether the old
                    // release is back or Gitea is down).
                    failMsg = rec.msg || "";
                }
                else {
                    if (rec.msg) phaseEl.textContent = rec.msg;
                    if (opts.onRecord) {
                        try { opts.onRecord(rec); } catch (e) { /* the caller's repaint is best-effort */ }
                    }
                }
            }
        }
        return null;
    };

    while (!done) {
        if (cancelled) return;
        try { const rd = await drainFull(); if (rd) { backdrop.remove(); return rd(); } }
        catch (e) { /* transient */ }
        try { const rd = await drainView(); if (rd) { backdrop.remove(); return rd(); } }
        catch (e) { /* transient */ }
        if (cancelled) return;
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
    if (cancelled) return;
    // In-place completion resolve for the caller (opts.onTerminal — passed
    // ONLY by the review dialog): fires ONCE, on a REAL terminal only (never
    // on cancelled/auth exits or a hard-killed interrupt), before the
    // operator touches Done — the page behind the open modal resolves
    // immediately; the Done click is the caller's onDone (the review
    // dialog's only resolves the cells a hard-killed op left unresolved).
    if (sawTerminal && opts.onTerminal) {
        try { opts.onTerminal(ok); } catch (e) { /* cell resolve is best-effort */ }
    }
    // opts.onEnd — the op is over, by a terminal OR as interrupted (never on
    // the cancelled/auth exits): the caller's in-flight registry drops it.
    if (opts.onEnd) {
        try { opts.onEnd({ ok: sawTerminal && ok, msg: "", interrupted: !sawTerminal }); }
        catch (e) { /* the caller's bookkeeping is best-effort */ }
    }
    if (bgBtn) bgBtn.remove();
    // The full log can be far longer than one tail chunk (a finished fleet
    // rebuild is megabytes, and a replay starts from 0): read on to its end so
    // where it failed or died is on screen. The reads end at the first empty
    // one: a finished child writes nothing more, and an interrupted build's
    // orphaned docker work (B151) writes far slower than these back-to-back
    // reads catch up. Collected first and set once: appending per chunk
    // re-lays the whole text each time.
    {
        phaseEl.textContent = "loading the rest of the log…";
        let rest = "";
        for (;;) {
            let b;
            try {
                const r = await fetch(`/broker/op/${encodeURIComponent(opId)}/fulllog?from=${logFrom}`);
                if (!r.ok) break;
                b = await r.json();
            } catch (e) { break; }
            if (!b.ok || !b.exists || !b.data || b.next === logFrom) break;
            rest += b.data;
            logFrom = b.next;
        }
        if (rest) { pre.textContent += rest; pre.scrollTop = pre.scrollHeight; }
    }
    if (interrupted) {
        phaseEl.textContent = "interrupted";
        failEl.textContent = "Interrupted — the process stopped without finishing. Check the log above.";
    } else if (ok) { phaseEl.textContent = "done"; }
    else {
        phaseEl.textContent = "failed";
        // Only a lane's operator text is worth repeating; the coarse tokens
        // other lanes write would just say "failed" twice.
        const coarse = ["build failed", "review failed", "invalid request",
                        "provision failed", "upgrade failed", "rollback failed",
                        "failed"];
        failEl.textContent = (failMsg && !coarse.includes(failMsg))
            ? `Failed — ${failMsg}. See the log above.`
            : "Failed — see the log above.";
    }
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
        const redirect = mgmtStatusRedirect(view, res.status, renderSoftwareInto);
        if (redirect) { backdrop.remove(); return redirect(); }
        let body; try { body = await res.json(); } catch (e) { body = {}; }
        if (!body.ok || !body.op_id) {
            go.disabled = false; cancel.disabled = false; go.textContent = cfg.confirmLabel;
            errEl.textContent = "Failed: " + mgmtErrText(body); return;
        }
        await softwareTail(view, backdrop, card, body.op_id, cfg.tailTitle || cfg.title,
                           swTarget(cfg.payload.verb, cfg.payload.agent), cfg.payload);
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

    // The check runs on the broker's one request slot for a few seconds; the
    // box can be closed meanwhile. A closed check's reply is dropped, and the
    // row's Refresh stays off until it arrives (no second check stacks behind).
    const target = swTarget(cfg.buildPayload.verb, cfg.buildPayload.agent);
    let closed = false;
    const closeCheck = el("button", { class: "btn btn-secondary" }, ["Close"]);
    closeCheck.onclick = () => {
        closed = true;
        backdrop.remove();
        softwareChecking.add(target);
        paintSoftwareLabels();
    };
    bodyEl.appendChild(el("p", { class: "mgmt-loading" }, ["Checking upstream…"]));
    bodyEl.appendChild(el("div", { class: "btn-row" }, [closeCheck]));
    let res, body;
    try {
        res = await fetch("/broker/software/refresh-check", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(cfg.checkPayload),
        });
        try { body = await res.json(); } catch (e) { body = {}; }
    } catch (e) { res = null; }
    if (softwareChecking.delete(target) && !softwareInFlight.has(target)) {
        for (const row of document.querySelectorAll("[data-swtarget]")) {
            // A finished row keeps its buttons off until the page is redrawn.
            if (row.getAttribute("data-swtarget") !== target
                    || row.getAttribute("data-swfinished")) continue;
            const b = row.querySelector(".sw-refresh-btn");
            if (b) { b.disabled = false; b.title = ""; }
        }
    }
    if (closed) return null;
    if (res === null) { fail("Broker unreachable."); return backdrop; }
    const redirect = mgmtStatusRedirect(view, res.status, renderSoftwareInto);
    if (redirect) { backdrop.remove(); return redirect(); }
    if (!body.ok || !body.result) {
        fail("Could not resolve upstream: " + mgmtErrText(body)); return backdrop;
    }
    const current = body.result.current || "(unset)";
    const latest = body.result.latest;
    // The agent variant carries a SECOND pin — the companion editor extension,
    // bumped in tandem with the CLI. The editor and reader variants send no ext
    // keys, so every branch below is skipped and their rendering is unchanged.
    const extLatest = body.result.ext_latest || "";
    const extCurrent = body.result.ext_current || "(unset)";
    // The broker says when upstream is OLDER than a pin (downgrade /
    // ext_downgrade). Refresh never moves a pin backwards — the build lane
    // re-checks the same rule — so an older upstream is "nothing to do" for that
    // pin. The comparison lives on the server; this only reads its verdict.
    const cliDown = !!body.result.downgrade;
    const extDown = !!body.result.ext_downgrade;
    // Moved is inequality on the RAW values, never the "(unset)" display text.
    const cliMoved = body.result.current !== body.result.latest && !cliDown;
    const extMoved = !!extLatest && body.result.ext_current !== body.result.ext_latest
        && !extDown;
    bodyEl.innerHTML = "";
    // A stale cached dist at the same pin is covered by Pull. EITHER pin moving
    // disqualifies up-to-date — the CLI and the extension publish independently,
    // so an extension-only bump is a real case.
    if (!cliMoved && !extMoved && (cliDown || extDown)) {
        if (cliDown) {
            bodyEl.appendChild(el("p", {}, [
                cfg.distLabel + ": upstream latest ",
                el("span", { class: "sw-mono" }, [latest]),
                " is older than the pin ",
                el("span", { class: "sw-mono" }, [current]),
                " — nothing to do.",
            ]));
        }
        if (extDown) {
            bodyEl.appendChild(el("p", {}, [
                "editor extension: upstream latest ",
                el("span", { class: "sw-mono" }, [extLatest]),
                " is older than the pin ",
                el("span", { class: "sw-mono" }, [extCurrent]),
                " — nothing to do.",
            ]));
        }
        bodyEl.appendChild(closeRow());
        return backdrop;
    }
    if (!cliMoved && !extMoved) {
        const kids = [
            cfg.distLabel + " is already at the upstream version ",
            el("span", { class: "sw-mono" }, [latest]),
        ];
        if (extLatest) {
            kids.push(" (extension ", el("span", { class: "sw-mono" }, [extLatest]), ")");
        }
        kids.push(". Use Pull to (re)build the dist if the cache is stale.");
        bodyEl.appendChild(el("p", {}, kids));
        bodyEl.appendChild(closeRow());
        return backdrop;
    }
    // Either pin moving gets us here, so ONE of them may be unchanged or held at
    // a pin newer than upstream — label it rather than drawing an arrow. A pin
    // that does not move is not written to the override, so "both pins" would
    // overstate it.
    const pinLine = (label, cur, up, moved, down) => el("p", {}, down ? [
        label + ": pin ",
        el("span", { class: "sw-mono" }, [cur]),
        " — kept (upstream ",
        el("span", { class: "sw-mono" }, [up]),
        " is older).",
    ] : [
        label + ": pin ",
        el("span", { class: "sw-mono" }, [cur]),
        moved ? " → upstream " : " — already at upstream ",
        el("span", { class: "sw-mono" }, [up]),
        ".",
    ]);
    bodyEl.appendChild(pinLine(cfg.distLabel, current, latest, cliMoved, cliDown));
    if (extLatest) {
        bodyEl.appendChild(pinLine("editor extension", extCurrent, extLatest,
                                   extMoved, extDown));
    }
    bodyEl.appendChild(el("p", {}, [
        (extLatest && cliMoved && extMoved)
            ? "Bump both pins to the versions above (at last check) in the local "
            : "Bump to the version above (at last check) in the local ",
        "override (versions.local.env, untracked — never committed) and re-pull ",
        "the dist? A few minutes; the rest of the webui stays responsive.",
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
        const rd = mgmtStatusRedirect(view, r.status, renderSoftwareInto);
        if (rd) { backdrop.remove(); return rd(); }
        let b; try { b = await r.json(); } catch (e) { b = {}; }
        if (!b.ok || !b.op_id) { reset(); errEl.textContent = "Failed: " + mgmtErrText(b); return; }
        await softwareTail(view, backdrop, card, b.op_id, cfg.tailTitle || cfg.title,
                           target, cfg.buildPayload);
    };
    bodyEl.appendChild(el("div", { class: "btn-row" }, [cancel, go]));
    bodyEl.appendChild(errEl);
    return backdrop;
}

// ---- create -----------------------------------------------------------------
// The create form for ONE chosen workflow (the workflows card is the picker —
// see renderWorkflowsScreen). The New Project box fetched the catalog and passes the
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

// ---------------------------------------------------------------------------
// Agent model + effort pickers (STAGE_MODEL_SELECT).
//
// One factory serves every surface — the create dialog, the per-project config
// box, the box window and the dev card — so a new container type cannot reach
// one and miss another. Controls pre-select from the catalog's `defaults`, which
// are the SAME defaults box_add / create fall back to, so what the browser sends
// back is by construction what the CLI would have resolved.
//
// Rendered as the standard `.field` + <label> + <select> idiom (exactly what
// Egress uses). Deliberately NOT checkboxes or radio inputs inside a `.field`:
// `.field input { width:100% }` and `.field label { display:block;
// text-transform:uppercase }` bleed into any raw input/label nested under a
// `.field`, which is what distorted the box window's tickboxes. A <select> is
// native `.field` content and inherits nothing harmful.
// ---------------------------------------------------------------------------

// Fetch + cache the model catalog. Returns null when the broker can't serve it
// (a bad operator override file, say). Callers degrade gracefully: no pickers
// render, no model fields go on the wire, and the backend applies its defaults —
// the researcher loses the choice, never the project.
async function ensureModelCatalog() {
    if (state.modelCatalog) return state.modelCatalog;
    try {
        const res = await fetch("/broker/models");
        if (!res.ok) return null;
        const body = await res.json();
        if (!body.ok || !body.result) return null;
        state.modelCatalog = body.result;
        return state.modelCatalog;
    } catch (e) { return null; }
}

const UNCHANGED = "— unchanged —";

/**
 * A model+effort picker for one container type. Model and effort sit on ONE line
 * (`.model-row`), which is what makes the same control usable in both places it
 * has to live: wrapped in a `.field` on the create dialog / config box, and bare
 * beside the agent cards inside the box window's Settings → Agent group.
 *
 * @param {object} cat      the /broker/models payload
 * @param {string} ctype    "supervisor" | "worker" | "role" | "box"
 * @param {string} title    the label the RESEARCHER sees ("Agent", not "supervisor" —
 *                          a docker box has no supervisor, and the vocabulary
 *                          shouldn't lie; the wire field keeps the internal key)
 * @param {string} hint     one line explaining what this type is
 * @param {object} opts     { unchanged?: bool } — the config box offers "leave
 *                          unchanged" because on UPDATE an empty field means
 *                          exactly that (never "reset to the default")
 * @returns {{node: Node, row: Node, value: () => {model: string, effort: string}}}
 *          `node` = the `.field`-wrapped form (labelled); `row` = the bare
 *          one-line control, for callers that supply their own caption.
 */
function modelPicker(cat, ctype, title, hint, opts) {
    opts = opts || {};
    const dflt = (cat.defaults && cat.defaults[ctype]) || { model: "", effort: "" };
    const modelSel = el("select", {});
    const effortSel = el("select", {});
    if (opts.unchanged) modelSel.appendChild(el("option", { value: "" }, [UNCHANGED]));
    (cat.models || []).forEach((m) => {
        modelSel.appendChild(el("option", { value: m.alias }, [m.label || m.alias]));
    });
    modelSel.value = opts.unchanged ? "" : dflt.model;

    // The effort options depend on the chosen model: a tier with no effort levels
    // (haiku) must offer none, and must not be able to send one. This is the
    // browser half of the rule the backend enforces — an effort-less model with
    // an explicit effort is refused server-side, so leaving the control live here
    // would just manufacture a rejection the researcher can't understand.
    const syncEfforts = () => {
        const alias = modelSel.value;
        const m = (cat.models || []).find((x) => x.alias === alias);
        const levels = m ? (m.efforts || []) : (cat.efforts || []);
        const prev = effortSel.value;
        effortSel.innerHTML = "";
        if (opts.unchanged || !alias) {
            effortSel.appendChild(el("option", { value: "" }, [UNCHANGED]));
        }
        levels.forEach((lv) => {
            effortSel.appendChild(el("option", { value: lv }, [lv]));
        });
        if (alias && !levels.length) {
            // No levels at all — say why, rather than showing an empty control.
            effortSel.innerHTML = "";
            effortSel.appendChild(el("option", { value: "" }, ["no effort levels"]));
            effortSel.disabled = true;
            effortSel.value = "";
            return;
        }
        effortSel.disabled = false;
        if (levels.includes(prev)) effortSel.value = prev;
        else if (!opts.unchanged && levels.includes(dflt.effort)) effortSel.value = dflt.effort;
        else if (opts.unchanged) effortSel.value = "";
    };
    modelSel.onchange = syncEfforts;
    syncEfforts();

    // Model + effort on ONE line. The per-select captions are <span>s, never
    // <label>s: this row is nested inside a `.field` on the create dialog and in
    // the box window's `.field box-settings`, where `.field label` forces
    // display:block + UPPERCASE onto anything it matches. (<select> is safe —
    // the width:100% bleed is `.field input, .field textarea`, which does not
    // match a select, and there is no `.field select` rule.)
    const row = el("div", { class: "model-row" }, [
        el("div", { class: "model-cell" }, [
            el("span", { class: "model-cap" }, ["model"]), modelSel,
        ]),
        el("div", { class: "model-cell" }, [
            el("span", { class: "model-cap" }, ["effort"]), effortSel,
        ]),
    ]);

    return {
        row: row,
        node: el("div", { class: "field" }, [
            el("label", {}, [title]),
            row,
            hint ? el("div", { class: "hint" }, [hint]) : null,
        ]),
        value: () => ({
            model: modelSel.value || "",
            effort: (effortSel.disabled ? "" : effortSel.value) || "",
        }),
    };
}

// The three project-level types, in the order the create dialog shows them. The
// `worker`/`role` pickers only render where a worker layer exists — elsewhere the
// backend rejects those fields outright, so offering them would be a dead end.
const MODEL_PICKER_SPECS = [
    ["supervisor", "Agent", "The agent you talk to in this project."],
    ["worker", "Workers", "Headless analysis workers the agent spawns."],
    ["role", "Role-MCPs", "Tool services workers call — one session per tool-call."],
];

// The egress the create dialog starts on: the workflow's `default_egress` from the
// broker's workflows read — the egress the server resolves for that workflow when
// none is chosen — and locked, the stricter side, when the field is absent or
// unrecognised (a broker older than this webui, or an unreadable configured
// default). The form always sends the value it shows.
function createEgressDefault(manifest) {
    const v = manifest ? manifest.default_egress : undefined;
    return (v === "open" || v === "locked") ? v : "locked";
}

// The working mode (I33) — dev projects and dev boxes. What the agent is told
// about HOW to work. The modes are RS's catalog (container/harness/modes.json),
// read through GET /broker/working-modes: each mode's id, label, description
// and the subagents (`doer`, `checker`) it uses — which decides the Doer and
// Checker rows. Its own read, apart from the model catalog, so either can fail
// alone. Every surface that draws a switch awaits it first.
async function ensureWorkingModes() {
    if (state.workingModes) return state.workingModes;
    const failed = "RS's working modes could not be loaded.";
    try {
        const res = await fetch("/broker/working-modes");
        const body = await res.json().catch(() => null);
        if (res.ok && body && body.ok && body.result && Array.isArray(body.result.modes)) {
            state.workingModes = body.result.modes;
            state.workingModesError = "";
            return state.workingModes;
        }
        // A broken catalog's own message says what is wrong; anything else (a
        // broker that predates the read, a transport failure) is one plain line.
        state.workingModesError = body && body.ok === false && body.error
            && body.error.kind === "validation" && body.error.message
            ? body.error.message : failed;
    } catch (e) {
        state.workingModesError = failed;
    }
    return null;
}

// One mode's catalog entry (null for a mode RS does not list).
function harnessModeSpec(id) {
    return (state.workingModes || []).find((m) => m && m.id === id) || null;
}

// The subagents a mode uses — [] for none, direct, and a mode RS does not list.
function harnessModeRoles(id) {
    const m = harnessModeSpec(id);
    return m && Array.isArray(m.subagents) ? m.subagents : [];
}
const HARNESS_PI_MODEL_PATTERN = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._:~@+\/-]+$/;
const HARNESS_PI_THINKING = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];

// The switch: one card per catalog mode, None first and selected. onChange runs
// after every change (and once at build) so the caller can show its rows.
// `initial` (the ⚙ boxes: the recorded mode) is selected; a recorded mode the
// catalog does not list gets its own card, never None, so an unrelated Apply
// can never reset it. Without the catalog there are no cards: the hint says
// why and what follows (a create surface starts in none; a ⚙ box keeps the
// recorded mode), and value() keeps that mode, so nothing about it is sent.
function harnessModeSwitch(onChange, initial) {
    const creating = initial === undefined;
    const keep = initial || "none";
    const specs = (state.workingModes || []).map((m) => [m.id, m.label, m.description]);
    if (!specs.length) {
        const node = el("div", { class: "field harness-mode" }, [
            el("label", {}, ["Working mode"]),
            el("div", { class: "hint" }, [
                (state.workingModesError || "RS's working modes could not be loaded.") +
                (creating ? " New sessions start with no working mode."
                          : " The working mode stays as it is."),
            ]),
        ]);
        if (onChange) onChange(keep);
        return { node, value: () => keep };
    }
    if (!specs.some((sp) => sp[0] === keep)) {
        specs.push([keep, keep, "A working mode this RS does not list; it is kept until you pick another."]);
    }
    let mode = keep;
    const hint = el("div", { class: "hint" });
    const cards = specs.map(([value, label]) => {
        const card = el("div", { class: "box-opt-card harness-mode-card" },
                        [el("span", { class: "box-opt-name" }, [label])]);
        card.onclick = () => { mode = value; sync(); };
        return { value, card };
    });
    const sync = () => {
        cards.forEach((c) => c.card.classList.toggle("selected", c.value === mode));
        hint.textContent = (specs.find((s) => s[0] === mode) || [])[2] || "";
        if (onChange) onChange(mode);
    };
    const node = el("div", { class: "field harness-mode" }, [
        el("label", {}, ["Working mode"]),
        el("div", { class: "box-opt-cards" }, cards.map((c) => c.card)),
        hint,
    ]);
    sync();
    return { node, value: () => mode };
}

// What the Doer/Checker rows' hint says about the roles the chosen mode uses.
function harnessRolesHint(roles) {
    const doer = roles.includes("doer"), checker = roles.includes("checker");
    if (doer && checker) return "RS's doer implements a unit of work; its checker reviews it, read-only.";
    if (doer) return "RS's doer implements a unit of work.";
    return "RS's checker reviews the plan and the change, read-only.";
}

// Claude's Doer and Checker rows: a tier each, "same as the session" first.
// No effort control: Claude Code runs every subagent at the effort the agent's
// own row sets (the container-wide setting wins over a per-subagent one).
function harnessClaudeRows(cat, initial) {
    const init = initial || {};
    const mk = (v) => {
        const sel = el("select", {}, [
            el("option", { value: "" }, ["same as the session"]),
            ...((cat && cat.models) || []).map((m) =>
                el("option", { value: m.alias }, [m.label || m.alias])),
        ]);
        // A recorded value (the ⚙ Agents box) is selected; one the catalog
        // does not list (an older tier) is kept as its own entry.
        if (v) {
            if (![...sel.options].some((o) => o.value === v)) {
                sel.appendChild(el("option", { value: v }, [v]));
            }
            sel.value = v;
        }
        return sel;
    };
    const doerS = mk(init.doer_model);
    const checkerS = mk(init.checker_model);
    const row = (title, sel) => el("div", { class: "field" }, [
        el("label", {}, [title]),
        el("div", { class: "model-row" }, [
            el("div", { class: "model-cell" }, [el("span", { class: "model-cap" }, ["model"]), sel]),
        ]),
    ]);
    const doerRow = row("Doer", doerS);
    const checkerRow = row("Checker", checkerS);
    // The roles the chosen mode uses (setRoles): only their rows show, and only
    // their fields are read.
    let roles = ["doer", "checker"];
    const hintN = el("div", { class: "hint" });
    const hintText = () => harnessRolesHint(roles) + (roles.length > 1
        ? " Both run at the effort set for the agent." : " It runs at the effort set for the agent.");
    hintN.textContent = hintText();
    const node = el("div", { class: "harness-rows" }, [doerRow, checkerRow, hintN]);
    return {
        node,
        // Without a model catalog the selects would hold only "same as the
        // session": callers keep the rows hidden then.
        available: !!(cat && cat.models && cat.models.length),
        setRoles: (r) => {
            roles = r || [];
            doerRow.style.display = roles.includes("doer") ? "" : "none";
            checkerRow.style.display = roles.includes("checker") ? "" : "none";
            hintN.textContent = hintText();
        },
        value: () => {
            const out = {};
            if (roles.includes("doer") && doerS.value) out.doer_model = doerS.value;
            if (roles.includes("checker") && checkerS.value) out.checker_model = checkerS.value;
            return out;
        },
        // Every field of the shown roles, empty included — the ⚙ Agents box
        // compares against the recorded values to find a change back to the default.
        fields: () => {
            const out = {};
            if (roles.includes("doer")) out.doer_model = doerS.value;
            if (roles.includes("checker")) out.checker_model = checkerS.value;
            return out;
        },
    };
}

// pi's Doer and Checker rows: the free-text model id + thinking level each,
// like the Agent row of pi's tab (pi lists models only after a provider login).
function harnessPiRows(initial) {
    const init = initial || {};
    const mk = (title, model, think) => {
        const modelI = el("input", { type: "text", autocomplete: "off", spellcheck: "false",
                                     placeholder: "provider/model (empty: pi's default)" });
        const thinkS = el("select", {}, [
            el("option", { value: "" }, ["pi's default"]),
            ...HARNESS_PI_THINKING.map((lv) => el("option", { value: lv }, [lv])),
        ]);
        if (model) modelI.value = model;
        if (HARNESS_PI_THINKING.includes(think)) thinkS.value = think;
        const node = el("div", { class: "field" }, [
            el("label", {}, [title]),
            el("div", { class: "model-row agent-pi-row" }, [
                el("div", { class: "model-cell agent-pi-model" }, [
                    el("span", { class: "model-cap" }, ["model"]), modelI,
                ]),
                el("div", { class: "model-cell" }, [
                    el("span", { class: "model-cap" }, ["thinking"]), thinkS,
                ]),
            ]),
        ]);
        return { node, modelI, thinkS };
    };
    const doer = mk("Doer", init.pi_doer_model, init.pi_doer_thinking);
    const checker = mk("Checker", init.pi_checker_model, init.pi_checker_thinking);
    // The roles the chosen mode uses (setRoles): only their rows show, and only
    // their fields are read and checked.
    let roles = ["doer", "checker"];
    const shown = () => [["Doer", "doer", doer], ["Checker", "checker", checker]]
        .filter((r) => roles.includes(r[1]));
    const hintN = el("div", { class: "hint" });
    const hintText = () => harnessRolesHint(roles) + " Empty: pi's default model and thinking level.";
    hintN.textContent = hintText();
    const node = el("div", { class: "harness-rows" }, [doer.node, checker.node, hintN]);
    return {
        node,
        setRoles: (r) => {
            roles = r || [];
            doer.node.style.display = roles.includes("doer") ? "" : "none";
            checker.node.style.display = roles.includes("checker") ? "" : "none";
            hintN.textContent = hintText();
        },
        value: () => {
            const out = {};
            shown().forEach(([, role, r]) => {
                const m = r.modelI.value.trim();
                if (m) out[`pi_${role}_model`] = m;
                if (r.thinkS.value) out[`pi_${role}_thinking`] = r.thinkS.value;
            });
            return out;
        },
        fields: () => {
            const out = {};
            shown().forEach(([, role, r]) => {
                out[`pi_${role}_model`] = r.modelI.value.trim();
                out[`pi_${role}_thinking`] = r.thinkS.value;
            });
            return out;
        },
        // The client half of rscore's id pattern (the server refuses anything else).
        invalid: () => {
            const rows = shown();
            for (let i = 0; i < rows.length; i++) {
                const v = rows[i][2].modelI.value.trim();
                if (v && !HARNESS_PI_MODEL_PATTERN.test(v)) {
                    return "Write pi's " + rows[i][0] + " model as provider/model, e.g. " +
                           "openrouter/anthropic/claude-opus-latest.";
                }
            }
            return null;
        },
    };
}

function mgmtCreateDialog(view, manifest, agents, nodePresent, dataDir, giteaEnabled) {
    manifest = manifest || {};
    agents = Array.isArray(agents) ? agents : [];
    dataDir = typeof dataDir === "string" ? dataDir : "";
    const workflow = manifest.name || "research";
    const isDocker = manifest.substrate === "docker";
    const hasWorkerLayer = !!manifest.has_worker_layer;
    // sandbox-dind is the docker `sandbox` flavor + DIND (STAGE_SANDBOX_DIND_AGENT):
    // same agents + light-path group as the docker box, plus an opt-in box-harness
    // toggle. box_capable is manifest-derived (the broker workflows verb).
    const boxCapable = !!manifest.box_capable;
    // Dev-flagged workflow (dev:true — built-in `dev` or a BYO sibling): the
    // form is the URL-driven dev variant. It POSTs the detached dev-project
    // provision (mirror + create as one op), so the light-path clone group is
    // hidden — the dev step owns the repo wiring. The agents cards DO show
    // (the dev agent may be any staged agent, at least one), outside showInBox
    // because the rest of that group does not apply to a dev project.
    const isDev = !!manifest.dev;
    const showInBox = !isDev && (isDocker || boxCapable);

    // Agent model + effort. Every workflow has an agent the researcher talks to,
    // so the "Agent" picker is universal (the dev card included — a dev project
    // has no worker layer, so it gets exactly one picker, like any other
    // non-research workflow). Workers/Role-MCPs only where a worker layer exists.
    const cat = state.modelCatalog;
    const modelPickers = cat
        ? MODEL_PICKER_SPECS
              .filter(([ctype]) => ctype === "supervisor" || hasWorkerLayer)
              .map(([ctype, title, hint]) => [ctype, modelPicker(cat, ctype, title, hint, {})])
        : [];
    // One `.field` per container type, with its model and effort side by side.
    const modelFields = modelPickers.map(([, p]) => p.node);
    // Collect the model half of the payload once — both the dev and the normal
    // branch below send the same field names (CREATE_WEBUI_FIELDS ⊃
    // DEV_PROJECT_WEBUI_FIELDS for these), so there is one place to get it wrong.
    const modelPayload = () => {
        const out = {};
        modelPickers.forEach(([ctype, p]) => {
            const v = p.value();
            if (v.model) out[ctype + "_model"] = v.model;
            if (v.effort) out[ctype + "_effort"] = v.effort;
        });
        return out;
    };

    const nameI = el("input", { type: "text", autocomplete: "off" });
    const egressS = el("select", {}, [
        el("option", { value: "open" }, ["open"]),
        el("option", { value: "locked" }, ["locked"]),
    ]);
    // Starts on the egress the server would resolve for this workflow (locked for
    // dev, sandbox-dind and docker workflows; the configured default on research).
    // Locked still allows 80/443/DNS — pip/apt/LLM work; gitea rides the
    // project bridge directly and is egress-mode-independent.
    egressS.value = createEgressDefault(manifest);
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
    // rs-fetch (opt-in fetch surface) — ANY non-dev workflow, default off: it
    // wires the PROJECT container (the supervisor, on dind — boxes keep their
    // own per-box toggle in the box window). Hidden on the dev workflow only
    // (from_kwargs refuses it there — the dev supervisor is the working
    // agent's own container; no dead-end control). Checked → `fetch: true`.
    const fetchCb = el("input", { type: "checkbox" });
    fetchCb.checked = false;
    const fetchCard = el("div", {
        class: "box-opt-card",
        title: "Wire this box to fetch dev-lane agent commits: rs-fetch " +
               "stages agent work from the shared Gitea into a git clone in " +
               "this box. Read-only — no write access to Gitea, no access " +
               "to dev containers. Needs Gitea enabled (Sandboxes page).",
    }, [el("span", { class: "box-opt-name" }, ["rs-fetch"])]);
    fetchCard.onclick = () => {
        if (fetchCard.classList.contains("disabled")) return;
        fetchCb.checked = !fetchCb.checked;
        fetchCard.classList.toggle("selected", fetchCb.checked);
    };
    // Keep history on Gitea — offered for a workflow that keeps it
    // (`gitea_history`) on a host whose Gitea is enabled, as the box window
    // does. Mutually exclusive with rs-fetch (from_kwargs refuses the pair).
    // Checked → `history: true`.
    const showHistory = !!manifest.gitea_history && !!giteaEnabled;
    const projHistoryCb = el("input", { type: "checkbox" });
    const projHistoryCard = el("div", {
        class: "box-opt-card",
        title: "Push this project's notes to a private repo on the shared Gitea " +
               "(OpenKnowledge's own sync, which the project turns on). " +
               "The project can add to the notes' main history but never delete " +
               "or rewrite it.",
    }, [el("span", { class: "box-opt-name" }, ["Gitea history"])]);
    const projHistoryHint = el("div", { class: "hint" }, [
        "The notes are pushed to a private Gitea repo (the project's (GIT) tab). " +
        "The project can add to their main history but never delete or rewrite " +
        "it. Destroying the project keeps the history read-only; creating a " +
        "project with the same name again continues it.",
    ]);
    projHistoryHint.style.display = "none";
    projHistoryCard.onclick = () => {
        projHistoryCb.checked = !projHistoryCb.checked;
        projHistoryCard.classList.toggle("selected", projHistoryCb.checked);
        projHistoryHint.style.display = projHistoryCb.checked ? "" : "none";
        // rs-fetch cannot ride a history project.
        if (projHistoryCb.checked) {
            fetchCb.checked = false;
            fetchCard.classList.remove("selected");
        }
        fetchCard.classList.toggle("disabled", projHistoryCb.checked);
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
    // Node (runtime SEED for node-based workflows, STAGE_NODE_SEED) — ALL flavors,
    // default OFF. Checked → enable:["node"] (merged in the payload). Node has no
    // webui tab (it is a container capability, not a service surface); this card is
    // the only place it's toggled at create. Disabled + hinted when no node seed is
    // cached — an AFFORDANCE only; from_kwargs is the real gate.
    //
    // A workflow can MANDATE node by declaring `services: {node: true}` (e.g.
    // open-knowledge): the manifest forces node on server-side regardless of this
    // card, so reflect that honestly — pre-check AND lock the card so the UI can't
    // imply node is off on a workflow that cannot run without it. (Mirrors how the
    // manifest already forces the flag in from_kwargs' service_defaults; the card is
    // display-only either way.)
    const nodeSeedPresent = !!nodePresent;
    const nodeMandatory = !!(manifest.services && manifest.services.node === true);
    const nodeCb = el("input", { type: "checkbox" });
    nodeCb.checked = nodeMandatory;
    let nodeName;
    if (nodeMandatory) nodeName = "Node (npm) — required by this workflow";
    else nodeName = "Node" + (nodeSeedPresent ? " (npm)" : " — pull it under Software");
    const nodeCard = el("div",
        { class: "box-opt-card"
            + (nodeMandatory ? " selected locked"
               : (nodeSeedPresent ? "" : " disabled")) },
        [el("span", { class: "box-opt-name" }, [nodeName])]);
    nodeCard.onclick = () => {
        if (nodeMandatory) return;      // locked on — the workflow requires node
        if (!nodeSeedPresent) return;   // affordance; the backend floor is the gate
        nodeCb.checked = !nodeCb.checked;
        nodeCard.classList.toggle("selected", nodeCb.checked);
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

    // The Agents box (I33): badges that turn agents on and off, and one tab
    // per agent that is on, holding that agent's model settings.
    // The badges show what the project container will actually RUN, which the
    // create path decides per workflow (code-dictated, not a preference):
    //  - a workflow with no agent selection (research, identified as today's
    //    "no agent cards" test, never by name) runs Claude whatever is sent, so
    //    its Claude badge is on and fixed and no agent set goes on the wire;
    //  - every other workflow (Docker-in-Docker: sandbox-dind, dev,
    //    social-data-pipeline; and the docker substrate) gives the project
    //    container exactly the badges that are on, each toggling on its own.
    // Staged agents only, the broker's default agent on: un-staged agents are
    // omitted so the POSTed set always validates in from_kwargs.
    const claudeOnlyWorkflow = !(showInBox || isDev);
    const stagedAgents = claudeOnlyWorkflow
        ? ["claude"]
        : agents.filter((a) => a && a.staged).map((a) => a.name);
    const AGENT_LABELS = { claude: "Claude", pi: "pi" };
    const agentLabel = (name) => AGENT_LABELS[name] || name;
    // Multi-select (independent on/off set): each agent is its OWN badge (cb
    // stays the value-holder), NOT the box window's single-select radio agent.
    const agentChecks = stagedAgents.map((name) => {
        const cb = el("input", { type: "checkbox", value: name });
        if (claudeOnlyWorkflow
            || agents.some((a) => a && a.name === name && a.isDefault)) cb.checked = true;
        const card = el("div", { class: "box-opt-card agent-badge" },
                        [el("span", { class: "box-opt-name" }, [agentLabel(name)])]);
        return { name, cb, card };
    });
    const agentOn = (name) => agentChecks.some((c) => c.name === name && c.cb.checked);
    // Claude is held on (not toggleable) only on a Claude-only workflow.
    const claudeHeld = () => claudeOnlyWorkflow;

    // pi's tab: its starting model (free text, `provider/model` — pi lists
    // models only after you log into a provider inside the project) and thinking
    // level. Written into pi's settings when they are first installed; a model
    // picked inside pi later is kept. The pattern mirrors rscore.PI_MODEL_RE
    // (an affordance; the server refuses anything else).
    const PI_MODEL_PATTERN = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._:~@+\/-]+$/;
    const PI_THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
    const piModelI = el("input", {
        type: "text", autocomplete: "off", spellcheck: "false",
        placeholder: "provider/model, e.g. openrouter/anthropic/claude-opus-latest",
    });
    const piThinkingS = el("select", {}, [
        el("option", { value: "" }, ["pi's default"]),
        ...PI_THINKING_LEVELS.map((lv) => el("option", { value: lv }, [lv])),
    ]);
    const piPanel = el("div", { class: "agent-tab-panel" }, [
        el("div", { class: "field" }, [
            el("label", {}, ["Agent"]),
            el("div", { class: "model-row agent-pi-row" }, [
                el("div", { class: "model-cell agent-pi-model" }, [
                    el("span", { class: "model-cap" }, ["model"]), piModelI,
                ]),
                el("div", { class: "model-cell" }, [
                    el("span", { class: "model-cap" }, ["thinking"]), piThinkingS,
                ]),
            ]),
            el("div", { class: "hint" }, [
                "The model pi starts on. pi lists its models only once you log into " +
                "a provider inside the project, so write the id as pi shows it there " +
                "(provider/model). Empty: pi picks. A model you choose inside pi " +
                "later is kept.",
            ]),
        ]),
    ]);
    const claudePanel = el("div", { class: "agent-tab-panel" },
        modelFields.length ? modelFields : [el("div", { class: "hint" }, [
            "Model choices are unavailable right now; Claude starts on its defaults.",
        ])]);
    // The working mode (I33), dev workflows only: the switch sits between the
    // badges and the tabs, and a mode that uses RS's subagents adds their rows
    // to every agent tab (the agents RS installs whatever the mode; the rows
    // only matter where the loop launches them).
    const harnessClaude = isDev ? harnessClaudeRows(cat) : null;
    const harnessPi = isDev ? harnessPiRows() : null;
    const syncHarnessRows = (mode) => {
        const roles = harnessModeRoles(mode);
        const on = roles.length > 0;
        if (harnessClaude) {
            harnessClaude.setRoles(roles);
            harnessClaude.node.style.display = on && harnessClaude.available ? "" : "none";
        }
        if (harnessPi) {
            harnessPi.setRoles(roles);
            harnessPi.node.style.display = on ? "" : "none";
        }
    };
    const harnessSwitch = isDev ? harnessModeSwitch(syncHarnessRows) : null;
    if (harnessClaude) claudePanel.appendChild(harnessClaude.node);
    if (harnessPi) piPanel.appendChild(harnessPi.node);
    syncHarnessRows(harnessSwitch ? harnessSwitch.value() : "none");
    const agentPanels = { claude: claudePanel, pi: piPanel };
    const agentTabsRow = el("div", { class: "agent-tabs" });
    const agentPanelsWrap = el("div", { class: "agent-tab-panels" },
        stagedAgents.filter((n) => agentPanels[n]).map((n) => agentPanels[n]));
    const agentsHint = el("div", { class: "hint" });
    let activeAgentTab = "";
    const syncAgents = () => {
        if (claudeHeld()) agentChecks.forEach((c) => { if (c.name === "claude") c.cb.checked = true; });
        agentChecks.forEach((c) => {
            const held = c.name === "claude" && claudeHeld();
            c.card.classList.toggle("selected", c.cb.checked);
            c.card.classList.toggle("locked", held);
            c.card.title = held ? "This workflow runs Claude." : "";
        });
        const on = stagedAgents.filter((n) => agentOn(n) && agentPanels[n]);
        if (!on.includes(activeAgentTab)) activeAgentTab = on[0] || "";
        agentTabsRow.innerHTML = "";
        on.forEach((n) => {
            const t = el("button", { type: "button",
                                     class: "agent-tab" + (n === activeAgentTab ? " active" : "") },
                         [agentLabel(n)]);
            t.onclick = () => { activeAgentTab = n; syncAgents(); };
            agentTabsRow.appendChild(t);
        });
        agentTabsRow.style.display = on.length ? "" : "none";
        Object.keys(agentPanels).forEach((n) => {
            agentPanels[n].style.display = n === activeAgentTab ? "" : "none";
        });
        agentsHint.textContent = !stagedAgents.length
            ? "No agents available yet — pull one under Sandboxes → Software."
            : on.length ? ""
            : isDev ? "A dev project needs at least one agent."
            : "No agent: the project starts without one.";
        agentsHint.style.display = agentsHint.textContent ? "" : "none";
    };
    agentChecks.forEach((c) => {
        c.card.onclick = () => {
            if (c.name === "claude" && claudeHeld()) return;
            c.cb.checked = !c.cb.checked;
            syncAgents();
        };
    });
    // A box like Extensions and Sharing, but open by default (the agents are
    // the first thing most creates change). Folding only hides: the payload
    // reads the badges, the switch and the tabs as before.
    const agentsSection = collapsibleBox("Agents", [
        el("div", { class: "agents-head" }, [
            el("div", { class: "box-opt-cards agent-badges" }, agentChecks.map((c) => c.card)),
        ]),
        harnessSwitch ? harnessSwitch.node : null,
        agentTabsRow,
        agentPanelsWrap,
        agentsHint,
    ], "agents-section");
    setCollapsibleOpen(agentsSection, true);
    syncAgents();
    // The model halves of the payload, by agent: Claude's pairs only while
    // Claude is on (a project without Claude has no Claude to configure), pi's
    // values only while pi is on and only when set (empty = pi's own pick).
    const agentModelPayload = () => {
        const out = agentOn("claude") ? modelPayload() : {};
        if (agentOn("pi")) {
            const m = piModelI.value.trim();
            if (m) out.pi_model = m;
            if (piThinkingS.value) out.pi_thinking = piThinkingS.value;
        }
        // The working mode: sent only when it is not None, the Doer/Checker
        // models only for the subagents the mode uses and only for an agent that is on
        // (every key is in DEV_PROJECT_WEBUI_FIELDS).
        const mode = harnessSwitch ? harnessSwitch.value() : "none";
        if (mode !== "none") out.harness_mode = mode;
        if (harnessModeRoles(mode).length) {
            if (agentOn("claude") && harnessClaude) Object.assign(out, harnessClaude.value());
            if (agentOn("pi") && harnessPi) Object.assign(out, harnessPi.value());
        }
        return out;
    };
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
    // Authentication inputs. The key cannot be an <input type=password> (it is
    // multi-line), so it is a textarea with autocomplete/spellcheck off; it stays
    // DISABLED until the acknowledgment tick below is checked.
    const sshKeyT = el("textarea", {
        rows: "4", autocomplete: "off", spellcheck: "false", disabled: "disabled",
        placeholder: "-----BEGIN OPENSSH PRIVATE KEY-----",
        title: "Optional. An alternative to the PAT: clones over ssh and stays " +
               "usable for pushes. Installed at ~/.ssh/github inside the box. " +
               "Sent once with this create; RS keeps no host-side copy.",
    });
    const gitNameI = el("input", { type: "text", autocomplete: "off",
                                   placeholder: "Ada Lovelace" });
    const gitEmailI = el("input", { type: "text", autocomplete: "off",
                                    placeholder: "ada@example.org" });
    // F3: pasting a key is a deliberate act. RS cannot tell an account-wide key
    // from a per-repo deploy key, and the box's agent runs under
    // bypassPermissions with read access to ~/.ssh/github — so the tick is the
    // operator's explicit acknowledgment, not an enforceable gate.
    const sshAckCb = el("input", { type: "checkbox" });
    const sshAck = el("label", { class: "mgmt-check" }, [
        sshAckCb, " I understand the agent running in this box can read this key",
    ]);
    sshAckCb.onchange = () => {
        sshKeyT.disabled = !sshAckCb.checked;
        // Clear on untick, and read it back only while ticked (see the payload
        // builder). A disabled control's .value is fully readable and this is a
        // hand-built JSON body, not a form submit — so `disabled` alone would
        // suppress nothing on the wire and tick→paste→untick→Create would ship
        // the key anyway. Same convention as the model/effort picker, which
        // clears on disable and reads `disabled ? "" : value`.
        if (!sshAckCb.checked) sshKeyT.value = "";
    };
    // Authentication group — collapsed by default, nested inside the clone group.
    // Both toggles are `.mgmt-check` labels OUTSIDE any `.field`: a raw checkbox
    // nested in a `.field` inherits `width:100%` + a block/uppercase label and
    // renders distorted.
    const authCb = el("input", { type: "checkbox" });
    const authToggle = el("label", { class: "mgmt-check" }, [authCb, " authentication"]);
    const authGroup = el("div", { class: "mgmt-docker-group" }, [
        el("div", { class: "field" }, [
            el("label", { title: patI.getAttribute("title") }, ["GitHub PAT (secret)"]),
            patI,
        ]),
        el("div", { class: "hint" }, [
            "…or a GitHub SSH key instead — supply one or the other, not both. " +
            "The key clones over ssh (port 443, so it works under locked egress) " +
            "and stays usable for pushes; a PAT is stored in the clone's remote " +
            "URL. Prefer a per-repo deploy key over an account-wide key: RS " +
            "cannot scope it, and the agent in this box can read it.",
        ]),
        sshAck,
        el("div", { class: "field" }, [
            el("label", { title: sshKeyT.getAttribute("title") },
               ["GitHub SSH private key (secret)"]),
            sshKeyT,
        ]),
        el("div", { class: "field" }, [el("label", {}, ["Git user.name"]), gitNameI]),
        el("div", { class: "field" }, [el("label", {}, ["Git user.email"]), gitEmailI]),
        el("div", { class: "hint" }, [
            "The identity is what the box's commits are authored as; without it " +
            "git refuses to commit. Both survive a project stop/start.",
        ]),
    ]);
    authGroup.style.display = "none";
    authCb.onchange = () => { authGroup.style.display = authCb.checked ? "" : "none"; };
    // The Extensions box — the extensions as bordered cards in a box that
    // opens folded, like the box window's; the agents have their own box
    // above (I33), open by default. Folding only hides: the payload reads every card as before.
    const extensionsBox = collapsibleBox("Extensions", [
        // Node is substrate-agnostic (all flavors); reader is dind-only;
        // rs-fetch is any-workflow-but-dev (the dev supervisor is the
        // agent's own container — from_kwargs refuses it, so no dead-end
        // control there). Node must appear on BOTH branches of the
        // isDocker fork — the docker box is where node-based store
        // workflows (open-knowledge) run.
        el("div", { class: "box-opt-cards" },
           isDocker ? [editorCard, fetchCard, nodeCard,
                       ...(showHistory ? [projHistoryCard] : [])]
                    : (isDev ? [editorCard, readerCard, nodeCard]
                             : [editorCard, fetchCard, readerCard, nodeCard])),
        ...(showHistory ? [projHistoryHint] : []),
    ], "create-extensions");

    // Clone-a-repo group: the light-path repo/ref/setup fields, shown only when
    // the operator opts in (and only showInBox). The agents have their own
    // section above; the PAT moved down into the collapsed Authentication
    // sub-box, which now holds every credential/identity field for the clone.
    const cloneCb = el("input", { type: "checkbox" });
    const cloneToggle = el("label", { class: "mgmt-check" }, [cloneCb, " clone a git repo"]);
    const cloneGroup = el("div", { class: "mgmt-docker-group" }, [
        el("div", { class: "field" }, [el("label", {}, ["Repo (https)"]), repoI]),
        el("div", { class: "field" }, [el("label", {}, ["Ref"]), refI]),
        el("div", { class: "field" }, [el("label", {}, ["Setup"]), setupT]),
        authToggle,
        authGroup,
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
    // A workflow that starts its own web app runs its own fixed setup in
    // /workspace and takes no repo (as the box window offers none): no clone
    // group at all, and nothing from it is posted (the server refuses a typed
    // repo or setup for it).
    const showClone = showInBox && !manifest.web;
    if (!showClone) cloneCb.checked = false;

    // Add folders (read-only, at /workspace/mnt/<name>/ — the webui path to
    // what the host CLI's --mount does), on every workflow, dev included.
    // Paths are RELATIVE to RS's data folder; the broker appends the folder and
    // refuses anything else (an absolute path, "..", a link, a location outside
    // it), so the check below is an affordance. Same shape as the clone group:
    // a `.mgmt-check` toggle OUTSIDE any `.field` (the `.field` bleed rule),
    // the group collapsed until ticked; the explanation rides an ⓘ.
    const mountCb = el("input", { type: "checkbox" });
    const mountToggle = el("label", { class: "mgmt-check" }, [mountCb, " Add folders"]);
    const mountI = el("input", { type: "text", autocomplete: "off",
                                placeholder: "my-dataset, surveys/2024" });
    const mountInfo = infoIcon(
        "Comma-separated paths relative to RS's data folder on this host" +
        (dataDir ? " (" + dataDir + ")" : "") + ", for example my-dataset or " +
        "surveys/2024. Each is mounted read-only at /workspace/mnt/<last name>/ " +
        "inside the project: the agent, every worker and every role service " +
        "see it, and a box when it is given to it. A folder that does not exist " +
        "yet is created. Put datasets in the data folder, or mount another disk " +
        "into it on the host; a link pointing out of it is refused.");
    const mountGroup = el("div", { class: "mgmt-docker-group" }, [
        el("div", { class: "box-opt-caption" }, ["Folders in RS's data folder ", mountInfo.icon]),
        mountInfo.detail,
        // Wrapped in a .field for the shared input style (and the 16px mobile
        // rule that stops iOS zooming on focus); no <label> inside, so the
        // .field label bleed does not apply.
        el("div", { class: "field" }, [mountI]),
        el("div", { class: "hint" }, [
            "Read-only in the project at /workspace/mnt/<name>/.",
        ]),
    ]);
    mountGroup.style.display = "none";
    mountCb.onchange = () => {
        mountGroup.style.display = mountCb.checked ? "" : "none";
        if (mountCb.checked) mountI.focus();
    };
    // The tokens the payload sends: split on commas, trimmed, empties dropped.
    const mountPaths = () => parseFolderList(mountI.value);
    // The Sharing box: everything that adds folders to the project, in a box
    // that opens folded, like Extensions above it.
    const sharingBox = collapsibleBox("Sharing", [
        mountToggle,
        mountGroup,
    ], "create-sharing");

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
    const devBranchI = el("input", {
        type: "text", autocomplete: "off", placeholder: "e.g. main",
        title: "The branch the agent works from: it syncs this branch, branches " +
               "off it, and targets it with pull requests. Must already exist on " +
               "the repo.",
    });
    const devGroup = el("div", { class: "mgmt-docker-group" }, [
        el("div", { class: "field" }, [el("label", {}, ["GitHub repo (https)"]), devUrlI]),
        el("div", { class: "field" }, [
            el("label", { title: devBranchI.getAttribute("title") }, ["Branch"]),
            devBranchI,
        ]),
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
        // The docker substrate stages no inner images and wires no workers.
        checklist: isDocker ? "create_docker" : undefined,
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
            agentsSection,
            enableField,
            extensionsBox,
            ...(showClone ? [cloneToggle, cloneGroup] : []),
            sharingBox,
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
            // Branch is REQUIRED in the webui (the CLI may omit it and take the
            // repo default). Client-side only: an omitted branch is a valid
            // server-side state, so this is an affordance, not a gate.
            if (isDev && !devBranchI.value.trim()) {
                return "A dev project needs a branch for the agent to work from.";
            }
            // Mirror the dev floor in from_kwargs (the broker refuses an empty
            // set too): a dev project exists for an agent to work its repo.
            if (isDev && !agentChecks.some((c) => c.cb.checked)) {
                return "A dev project needs at least one agent to work its repo.";
            }
            // Mirror rscore.PI_MODEL_RE: pi's model id is written provider/model.
            if (agentOn("pi") && piModelI.value.trim()
                && !PI_MODEL_PATTERN.test(piModelI.value.trim())) {
                return "Write pi's model as provider/model, e.g. " +
                       "openrouter/anthropic/claude-opus-latest.";
            }
            if (harnessPi && harnessSwitch && agentOn("pi")
                    && harnessModeRoles(harnessSwitch.value()).length) {
                const bad = harnessPi.invalid();
                if (bad) return bad;
            }
            // Mirror from_kwargs: an in-box repo needs a ref (pin the clone).
            if (showClone && cloneCb.checked && repoI.value.trim() && !refI.value.trim()) {
                return "A workflow repo requires a ref.";
            }
            // Mirror the broker gate's first refusal (an absolute path); the
            // data-folder check itself is server-side only.
            if (mountCb.checked) {
                const paths = mountPaths();
                // A folder error opens Sharing (it may have been folded since
                // the tick) and puts the cursor in the field it names.
                const bad = paths.find((s) => s.startsWith("/") || s.startsWith("~"));
                if (!paths.length || bad) {
                    setCollapsibleOpen(sharingBox, true);
                    mountI.focus();
                }
                // Ticked with nothing typed would silently create a project with no
                // mount (the payload omits an empty list) — say so instead.
                if (!paths.length) return "Enter at least one folder, or untick \"Add folders\".";
                if (bad) return "Folder paths are relative to RS's data folder (got: " + bad + ").";
            }
            // Mirror the from_kwargs auth refusals so they surface in the form
            // rather than as a failed op. Read the key the same way the payload
            // does (disabled ⇒ not sent, so it must not trip these either).
            if (showInBox && cloneCb.checked) {
                const sshKey = sshKeyT.disabled ? "" : sshKeyT.value.trim();
                if (sshKey && patI.value.trim()) {
                    return "Supply either a GitHub PAT or a GitHub SSH key, not both.";
                }
                const repo = repoI.value.trim();
                if (sshKey && repo) {
                    let host = "";
                    try { host = new URL(repo).hostname; } catch (e) { host = ""; }
                    if (host !== "github.com") {
                        return "The GitHub SSH key configures github.com only — " +
                               "point at a github.com repo or drop the key.";
                    }
                }
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
                    // Unconditional — the validator above guarantees non-empty.
                    branch: devBranchI.value.trim(),
                    egress: egressS.value,
                    // A dev project has no worker layer, so the Claude half yields
                    // supervisor_* only, and the pi half pi_model/pi_thinking —
                    // exactly the fields in DEV_PROJECT_WEBUI_FIELDS. A field
                    // absent from that set is dropped silently by the broker,
                    // so this is a lockstep.
                    ...agentModelPayload(),
                    // ALWAYS the explicit selection (never omitted): an absent
                    // key would fall back to the workflow's preset, silently
                    // replacing what the operator ticked. The validator above
                    // guarantees it is non-empty.
                    agents: agentChecks.filter((c) => c.cb.checked).map((c) => c.name),
                };
                const pat = devPatI.value.trim();
                if (pat) payload.pat = pat;
                if (!editorCb.checked) payload.disable = ["code-server"];
                // ONE merged enable array (STAGE_NODE_SEED): reader + node must not
                // clobber each other — the broker drops a field silently, so a second
                // bare `payload.enable = [...]` would just lose the other's token.
                const devEnable = [];
                if (readerCb.checked) devEnable.push("reader");
                if (nodeCb.checked) devEnable.push("node");
                if (devEnable.length) payload.enable = devEnable;
                // Folders ride as a LIST only when ticked and non-empty; the
                // dev request resolves them against RS's data folder.
                if (mountCb.checked) {
                    const paths = mountPaths();
                    if (paths.length) payload.mount = paths;
                }
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
                // supervisor_* while Claude is on (always, on research);
                // worker_*/role_* only where a worker layer exists (from_kwargs
                // rejects them elsewhere, and the pickers are filtered to match);
                // pi_model/pi_thinking while pi is on. Every key is in
                // CREATE_WEBUI_FIELDS.
                ...agentModelPayload(),
            };
            // Editor on by default; unchecking disables the code-server service.
            if (!editorCb.checked) payload.disable = ["code-server"];
            // Opt-in rs-fetch — any non-dev workflow (this payload branch never
            // runs for dev, and the card is hidden there). Sent only when checked.
            if (fetchCb.checked) payload.fetch = true;
            // Gitea history — only where the card is offered.
            if (showHistory && projHistoryCb.checked) payload.history = true;
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
            // Node is substrate-agnostic (STAGE_NODE_SEED) — no isDocker gate, unlike
            // reader; the docker box is exactly where node-based workflows run.
            if (nodeCb.checked) enableTokens.push("node");
            if (enableTokens.length) payload.enable = enableTokens;
            // Folders ride as a LIST only when ticked and non-empty.
            // Lockstep: `mount` is deliberately NOT in CREATE_WEBUI_FIELDS — the
            // broker honours it through its data-folder gate (_verb_create) and
            // refuses anything outside RS's data folder before creating anything.
            if (mountCb.checked) {
                const paths = mountPaths();
                if (paths.length) payload.mount = paths;
            }
            if (showInBox) {
                // ALWAYS send the explicit selection, [] included. An omitted key
                // means UNSET server-side (from_kwargs falls back to the workflow's
                // agent preset), so omitting on deselect-all would silently
                // resurrect the preset's claude — the explicit [] IS the
                // agent-less create. Outside showInBox no key is sent here (a
                // research create keeps its fixed set; the dev branch above sends
                // its own explicit selection).
                payload.agents = agentChecks.filter((c) => c.cb.checked)
                                            .map((c) => c.name);
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
                    // Read the key only while the acknowledgment tick is checked
                    // (`disabled ? "" : value`, the model-picker convention) —
                    // `disabled` alone suppresses nothing in a hand-built body,
                    // so tick→paste→untick→Create must not ship the key. The
                    // tick itself is never sent: it is a client-side affordance,
                    // and a relayed `ack` would imply a server gate that cannot
                    // exist. Lockstep: these three keys are in CREATE_WEBUI_FIELDS.
                    const sshKey = sshKeyT.disabled ? "" : sshKeyT.value.trim();
                    const gitName = gitNameI.value.trim();
                    const gitEmail = gitEmailI.value.trim();
                    if (sshKey) payload.github_ssh_key = sshKey;
                    if (gitName) payload.git_user_name = gitName;
                    if (gitEmail) payload.git_user_email = gitEmail;
                }
            }
            return fetch("/broker/project", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
        },
        // The New Project box under this dialog has done its job either way:
        // the dialog has shown the outcome, and a dev create's tail cannot tell
        // success from failure here. On success, surface the new (running)
        // project in the sidebar — JIT-fetch its creds the same way Attach does,
        // then refresh the rail — and the Sandboxes table when it is on screen.
        onDone: async (ok) => {
            if (floatBox && floatBox.backdrop.contains(view)) closeFloatBox();
            if (!ok) return;
            if (await attachIntoVault(nameI.value.trim())) refreshProjectRail();
            const table = hostBody();
            if (table && state.hostPage === "sandboxes" && sandboxesActiveTab === "management"
                    && !mgmtViewHoldsInput(table)) {
                renderManagementInto(table);
            }
        },
        focus: () => nameI.focus(),
    });
}

// ---- workflows screen (clickable workflow cards) ---------------------------
// The New Project box is the create entry point: a grid of workflow cards.
// Clicking a card opens mgmtCreateDialog prefilled to that workflow, ABOVE the
// box (Cancel comes back to the cards; the box is the dialog's view, so its
// sign-in landing has somewhere visible to draw). Gated
// behind the management session (the broker `workflows` verb is token-gated) — a
// 401 routes through the same login screen, returning here on success. Substrate
// is never shown (Q7): a card is a workflow, not a containment runtime.


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
    // The model catalog, so the create dialog (opened synchronously from a card
    // click) can render its pickers without an await. Best-effort: if it fails,
    // the dialog simply omits the pickers and the backend applies its defaults.
    await ensureModelCatalog();
    await ensureWorkingModes();
    renderWorkflowsScreen(view, body.result);
}

function renderWorkflowsScreen(view, result) {
    view.innerHTML = "";
    const workflows = Array.isArray(result.workflows) ? result.workflows : [];
    const agents = Array.isArray(result.agents) ? result.agents : [];
    // The default-on agent card comes from the broker (never a literal here).
    // Fallback to the first staged agent: app.js and the broker rebuild
    // independently, and a form with no card checked would be a regression.
    let defaultAgent = typeof result.default_agent === "string" ? result.default_agent : "";
    if (!defaultAgent) {
        const firstStaged = agents.find((a) => a && a.staged);
        defaultAgent = firstStaged ? firstStaged.name : "";
    }
    agents.forEach((a) => { if (a) a.isDefault = a.name === defaultAgent; });
    const nodePresent = !!result.node_present;   // STAGE_NODE_SEED: Node tickbox affordance
    // RS's data folder, named in the create dialog's "Add folders" ⓘ (every
    // browser folder path is relative to it). Refetched per render (no cache).
    const dataDir = typeof result.data_dir === "string" ? result.data_dir : "";
    const giteaEnabled = !!result.gitea_enabled;   // offers a history workflow's switch
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
        card.onclick = () => mgmtCreateDialog(view, m, agents, nodePresent, dataDir,
                                              giteaEnabled);
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
        // Only a transient (_jit) row takes the keyring's coordinates. A saved
        // bookmark (manual Import) keeps the ones the operator entered: copying
        // the keyring password onto it would let persistVault write it into the
        // encrypted vault, and would overwrite what the operator typed.
        if (existing._jit) {
            existing.host = info.host; existing.port = info.port;
            existing.username = info.username; existing.password = info.password;
        }
    } else {
        const row = {
            name: info.name, host: info.host, port: info.port,
            username: info.username, password: info.password, _jit: true,
        };
        rehydrateProjectUiState(row);
        state.vault.projects.push(row);
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
        project: name,
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
            // reconnect spam), drop it from the sidebar + persisted vault —
            // including its saved tab arrangement, so a reused name starts
            // with the default layout — then refresh the rail behind the box
            // so the row disappears.
            teardownProjectState(name);
            state.vault.projects = state.vault.projects.filter((p) => p.name !== name);
            delete projectUiMap()[name];
            pruneProjectOrder(name);
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
// re-activate it to surface the new / removed box-term tab. The box list itself
// re-loads on the next config-box open (the config box is dismissed when the op
// modal takes a pointer-down).
async function refreshAfterBoxChange(project) {
    // Re-activate only when the project's own view is what is on screen: this
    // also runs when an update finishes in the background, and re-activating
    // closes whatever host page (Management, Workflows) the operator moved on to.
    // activateProject drops the cached tabs itself, carrying detected ones over.
    if (state.activeProject === project && !state.hostPage) {
        try { await activateProject(project, { carry: true }); } catch (e) { /* best-effort */ }
    } else {
        delete state.projectServices[project];
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
    let presets, allowed, folderChoices = [], folderChoicesError = "";
    let giteaEnabled = false;
    // The agents a box in THIS project can deploy: the set the project staged
    // (claude is always staged as the box copy-source; the marker adds the rest).
    // Never the host's global staged list — a box cannot get what its project
    // did not stage.
    let projectAgents = [];
    // The project's flavor: a research project stages no working-mode suite, so
    // its dev boxes get no working-mode controls (box_add refuses them there).
    let projectFlavor = "";
    try {
        const sres = await fetch(`/projects/status?names=${encodeURIComponent(project)}`);
        const sbody = await sres.json();
        const st = sbody && sbody[project];
        if (st && Array.isArray(st.agents_staged)) projectAgents = st.agents_staged.slice();
        if (st && typeof st.flavor === "string") projectFlavor = st.flavor;
    } catch (e) { /* only "None" is offered; a preset-default agent then submits with
                     no `agent` field and the server applies the preset default */ }
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
        folderChoices = body.result.folders || [];
        folderChoicesError = body.result.folders_error || "";
        giteaEnabled = !!body.result.gitea_enabled;
    } catch (e) {
        alert("Can't add a box: broker unreachable.");
        return;
    }
    if (!presets.length) { alert("No box presets available for this project."); return; }

    // This box's own agent model + effort. A box does not inherit the project's
    // agent pair — it has its own default (the `box` type), which is what makes a
    // future mixed-agent box expressible. Unset ⇒ box_add falls back to the same
    // `box` default from the project marker, so the picker and the fallback agree.
    const boxCat = await ensureModelCatalog();
    await ensureWorkingModes();
    const boxModelP = boxCat
        ? modelPicker(boxCat, "box", "Agent",
                      "The model this box's agent runs on.", {})
        : null;
    // The model/effort row lives INSIDE Settings → Agent, beside the agent cards
    // (see the group below), and is meaningful only when an agent actually runs
    // in the box — a blank box (agent "none") has nothing to run a model. Shown /
    // hidden from markAgent(), the single choke point every agent change passes
    // through (card click, preset pre-select, and the MCP coupling that forces
    // claude on). `agentModelOn()` is the one predicate the payloads also use, so
    // a hidden control can never put a model on the wire.
    const boxModelRow = boxModelP ? boxModelP.row : null;
    const agentModelOn = () => !!boxModelP && agentS.value === "claude";
    const syncAgentModel = () => {
        if (boxModelRow) boxModelRow.style.display = agentModelOn() ? "" : "none";
    };

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
    // The preset default is the FIRST staged agent (the project's floor agent);
    // a box that selects any agent receives the project's whole staged set.
    const defaultBoxAgent = projectAgents[0] || "";
    const agentS = el("select", {}, [
        el("option", { value: "none" }, ["none (blank box)"]),
        ...projectAgents.map((a) => el("option", { value: a }, [a])),
    ]);
    const agentSpecs = [
        { value: "none", label: "None" },
        ...projectAgents.map((a) => ({ value: a, label: a })),
    ];
    const agentCardEls = [];
    // With an MCP ticked only "None" is off the table (an MCP needs some agent);
    // the agent choice itself stays the operator's.
    let noneLocked = false;
    const agentCards = agentSpecs.map((s) => {
        const card = el("div", { class: "box-opt-card" },
                        [el("span", { class: "box-opt-name" }, [s.label])]);
        card.onclick = () => {
            if (agentS.disabled) return;
            if (s.value === "none" && noneLocked) return;
            agentS.value = s.value; markAgent();
        };
        agentCardEls.push({ value: s.value, card });
        return card;
    });
    const agentCardsWrap = el("div", { class: "box-opt-cards" }, agentCards);
    // The working mode (I33): a dev box with an agent, outside research. The
    // Doer/Checker rows follow the box's ONE agent (Claude tiers or pi's id +
    // thinking); the box's other agent, if the project carries two, runs RS's
    // subagents on the session's model.
    const boxHarnessClaude = harnessClaudeRows(boxCat);
    const boxHarnessPi = harnessPiRows();
    const boxHarnessOtherHint = el("div", { class: "hint" }, [
        "The box's other agent, if the project has two, runs RS's subagents " +
        "on the session's model.",
    ]);
    const syncBoxHarnessRows = (mode) => {
        const roles = harnessModeRoles(mode);
        const pdc = roles.length > 0;
        boxHarnessClaude.setRoles(roles);
        boxHarnessPi.setRoles(roles);
        boxHarnessClaude.node.style.display =
            pdc && agentS.value === "claude" && boxHarnessClaude.available ? "" : "none";
        boxHarnessPi.node.style.display = pdc && agentS.value === "pi" ? "" : "none";
        boxHarnessOtherHint.style.display = pdc && projectAgents.length > 1 ? "" : "none";
    };
    const boxHarnessSwitch = harnessModeSwitch(syncBoxHarnessRows);
    const boxHarnessGroup = el("div", { class: "box-opt-group box-harness" }, [
        boxHarnessSwitch.node, boxHarnessClaude.node, boxHarnessPi.node, boxHarnessOtherHint,
    ]);
    const boxHarnessOn = () => !!(selectedPreset && selectedPreset.dev)
        && agentS.value !== "none" && projectFlavor !== "research";
    const syncBoxHarness = () => {
        boxHarnessGroup.style.display = boxHarnessOn() ? "" : "none";
        syncBoxHarnessRows(boxHarnessSwitch.value());
    };
    function markAgent() {
        for (const { value, card } of agentCardEls) {
            card.classList.toggle("selected", value === agentS.value);
            if (value === "none") card.classList.toggle("disabled", noneLocked);
        }
        // The model/effort row follows the agent: no agent, no model. Hooking it
        // here rather than at each call site means every path that can change the
        // agent — card click, preset pre-select, MCP coupling — keeps it honest.
        syncAgentModel();
        // The working mode follows the agent (and the preset) the same way.
        syncBoxHarness();
    }

    const editorCb = el("input", { type: "checkbox" });   // box-level toggle, default off
    const editorCard = el("div", { class: "box-opt-card" },
                          [el("span", { class: "box-opt-name" }, ["Editor"])]);
    editorCard.onclick = () => {
        editorCb.checked = !editorCb.checked;
        editorCard.classList.toggle("selected", editorCb.checked);
    };

    // rs-fetch (opt-in fetch surface, default off). The card's `title` is the
    // toggle's explainer; fetchCb stays the detached value-holder (the editor
    // pattern). A dev preset disables it in applyPreset — box_add rejects the
    // combination, so the dialog must not offer a guaranteed rejection.
    const fetchCb = el("input", { type: "checkbox" });    // box-level toggle, default off
    const fetchCard = el("div", {
        class: "box-opt-card",
        title: "Wire this box to fetch dev-lane agent commits: rs-fetch " +
               "stages agent work from the shared Gitea into a git clone in " +
               "this box. Read-only — no write access to Gitea, no access " +
               "to dev containers.",
    }, [el("span", { class: "box-opt-name" }, ["rs-fetch"])]);
    fetchCard.onclick = () => {
        if (fetchCard.classList.contains("disabled")) return;
        fetchCb.checked = !fetchCb.checked;
        fetchCard.classList.toggle("selected", fetchCb.checked);
    };

    // Keep history on Gitea (a gitea_history preset, on a host whose Gitea is
    // enabled): the box's notes are pushed to a private notes repo by
    // OpenKnowledge's own sync. Mutually exclusive with rs-fetch (box_add
    // refuses the pair). historyCb is the detached value holder.
    const historyCb = el("input", { type: "checkbox" });
    const historyCard = el("div", {
        class: "box-opt-card",
        title: "Push this box's notes to a private repo on the shared Gitea " +
               "(OpenKnowledge's own sync, which the box turns on). " +
               "The box can add to the notes' main history but never delete or " +
               "rewrite it.",
    }, [el("span", { class: "box-opt-name" }, ["Gitea history"])]);
    const historyHint = el("div", { class: "hint" }, [
        "The notes are pushed to a private Gitea repo (the box's (GIT) tab). " +
        "The box can add to their main history but never delete or rewrite it. " +
        "Removing the box keeps the history read-only; adding a box with the " +
        "same name in this project again continues it.",
    ]);
    const syncHistory = () => {
        historyCard.classList.toggle("selected", historyCb.checked);
        historyHint.style.display = historyCb.checked ? "" : "none";
        // rs-fetch cannot ride a history box.
        if (historyCb.checked) {
            fetchCb.checked = false;
            fetchCard.classList.remove("selected");
        }
        fetchCard.classList.toggle("disabled",
            !!selectedPreset.dev || historyCb.checked);
    };
    historyCard.onclick = () => {
        if (historyCard.classList.contains("disabled")) return;
        historyCb.checked = !historyCb.checked;
        syncHistory();
    };

    // Share files with the project (off by default; the box type may pre-tick
    // it — applyPreset seeds it from share_default). Set at creation only.
    // shareCb is the detached value holder; the payload always sends it.
    const shareCb = el("input", { type: "checkbox" });
    const shareCard = el("div", {
        class: "box-opt-card",
        title: "Give this box a mailbox the project agent and the workers read, " +
               "and let it read the project agent's and the other sharing " +
               "boxes' mailboxes. Off: the box has no mailbox and no /workspace/shared.",
    }, [el("span", { class: "box-opt-name" }, ["Share files"])]);
    const shareHint = el("div", { class: "hint" }, [BOX_SHARE_HINT]);
    const syncShare = () => {
        shareCard.classList.toggle("selected", shareCb.checked);
        shareHint.style.display = shareCb.checked ? "" : "none";
    };
    shareCard.onclick = () => {
        shareCb.checked = !shareCb.checked;
        syncShare();
    };

    // Browser (spawn-time image toggle, tri-state server-side): checked ⇒ the
    // Playwright + headless Chromium box image, unchecked ⇒ the base image. The
    // preset's image is only the DEFAULT (applyPreset seeds the checkbox from
    // it); the payload always sends the explicit checkbox state for non-dev
    // presets. Disabled on dev presets (base-image only — box_add rejects the
    // combination), the fetch-card pattern.
    const browserCb = el("input", { type: "checkbox" });  // value holder
    const browserCard = el("div", {
        class: "box-opt-card",
        title: "Run this box on the Playwright + headless Chromium image so " +
               "its agent can browse the web. Heavier image; websearcher " +
               "pre-checks it (its instructions assume the browser tools).",
    }, [el("span", { class: "box-opt-name" }, ["Browser"])]);
    browserCard.onclick = () => {
        if (browserCard.classList.contains("disabled")) return;
        browserCb.checked = !browserCb.checked;
        browserCard.classList.toggle("selected", browserCb.checked);
        syncTrusted();
    };

    // Trusted browser (off by default = restricted): shown only while Browser
    // is selected; unticked whenever the browser goes off or the preset
    // changes. Set at creation only. trustedCb is the detached value holder;
    // the payload sends it only together with the browser.
    const trustedCb = el("input", { type: "checkbox" });
    const trustedCard = el("div", {
        class: "box-opt-card",
        title: "Give this box's agents every browser tool. Without it the " +
               "browser is restricted: nine risky actions a web page could " +
               "talk the agent into (running page script or code, uploading " +
               "or dropping files, dragging, answering dialogs, saving PDFs, " +
               "installing browsers, resizing) are blocked for every agent.",
    }, [el("span", { class: "box-opt-name" }, ["Trusted browser"])]);
    const trustedHint = el("div", { class: "hint" }, [
        "Every browser tool is available to the agents in this box, and extra " +
        "Playwright features can be switched on in its environment. Only for " +
        "pages you trust: a page's text can steer the agent. Fixed once the " +
        "box is added.",
    ]);
    const syncTrusted = () => {
        if (!browserCb.checked) trustedCb.checked = false;
        trustedCard.style.display = browserCb.checked ? "" : "none";
        trustedCard.classList.toggle("selected", trustedCb.checked);
        trustedHint.style.display = trustedCb.checked ? "" : "none";
    };
    trustedCard.onclick = () => {
        trustedCb.checked = !trustedCb.checked;
        syncTrusted();
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

    // Read-only folders: one "Add folders" tickbox opening a field of
    // workspace-relative paths (the server validates and checks every level).
    // The project's top-level folders are listed under it as examples. Off by
    // default; hidden for a dev preset (it takes none).
    const foldersCb = el("input", { type: "checkbox" });
    const foldersI = el("input", { type: "text", autocomplete: "off",
                                   placeholder: "data, results/run3" });
    // The input sits in a .field for the shared input style (the Sharing box
    // around it is deliberately not one — see the dialog body); no <label>
    // inside, so the .field label bleed does not apply.
    const foldersEntry = el("div", { class: "box-folders-entry" }, [
        el("div", { class: "field" }, [foldersI]),
        el("div", { class: "hint" }, [BOX_FOLDERS_SHORT]),
    ]);
    const foldersInfo = infoIcon(BOX_FOLDERS_HINT + "\n\n" +
                                 folderChoicesLine(folderChoices, folderChoicesError));
    foldersEntry.style.display = "none";
    foldersCb.onchange = () => {
        foldersEntry.style.display = foldersCb.checked ? "" : "none";
        if (foldersCb.checked) foldersI.focus();
    };
    const foldersGroup = el("div", { class: "box-opt-group" }, [
        el("div", { class: "box-opt-caption" }, ["Read-only folders ", foldersInfo.icon]),
        foldersInfo.detail,
        el("label", { class: "mgmt-check" }, [foldersCb, " Add folders"]),
        foldersEntry,
    ]);

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
    const devBranchI = el("input", {
        type: "text", autocomplete: "off", placeholder: "e.g. main",
        title: "The branch the agent works from: it syncs this branch, branches " +
               "off it, and targets it with pull requests. Must already exist on " +
               "the repo.",
    });
    const devGroup = el("div", { class: "mgmt-docker-group" }, [
        el("div", { class: "field" }, [el("label", {}, ["GitHub repo (https)"]), devUrlI]),
        el("div", { class: "field" }, [
            el("label", { title: devBranchI.getAttribute("title") }, ["Branch"]),
            devBranchI,
        ]),
        el("div", { class: "field" }, [el("label", {}, ["GitHub PAT (optional)"]), devPatI]),
        el("div", { class: "hint" },
           ["Mirrored + forked into the shared Gitea; the agent's fork is " +
            "cloned at boot and work is delivered via PRs. The PAT (private " +
            "repos only, prefer read-only fine-grained) is kept by Gitea " +
            "per-repo — to change it, remove the repo and re-create the box."]),
    ]);

    // Preset-declared input fields (secret ⇒ password input). Rebuilt on every
    // preset switch; values are read at submit into payload.field_values. The
    // values travel to a private in-box env file server-side — the dialog never
    // stores them anywhere else.
    let fieldInputs = [];   // [{name, label, input}]
    const fieldsGroup = el("div", { class: "mgmt-docker-group" });
    function buildFields() {
        fieldInputs = [];
        fieldsGroup.innerHTML = "";
        const fs = selectedPreset.fields || [];
        for (const f of fs) {
            const input = el("input", {
                type: f.secret ? "password" : "text", autocomplete: "off",
            });
            fieldInputs.push({ name: f.name, label: f.label, input });
            fieldsGroup.appendChild(
                el("div", { class: "field" }, [el("label", {}, [f.label]), input]));
        }
        if (fs.length && selectedPreset.setup) {
            fieldsGroup.appendChild(el("div", { class: "hint" },
                ["Runs at first boot: " + selectedPreset.setup]));
        }
        fieldsGroup.style.display = fs.length ? "" : "none";
    }

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
        if (!agentS.disabled) {
            agentS.value = selectedPreset.agent_default ? defaultBoxAgent : "none";
            if (agentS.value === "none" && noneLocked) agentS.value = defaultBoxAgent;
        }
        markAgent();
        // Pre-check the editor toggle from the preset's UI default (still un-checkable).
        editorCb.checked = !!selectedPreset.editor_default;
        editorCard.classList.toggle("selected", editorCb.checked);
        // A dev box refuses rs-fetch (it works its own fork; box_add rejects
        // the combination) — uncheck + disable rather than offer a rejection.
        if (isDev) {
            fetchCb.checked = false;
            fetchCard.classList.remove("selected");
        }
        fetchCard.classList.toggle("disabled", isDev);
        // The history switch follows the preset (and the host's Gitea).
        const offersHistory = !!selectedPreset.gitea_history && giteaEnabled && !isDev;
        if (!offersHistory) historyCb.checked = false;
        historyCard.style.display = offersHistory ? "" : "none";
        syncHistory();
        // Share files follows the type's default on every preset switch.
        shareCb.checked = selectedPreset.share_default === true;
        syncShare();
        // Browser follows the preset's image default on every preset switch; a
        // dev preset forces it off + disabled (base-image only — both gates
        // reject the combination, so the dialog must not offer it).
        browserCb.checked = !isDev && selectedPreset.image === "browser";
        browserCard.classList.toggle("selected", browserCb.checked);
        browserCard.classList.toggle("disabled", isDev);
        // A preset switch always starts restricted.
        trustedCb.checked = false;
        syncTrusted();
        // A dev box takes no read-only folders: untick + hide the group.
        if (isDev) {
            foldersCb.checked = false;
            foldersEntry.style.display = "none";
        }
        foldersGroup.style.display = isDev ? "none" : "";
        // Show the BYO clone fields only when the preset clones AND the repo isn't
        // baked into the preset (a baked-repo preset like paper-orchestra hides them);
        // a dev preset shows the attached-repo picker instead.
        byoGroup.style.display = (selectedPreset.clone && !selectedPreset.repo) ? "" : "none";
        devGroup.style.display = isDev ? "" : "none";
        // Declared input fields follow the preset. A fields preset also needs
        // an EXPLICIT box name (its env file is keyed by name before the box
        // exists) — prefill with the preset name so the server-side gate never
        // bites in the UI; only fill an empty input, never clobber a typed one.
        buildFields();
        if ((selectedPreset.fields || []).length && !nameI.value.trim()) {
            nameI.value = selectedPreset.name;
        }
    }
    function applyMcpCoupling() {
        // An MCP needs an agent: lock out "None" (the server coerces a bare
        // "none" to the default too), keep the agent choice open.
        noneLocked = mcpBoxes.some((b) => b.cb.checked);
        if (noneLocked && agentS.value === "none") agentS.value = defaultBoxAgent;
        markAgent();
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
                    // Cards, then the model/effort row to their right (hidden
                    // unless an agent is actually selected). One line, one group:
                    // "which agent, and which model it runs on" is one decision.
                    el("div", { class: "box-agent-row" }, [
                        agentCardsWrap,
                        boxModelRow,
                    ]),
                ]),
                boxHarnessGroup,
                el("div", { class: "box-opt-group" }, [
                    el("div", { class: "box-opt-caption" }, ["MCP tools"]),
                    mcpList,
                ]),
            ]),
            // Extensions and Sharing: two boxes under Settings, both folded
            // until opened (a box type's pre-selections still apply and are
            // sent). Sharing is what the box shares with the project — its
            // mailbox (Share files, every box type) and the project folders it
            // reads (hidden on a dev preset, which takes none).
            collapsibleBox("Extensions", [
                el("div", { class: "box-opt-cards" },
                   [editorCard, fetchCard, browserCard, trustedCard, historyCard]),
                trustedHint,
                historyHint,
            ], "box-extensions"),
            collapsibleBox("Sharing", [
                el("div", { class: "box-opt-cards" }, [shareCard]),
                shareHint,
                foldersGroup,
            ], "box-sharing"),
            fieldsGroup,
            byoGroup,
            devGroup,
        ],
        validate: () => {
            const n = nameI.value.trim();
            if (n && !/^[a-z][a-z0-9-]*$/.test(n)) {
                return "Box name must be lowercase, start with a letter, and use only letters, digits, or '-'.";
            }
            // Every declared field is required (the server gate demands the
            // exact declared set), and a fields preset needs an explicit name.
            for (const f of fieldInputs) {
                if (!f.input.value.trim()) return f.label + " is required.";
            }
            if (fieldInputs.length && !n) {
                return "This box type needs a name (its inputs are stored per box).";
            }
            if (historyCb.checked && !n) {
                return "A box keeping history on Gitea needs a name (its notes repo is named after it).";
            }
            if (selectedPreset.clone && repoI.value.trim() && !refI.value.trim()) {
                return "A repo requires a ref (pin the clone).";
            }
            if (!selectedPreset.dev && foldersCb.checked) {
                const paths = parseFolderList(foldersI.value);
                if (!paths.length) return "Enter at least one folder, or untick \"Add folders\".";
                const bad = folderListProblem(paths);
                if (bad) return bad;
            }
            if (selectedPreset.dev
                    && !devUrlI.value.trim().startsWith("https://github.com/")) {
                return "A dev box needs a GitHub repo URL (https://github.com/owner/repo).";
            }
            // Branch is REQUIRED in the webui (the in-supervisor CLI may omit it
            // and take the repo default). Client-side only: an omitted branch is
            // a valid server-side state, so this is an affordance, not a gate.
            if (selectedPreset.dev && !devBranchI.value.trim()) {
                return "A dev box needs a branch for the agent to work from.";
            }
            if (boxHarnessOn() && agentS.value === "pi"
                    && harnessModeRoles(boxHarnessSwitch.value()).length) {
                const bad = boxHarnessPi.invalid();
                if (bad) return bad;
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
                    // Unconditional — the validator above guarantees non-empty.
                    branch: devBranchI.value.trim(),
                    // Always explicit, like the plain box payload.
                    share: shareCb.checked,
                };
                if (agentS.value) payload.agent = agentS.value;
                // The dev preset posts to a DIFFERENT route (the detached
                // provision lane), so the model must be threaded here too — it is
                // the box type where the choice matters most, and a field missing
                // from DEV_BOX_WEBUI_FIELDS is dropped silently. Same
                // agent-gate as the control's visibility: a blank box sends none.
                if (agentModelOn()) {
                    const v = boxModelP.value();
                    if (v.model) payload.model = v.model;
                    if (v.effort) payload.effort = v.effort;
                }
                // The working mode, only where its controls show; the Doer/
                // Checker models only for the subagents the mode uses, for the box's
                // agent (lockstep: DEV_BOX_WEBUI_FIELDS).
                if (boxHarnessOn()) {
                    const mode = boxHarnessSwitch.value();
                    if (mode !== "none") payload.harness_mode = mode;
                    if (harnessModeRoles(mode).length) {
                        if (agentS.value === "claude") Object.assign(payload, boxHarnessClaude.value());
                        if (agentS.value === "pi") Object.assign(payload, boxHarnessPi.value());
                    }
                }
                return fetch(`/broker/project/${encodeURIComponent(project)}/dev-box`, {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
            }
            const payload = {
                name: nameI.value.trim() || null,
                preset: selectedPreset.name,
                editor: editorCb.checked,
                // Always explicit for non-dev (this payload branch): the
                // checkbox state IS the choice — applyPreset seeded it from the
                // preset's image, so sending it unconditionally can never
                // surprise, and the tri-state's None stays reachable only for
                // callers that genuinely omit the field (CLI).
                browser: browserCb.checked,
                // Only with the browser (both gates refuse it without).
                trusted_browser: browserCb.checked && trustedCb.checked,
                fetch: fetchCb.checked,
                mcps: mcpBoxes.filter((b) => b.cb.checked).map((b) => b.name),
                // Always explicit: the card's state IS the choice (applyPreset
                // seeded it from the type's default).
                share: shareCb.checked,
            };
            const folders = foldersCb.checked ? parseFolderList(foldersI.value) : [];
            if (folders.length) payload.folders = folders;
            if (historyCb.checked) payload.history = true;
            // Agent is always explicit now (the preset default is pre-selected,
            // not a sentinel).
            if (agentS.value) payload.agent = agentS.value;
            // Gated on the agent, exactly like the control's visibility — a blank
            // box (agent "none") runs no agent, so it sends no model.
            if (agentModelOn()) {
                const v = boxModelP.value();
                if (v.model) payload.model = v.model;
                if (v.effort) payload.effort = v.effort;
            }
            if (selectedPreset.clone) {
                const repo = repoI.value.trim(), ref = refI.value.trim(),
                    setup = setupT.value.trim();
                if (repo) payload.repo = repo;
                if (ref) payload.ref = ref;
                if (setup) payload.setup = setup;
            }
            // Declared field values ride ONE keyed object; the relay passes it
            // raw and the broker gates names against the preset's declaration.
            if (fieldInputs.length) {
                const fv = {};
                for (const f of fieldInputs) fv[f.name] = f.input.value.trim();
                payload.field_values = fv;
            }
            return fetch(`/broker/project/${encodeURIComponent(project)}/box`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
        },
        // Refresh on failure too: a box that failed to start stays in the
        // list (stopped) for the operator to remove; other failed adds leave
        // no box, and the refresh is then a harmless re-fetch.
        onDone: async () => { await refreshAfterBoxChange(project); },
        focus: () => nameI.focus(),
    });
}

// The remove dialog's mailbox sentence: only a box that shares files has one.
const BOX_REMOVE_MAILBOX_NOTE =
    "If it shares files with the project, its mailbox under shared/.boxes/ " +
    "stays either way.";

function mgmtBoxRemoveDialog(project, box) {
    const pwI = el("input", { type: "password", autocomplete: "current-password" });
    const keepCb = el("input", { type: "checkbox" });
    const warn = el("p", {}, [
        `This discards box "${box}" in "${project}" — its container and ` +
        "workspace are wiped. This cannot be undone. " + BOX_REMOVE_MAILBOX_NOTE,
    ]);
    // Toggle the warning copy: preserving artifacts is recoverable, wiping is not.
    keepCb.onchange = () => {
        warn.textContent = keepCb.checked
            ? `This removes box "${box}" in "${project}" — its container is torn ` +
              "down but its workspace artifacts stay on disk. " + BOX_REMOVE_MAILBOX_NOTE
            : `This discards box "${box}" in "${project}" — its container and ` +
              "workspace are wiped. This cannot be undone. " + BOX_REMOVE_MAILBOX_NOTE;
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

// ---- project rail drag-to-reorder (desktop only) ---------------------------
// The rail's twin of the tab strip's armTabDrag, following the same three
// rules. Pointer capture, NEVER HTML5 drag-and-drop: captured moves keep
// routing to the row while the pointer crosses a cross-origin editor iframe,
// where dragover events die inside the frame's browsing context. Hit-testing
// is MATHEMATICAL over row midpoints, never elementFromPoint, for the same
// reason. And lostpointercapture is a cancel path: a rail rebuild mid-drag (a
// create/destroy op completing, or syncSidebarFromBroker landing a late
// project) replaces the whole <aside> and detaches the captured row, which
// implicitly releases capture and sends the pointerup elsewhere — without it
// the ghost leaks and body user-select sticks.
// Press-vs-drag threshold: TAB_DRAG_SLOP_PX, one slop for every pointer drag
// in the app.

// With pointer capture the browser synthesises the post-drag click on the
// captured row (already detached by the drop's rebuild) — this flag lets the
// row's onclick swallow exactly that one click instead of switching project.
let projectDragClickPending = false;
function consumeProjectDragClick() {
    const pending = projectDragClickPending;
    projectDragClickPending = false;
    return pending;
}

function armProjectDrag(row, project) {
    row.onpointerdown = (ev) => {
        // Self-heal: a drag that ends without the browser synthesising a click
        // (a touch pointer wide enough to miss the mobile gate) would leave the
        // flag set and swallow the next legitimate press. A new press is always
        // the start of a new gesture, so clearing here is safe.
        projectDragClickPending = false;
        if (mobileModeActive()) return;         // desktop-only affordance
        if (ev.button !== 0) return;
        // The per-row gear is a <span>, not a <button>: closest("button")
        // alone would capture its pointer, swallow its own click and switch
        // project instead of opening the per-project config box.
        if (ev.target.closest("button, .project-config-btn")) return;
        const rail = row.closest(".project-rail");
        if (!rail) return;
        const startX = ev.clientX, startY = ev.clientY;
        let dragging = false;
        let ghost = null, indicator = null;
        let target = null;

        // Resolve the pointer to an insertion index over the rail's items —
        // the project rows and the group lines, in screen order.
        // Rects are re-read on EVERY move, never cached at drag start: it is
        // cheap, and it keeps the drop index right if anything reflows the
        // rail mid-gesture (a badge paint landing, a resize).
        const resolveTarget = (mv) => {
            const rr = rail.getBoundingClientRect();
            if (mv.clientX < rr.left || mv.clientX > rr.right
                || mv.clientY < rr.top || mv.clientY > rr.bottom) return null;
            const rows = Array.from(rail.querySelectorAll(".project, .rail-separator"));
            if (!rows.length) return null;
            let index = rows.length;
            for (let i = 0; i < rows.length; i++) {
                const r = rows[i].getBoundingClientRect();
                if (mv.clientY < r.top + r.height / 2) { index = i; break; }
            }
            return { index, rows };
        };

        // An accent caret at the insertion edge, anchored in the rail (which is
        // position: relative) so it scrolls with the rows: offsetTop is already
        // rail-relative, the rail being every row's offsetParent.
        const showFeedback = (t) => {
            if (indicator) { indicator.remove(); indicator = null; }
            if (!t) return;
            const anchor = t.rows[Math.min(t.index, t.rows.length - 1)];
            if (!anchor) return;
            const edgeY = t.index < t.rows.length
                ? anchor.offsetTop : anchor.offsetTop + anchor.offsetHeight;
            indicator = el("div", { class: "project-drop-indicator" });
            indicator.style.top = `${edgeY}px`;
            rail.appendChild(indicator);
        };

        const onMove = (mv) => {
            if (!dragging) {
                if (Math.abs(mv.clientX - startX) < TAB_DRAG_SLOP_PX
                    && Math.abs(mv.clientY - startY) < TAB_DRAG_SLOP_PX) return;
                dragging = true;
                row.classList.add("dragging");
                document.body.style.userSelect = "none";
                // A body-level LABEL, not a row clone: rail descendant rules
                // don't reach document.body (so a clone would render unstyled),
                // and a clone would carry the row's data-name into the three
                // document-wide [data-name] lookups (probe / status / activate).
                ghost = el("div", { class: "project-drag-ghost" },
                    [RAIL_SEPARATORS.includes(project.name) ? "Separator" : project.name]);
                document.body.appendChild(ghost);
            }
            ghost.style.left = `${mv.clientX + 10}px`;
            ghost.style.top = `${mv.clientY + 10}px`;
            target = resolveTarget(mv);
            showFeedback(target);
        };
        const cleanup = () => {
            // lostpointercapture FIRST: the explicit release below fires it
            // synchronously, and removing this listener before releasing is
            // what prevents a re-entrant onCancel -> cleanup cycle.
            row.removeEventListener("lostpointercapture", onCancel);
            row.removeEventListener("pointermove", onMove);
            row.removeEventListener("pointerup", onUp);
            row.removeEventListener("pointercancel", onCancel);
            try { row.releasePointerCapture(ev.pointerId); } catch (_) {}
            if (ghost) ghost.remove();
            if (indicator) indicator.remove();
            row.classList.remove("dragging");
            document.body.style.userSelect = "";
        };
        const onCancel = () => { cleanup(); };
        const onUp = async () => {
            const didDrag = dragging;
            const t = target;
            cleanup();
            if (!didDrag) return;               // plain click — let it through
            // Set SYNCHRONOUSLY, before any await: the synthesised click
            // arrives long before the persist below resolves.
            projectDragClickPending = true;
            if (!t) return;                     // dropped outside the rail
            // The new order is read off the SAME item list the index was
            // resolved over — never rebuilt from state.vault.projects, which a
            // broker sync can grow (one push per landed fetch) before the rail
            // repaints, and which would then put the row on the wrong side of
            // a separator and save a never-arranged name as arranged.
            const rows = t.rows.map((r) => r.classList.contains("rail-separator")
                ? r.getAttribute("data-sep") : r.getAttribute("data-name"));
            const from = t.rows.indexOf(row);
            if (from < 0) return;               // row went away mid-drag
            const to = t.index > from ? t.index - 1 : t.index;
            if (to === from) return;            // dropped where it already was
            const [moved] = rows.splice(from, 1);
            rows.splice(to, 0, moved);
            // Best-effort persist (the destroy path's precedent): a locked
            // vault or a full localStorage must not leave the rail showing the
            // pre-drag order while the live list already holds the new one.
            try {
                await writeProjectOrder(rows);
            } catch (e) { /* best-effort */ }
            refreshProjectRail();
        };
        row.setPointerCapture(ev.pointerId);
        row.addEventListener("pointermove", onMove);
        row.addEventListener("pointerup", onUp);
        row.addEventListener("pointercancel", onCancel);
        row.addEventListener("lostpointercapture", onCancel);
    };
}

// One of the rail's two group separators: a line the operator moves like a
// row (and moves rows across) to split the projects into groups. `name` is
// its entry in the saved order (RAIL_SEPARATORS), stamped as data-sep so the
// drop can read it back off the screen. Its own click only swallows the
// click a drag of it synthesises.
function makeRailSeparator(name) {
    const sep = el("div", {
        class: "rail-separator",
        "data-sep": name,
        title: "Drag to move the separator",
        onclick: () => { consumeProjectDragClick(); },
    });
    armProjectDrag(sep, { name });
    return sep;
}

function makeProjectRow(project) {
    const dot = el("span", { class: "status-dot" });
    const name = el("span", { class: "name" }, [project.name]);
    // No per-row DESTROY: project lifecycle is the Management panel's job
    // (broker create/destroy). The rail just reflects the running set — create
    // adds a row, destroy removes it (see mgmtConfirmThenTail onDone hooks).
    // The gear's Sidebar section does carry a per-row REMOVE, which is a
    // different thing: it forgets this browser's bookmark and touches nothing
    // on the sandbox (appendSidebarSection).
    const head = el("div", { class: "project-head" }, [dot, name]);
    // Second line: a project-type badge (research/sandbox/dev, painted once per
    // rail render by paintProjectBadges) plus the always-present config gear,
    // which opens the per-project floating config box. The badge starts empty
    // (collapsed) until the marker read lands; the gear keeps the line
    // non-empty so it shows from row construction. There is deliberately NO
    // activity sub-line here: filling one meant walking every project tree on
    // a timer. Disk size lives in the Management table, behind its button.
    const typeBadge = el("span", { class: "type-badge" });
    const configBtn = el("span", {
        class: "project-config-btn",
        title: "Project settings",
    }, ["⚙"]);
    configBtn.onclick = (ev) => {
        ev.stopPropagation();
        openProjectConfigBox(project, configBtn);
    };
    // "starting…" / "stopping…" / "updating…" / "queued…" while a lifecycle-lane
    // op is in flight for this project (paintLifecycleLabels keeps it current).
    const flight = lifecycleInFlight.get(project.name);
    const statusLine = el("div", { class: "project-status-line" },
        flight ? [typeBadge, el("span", { class: "project-inflight" },
                                [lifecycleLabel(flight)]), configBtn]
               : [typeBadge, configBtn]);
    // .active is stamped here so a rail rebuild (a drop, a create, a destroy)
    // keeps the "you are here" highlight — refreshProjectRail replaces the
    // whole <aside> and never re-activates. Gated on no host page being open:
    // entering Management / Workflows / Development / Settings deliberately
    // clears the rail highlight while LEAVING state.activeProject set (it is
    // what leaveHostView reads to come back), and create/destroy both rebuild
    // the rail from inside those pages — an ungated stamp would paint the
    // highlight back underneath a host view.
    const row = el("div", {
        class: "project" + (!state.hostPage && state.activeProject === project.name
            ? " active" : ""),
        "data-name": project.name,
        onclick: () => {
            if (consumeProjectDragClick()) return;
            activateProject(project.name);
        },
    }, [head, statusLine]);
    armProjectDrag(row, project);
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
    // The refresh does nothing while the tab is in the background; catch up
    // once the moment it is visible again. Installed once per page.
    if (!state.visibilityRefreshInstalled) {
        state.visibilityRefreshInstalled = true;
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) refreshActiveServices();
        });
    }
}

// ---- host CPU / RAM bars (rail bottom) -------------------------------------
// Host-wide figures from GET /broker/host-stats — a webui-local /proc read,
// gated on the Management session (that path is the session cookie's Path).
// The server returns raw counters; the CPU figure is the busy share of the
// jiffies that passed between THIS page's oldest sample of the last minute
// and its newest — a true one-minute average, so a single busy tick does not
// make the bar jump. No data (locked, no session, unreadable /proc) = the
// block is hidden — never a placeholder or a hint.

function makeHostStats() {
    const bar = (key, label, title) => el("div", { class: "host-stat-bar", "data-stat": key, title }, [
        el("div", { class: "host-stat-fill" }),
        el("span", { class: "host-stat-label" }, [label]),
        el("span", { class: "host-stat-text" }),
    ]);
    return el("div", { class: "host-stats" }, [
        bar("cpu", "CPU:", "Host CPU in use, averaged over the last minute"),
        bar("ram", "RAM:", "Host memory in use / total"),
        el("div", { class: "host-stat-note", "data-stat": "stale", hidden: "" },
            ["The broker is running older code than is installed on the host — restart the broker to load it."]),
    ]);
}

// Paint one stats block from state.hostStats. Takes the node rather than
// querying the document: inside refreshProjectRail the block being built is
// not attached yet, and a query would find the outgoing rail's.
function paintHostStats(node) {
    if (!node) return;
    const s = state.hostStats;
    node.hidden = !s;
    if (!s) return;
    const set = (key, frac, text) => {
        const bar = node.querySelector(`.host-stat-bar[data-stat="${key}"]`);
        bar.querySelector(".host-stat-fill").style.width =
            frac == null ? "0%" : (Math.min(1, Math.max(0, frac)) * 100).toFixed(1) + "%";
        bar.querySelector(".host-stat-text").textContent = text;
    };
    set("cpu", s.cpu, s.cpu == null ? "…" : `${Math.round(s.cpu * 100)} %`);
    set("ram", s.memUsed / s.memTotal,
        `${(s.memUsed / BYTES_PER_GB).toFixed(1)}/${(s.memTotal / BYTES_PER_GB).toFixed(1)} GB`);
    node.querySelector('.host-stat-note[data-stat="stale"]').hidden = !state.brokerStale;
}

// The painted figures are older than the CPU window: the webui has not
// answered for a minute (refused, hung, or replying with something that is
// not the stats). Drop them rather than show minute-old numbers as live.
function dropStaleHostStats() {
    const last = state.hostCpuSamples[state.hostCpuSamples.length - 1];
    if (!state.hostStats || (last && Date.now() - last.t <= CPU_AVG_WINDOW_MS)) return;
    state.hostStats = null;
    state.hostCpuSamples = [];
    paintHostStats(document.querySelector(".project-rail .host-stats"));
}

async function refreshHostStats() {
    // Nobody is looking: skip the request. The sample window is pruned by
    // age, so a long hidden spell ages its samples out by itself.
    if (!state.vault || document.hidden) return;
    if (state.hostStatsInFlight) { dropStaleHostStats(); return; }
    state.hostStatsInFlight = true;
    let res = null;
    let body = null;
    try {
        res = await fetch("/broker/host-stats");
        if (res.ok) body = await res.json();
    } catch (e) {
        res = null;
    } finally {
        state.hostStatsInFlight = false;
    }
    if (!state.vault) return;   // locked while the request was in flight
    // A transport blip (no response, or a 200 we could not read) keeps what
    // is painted and the minute of samples; the next tick retries — until
    // the newest good sample is a minute old.
    if (!res || (res.ok && body === null)) { dropStaleHostStats(); return; }
    state.brokerStale = !!(body && body.ok && body.broker_stale === true);
    const r = body && body.ok && body.result;
    if (!r || !r.cpu || !r.mem || !(r.mem.total > 0)) {
        // An answer that says no: no session (401 — the poll keeps running,
        // so a later Management login brings the bars back), unreadable
        // /proc (503), or an error from a proxy in front of the webui.
        state.hostStats = null;
        state.hostCpuSamples = [];
    } else {
        const now = Date.now();
        // x.t <= now: a clock set backwards restarts the window rather than
        // stretching it over samples stamped in the "future".
        const samples = state.hostCpuSamples.filter((x) => x.t <= now && now - x.t <= CPU_AVG_WINDOW_MS);
        // No sample in the window (first reading, or the first after a long
        // hidden spell) → no CPU figure yet, never the one from before.
        let cpu = samples.length && state.hostStats ? state.hostStats.cpu : null;
        if (samples.length) {
            const first = samples[0];
            const dTotal = r.cpu.total - first.total;
            // total includes iowait, which the kernel can run backwards: a
            // non-positive window keeps the last figure, and the share is
            // clamped so a wobble never paints past either end.
            if (dTotal > 0) cpu = Math.min(1, Math.max(0, (r.cpu.busy - first.busy) / dTotal));
        }
        samples.push({ t: now, busy: r.cpu.busy, total: r.cpu.total });
        state.hostCpuSamples = samples;
        state.hostStats = {
            cpu,
            memUsed: Math.max(0, r.mem.total - r.mem.available),
            memTotal: r.mem.total,
        };
    }
    paintHostStats(document.querySelector(".project-rail .host-stats"));
}

// Coming back to a tab whose newest sample is at least a tick old (a tick was
// skipped while hidden): sample at once so RAM is current immediately, and
// restart the tick so an overdue throttled one cannot follow milliseconds
// later. A hidden spell shorter than a tick needs nothing.
function installHostStatsResume() {
    document.addEventListener("visibilitychange", () => {
        if (document.hidden || !state.vault) return;
        const last = state.hostCpuSamples[state.hostCpuSamples.length - 1];
        if (!last || Date.now() - last.t >= HOST_STATS_INTERVAL_MS) scheduleHostStats(true);
    });
}

// (Re)start the tick. Every out-of-band sample goes through here, so the next
// tick is always a full interval after it. A shell re-render (breakpoint
// flip, Layout change) already paints the cached figures, so it samples at
// once only when nothing is cached — or when `now` asks for it.
function scheduleHostStats(now) {
    if (state.hostStatsTimer) clearInterval(state.hostStatsTimer);
    state.hostStatsTimer = null;
    // Locked meanwhile (a login still pending at lock time): start nothing.
    if (!state.vault) return;
    if (now || !state.hostStats) refreshHostStats();
    state.hostStatsTimer = setInterval(refreshHostStats, HOST_STATS_INTERVAL_MS);
}

// Tab-visible fingerprint: the sorted service ids plus their labels, the
// detected/stopped marks and the address. A newly-listening port adds an id; a
// re-add relabels; a stopped listener drops one; a hand-add clears `detected`; a
// stopped detected app gains `stopped`; a freed address changes `origin_url` —
// all move the signature, so the guard in refreshActiveServices fires only then.
function serviceTabsSignature(map) {
    return Object.keys(map || {}).sort()
        .map((id) => {
            const s = map[id] || {};
            return `${id}\x00${s.label || ""}\x00${s.detected ? 1 : 0}` +
                `${s.stopped ? 1 : 0}\x00${s.origin_url || ""}`;
        })
        .join("\x01");
}

// Repaint the strip's underline on `serviceId` without activating anything
// (a re-render drops it); desktop columns repaint their own actives.
function markActiveTab(serviceId) {
    document.querySelectorAll("#service-tabs .tab").forEach((t) => t.classList.remove("active"));
    const tabEl = document.querySelector(`#service-tabs .tab[data-service="${CSS.escape(serviceId)}"]`);
    if (tabEl) tabEl.classList.add("active");
    if (!mobileModeActive()) paintColumnActives();
}

// The service ids on screen right now: the focused tab and every column's
// shown tab (an app in one column beside the agent's terminal is on screen).
function onScreenServiceIds() {
    return [state.activeService, ...(state.columnActive || [])].filter(Boolean);
}

// Fold the current detected tabs into a fresh tab map BEFORE the signature
// comparison, so a kept tab never redraws the strip on its own. `detection`
// is the fetch's marker:
//   ""        authoritative — a vanished detected tab stays only while on
//             screen, marked stopped;
//   busy / unavailable / withheld — detection unknown: every detected tab
//             is kept as it was;
//   fallback  (/services) — on-screen detected tabs kept, unmarked (detection
//             is unavailable, not negative); off-screen ones dropped;
//   off       — detected tabs dropped.
// On every gated reply (all but fallback) a kept tab keeps its address only
// while that address is still on the payload's Opened apps list — so a Remove
// made in another browser is seen. Pure: no DOM, no state.
function mergeDetectedTabs(fresh, current, onScreen, detection, opened) {
    const out = Object.assign({}, fresh || {});
    if (detection === "off") return out;
    const unknown = detection === "busy" || detection === "unavailable"
        || detection === "withheld";
    const urls = new Set((opened || []).map((o) => o.origin_url));
    for (const [id, spec] of Object.entries(current || {})) {
        if (!spec || !spec.detected || id in out) continue;
        const shown = onScreen.includes(id);
        if (!unknown && !shown) continue;
        const kept = Object.assign({}, spec);
        if (detection === "") kept.stopped = true;
        if (detection !== "fallback" && kept.origin_url && !urls.has(kept.origin_url)) {
            delete kept.origin_url;
        }
        out[id] = kept;
    }
    return out;
}

// Which open http panes of `projectName` must close because their tab's
// address went away or changed. A pane counts only if it loaded a per-project
// address (editor, box editor, port tabs — never Gitea, Fetch or launcher
// panes, which record none). Missing means: the tab is in the fresh map with
// no address or a different one, or a port-<n> tab is absent from a payload
// authoritative for it (a hand-added one: always; a detected one: an
// authoritative detection, or detection switched off — its address was freed). An editor absent from a reply is no change — its
// tab is probe-gated and its address cannot change while the project lives.
// Pure: returns [{key, id, reopen}] where reopen says what replaces it.
function panesToClose(terminals, projectName, prev, fresh, detection, onScreen) {
    const out = [];
    for (const [key, t] of Object.entries(terminals || {})) {
        if (!t || t.kind !== "http" || !t.loadedUrl) continue;
        if (!t.project || t.project.name !== projectName) continue;
        const id = t.service;
        const spec = fresh[id];
        let gone;
        if (spec) {
            gone = !spec.origin_url || spec.origin_url !== t.loadedUrl;
        } else if (id.startsWith("port-")) {
            const was = (prev || {})[id];
            gone = !(was && was.detected) || detection === "" || detection === "off";
        } else {
            gone = false;
        }
        if (!gone) continue;
        let reopen = "none";
        if (spec && onScreen.includes(id)) reopen = spec.origin_url ? "open" : "note";
        out.push({ key, id, reopen });
    }
    return out;
}

const TAB_ADDRESS_FREED_TEXT = "This app's view was closed because its tab " +
    "address was freed. Click the tab to reopen it.";
const CLICK_TO_OPEN_TEXT = "Click the tab to open this app.";

// Apply panesToClose to the live DOM: tear each pane down the way
// refreshService does, then reopen an on-screen one in place (never moving
// focus) or leave the one-line note as that tab's pane entry — registered
// disconnected, so the column engine positions it and a click takes
// activateService's disconnected branch, which removes it and reopens.
function followTabAddresses(projectName, prev, fresh, detection, onScreenIds) {
    const onScreen = onScreenIds || onScreenServiceIds();
    for (const c of panesToClose(state.terminals, projectName, prev, fresh,
                                 detection, onScreen)) {
        const t = state.terminals[c.key];
        try { if (t.container) t.container.remove(); } catch (_) {}
        delete state.terminals[c.key];
        if (c.reopen === "open") {
            activateService(c.id, { background: c.id !== state.activeService });
        } else if (c.reopen === "note") {
            const container = el("div", { class: "terminal-instance http-instance" }, [
                el("div", { class: "http-status" }, [TAB_ADDRESS_FREED_TEXT]),
            ]);
            const area = document.getElementById("terminal-area");
            if (area) area.appendChild(container);
            state.terminals[c.key] = {
                kind: "http", container, service: c.id, disconnected: true, note: true,
                project: state.vault.projects.find((p) => p.name === projectName),
            };
        }
    }
    if (!mobileModeActive()) applyColumnLayout();
}

async function refreshActiveServices() {
    // A background browser tab does nothing (the visibilitychange listener
    // refreshes once on return).
    if (document.hidden) return;
    const name = state.activeProject;
    if (!name) return;
    // One detection per project at a time; one more is queued behind it.
    if (state.detectInFlight[name]) { state.detectPending[name] = true; return; }
    state.detectInFlight[name] = true;
    let r;
    try {
        r = await fetchProjectTabs(name, true);
    } finally {
        state.detectInFlight[name] = false;
    }
    if (state.detectPending[name]) {
        state.detectPending[name] = false;
        setTimeout(() => { if (state.activeProject === name) refreshActiveServices(); }, 0);
    }
    if (!r) return;   // transient; the next tick retries
    // The active project (or the whole vault) may have changed while the fetch
    // was in flight — bail rather than render stale tabs onto a different view.
    if (state.activeProject !== name) return;
    const current = state.projectServices[name] || {};
    const fresh = mergeDetectedTabs(r.services, current, onScreenServiceIds(),
                                    r.detection, r.opened);
    if (serviceTabsSignature(fresh) === serviceTabsSignature(current)) return;
    state.projectServices[name] = fresh;
    renderServiceTabs(name, fresh);
    // A pane whose tab's address went away or changed closes (or reopens on
    // the new one) before the re-activation below can resurface it.
    followTabAddresses(name, current, fresh, r.detection);
    // The column engine must reconcile on THIS signature-change path too —
    // a vanished service's tab would otherwise linger in its column strip
    // and an appeared one would never join. Reconciliation is pure (no
    // vault write), so a flapping probe can't churn the vault.
    applyColumnLayout();
    // renderServiceTabs rebuilds the strip without the active underline; re-apply
    // it. activateService returns early on an existing, connected terminal, so no
    // open pane is torn down. If the active service vanished (a listener stopped),
    // fall back to the first visible tab — mirrors setServiceHidden.
    const visible = visibleServiceIds(name, fresh);
    const activeEntry = state.activeService
        ? state.terminals[tkey(name, state.activeService)] : null;
    if (state.activeService && visible.includes(state.activeService)
            && activeEntry && activeEntry.note) {
        // The focused tab's view was closed because its address was freed:
        // keep the note on screen (re-activating would reserve an address
        // again by itself); only repaint the underline the re-render dropped.
        markActiveTab(state.activeService);
    } else if (state.activeService && visible.includes(state.activeService)) {
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

// Paint each rail row's workflow badge from ONE marker-only /projects/status
// call. Runs once per rail render (initial build, every refreshProjectRail) —
// never on a timer: the label changes only when a project is created or
// destroyed, and both rebuild the rail. A project with no workspace on disk
// (or a name the server rejects) keeps an empty, collapsed badge.
async function paintProjectBadges() {
    if (!state.vault || state.vault.projects.length === 0) return;
    const names = state.vault.projects.map((p) => p.name).join(",");
    let data;
    try {
        const res = await fetch(`/projects/status?names=${encodeURIComponent(names)}`);
        if (!res.ok) return;
        data = await res.json();
    } catch (_) {
        return;   // the next rail render repaints
    }
    for (const [name, st] of Object.entries(data)) {
        const row = document.querySelector(`.project[data-name="${CSS.escape(name)}"]`);
        const badge = row && row.querySelector(".project-status-line .type-badge");
        if (!badge) continue;
        setTypeBadge(badge, st && st.error !== "not_found" ? st.workflow : null);
    }
}

function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    const units = ["kB", "MB", "GB", "TB"];
    let v = n / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v >= 10 ? `${Math.round(v)} ${units[i]}` : `${v.toFixed(1)} ${units[i]}`;
}

function lockVault() {
    closeFloatBox();
    if (state.probeTimer) { clearInterval(state.probeTimer); state.probeTimer = null; }
    if (state.servicesTimer) { clearInterval(state.servicesTimer); state.servicesTimer = null; }
    if (state.hostStatsTimer) { clearInterval(state.hostStatsTimer); state.hostStatsTimer = null; }
    state.hostStats = null;
    state.hostCpuSamples = [];
    state.brokerStale = false;
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
    state.columnActive = [];
    state.tabLayout = null;
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

// ---- CLI pane max width (Settings dropdown) --------------------------------

function loadCliMaxWidth() {
    const v = parseInt(localStorage.getItem(CLI_WIDTH_KEY) || "", 10);
    return CLI_MAX_WIDTHS.includes(v) ? v : 0;
}

// The cap rides a root CSS var consumed by .terminal-pad (style.css) — the
// engine-invisible inset wrapper xterm-addon-fit measures — so a change
// reflows terminal cols with zero xterm configuration. Off removes the
// property; the CSS falls back to max-width: none (byte-inert rule).
function applyCliMaxWidthVar(v) {
    if (v > 0) document.documentElement.style.setProperty("--cli-max-width", `${v}px`);
    else document.documentElement.style.removeProperty("--cli-max-width");
}

function setCliMaxWidth(v) {
    state.cliMaxWidth = v;
    applyCliMaxWidthVar(v);
    if (v > 0) localStorage.setItem(CLI_WIDTH_KEY, String(v));
    else localStorage.removeItem(CLI_WIDTH_KEY);
    // Belt-and-braces: Settings is a host page (terminal area hidden, so this
    // no-ops there — fit() proposes nothing at zero size) and leaving Settings
    // refits via activateProject → applyColumnLayout anyway; the explicit call
    // covers any future setting surface that changes the var while panes show.
    scheduleColumnRefit();
}

function makeCliWidthSelector() {
    const sel = document.createElement("select");
    sel.className = "theme-select";
    sel.title = "Max width of CLI terminal panes (readability on wide windows)";
    // Col counts are honest approximations at the 13px terminal font (see the
    // CLI_MAX_WIDTHS comment); "~" is doing real work — the exact advance
    // width varies slightly by platform monospace font.
    const labels = {
        0: "Full width",
        800: "800 px (~100 cols)",
        1000: "1000 px (~125 cols)",
        1200: "1200 px (~150 cols)",
    };
    for (const w of CLI_MAX_WIDTHS) {
        const opt = document.createElement("option");
        opt.value = String(w);
        opt.textContent = labels[w] || `${w} px`;
        if (w === state.cliMaxWidth) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.onchange = () => setCliMaxWidth(parseInt(sel.value, 10));
    return sel;
}

// ---- CLI terminal font size (Settings dropdown) ----------------------------

function loadTermFontSize() {
    const v = parseInt(localStorage.getItem(TERM_FONT_SIZE_KEY) || "", 10);
    return TERM_FONT_SIZES.includes(v) ? v : TERM_FONT_SIZE_DEFAULT;
}

// DEFER-APPLY: the setter stores + persists only — the option lands
// per-terminal via syncTermFontSize at the next refit/show, NEVER here. This
// setter is only reachable from Settings, a host page that display:none's the
// whole terminal area, and xterm 5.3.0 cannot take a font change while
// hidden: CharSizeService.measure() RETAINS the previous cell metrics on a
// zero-geometry measure, and nothing re-measures on re-show — so a direct
// option write here leaves stale metrics that every later fit() consumes,
// desyncing the pty grid from the render (missing byobu/claude bottom bars,
// freeze-until-typing). New terminals pick the value up at the constructor
// (fontSize: state.termFontSize).
function setTermFontSize(v) {
    state.termFontSize = v;
    if (v !== TERM_FONT_SIZE_DEFAULT) localStorage.setItem(TERM_FONT_SIZE_KEY, String(v));
    else localStorage.removeItem(TERM_FONT_SIZE_KEY);
    scheduleColumnRefit();
}

// Apply the stored font size to one terminal iff its pane is actually VISIBLE
// — the sole writer of options.fontSize on live terminals. Guards: the
// engine's own .hidden vocabulary (pane-level), then offsetParent === null,
// which catches display:none on ANY ancestor (the host-page case: the pane's
// class says visible while #terminal-area is hidden — valid because
// .terminal-instance is position:absolute under the position:relative area,
// so offsetParent is the area normally and null exactly when an ancestor is
// display:none). Applying while visible makes xterm's re-measure correct by
// construction. Fail-safe by design: a hidden pane is REFUSED, so a missed
// application site degrades to "old font, self-consistent metrics, applies
// at the next covered show" — never the desync above. Call sites: the
// scheduleColumnRefit loop, the activateService fast path, and the mobile
// drawer-close refit (the one leave-Settings route that bypasses the other
// two).
function syncTermFontSize(t) {
    if (!t || !t.term || !t.container) return;
    if (t.container.classList.contains("hidden")) return;
    if (t.container.offsetParent === null) return;
    if (t.term.options.fontSize !== state.termFontSize) {
        t.term.options.fontSize = state.termFontSize;
    }
}

function makeTermFontSizeSelector() {
    const sel = document.createElement("select");
    sel.className = "theme-select";
    sel.title = "Font size of CLI terminal panes (applies to open tabs immediately)";
    for (const n of TERM_FONT_SIZES) {
        const opt = document.createElement("option");
        opt.value = String(n);
        opt.textContent = n === TERM_FONT_SIZE_DEFAULT ? `${n} px (default)` : `${n} px`;
        if (n === state.termFontSize) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.onchange = () => setTermFontSize(parseInt(sel.value, 10));
    return sel;
}

function makeThemeSelector() {
    const sel = document.createElement("select");
    sel.className = "theme-select";
    sel.title = "Interface theme";
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

// Terminal theme (Settings): "" = follow the interface theme; the named
// palettes sit in dark/light optgroups. Applies live to open terminals.
function makeTermThemeSelector() {
    const sel = document.createElement("select");
    sel.className = "theme-select";
    sel.title = "Terminal theme (CLI tabs only)";
    const follow = document.createElement("option");
    follow.value = "";
    follow.textContent = "Same as interface theme";
    if (!state.termTheme) follow.selected = true;
    sel.appendChild(follow);
    for (const [tone, label] of [["dark", "Dark palettes"], ["light", "Light palettes"]]) {
        const group = document.createElement("optgroup");
        group.label = label;
        for (const [id, t] of Object.entries(TERM_THEMES)) {
            if (t.tone !== tone) continue;
            const opt = document.createElement("option");
            opt.value = id;
            opt.textContent = t.label;
            if (id === state.termTheme) opt.selected = true;
            group.appendChild(opt);
        }
        sel.appendChild(group);
    }
    sel.onchange = () => applyTermTheme(sel.value || null);
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
    // command. A project on this sandbox attaches with one click (Sandboxes →
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
            // The rebuild closes any open page; Import (from the New Project
            // box) must not change the page underneath, so reopen it.
            const page = state.hostPage;
            await renderDashboard();
            if (page) openHostPage(page);
        } catch (e) {
            errEl.textContent = "Save failed: " + e.message;
        }
    };

    const card = el("div", { class: "card" }, [
        el("h2", {}, ["Import project"]),
        el("div", { class: "hint" }, [
            "For a project on this sandbox, use Attach under Sandboxes → Management — it "
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
            // The pane node goes too, as at every other teardown site. Nothing
            // sweeps an orphan: showWelcome and the column engine both iterate
            // state.terminals, which no longer carries this one, so a surviving
            // .terminal-instance stays inset:0 over the welcome pane until the
            // page is reloaded. An http pane is the visible case — it has
            // neither ws nor term, so the two lines above are no-ops for it,
            // and its iframe is opaque.
            try { if (t.container) t.container.remove(); } catch (_) {}
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

// ---- column layout engine (flat host) --------------------------------------

// The terminal area is a FLAT host: every .terminal-instance stays its direct
// child forever — the engine assigns visible containers computed rectangles
// (percent-based inline left/width/top) instead of re-parenting them into
// pane wrappers. Load-bearing: moving an <iframe> node ANYWHERE in the DOM
// discards and reloads its browsing context (spec behavior — the retired
// split-pane code did exactly that on pin toggles), so arrangement must be
// geometry-only. Percent units make window resizes self-adapting; the global
// refit listener + scheduleColumnRefit handle xterm re-measurement.

// Service id back out of a terminals key (`${project}:${service}`) for the
// active project.
function serviceOfKey(k) {
    return k.slice(state.activeProject.length + 1);
}

const pctOf = (f) => `${(f * 100).toFixed(4)}%`;

// Reconcile stored per-column width fractions against the column count:
// filter junk, truncate/refill, normalize, floor at COLUMN_MIN_FRAC,
// renormalize. Pure — never writes the vault.
function reconcileColumnRatios(project, nColumns) {
    const stored = Array.isArray(project?.column_ratios) ? project.column_ratios : [];
    const ratios = stored.filter((r) => typeof r === "number" && r > 0).slice(0, nColumns);
    while (ratios.length < nColumns) ratios.push(1 / nColumns);
    const sum = ratios.reduce((a, b) => a + b, 0);
    const floored = ratios.map((r) => Math.max(COLUMN_MIN_FRAC, r / sum));
    const sum2 = floored.reduce((a, b) => a + b, 0);
    return floored.map((r) => r / sum2);
}

// Remove the engine's own chrome. The floating search bar is also an
// absolute child of the area and must survive — the selector names only
// engine-owned classes.
function sweepColumnChrome(area) {
    area.querySelectorAll(".column-splitter, .column-drop-overlay")
        .forEach((n) => n.remove());
}

function clearContainerGeometry(t) {
    if (!t.container) return;
    t.container.style.left = "";
    t.container.style.width = "";
    t.container.style.top = "";
}

// Deferred xterm refit after any geometry change (a container resize is
// invisible to xterm until fit() re-measures). Iframes reflow on their own.
// Also the font-size application choke point: sync BEFORE fit, per terminal —
// the option set triggers xterm's synchronous re-measure at the new font, and
// the fit then consumes the fresh metrics (see syncTermFontSize).
function scheduleColumnRefit() {
    setTimeout(() => {
        for (const t of Object.values(state.terminals)) {
            syncTermFontSize(t);
            if (t.fitAddon) { try { t.fitAddon.fit(); } catch (_) {} }
        }
    }, 0);
}

// Position-only pass: set left/width on the splitters and the visible
// containers from a candidate ratio set (top stays the base inset:0 — the
// tabs live in the ONE top strip, so panes span the area's full height).
// Split out of the full engine so the splitter drag can run it per frame
// without any DOM rebuild — a mid-drag rebuild would detach the splitter's
// pointer capture (the class of bug the tab drag's lostpointercapture path
// covers).
function positionColumnChrome(columns, ratios) {
    const area = document.getElementById("terminal-area");
    if (!area || !state.activeProject) return;
    const lefts = [];
    let x = 0;
    for (const r of ratios) { lefts.push(x); x += r; }
    area.querySelectorAll(".column-splitter").forEach((s, i) => {
        s.style.left = `calc(${pctOf(lefts[i + 1])} - 3px)`;
    });
    // The strip's tab groups track the pane boundaries — the strip spans
    // the same width as the area, so the fractions map 1:1.
    positionStripGroups(ratios);
    for (const [k, t] of Object.entries(state.terminals)) {
        if (!k.startsWith(`${state.activeProject}:`) || !t.container) continue;
        if (t.container.classList.contains("hidden")) continue;
        const col = columns.findIndex((c) => c.includes(serviceOfKey(k)));
        if (col === -1) continue;
        t.container.style.left = pctOf(lefts[col]);
        t.container.style.width = pctOf(ratios[col]);
    }
}

// Geometry for the strip's column tab groups (desktop multi-column): group 0
// runs from where the chrome cluster ends (its pane starts at x=0, which the
// chrome occupies) to the first boundary; each further group spans exactly
// its pane's fraction. Width-capping is what makes NO-OVERLAP structural —
// tabs inside a capped group flex-shrink with ellipsized labels, the
// wrapper clips as the last resort. The px/% calc mix keeps window resizes
// self-adapting: the chrome width is content-fixed px (re-measured every
// render via the divider's offset edge), the boundaries are fractions.
// Degenerate accepted edges, both from a first boundary landing near/left
// of the chrome end: (a) group 0's width computes negative — CSS floors it
// at 0 and column 0's tabs vanish until the splitter widens it; (b) group
// 1's anchor slides under the chrome cluster — the chrome's z-lift
// (style.css) keeps it visible + clickable while that group's leftmost
// tabs clip beneath it.
function positionStripGroups(ratios) {
    const strip = document.getElementById("service-tabs");
    if (!strip) return;
    const groups = strip.querySelectorAll(".column-tab-group");
    if (!groups.length) return;
    const divider = strip.querySelector(".tab-group-divider");
    const chromeEnd = divider ? divider.offsetLeft + divider.offsetWidth : 0;
    let x = 0;
    groups.forEach((g, i) => {
        if (i === 0) {
            g.style.left = `${chromeEnd}px`;
            g.style.width = `calc(${pctOf(ratios[0])} - ${chromeEnd}px)`;
        } else {
            g.style.left = pctOf(x);
            g.style.width = pctOf(ratios[i]);
        }
        x += ratios[i];
    });
}

// Repaint the top strip's per-column actives from columnActive: each
// column's shown tab carries .active (several at once in a split layout).
// The tabs' data-column stamps come from renderServiceTabs, which derives
// the same reconciled columns the engine stashed.
function paintColumnActives() {
    const layout = state.tabLayout;
    if (!layout) return;
    document.querySelectorAll("#service-tabs .tab[data-service]").forEach((t) => {
        const id = t.getAttribute("data-service");
        const col = layout.columns.findIndex((c) => c.includes(id));
        t.classList.toggle("active", col !== -1 && state.columnActive[col] === id);
    });
}

// The full engine: derive the reconciled layout from vault + live services,
// stash it as state.tabLayout (the drag/splitter working copy), repair the
// per-column actives, maintain the splitters (>= 2 columns; the tabs stay
// in the ONE top strip, each column's group anchored above its pane — PI
// decisions across two dogfood rounds: per-pane strip rows wasted a line,
// and flowing groups lost the tab-to-pane alignment), and set every
// container's visibility + geometry. Desktop-only — the
// mobile shell keeps the flat single-strip model — so on mobile (and with
// no project / no services / no vault entry) it sweeps its chrome and
// clears inline geometry: that reset IS the explicit teardown arm for
// project switches and the zero-visible welcome path.
function applyColumnLayout() {
    const area = document.getElementById("terminal-area");
    if (!area) return;
    const reset = () => {
        sweepColumnChrome(area);
        for (const t of Object.values(state.terminals)) clearContainerGeometry(t);
        state.tabLayout = null;
    };
    if (!state.activeProject || mobileModeActive()) { reset(); return; }
    const project = state.vault?.projects.find((p) => p.name === state.activeProject);
    const enabled = state.projectServices[state.activeProject] || {};
    const ids = visibleServiceIds(state.activeProject, enabled);
    if (!project || ids.length === 0) { reset(); return; }

    const columns = reconcileTabLayout(project, ids, enabled);
    const ratios = reconcileColumnRatios(project, columns.length);
    state.tabLayout = { columns, ratios };
    // Repair per-column actives: each column shows its remembered tab when
    // that tab still lives there, else its first.
    state.columnActive.length = columns.length;
    for (let i = 0; i < columns.length; i++) {
        if (!columns[i].includes(state.columnActive[i])) {
            state.columnActive[i] = columns[i][0];
        }
    }

    sweepColumnChrome(area);
    if (columns.length === 1) {
        // Single column: today's exact shape — no engine chrome; containers
        // fall back to the base inset:0 (stale rects from a fold MUST be
        // cleared or they survive over the base rule); visibility = the one
        // active tab.
        const activeKey = tkey(state.activeProject, state.columnActive[0]);
        for (const [k, t] of Object.entries(state.terminals)) {
            if (!t.container) continue;
            // Another project's pane must NEVER stay visible: containers are
            // absolutely-stacked siblings in the flat host, so a skipped
            // foreign pane sits (selectable) behind the new project's — the
            // retired activateService hide-loop swept ALL projects and the
            // engine owns that job now.
            if (!k.startsWith(`${state.activeProject}:`)) {
                t.container.classList.add("hidden");
                continue;
            }
            clearContainerGeometry(t);
            t.container.classList.toggle("hidden", k !== activeKey);
        }
        scheduleColumnRefit();
        return;
    }

    // Multi-column: splitters between neighbors (the tabs live in the top
    // strip, grouped per column by renderServiceTabs), then visibility +
    // geometry + the strip's per-column actives.
    for (let i = 1; i < columns.length; i++) {
        const sp = el("div", { class: "column-splitter" });
        installColumnSplitterDrag(sp, i - 1);
        area.appendChild(sp);
    }
    for (const [k, t] of Object.entries(state.terminals)) {
        if (!t.container) continue;
        // Foreign-project panes are hidden, never skipped — same rationale
        // as the single-column arm above.
        if (!k.startsWith(`${state.activeProject}:`)) {
            t.container.classList.add("hidden");
            continue;
        }
        const svcId = serviceOfKey(k);
        const col = columns.findIndex((c) => c.includes(svcId));
        const show = col !== -1 && svcId === state.columnActive[col];
        t.container.classList.toggle("hidden", !show);
    }
    positionColumnChrome(columns, ratios);
    paintColumnActives();
    scheduleColumnRefit();
}

// Splitter between column `boundary` and `boundary+1`: pointer-captured
// drag — capture is load-bearing, without it a pointermove crossing into a
// code-server iframe is delivered to the iframe's browsing context and the
// drag freezes (the original pane-splitter's discovery) — adjusting the two
// adjacent ratios under the COLUMN_MIN_FRAC clamp. Geometry-only per frame
// (a mid-drag strip rebuild would detach this very capture); persist + refit
// on release. lostpointercapture is removed FIRST in the shared release
// handler, before releasePointerCapture — the explicit release fires it
// synchronously and remove-first prevents the re-entrant cycle (the tab
// drag's discipline).
function installColumnSplitterDrag(splitter, boundary) {
    splitter.onpointerdown = (ev) => {
        const layout = state.tabLayout;
        const area = document.getElementById("terminal-area");
        const project = state.vault?.projects.find((p) => p.name === state.activeProject);
        if (!layout || !area || !project) return;
        ev.preventDefault();
        const pair = layout.ratios[boundary] + layout.ratios[boundary + 1];
        const onMove = (mv) => {
            const rect = area.getBoundingClientRect();
            if (rect.width <= 0) return;
            let leftEdge = 0;
            for (let i = 0; i < boundary; i++) leftEdge += layout.ratios[i];
            let share = (mv.clientX - rect.left) / rect.width - leftEdge;
            share = Math.max(COLUMN_MIN_FRAC, Math.min(pair - COLUMN_MIN_FRAC, share));
            layout.ratios[boundary] = share;
            layout.ratios[boundary + 1] = pair - share;
            positionColumnChrome(layout.columns, layout.ratios);
        };
        const onUp = async () => {
            splitter.removeEventListener("lostpointercapture", onUp);
            splitter.removeEventListener("pointermove", onMove);
            splitter.removeEventListener("pointerup", onUp);
            splitter.removeEventListener("pointercancel", onUp);
            try { splitter.releasePointerCapture(ev.pointerId); } catch (_) {}
            splitter.classList.remove("dragging");
            document.body.style.userSelect = "";
            // Refit on drag-end only; per-frame refits would thrash xterm.
            scheduleColumnRefit();
            await persistTabLayout(project, layout.columns, layout.ratios);
        };
        splitter.classList.add("dragging");
        document.body.style.userSelect = "none";
        try { splitter.setPointerCapture(ev.pointerId); } catch (_) {}
        splitter.addEventListener("pointermove", onMove);
        splitter.addEventListener("pointerup", onUp);
        splitter.addEventListener("pointercancel", onUp);
        splitter.addEventListener("lostpointercapture", onUp);
    };
}

// ---- project / service activation ------------------------------------------

async function activateProject(name, opts) {
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
    // What is on screen for THIS project before the reset below: a box change
    // re-activates the open project, whose columns must keep their detected
    // apps; a switch from another project has nothing of this one on screen.
    const onScreen = (opts && opts.carry) ? onScreenServiceIds() : [];
    // Column actives are per-project session state — reset before anything
    // reads them; the engine reseeds each column's active from its layout.
    state.columnActive = [];
    state.tabLayout = null;
    // Bust the per-project service cache on every activation so a service that
    // came up AFTER the first activation (a box editor's code-server stub takes a
    // few seconds to boot) surfaces on re-select — without a full page reload.
    // fetchProjectServices otherwise caches the first probe forever. Idiomatic:
    // mirrors the delete-then-reactivate busts already used on service toggles.
    const prev = state.projectServices[name] || null;
    delete state.projectServices[name];
    const enabled = await fetchProjectServices(name, (opts && opts.carry) ? prev : null,
                                               onScreen);
    // The operator may have switched to another project during the fetch:
    // that switch owns the screen now, so this one stops here (the
    // refreshActiveServices idiom). The fetched map stays cached for the next
    // visit.
    if (state.activeProject !== name) return;

    renderServiceTabs(name, enabled);
    applyColumnLayout();
    // A pane whose tab's address changed while the project was not shown (a
    // port removed and re-added elsewhere) closes before the landing reuses it.
    if (prev) {
        const marker = state.lastDetection[name];
        followTabAddresses(name, prev, enabled,
                           marker === undefined ? "fallback" : marker, onScreen);
    }

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
        if (!next || !enabled[next] || !visible.includes(next)) {
            next = (visible.includes("code-server") && "code-server")
                || visible.find((id) => enabled[id].always_on)
                || visible[0];
        }
    }
    // keepNote: landing on a tab whose view was closed (its address freed)
    // keeps the note — returning to a project is not the operator opening it.
    activateService(next, { keepNote: true });

    // Auto-open every OTHER column's active tab so no pane sits empty after
    // activation (the generalization of the retired pin auto-open — up to
    // N-1 extra sessions, the accepted multi-column activation cost). The
    // background flag keeps focus + the landing bookkeeping on `next`.
    const layout = state.tabLayout;
    if (layout && layout.columns.length > 1) {
        for (let i = 0; i < layout.columns.length; i++) {
            const cid = state.columnActive[i];
            if (!cid || cid === next) continue;
            if (!state.terminals[tkey(name, cid)]) {
                activateService(cid, { background: true });
            }
        }
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
// Watches the open panel for a drag of its corner, to remember its width and height (and to re-stack the boxes for a new width).
let projectConfigResize = null;

function closeProjectConfigBox() {
    if (!projectConfigBox) return;
    if (projectConfigResize) { projectConfigResize.disconnect(); projectConfigResize = null; }
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
    // The width and height the operator last dragged the panel to, each
    // shrunk to the window (desktop only: the phone panel has no drag
    // corner). Applied BEFORE the first place(), which reads the size.
    // `applied` holds the inline sizes this code set; `last` holds the inline
    // sizes the observer below has accounted for (this code's own, a saved
    // drag, place()'s shrink), so it saves only a drag, never these.
    const mobile = mobileModeActive();
    const applied = { width: "", height: "" };
    if (!mobile) {
        const remembered = (key, room) => {
            let saved = NaN;
            try { saved = parseInt(localStorage.getItem(key) || "", 10); }
            catch (e) { /* storage blocked: the default size */ }
            return Number.isFinite(saved) && saved > 0 ? `${Math.min(saved, room)}px` : "";
        };
        applied.width = remembered(CONFIG_PANEL_WIDTH_KEY, window.innerWidth - 16);
        applied.height = remembered(CONFIG_PANEL_HEIGHT_KEY, window.innerHeight - 16);
        if (applied.width) box.style.width = applied.width;
        if (applied.height) box.style.height = applied.height;
    }
    const last = { width: applied.width, height: applied.height };
    document.body.appendChild(box);
    projectConfigBox = box;

    // position: fixed, anchored under the gear, clamped to the viewport. The
    // rail can shrink to RAIL_WIDTH_MIN, so the box overlays outside the
    // rail rather than trying to fit inside it. The gear's spot is read once:
    // a rail rebuild while the box is open may detach the gear itself.
    const r = anchorEl.getBoundingClientRect();
    // The box is capped to the room left below its top (style.css reads
    // --config-room), so sections that load or unfold later scroll inside it
    // instead of running off the window; the cap is lifted while measuring.
    const place = () => {
        // A drag pushed past the window's right or bottom edge leaves an
        // inline size larger than the panel shows (the caps below hold it):
        // bring it back to the shown size before the caps are lifted, or
        // measuring would grow the panel to a size the operator never saw
        // (and the observer would save it). The same shrink follows a window
        // made smaller while the panel is open: not a drag, so `last` takes it
        // and the remembered size stays.
        if (box.style.width && box.offsetWidth < parseFloat(box.style.width)) {
            box.style.width = `${box.offsetWidth}px`;
            last.width = box.style.width;
        }
        if (box.style.height && box.offsetHeight < parseFloat(box.style.height)) {
            box.style.height = `${box.offsetHeight}px`;
            last.height = box.style.height;
        }
        box.style.removeProperty("--config-room");
        box.style.removeProperty("--config-room-w");
        const bw = box.offsetWidth;
        const bh = box.offsetHeight;
        let left = r.left;
        let top = r.bottom + 4;
        if (left + bw > window.innerWidth - 8) left = window.innerWidth - 8 - bw;
        if (left < 8) left = 8;
        if (top + bh > window.innerHeight - 8) top = Math.max(8, r.top - 4 - bh);
        box.style.left = `${Math.round(left)}px`;
        box.style.top = `${Math.round(top)}px`;
        box.style.setProperty("--config-room",
                              `${Math.max(0, Math.round(window.innerHeight - 8 - top))}px`);
        // A drag of the corner stops at the window's right edge.
        box.style.setProperty("--config-room-w",
                              `${Math.max(0, Math.round(window.innerWidth - 8 - left))}px`);
    };
    // Stack the boxes for the panel's width (it is attached, so they measure),
    // then place the panel.
    const grid = box.querySelector(".config-boxes");
    const reflow = () => {
        if (grid) layoutConfigFolds(grid);
        place();
    };
    reflow();
    // Unfolding or folding a box changes its height: stack again and place the
    // panel again (this runs after the fold's own click handler, which is set
    // first), so it can move up to show what unfolded.
    for (const head of box.querySelectorAll(".config-fold > .box-collapse-head")) {
        head.addEventListener("click", reflow);
    }
    // A drag of the corner writes the panel's inline width and/or height:
    // stack the boxes again for a new width (not placed again, so the panel
    // stays under the pointer), and remember each side the drag changed as
    // rendered (a drag pushed past the window edge is capped on screen).
    if (!mobile && typeof ResizeObserver === "function") {
        // Keyed on the panel's outer width, which a scrollbar does not change:
        // keyed on the inner width, a re-stack that adds or drops the
        // scrollbar would change the column count again, every frame.
        let panelWidth = box.offsetWidth;
        projectConfigResize = new ResizeObserver(() => {
            if (grid && box.offsetWidth !== panelWidth) {
                panelWidth = box.offsetWidth;
                layoutConfigFolds(grid);
            }
            for (const [side, key, size] of [["width", CONFIG_PANEL_WIDTH_KEY, box.offsetWidth],
                                             ["height", CONFIG_PANEL_HEIGHT_KEY, box.offsetHeight]]) {
                if (!box.style[side] || box.style[side] === last[side]) continue;
                last[side] = box.style[side];
                try { localStorage.setItem(key, String(Math.round(size))); }
                catch (e) { /* storage blocked: the size lasts until the panel closes */ }
            }
        });
        projectConfigResize.observe(box);
    }

    document.addEventListener("pointerdown", onConfigOutsidePointer, true);
}

// Stack the panel's boxes masonry-style (maintainer 2026-11-07): as many
// columns as the width fits at --config-box-min each (style.css; one on a
// phone), and each box, in the panel's order, into the column that is
// shortest at that moment (the leftmost on a tie). A box's place depends only
// on the boxes before it, so unfolding one never moves it, only those after.
// Re-run whenever a box folds or unfolds and whenever the width changes;
// moving the nodes keeps every control's value (there are no frames inside),
// but a moved node loses focus and emptying the columns scrolls the panel to
// the top, so both are put back afterwards.
function layoutConfigFolds(container) {
    const folds = container.__folds || [];
    const panel = container.parentNode;
    const scrolled = panel ? panel.scrollTop : 0;
    const act = document.activeElement;
    const inside = !!act && act !== document.body && container.contains(act);
    let selStart = null, selEnd = null;
    if (inside) {
        try { selStart = act.selectionStart; selEnd = act.selectionEnd; }
        catch (e) { /* not a text control */ }
    }
    const cs = getComputedStyle(container);
    const min = parseFloat(cs.getPropertyValue("--config-box-min"));
    const gap = parseFloat(cs.getPropertyValue("--config-gap")) || 0;
    const n = min > 0 ? Math.max(1, Math.floor((container.clientWidth + gap) / (min + gap))) : 1;
    const cols = [];
    for (let i = 0; i < n; i++) cols.push(el("div", { class: "config-col" }));
    container.replaceChildren(...cols);
    const heights = cols.map(() => 0);
    for (const f of folds) {
        let k = 0;
        for (let i = 1; i < n; i++) if (heights[i] < heights[k]) k = i;
        cols[k].appendChild(f);
        heights[k] = cols[k].offsetHeight;
    }
    if (panel) panel.scrollTop = scrolled;
    if (inside && document.activeElement !== act && container.contains(act)) {
        act.focus({ preventScroll: true });
        if (selStart !== null && selEnd !== null) {
            try { act.setSelectionRange(selStart, selEnd); }
            catch (e) { /* not a text control */ }
        }
    }
}

// The panel is a set of folded boxes (maintainer 2026-11-07): Tabs and
// Extensions open, Agents, Sharing, Ports and Sidebar folded, in that order,
// stacked into columns by layoutConfigFolds once the panel is on screen. Each
// box is as wide as style.css allows, so a panel dragged wide shows them side
// by side. Every section builder below fills the BODY of its box. Which boxes
// are open is not remembered.
function makeProjectConfigBox(project) {
    const panel = el("div", { class: "project-config-box" });
    panel.appendChild(el("div", { class: "config-title" }, [project.name]));
    const folds = el("div", { class: "config-boxes" });
    // The boxes in the panel's order: layoutConfigFolds reads it, since the
    // columns hold them in a different order.
    folds.__folds = [];
    panel.appendChild(folds);
    const fold = (title, name, open) => {
        const f = collapsibleBox(title, [], "config-fold-" + name, "config-fold");
        setCollapsibleOpen(f, open);
        folds.appendChild(f);
        folds.__folds.push(f);
        return f.children[1];
    };

    const section = el("div", { class: "config-section" });
    const tabsBox = fold("Tabs", "tabs", true);

    const enabled = state.projectServices[project.name];
    if (!enabled || Object.keys(enabled).length === 0) {
        section.appendChild(el("div", { class: "config-empty" }, [
            "No tabs yet — open this project once to load its services.",
        ]));
        tabsBox.appendChild(section);
        appendSharingSection(fold("Sharing", "sharing", false), project, null);
        // Ports render even with no tabs loaded — register before you start serving.
        const portsBox = fold("Ports", "ports", false);
        appendExportedPortsSection(portsBox, project);
        appendDetectedAppsSection(portsBox, project);
        appendSidebarSection(fold("Sidebar", "sidebar", false), project);
        return panel;
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

    tabsBox.appendChild(section);
    tabsBox.appendChild(el("div", { class: "config-hint" }, [
        "Hidden tabs stay enabled on the supervisor — this only controls what shows here.",
    ]));
    appendEditorExtensionSection(fold("Extensions", "extensions", true), project, enabled);
    appendAgentsBox(fold("Agents", "agents", false), project, enabled);
    appendSharingSection(fold("Sharing", "sharing", false), project, enabled);
    const portsBox = fold("Ports", "ports", false);
    appendExportedPortsSection(portsBox, project);
    appendDetectedAppsSection(portsBox, project);
    appendSidebarSection(fold("Sidebar", "sidebar", false), project);
    return panel;
}

// The ⚙ Agents box (I33 slice 3): what the create dialog's Agents box chose,
// changed after create, in the same shape — the agent badges (switching a
// deployed agent off removes it: the project restarts, the agent's login and
// settings are kept), the working mode on a dev project, and one tab per agent
// showing what it runs now, read from the agents' own settings files (a model
// picked inside the agent with /model shows up). One Apply; its confirmation
// says what each change costs. Update (bring the agents level with the cached
// versions) stays beside it. A research project shows Claude's rows only (plus
// its workers and role services); the docker substrate fixes its agent set at
// create but its models are editable.
const AGENTS_BOX_ORDER = ["claude", "pi"];
const AGENTS_BOX_LABELS = { claude: "Claude", pi: "pi" };
const AGENTS_BOX_CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"];
// The client half of rscore.CLAUDE_MODEL_RE (the server refuses anything else).
const AGENTS_BOX_CLAUDE_MODEL = /^[A-Za-z0-9][A-Za-z0-9._\[\]-]*$/;
let agentsBoxSeq = 0;

function appendAgentsBox(box, project, enabled) {
    // `box` is the panel's Agents box (makeProjectConfigBox).
    const content = el("div", {}, [el("div", { class: "config-empty" }, ["Loading…"])]);
    box.appendChild(content);
    // The Box agents section's place (filled from the same read below).
    const boxesHolder = el("div", {});
    box.appendChild(boxesHolder);
    (async () => {
        let res = null, b = null;
        try {
            res = await fetch(`/broker/project/${encodeURIComponent(project.name)}/agents`);
            try { b = await res.json(); } catch (e) { b = {}; }
        } catch (e) { b = null; }
        if (!b || !res.ok || !b.ok || !b.result) {
            content.replaceChildren(el("div", { class: "config-empty" },
                [(b && mgmtErrText(b)) || "Couldn't read the project's agents."]));
            return;
        }
        const d = b.result;
        const cat = await ensureModelCatalog();
        await ensureWorkingModes();
        // What can be added: the host's pulled agents (the create form's read).
        let staged = [];
        if (d.kind === "dind") {
            try {
                const wres = await fetch("/broker/workflows");
                const wbody = await wres.json();
                const list = wbody && wbody.result && Array.isArray(wbody.result.agents)
                    ? wbody.result.agents : [];
                staged = list.filter((a) => a && a.staged).map((a) => a.name);
            } catch (e) { /* no additions offered */ }
        }
        content.replaceChildren(...agentsBoxContent(project.name, d, cat, staged));
        const boxesSection = boxAgentsSection(project.name, d, cat);
        if (boxesSection) boxesHolder.replaceChildren(boxesSection);
    })();
}

function agentsBoxContent(name, d, cat, staged) {
    const local = Array.isArray(d.agents_local) ? d.agents_local : [];
    const harness = d.harness || {};
    const isDind = d.kind === "dind";
    const isResearch = d.kind === "research";
    const on = new Set(isResearch ? ["claude"] : local);
    // ---- the badges ----
    const offered = AGENTS_BOX_ORDER.filter((a) =>
        isResearch ? a === "claude" : (local.includes(a) || (isDind && staged.includes(a))));
    const badges = offered.map((a) => {
        const card = el("div", { class: "box-opt-card" + (on.has(a) ? " selected" : "") },
                        [el("span", { class: "box-opt-name" }, [AGENTS_BOX_LABELS[a] || a])]);
        if (isDind) {
            card.onclick = () => {
                if (on.has(a)) on.delete(a); else on.add(a);
                card.classList.toggle("selected", on.has(a));
                sync();
            };
        } else {
            card.classList.add("disabled");
            card.title = "This project's agents are fixed when it is created.";
        }
        return { a, card };
    });
    // ---- Claude's tab ----
    const listId = "agents-box-claude-models-" + (++agentsBoxSeq);
    const claudeList = el("datalist", { id: listId },
        ((cat && cat.models) || []).map((m) => el("option", { value: m.alias }, [m.label || m.alias])));
    const claudeInit = d.claude || { model: "", effort: "" };
    const claudeModelI = el("input", { type: "text", autocomplete: "off", spellcheck: "false",
                                       list: listId, placeholder: "Claude's own default" });
    claudeModelI.value = claudeInit.model || "";
    const claudeEffortS = el("select", {}, [
        el("option", { value: "" }, ["Claude's default"]),
        ...AGENTS_BOX_CLAUDE_EFFORTS.map((lv) => el("option", { value: lv }, [lv])),
    ]);
    claudeEffortS.value = AGENTS_BOX_CLAUDE_EFFORTS.includes(claudeInit.effort) ? claudeInit.effort : "";
    const claudeRows = harnessClaudeRows(cat, harness);
    const researchPickers = isResearch && cat
        ? MODEL_PICKER_SPECS.filter(([ct]) => ct !== "supervisor").map(([ct, title, hint]) =>
            [ct, modelPicker(cat, ct, title, hint, { unchanged: true })])
        : [];
    const claudeNotes = [];
    if (claudeInit.unrecognised) {
        claudeNotes.push("Claude's settings hold a value RS does not recognise; it is shown empty.");
    }
    // An older docker-substrate container keeps Claude's model where it was
    // put at create (the server refuses a change there); elsewhere an older
    // container is restarted to take one.
    const claudeFixed = d.kind === "docker" && !d.model_in_settings;
    if (claudeFixed) {
        claudeModelI.disabled = true;
        claudeEffortS.disabled = true;
        claudeNotes.push("Claude's model on this project is fixed when it is created.");
    } else if (d.running && !d.model_in_settings) {
        claudeNotes.push("This project's container predates settings-file models: changing " +
                         "Claude's model or effort restarts the project.");
    }
    // A tier that takes no effort level (haiku): its effort stays the default,
    // so the server's refusal is never reached from here.
    // At build the picker only locks (what the file holds stays shown and is
    // never sent unasked); a model typed by the operator also clears it.
    const syncClaudeEffort = (clear) => {
        if (claudeFixed) return;
        const tier = ((cat && cat.models) || []).find((m) => m.alias === claudeModelI.value.trim());
        const none = !!tier && Array.isArray(tier.efforts) && !tier.efforts.length;
        if (none && clear) claudeEffortS.value = "";
        claudeEffortS.disabled = none;
    };
    claudeModelI.addEventListener("input", () => syncClaudeEffort(true));
    syncClaudeEffort(false);
    const claudePanel = el("div", { class: "agent-tab-panel" }, [
        claudeList,
        el("div", { class: "field" }, [
            el("label", {}, ["Agent"]),
            el("div", { class: "model-row" }, [
                el("div", { class: "model-cell agent-pi-model" }, [
                    el("span", { class: "model-cap" }, ["model"]), claudeModelI]),
                el("div", { class: "model-cell" }, [
                    el("span", { class: "model-cap" }, ["effort"]), claudeEffortS]),
            ]),
        ]),
        ...claudeNotes.map((t) => el("div", { class: "hint" }, [t])),
        claudeRows.node,
        ...researchPickers.map(([, p]) => p.node),
    ]);
    // ---- pi's tab ----
    const piInit = d.pi || { model: "", thinking: "" };
    let piTouched = false;
    const piModelI = el("input", { type: "text", autocomplete: "off", spellcheck: "false",
                                   placeholder: "provider/model (empty: pi's own pick)" });
    piModelI.value = piInit.model || "";
    const piThinkS = el("select", {}, [
        el("option", { value: "" }, ["pi's default"]),
        ...HARNESS_PI_THINKING.map((lv) => el("option", { value: lv }, [lv])),
    ]);
    piThinkS.value = HARNESS_PI_THINKING.includes(piInit.thinking) ? piInit.thinking : "";
    piModelI.addEventListener("input", () => { piTouched = true; });
    piThinkS.addEventListener("change", () => { piTouched = true; });
    const piRows = harnessPiRows(harness);
    const piPanel = el("div", { class: "agent-tab-panel" }, [
        el("div", { class: "field" }, [
            el("label", {}, ["Agent"]),
            el("div", { class: "model-row agent-pi-row" }, [
                el("div", { class: "model-cell agent-pi-model" }, [
                    el("span", { class: "model-cap" }, ["model"]), piModelI]),
                el("div", { class: "model-cell" }, [
                    el("span", { class: "model-cap" }, ["thinking"]), piThinkS]),
            ]),
        ]),
        piInit.unrecognised
            ? el("div", { class: "hint" }, ["pi's settings hold a value RS does not recognise; it is shown empty."])
            : null,
        el("div", { class: "hint" }, [
            "What pi's settings say now — a model picked inside pi shows here.",
        ]),
        piRows.node,
    ]);
    const panels = { claude: claudePanel, pi: piPanel };
    // ---- the mode switch, the tabs ----
    let active = null;
    const tabsRow = el("div", { class: "agent-tabs" });
    const panelsWrap = el("div", { class: "agent-tab-panels" });
    // `sw` is assigned after `sync` exists: the switch calls its onChange while
    // it is being built, and a `const sync` read before its line throws.
    let sw = null;
    const sync = () => {
        const tabs = AGENTS_BOX_ORDER.filter((a) => on.has(a) && panels[a]);
        if (!tabs.includes(active)) active = tabs[0] || null;
        tabsRow.replaceChildren(...tabs.map((a) => {
            const t = el("button", { type: "button",
                                     class: "agent-tab" + (a === active ? " active" : "") },
                         [AGENTS_BOX_LABELS[a] || a]);
            t.onclick = () => { active = a; sync(); };
            return t;
        }));
        panelsWrap.replaceChildren(...(active ? [panels[active]] : []));
        const roles = sw ? harnessModeRoles(sw.value()) : [];
        const pdc = roles.length > 0;
        claudeRows.setRoles(roles);
        piRows.setRoles(roles);
        claudeRows.node.style.display = pdc && claudeRows.available ? "" : "none";
        piRows.node.style.display = pdc ? "" : "none";
    };
    sw = d.dev ? harnessModeSwitch(() => sync(), harness.harness_mode || "none") : null;
    sync();
    // ---- Apply ----
    const apply = el("button", { class: "btn btn-secondary" }, ["Apply"]);
    const applyMsg = el("div", { class: "config-empty" });
    apply.onclick = () => { applyMsg.textContent = agentsBoxApply(name, d, {
        on, local, isDind, isResearch, harness, sw, claudeRows, piRows, researchPickers,
        claude: { model: () => claudeModelI.value.trim(), effort: () => claudeEffortS.value,
                  init: claudeInit },
        pi: { model: () => piModelI.value.trim(), thinking: () => piThinkS.value,
              init: piInit, touched: () => piTouched },
    }) || ""; };
    // ---- Update (the cached versions, live) ----
    const upd = el("button", { class: "btn btn-secondary" }, ["Update"]);
    const deployed = isResearch ? ["claude"]
        : isDind ? (Array.isArray(d.agents_staged) && d.agents_staged.length ? d.agents_staged : ["claude"])
        : (d.agents || []);
    upd.disabled = !deployed.length || !d.running;
    upd.title = d.running ? "Deploy the cached agent versions into this project"
                          : "Start the project to update its agents";
    upd.onclick = () => mgmtAgentsUpdateDialog(name, deployed, d.kind !== "docker",
                                               isResearch ? ["claude"] : local);
    return [
        badges.length ? el("div", { class: "box-opt-cards agent-badges" }, badges.map((x) => x.card))
                      : null,
        d.kind === "docker"
            ? el("div", { class: "hint" }, ["This project's agents are fixed when it is created."])
            : null,
        sw ? sw.node : null,
        tabsRow,
        panelsWrap,
        d.running ? null : el("div", { class: "hint" }, [
            "The project is stopped: a change is saved and applied at its next start; " +
            "nothing is started."]),
        el("div", { class: "config-box-row" }, [apply, upd]),
        applyMsg,
    ].filter((n) => n);
}

// The ⚙ Box agents section (I33 slice 3, the box half): a box's agent
// settings after it was added — a Claude box's model and effort (any project),
// and a dev box's working mode and Doer/Checker models (outside research
// projects). Same data as the Agents box (its one read); same inline save as
// ⚙ Box folders. A model change is written into the running box at once; a
// working-mode change restarts the box fresh; a stopped box takes either when
// it next starts.
function boxAgentsSection(name, d, cat) {
    const state = d.boxes_state;
    // No boxes to speak of: a project without Docker-in-Docker ("none"), or a
    // read from a broker that predates boxes in it (no state at all).
    if (!state || state === "none") return null;
    const content = el("div", {});
    // A sub-heading inside the panel's Agents box, not a box of its own.
    const section = el("div", { class: "config-section config-box-agents" }, [
        el("div", { class: "config-section-label" }, ["Box agents"]), content]);
    if (state === "stopped") {
        content.replaceChildren(el("div", { class: "config-empty" },
            ["Start the project to change its boxes."]));
        return section;
    }
    if (state === "old-tool") {
        content.replaceChildren(el("div", { class: "config-empty" },
            ["Restart the project to change its boxes here."]));
        return section;
    }
    if (state !== "ok") {
        content.replaceChildren(el("div", { class: "config-empty" },
            ["Couldn't read the project's boxes."]));
        return section;
    }
    const boxes = (Array.isArray(d.boxes) ? d.boxes : [])
        .filter((b) => b.agent === "claude" || b.mode_editable);
    if (!boxes.length) return null;
    const sel = el("select", {}, boxes.map((b) =>
        el("option", { value: b.name }, [b.name + " (" + (AGENTS_BOX_LABELS[b.agent] || b.agent) +
                                         (b.running ? "" : ", stopped") + ")"])));
    const rowsWrap = el("div", {});
    const what = el("div", { class: "config-hint" });
    const status = el("div", { class: "config-box-status" });
    const saveBtn = el("button", { class: "btn btn-secondary" }, ["Save"]);
    let cur = null;
    const draw = () => {
        const b = boxes.find((x) => x.name === sel.value) || boxes[0];
        cur = boxAgentsRows(b, cat, () => { what.textContent = boxAgentsWhat(b, cur); });
        rowsWrap.replaceChildren(cur.node);
        what.textContent = boxAgentsWhat(b, cur);
        status.replaceChildren();
    };
    sel.onchange = draw;
    saveBtn.onclick = async () => {
        const b = boxes.find((x) => x.name === sel.value) || boxes[0];
        const p = boxAgentsPayload(b, cur);
        if (p.error) { status.replaceChildren(el("div", {}, [p.error])); return; }
        if (!p.changed) { status.replaceChildren(el("div", {}, ["Nothing to change."])); return; }
        saveBtn.disabled = true;
        status.replaceChildren(el("div", {}, ["Saving…"]));
        try {
            const r = await fetch(`/broker/project/${encodeURIComponent(name)}/box-settings`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(Object.assign({ name: b.name }, p.payload)),
            });
            let rb; try { rb = await r.json(); } catch (e) { rb = {}; }
            if (!r.ok || !rb.ok || !rb.result) {
                status.replaceChildren(el("div", {}, ["Couldn't save: " + (mgmtErrText(rb) || r.status)]));
                return;
            }
            const out = rb.result;
            const lines = [el("div", {}, [!out.changed ? "Nothing changed."
                : out.restarted ? "Saved — the box restarted fresh."
                : b.running ? "Saved — its next Claude session uses it."
                : "Saved — the box takes it when it next starts."])];
            if (out.effort_dropped) {
                lines.push(el("div", {}, ["The effort went back to the default: this model takes none."]));
            }
            for (const w of (Array.isArray(out.warnings) ? out.warnings : [])) {
                lines.push(el("div", { class: "config-box-warn" }, ["⚠ " + w]));
            }
            status.replaceChildren(...lines);
            // What was saved is the new starting point for the next change —
            // except a model the box could not take yet (a warning came back):
            // the same values must stay sendable again.
            // (A restart writes the model itself, so only a save without one can
            // carry a model warning.)
            if (p.next.claude && !out.restarted && (out.warnings || []).length) delete p.next.claude;
            if (p.next.claude && out.effort_dropped) p.next.claude.effort = "";
            Object.assign(b, p.next);
            what.textContent = boxAgentsWhat(b, cur);
            if (out.restarted) await refreshAfterBoxChange(name);
        } catch (e) {
            status.replaceChildren(el("div", {}, ["Couldn't save: connection failed."]));
        } finally {
            saveBtn.disabled = false;
        }
    };
    draw();
    content.replaceChildren(
        el("div", { class: "config-box-row" }, [sel, saveBtn]),
        rowsWrap, what, status,
        el("div", { class: "config-hint" }, [
            "A box starts from a fresh home whenever it is restarted fresh — at every " +
            "project start, and when a working-mode, folder or port change restarts it — " +
            "so a model picked inside the box with /model lasts until then — unless " +
            "you save it here, which keeps what this section shows."]));
    return section;
}

// One box's controls: Claude's model + effort (a Claude box), the working-mode
// switch and its Doer/Checker rows (a dev box outside research).
function boxAgentsRows(b, cat, onChange) {
    const parts = [];
    let modelI = null, effortS = null, sw = null, claudeRows = null, piRows = null;
    if (b.agent === "claude") {
        const init = b.claude || { model: "", effort: "" };
        const listId = "box-agents-models-" + (++agentsBoxSeq);
        modelI = el("input", { type: "text", autocomplete: "off", spellcheck: "false",
                               list: listId, placeholder: "Claude's own default" });
        modelI.value = init.model || "";
        effortS = el("select", {}, [
            el("option", { value: "" }, ["Claude's default"]),
            ...AGENTS_BOX_CLAUDE_EFFORTS.map((lv) => el("option", { value: lv }, [lv])),
        ]);
        effortS.value = AGENTS_BOX_CLAUDE_EFFORTS.includes(init.effort) ? init.effort : "";
        const syncEffort = (clear) => {
            const tier = ((cat && cat.models) || []).find((m) => m.alias === modelI.value.trim());
            const none = !!tier && Array.isArray(tier.efforts) && !tier.efforts.length;
            if (none && clear) effortS.value = "";
            effortS.disabled = none;
        };
        modelI.addEventListener("input", () => { syncEffort(true); onChange(); });
        effortS.addEventListener("change", onChange);
        syncEffort(false);
        parts.push(
            el("datalist", { id: listId },
               ((cat && cat.models) || []).map((m) => el("option", { value: m.alias }, [m.label || m.alias]))),
            el("div", { class: "field" }, [
                el("label", {}, ["Agent"]),
                el("div", { class: "model-row" }, [
                    el("div", { class: "model-cell agent-pi-model" }, [
                        el("span", { class: "model-cap" }, ["model"]), modelI]),
                    el("div", { class: "model-cell" }, [
                        el("span", { class: "model-cap" }, ["effort"]), effortS]),
                ]),
            ]),
            init.unrecognised
                ? el("div", { class: "hint" }, ["The box's settings hold a value RS does not recognise; it is shown empty."])
                : null);
    }
    if (b.mode_editable) {
        const h = b.harness || {};
        claudeRows = b.agent === "claude" ? harnessClaudeRows(cat, h) : null;
        piRows = b.agent === "pi" ? harnessPiRows(h) : null;
        const rows = claudeRows || piRows;
        const syncRows = () => {
            const roles = sw ? harnessModeRoles(sw.value()) : [];
            const pdc = roles.length > 0;
            if (rows) rows.setRoles(roles);
            if (rows) rows.node.style.display = pdc && (!claudeRows || claudeRows.available) ? "" : "none";
            onChange();
        };
        // `sw` is assigned after syncRows exists (the switch calls back while
        // it is built — the TDZ lesson of the Agents box).
        sw = harnessModeSwitch(() => syncRows(), h.harness_mode || "none");
        parts.push(sw.node, rows ? rows.node : null);
        syncRows();
    }
    return {
        node: el("div", { class: "box-agents-rows" }, parts.filter((n) => n)),
        model: () => (modelI ? modelI.value.trim() : null),
        effort: () => (effortS ? effortS.value : null),
        mode: () => (sw ? sw.value() : null),
        rowsFields: () => (sw && harnessModeRoles(sw.value()).length
            ? Object.assign({}, claudeRows ? claudeRows.fields() : {}, piRows ? piRows.fields() : {})
            : {}),
        piInvalid: () => (piRows && sw && harnessModeRoles(sw.value()).length ? piRows.invalid() : null),
    };
}

// What the box settings Save sends: only what differs from what was read; an
// emptied value as a reset. `next` is what the box holds after a save.
function boxAgentsPayload(b, cur) {
    const payload = {};
    const reset = [];
    const next = {};
    let modeChanged = false;
    const diff = (field, now, was) => {
        if (now === null || now === was) return false;
        if (now) payload[field] = now; else reset.push(field);
        return true;
    };
    const claudeInit = b.claude || { model: "", effort: "" };
    if (cur.model() !== null) {
        const m = cur.model();
        if (m && !AGENTS_BOX_CLAUDE_MODEL.test(m)) {
            return { error: "Claude's model may use only letters, digits and . _ - [ ]." };
        }
        const mc = diff("model", m, claudeInit.model || "");
        const ec = diff("effort", cur.effort(), claudeInit.effort || "");
        // An effort goes with the model the box shows (its file may hold a
        // /model pick the record does not): the host checks the pair.
        if (ec && !mc && m) payload.model = m;
        if (mc || ec) next.claude = Object.assign({}, claudeInit, { model: m, effort: cur.effort() });
    }
    if (cur.mode() !== null) {
        const bad = cur.piInvalid();
        if (bad) return { error: bad };
        const h = b.harness || {};
        const was = h.harness_mode || "none";
        const mode = cur.mode();
        const nh = Object.assign({}, h);
        if (mode !== was) {
            modeChanged = true;
            if (mode === "none") { reset.push("harness_mode"); delete nh.harness_mode; }
            else { payload.harness_mode = mode; nh.harness_mode = mode; }
        }
        const rows = cur.rowsFields();
        Object.keys(rows).forEach((k) => {
            const now = rows[k] || "", w = h[k] || "";
            if (now !== w) {
                modeChanged = true;
                if (now) { payload[k] = now; nh[k] = now; } else { reset.push(k); delete nh[k]; }
            }
        });
        if (modeChanged) next.harness = nh;
    }
    if (reset.length) payload.reset = reset;
    const changed = Object.keys(payload).length > 0;
    return { payload, changed, modeChanged, next };
}

// The line under a box's controls saying what Save will do.
function boxAgentsWhat(b, cur) {
    if (!cur) return "";
    const p = boxAgentsPayload(b, cur);
    if (p.error || !p.changed) return "";
    if (!b.running) return "The box is stopped: Save keeps the change for its next start.";
    if (p.modeChanged) {
        return "Save restarts the box fresh: its sessions end, its agent signs in again " +
               "and its setup runs again. Its files are kept.";
    }
    return "Save writes the model into the box at once; its next Claude session uses it.";
}

// The Apply of the ⚙ Agents box: what changed against what was read, as one
// update; Claude's rows only when they differ (an unchanged value must never
// cost a restart on an older container), pi's when touched (pi may run a
// default picked inside it), an emptied row as an explicit reset. Returns a
// line for the box when there is nothing to send.
function agentsBoxApply(name, d, f) {
    const payload = {};
    const reset = [];
    const lines = [];
    const setOrReset = (field, now, was, line) => {
        if (now === was) return;
        if (now) payload[field] = now; else reset.push(field);
        lines.push(line);
    };
    const restartNote = d.running ? " — restarts the project container" : "";
    if (f.isDind) {
        const next = AGENTS_BOX_ORDER.filter((a) => f.on.has(a));
        const added = next.filter((a) => !f.local.includes(a));
        const removed = f.local.filter((a) => !next.includes(a));
        if (added.length || removed.length) {
            payload.agents = next;
            added.forEach((a) => lines.push(`Add ${AGENTS_BOX_LABELS[a] || a}: live, nothing restarted.`));
            removed.forEach((a) => lines.push(
                `Remove ${AGENTS_BOX_LABELS[a] || a}${restartNote}. Its login and settings are kept ` +
                `for a later re-add` + (a === "claude" ? "; Claude Code leaves the project's editor too." : ".")));
        }
    }
    const legacy = d.running && !d.model_in_settings;
    if (f.on.has("claude") && !(d.kind === "docker" && !d.model_in_settings)) {
        const cm = f.claude.model(), ce = f.claude.effort();
        setOrReset("supervisor_model", cm, f.claude.init.model || "",
                   `Claude's model: ${cm || "Claude's own default"}` + (legacy ? restartNote : "") + ".");
        setOrReset("supervisor_effort", ce, f.claude.init.effort || "",
                   `Claude's effort: ${ce || "Claude's default"}` + (legacy ? restartNote : "") + ".");
    }
    // pi's tab shows whenever pi is ticked — an agent added in this same Apply
    // included (the server writes its model right after the install).
    if (f.on.has("pi") && f.pi.touched()) {
        const pm = f.pi.model(), pt = f.pi.thinking();
        if (pm) payload.pi_model = pm; else reset.push("pi_model");
        if (pt) payload.pi_thinking = pt; else reset.push("pi_thinking");
        lines.push(`pi's model: ${pm || "pi's own pick"}, thinking ${pt || "pi's default"} — ` +
                   "replaces a default picked inside pi.");
    }
    if (f.sw) {
        const mode = f.sw.value();
        const was = f.harness.harness_mode || "none";
        if (mode !== was) {
            if (mode === "none") reset.push("harness_mode"); else payload.harness_mode = mode;
            const spec = harnessModeSpec(mode);
            lines.push(`Working mode: ${spec ? spec.label : mode}.`);
        }
        if (harnessModeRoles(mode).length) {
            const rows = Object.assign({},
                f.on.has("claude") && f.claudeRows.available ? f.claudeRows.fields() : {},
                f.on.has("pi") ? f.piRows.fields() : {});
            let changed = false;
            Object.keys(rows).forEach((k) => {
                const now = rows[k] || "", was2 = f.harness[k] || "";
                if (now !== was2) {
                    changed = true;
                    if (now) payload[k] = now; else reset.push(k);
                }
            });
            if (changed) {
                const names = harnessModeRoles(mode).map((r) => (r === "doer" ? "Doer" : "Checker"));
                lines.push(`${names.join(" and ")} model${names.length > 1 ? "s" : ""} updated.`);
            }
        }
    }
    f.researchPickers.forEach(([ct, p]) => {
        const v = p.value();
        if (v.model) payload[ct + "_model"] = v.model;
        if (v.effort) payload[ct + "_effort"] = v.effort;
        if (v.model || v.effort) {
            lines.push(ct === "worker" ? "Workers' model: the next worker spawned uses it."
                                       : "Role-MCPs' model: re-runs those services.");
        }
    });
    if (reset.length) payload.reset = reset;
    if (!lines.length) return "Nothing to change.";
    const validate = () => {
        const cm = payload.supervisor_model;
        if (cm && !AGENTS_BOX_CLAUDE_MODEL.test(cm)) {
            return "Claude's model may use only letters, digits and . _ - [ ].";
        }
        if (payload.pi_model && !HARNESS_PI_MODEL_PATTERN.test(payload.pi_model)) {
            return "Write pi's model as provider/model, e.g. openrouter/anthropic/claude-opus-latest.";
        }
        if (f.sw && harnessModeRoles(f.sw.value()).length && f.on.has("pi")) {
            const bad = f.piRows.invalid();
            if (bad) return bad;
        }
        if (d.dev && f.isDind && !AGENTS_BOX_ORDER.some((a) => f.on.has(a))) {
            return "A dev project needs at least one agent.";
        }
        return null;
    };
    mgmtConfirmThenTail(boxOpView(), {
        title: `Change the agents of ${name}`,
        tailTitle: `Changing the agents of ${name}`,
        verb: "update",
        project: name,
        confirmLabel: "Apply",
        body: [
            el("ul", {}, lines.map((t) => el("li", {}, [t]))),
            el("div", { class: "hint" }, [
                d.running
                    ? "Each agent's next session uses its new settings; a session running now " +
                      "keeps them until it is restarted (switch the working mode in a running " +
                      "session with /rs-mode, or /skill:rs-mode in pi)."
                    : "The project is stopped: the change is saved and applied at its next " +
                      "start; nothing is started.",
            ]),
        ],
        validate,
        request: () => fetch(`/broker/project/${encodeURIComponent(name)}/update`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        }),
        onDone: (ok) => { if (ok) closeProjectConfigBox(); },
    });
}

// What the Agents Update dialog says about where the versions go. `local`: the
// agents in the project's own container (its terminal and editor), `deployed`:
// the agents the update refreshes (the staged set), `hasBoxes`: a project whose
// boxes are refreshed too. A pi-only Docker-in-Docker project still refreshes
// Claude for its boxes, but its own terminal and editor carry no Claude.
function agentsUpdateHint(local, deployed, hasBoxes) {
    const terminalAll = local.length > 0 && deployed.every((a) => local.includes(a));
    const who = local.length && hasBoxes && terminalAll
        ? "The project's terminal and every running box that carries agents get these versions"
        : local.length && hasBoxes
            ? "The project's terminal gets the " + local.join(", ") + " version" +
              (local.length > 1 ? "s" : "") + ", and every running box that carries " +
              "agents gets the versions of the agents it carries"
        : local.length ? "The project's terminal gets these versions"
        : "Every running box that carries agents gets these versions";
    const extHere = local.includes("claude");
    const extBoxes = hasBoxes && deployed.includes("claude");
    const ext = extHere
        ? " Claude Code's editor extension returns to the cached version too (reload " +
          "an open editor window to pick it up)."
        : extBoxes
            ? " Claude Code's editor extension in the boxes returns to the cached version " +
              "too (reload an open editor window to pick it up); the project's own editor " +
              "has none."
        : "";
    return "Applied live — nothing is recreated, and logins, sessions and settings " +
        "are kept. " + who + ", including over an agent you updated yourself." + ext +
        " Restart running agent sessions afterwards to use the new versions." +
        (hasBoxes ? " A stopped box keeps its copy until it runs during an update " +
            "or is re-created." : "");
}

// The Update dialog behind the Agents section: names the cached versions it will
// deploy (the Sandboxes → Software tab's own data, flagging a row that reads stale), says
// what an update does and does not touch, and runs `agents_update` on the
// lifecycle lane (its op log names each box refreshed or not).
async function mgmtAgentsUpdateDialog(name, deployed, hasBoxes, local) {
    let rows = [];
    try {
        const res = await fetch("/broker/software");
        const body = await res.json();
        rows = (body && body.result && Array.isArray(body.result.agents)) ? body.result.agents : [];
    } catch (e) { /* versions unavailable: the dialog still works */ }
    const versions = rows.filter((r) => deployed.includes(r.agent) && r.present).map((r) =>
        el("li", {}, [
            r.agent + " " + (r.cached_version || "?"),
            r.matches_pin ? "" : " — the cached copy is stale; press Pull in " +
                "Sandboxes → Software first to get the pinned version",
        ]));
    mgmtConfirmThenTail(boxOpView(), {
        title: `Update the agents of ${name}`,
        tailTitle: `Updating the agents of ${name}`,
        verb: "agents_update",
        project: name,
        confirmLabel: "Update",
        body: [
            el("p", {}, ["Deploys the versions cached on this host:"]),
            versions.length ? el("ul", {}, versions)
                : el("p", { class: "hint" }, ["(the cached versions could not be read)"]),
            el("div", { class: "hint" }, [agentsUpdateHint(local, deployed, hasBoxes)]),
        ],
        request: () => fetch(`/broker/project/${encodeURIComponent(name)}/agents_update`, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
        }),
    });
}

// The "Sidebar" section — drop this browser's bookmark for a project. It is the
// ONLY way to get rid of a row whose project is no longer here: the sidebar is a
// per-browser list, and the one other place that ever removed a row is the
// completion hook of Management's Destroy. That table also lists a project whose
// container is gone while its workspace, network or volume remain (an orphaned
// row, Destroy only), but a project with NOTHING left on this host — destroyed
// elsewhere, or a box on another host entered by hand — is in no table at all
// and has only this exit.
//
// Appended at BOTH exits of makeProjectConfigBox on purpose. The early one is
// taken while a row has never been opened; the full one the moment it IS opened,
// because the services list carries the always-on entries for ANY name, existing
// or not. A tail-only append would hide the affordance in half the cases it
// exists for — including the reported one.
function appendSidebarSection(box, project) {
    // `box` is the panel's Sidebar box, last and folded (makeProjectConfigBox).
    const section = el("div", { class: "config-section" });
    const btn = el("button", { class: "btn-small btn-danger" }, ["Remove from sidebar"]);
    btn.onclick = () => removeProjectFromSidebar(project);
    section.appendChild(btn);
    box.appendChild(section);
    box.appendChild(el("div", { class: "config-hint" }, [
        "Removes the bookmark from this browser only — nothing on the sandbox is "
        + "created, changed or destroyed.",
    ]));
}

// Confirm, then forget the row. This performs EXACTLY the browser-state half of
// the destroy hook (teardown, drop the row, prune the two saved per-project
// surfaces, persist, repaint) and issues NO request of any kind: removing a
// bookmark must never be confusable with destroying a project.
function removeProjectFromSidebar(project) {
    // A dialog is already open — the guard every other confirm here uses.
    if (document.querySelector(".modal-backdrop")) return;
    // Close the config box FIRST. It is z-index 200 against the modal's 100 and
    // is dismissed only by the NEXT pointerdown, so leaving it open floats an
    // opaque card over the dialog — over its buttons on a narrow viewport —
    // until the operator's first press.
    closeProjectConfigBox();
    const name = project.name;

    const backdrop = el("div", { class: "modal-backdrop" });
    const cancel = el("button", { class: "btn btn-secondary" }, ["Cancel"]);
    cancel.onclick = () => backdrop.remove();
    const go = el("button", { class: "btn btn-danger" }, ["Remove"]);
    go.onclick = async () => {
        go.disabled = true; cancel.disabled = true;
        backdrop.remove();
        // Capture before the teardown: it nulls state.activeProject itself.
        const wasActive = state.activeProject === name;
        teardownProjectState(name);
        state.vault.projects = state.vault.projects.filter((p) => p.name !== name);
        // Same prune as destroy: a project that later arrives under this name
        // starts with the default tab arrangement and at the tail of the list,
        // rather than inheriting a stranger's. The confirm says so, since for a
        // project that is still running here the row itself comes back.
        delete projectUiMap()[name];
        pruneProjectOrder(name);
        try { await persistVault(); } catch (e) { /* best-effort, as destroy */ }
        refreshProjectRail();
        // Unconditional, and NOT gated on a host page being open: closeManagement
        // restores the terminal area but never re-shows #welcome, which
        // activateService had hidden — so skipping this leaves a blank pane
        // behind whenever the removed project had a service open. Running it
        // under a host view is inert (#welcome sits inside the hidden terminal
        // area, and the tab wipe only touches tabs that view already hid).
        if (wasActive) showWelcome(true);
    };
    const card = el("div", { class: "card" }, [
        el("h2", {}, ["Remove from sidebar"]),
        el("p", {}, [
            `Remove "${name}" from this browser's sidebar. Nothing on the sandbox `
            + "is created, changed or destroyed — no project is stopped, and none "
            + "is deleted.",
        ]),
        el("p", {}, [
            "Its tab arrangement and its position in the list are forgotten too. A "
            + "project on this sandbox comes back on its own once it is running — on "
            + "the next page load, or as soon as you open Sandboxes. A box you run "
            + "elsewhere, entered by hand under New Project, is known only to this "
            + "browser: it comes back only by entering it again, with its host, port, "
            + "user and password.",
        ]),
        el("div", { class: "btn-row" }, [cancel, go]),
    ]);
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
}

// The editor "Extensions" surface (STAGE_BOX_EXT_UX C) — host-container webui
// surfaces, the unified "Add extension" sense (Editor now; Overleaf-style compose
// surfaces later). Client-rendered from the services map: `code-server` present ⇒
// on. The toggle goes through `update` enable/disable code-server, which RECREATES
// the container — supported on BOTH substrates now (dind via _recreate_supervisor,
// docker via _recreate_docker_substrate). Universal (renders on every flavor).
function appendEditorExtensionSection(box, project, enabled) {
    // `box` is the panel's Extensions box; its title is the heading.
    const section = el("div", { class: "config-section" });
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
// THEN start serving it. The list is the broker's port_list; page tabs appear
// as soon as the port is registered (unconditional — a not-yet-serving port
// surfaces its connection error in the iframe on click), and pass-through
// claims appear as launcher tabs.

// The pass-through launcher pane (pass-<n> tabs). Two scheme buttons instead
// of a stored scheme field (PI decision): the claim doesn't record what
// protocol the app speaks, and a wrong guess is one click away either way.
function renderPassthroughLauncher(container, status, svc) {
    status.remove();
    const host = svc.passthrough_host || window.location.hostname;
    const port = svc.passthrough_port;
    const count = svc.passthrough_count || 1;
    const span = count > 1 ? `${port}-${port + count - 1}` : `${port}`;
    const wrap = el("div", { class: "pt-launcher" });
    wrap.appendChild(el("h3", {}, [svc.label || `Port ${port}`]));
    wrap.appendChild(el("div", { class: "pt-launcher-addr" }, [`${host}:${span}`]));
    wrap.appendChild(el("div", { class: "pt-launcher-note" }, [
        "Pass-through: traffic reaches the app untouched and is NOT behind " +
        "the webui login — the app's own login is the only lock. Opens in a " +
        "new browser tab; the app brings its own certificate, so expect a " +
        "browser warning if it is self-signed.",
    ]));
    const mk = (scheme) => {
        const b = el("button", { class: "btn btn-secondary" },
                     [`Open ${scheme} ↗`]);
        b.onclick = () => window.open(`${scheme}://${host}:${port}/`, "_blank",
                                      "noopener,noreferrer");
        return b;
    };
    wrap.appendChild(el("div", { class: "pt-launcher-btns" },
                        [mk("https"), mk("http")]));
    container.appendChild(wrap);
}

// Shared pass-through claim form — used by the per-project Ports section
// (projectName fixed) and the Management "Pass-through ports" subsection
// (projects picker: ALL broker-known rows, stopped included — a claim on a
// stopped project takes effect at its next start, same contract as the
// per-project section — orphaned excluded: a project whose container is gone
// can never use a claim). Payload discipline: host_port is OMITTED when it
// equals the app port and count is OMITTED when 1 — the server defaults
// them, and an explicit null/0 refuses loudly by design (never send
// placeholders).
// The warning lines a port add/remove returns (a reserved entry it dropped from
// the project's port list; the folders a re-run box started without), one ⚠
// line each in `warnEl`, replacing what it showed. Shared by the four port
// call sites: ⚙ Ports add/remove, the claim form, Sandboxes → Infrastructure's claim remove.
function showPortWarnings(warnEl, result) {
    const lines = (result && Array.isArray(result.warnings)) ? result.warnings : [];
    warnEl.replaceChildren(...lines.map((l) => el("div", {}, ["⚠ " + l])));
}

function buildPassthroughClaimForm(opts) {
    const wrap = el("div", { class: "pt-claim-form" });
    let projSel = null;
    if (!opts.projectName) {
        projSel = el("select", { class: "config-port-box" });
        for (const p of (opts.projects || [])) {
            projSel.appendChild(el("option", { value: p }, [p]));
        }
    }
    const hostI = el("input", { type: "number", min: "1", max: "65535",
                                class: "config-port-num",
                                placeholder: "host port" });
    const countI = el("input", { type: "number", min: "1",
                                 class: "config-port-num",
                                 placeholder: "count (1)" });
    const appI = el("input", { type: "number", min: "1", max: "65535",
                               class: "config-port-num",
                               placeholder: "app port (same)" });
    const labelI = el("input", { type: "text", class: "config-port-label",
                                 placeholder: "label" });
    const claimBtn = el("button", { class: "btn btn-secondary" }, ["Claim"]);
    const errEl = el("div", { class: "pt-claim-err" });
    errEl.style.display = "none";
    const warnEl = el("div", { class: "config-hint config-box-warn" });
    const hint = el("div", { class: "config-hint" },
                    ["Loading the pass-through block…"]);
    (async () => {
        try {
            const res = await fetch("/broker/passthrough");
            let body; try { body = await res.json(); } catch (e) { body = null; }
            if (res.ok && body && body.ok && body.result) {
                const r = body.result;
                const used = (r.claimed || [])
                    .reduce((n, c) => n + (c.count || 1), 0);
                hint.textContent =
                    `Block ${r.lo}-${r.hi} · ${used} claimed · ` +
                    `${r.hi - r.lo + 1 - used} free. Pass-through traffic is ` +
                    "NOT behind the webui login — the app's own login is the " +
                    "only lock. Count > 1 claims a consecutive span (host and " +
                    "app ports advance together).";
            } else {
                hint.textContent = "Couldn't load the pass-through block.";
            }
        } catch (e) {
            hint.textContent = "Couldn't load the pass-through block.";
        }
    })();
    claimBtn.onclick = async () => {
        errEl.style.display = "none";
        const project = opts.projectName || (projSel && projSel.value) || "";
        if (!project) { alert("Pick a project."); return; }
        const host = parseInt(hostI.value, 10);
        if (!Number.isInteger(host)) {
            alert("Enter the host port to claim."); return;
        }
        const label = labelI.value.trim();
        if (!label) { alert("Enter a label for the tab."); return; }
        const cnt = countI.value.trim() ? parseInt(countI.value, 10) : 1;
        const app = appI.value.trim() ? parseInt(appI.value, 10) : host;
        const payload = { port: app, label, box: "", passthrough: true };
        if (host !== app) payload.host_port = host;
        if (cnt > 1) payload.count = cnt;
        claimBtn.disabled = true;
        try {
            const res = await fetch(
                `/broker/project/${encodeURIComponent(project)}/port`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
            let body; try { body = await res.json(); } catch (e) { body = {}; }
            if (opts.onStatus && opts.onStatus(res.status)) return;
            if (!res.ok || !body.ok) {
                errEl.textContent =
                    "Couldn't claim: " + (mgmtErrText(body) || res.status);
                errEl.style.display = "";
                return;
            }
            showPortWarnings(warnEl, body.result);
            hostI.value = ""; countI.value = ""; appI.value = "";
            labelI.value = "";
            if (opts.onClaimed) await opts.onClaimed(project);
        } catch (e) {
            errEl.textContent = "Couldn't claim: connection failed.";
            errEl.style.display = "";
        } finally {
            claimBtn.disabled = false;
        }
    };
    wrap.appendChild(el("div", { class: "config-box-row config-port-add" },
        [projSel, hostI, countI, appI, labelI, claimBtn].filter(Boolean)));
    wrap.appendChild(errEl);
    wrap.appendChild(warnEl);
    wrap.appendChild(hint);
    return wrap;
}

// Sandboxes → the moved-address notice: tabs whose browser address fell
// outside the tab address range at the webui's last start, so they opened on a
// new address where the browser's saved state for them starts fresh. The
// placeholder is appended SYNCHRONOUSLY and filled async (section order must
// not depend on fetch timing, like the sections below). Advisory only: any
// failure leaves it hidden — the page's own read already routed 401/503, so it
// never redirects. No CLI is ever mentioned here.
function appendOriginNotice(view) {
    const box = el("div", { class: "mgmt-origin-notice" });
    box.hidden = true;
    view.appendChild(box);
    fillOriginNotice(box);
    return box;
}

async function fillOriginNotice(box) {
    let body = null;
    try {
        const res = await fetch("/broker/origin-ports/dropped");
        if (res.status !== 200) return;
        try { body = await res.json(); } catch (e) { return; }
    } catch (e) {
        return;
    }
    if (!body || !body.ok || !body.result || !Array.isArray(body.result.dropped)) return;
    const dropped = body.result.dropped;
    if (dropped.length === 0) return;
    const dismiss = el("button", { class: "btn-small" }, ["Dismiss"]);
    dismiss.onclick = async () => {
        try {
            const res = await fetch("/broker/origin-ports/dropped/dismiss", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: "{}",
            });
            if (res.status === 200) box.hidden = true;
        } catch (e) {
            // Connection failed: leave the notice up so the operator can retry.
        }
    };
    box.appendChild(el("div", { class: "mgmt-origin-title" },
        ["Some tabs moved to a new address"]));
    box.appendChild(el("p", {}, [
        "The range of tab addresses changed, and these tabs' previous ports " +
        "are outside it. They now open on a new address, where what the " +
        "browser kept for them (an editor's layout, open files and Recents " +
        "list, a web app's saved settings) starts fresh. Project files and " +
        "agent sessions are not affected.",
    ]));
    box.appendChild(el("ul", { class: "mgmt-origin-list" }, dropped.map((d) =>
        el("li", {}, [`${d.where} — ${d.what} (was port ${d.port})`]))));
    box.appendChild(dismiss);
    box.hidden = false;
}

// Sandboxes → Infrastructure → Pass-through ports: the host-global view — the block, every
// claim, remove-any, claim-for-any-project. Placed ABOVE Infrastructure at
// both renderMgmtTable call sites. Static DOM is built SYNCHRONOUSLY before
// any await (the appendInfraSection D-placement invariant: section order on
// the page must not depend on fetch timing). No CLI is ever mentioned here.
async function appendPassthroughSection(view, projects) {
    const table = el("div", { class: "mgmt-pt-list" });
    // Outside the table, which every refill empties: a remove's warnings stay.
    const warnEl = el("div", { class: "config-hint config-box-warn" });
    const wrap = el("div", { class: "mgmt-infra" }, [
        el("h3", { class: "mgmt-infra-title" }, ["Pass-through ports"]),
        table,
        warnEl,
    ]);
    const names = (projects || []).filter((p) => p.state !== ORPHANED_STATE)
        .map((p) => p.project);
    wrap.appendChild(buildPassthroughClaimForm({
        projects: names,
        onClaimed: async (project) => {
            await fillPassthroughList(view, table, warnEl);
            await refreshAfterBoxChange(project);
        },
        onStatus: (status) => {
            // Relay-status arm (the transport-vs-verb split): 401/403/503
            // route to the Management landing; anything else falls through to
            // the inline verb text beside the form.
            const redirect = mgmtStatusRedirect(view, status);
            if (redirect) { redirect(); return true; }
            return false;
        },
    }));
    view.appendChild(wrap);
    await fillPassthroughList(view, table, warnEl);
}

async function fillPassthroughList(view, table, warnEl) {
    table.innerHTML = "";
    let body = null;
    try {
        const res = await fetch("/broker/passthrough");
        try { body = await res.json(); } catch (e) { body = null; }
        if (!res.ok || !body || !body.ok || !body.result) {
            table.appendChild(el("div", { class: "mgmt-pt-status" },
                ["Couldn't load pass-through claims."]));
            return;
        }
    } catch (e) {
        table.appendChild(el("div", { class: "mgmt-pt-status" },
            ["Couldn't load pass-through claims."]));
        return;
    }
    const r = body.result;
    const used = (r.claimed || []).reduce((n, c) => n + (c.count || 1), 0);
    table.appendChild(el("div", { class: "mgmt-pt-status" }, [
        `Block ${r.lo}-${r.hi} · ${used} port(s) claimed · ` +
        `${r.hi - r.lo + 1 - used} free`,
    ]));
    for (const c of (r.claimed || [])) {
        const cnt = c.count || 1;
        const span = cnt > 1 ? `${c.host_port}-${c.host_port + cnt - 1}`
                             : `${c.host_port}`;
        const rm = el("button", { class: "close-tab-btn",
                                  title: `Remove claim ${span}` }, ["✕"]);
        rm.onclick = async () => {
            try {
                const res = await fetch(
                    `/broker/project/${encodeURIComponent(c.project)}/port-remove`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ port: c.host_port,
                                               passthrough: true }),
                    });
                let b; try { b = await res.json(); } catch (e) { b = {}; }
                const redirect = mgmtStatusRedirect(view, res.status);
                if (redirect) { redirect(); return; }
                if (!res.ok || !b.ok) {
                    alert("Couldn't remove: " + (mgmtErrText(b) || res.status));
                    return;
                }
                if (warnEl) showPortWarnings(warnEl, b.result);
                await fillPassthroughList(view, table, warnEl);
                await refreshAfterBoxChange(c.project);
            } catch (e) { alert("Couldn't remove: connection failed."); }
        };
        table.appendChild(el("div", { class: "mgmt-row mgmt-pt-row" }, [
            el("span", { class: "mgmt-name" }, [c.project]),
            el("span", { class: "mgmt-pt-span" }, [`⇉ ${span} → ${c.port}`]),
            el("span", {}, [c.label]),
            rm,
        ]));
    }
    if ((r.ambiguous || []).length) {
        table.appendChild(el("div", { class: "mgmt-pt-status" }, [
            `Conflicting claims on: ${r.ambiguous.join(", ")} — these ports ` +
            "refuse connections until one of the claims is removed.",
        ]));
    }
}

// ⚙ Sharing: everything that shares folders with the project, in one bordered
// box on every project (stopped ones and ones whose tabs never loaded too) —
// the project's own folders, then, on a dind project with boxes, Box folders.
// It opens folded at every gear open; openProjectConfigBox re-places the panel
// when it unfolds. `enabled` is null in the panel's short form (no tabs
// loaded), where only the project's folders show.
// `box` is the panel's Sharing box (makeProjectConfigBox).
function appendSharingSection(box, project, enabled) {
    appendProjectFoldersSection(box, project);
    if (enabled) appendBoxFoldersSection(box, project, enabled);
}

// The ⚙ text for a saved folder change that waits for the next start. A dind
// project applies it at its next start or Update; a docker project (a plain
// sandbox) only at Stop then Start, which then re-creates its container.
function projectFoldersPendingText(r) {
    if (r.rebuilds) {
        return "Saved. The change applies when the project next starts " +
               "(Stop, then Start, in Sandboxes → Management). That start rebuilds the " +
               "project: its agent must log in again, and anything installed " +
               "outside /workspace is gone.";
    }
    return "Saved. The change applies when the project next starts: Stop, " +
           "then Start, or Update, in Sandboxes → Management.";
}

// ⚙ Project folders: the project's folders in RS's data folder, read-only at
// /workspace/mnt/<last name>/. Saving checks the paths exactly as create does
// and RECORDS them; nothing restarts — they apply at the project's next start
// (maintainer, 2026-11-05), and the section says so while a change waits.
// Folders set when the project was created from outside the data folder are
// listed by name and cannot be changed here.
function appendProjectFoldersSection(box, project) {
    const section = el("div", { class: "config-section" });
    const label = el("div", { class: "config-section-label" }, ["Project folders"]);
    section.appendChild(label);
    const content = el("div", {}, [el("div", { class: "config-empty" }, ["Loading…"])]);
    section.appendChild(content);
    box.appendChild(section);
    const url = `/broker/project/${encodeURIComponent(project.name)}/folders`;
    (async () => {
        let res = null, b = null;
        try {
            res = await fetch(url);
            try { b = await res.json(); } catch (e) { b = {}; }
        } catch (e) { b = null; }
        if (!b || !res.ok || !b.ok || !b.result) {
            content.replaceChildren(el("div", { class: "config-empty" },
                [(b && mgmtErrText(b)) || "Couldn't load the folders."]));
            return;
        }
        const first = b.result;
        const foldersI = el("input", { type: "text", autocomplete: "off",
                                       class: "config-folders-input",
                                       placeholder: "my-dataset, surveys/2024" });
        const saveBtn = el("button", { class: "btn btn-secondary" }, ["Save"]);
        const fixedEl = el("div", { class: "config-hint" });
        const pendingEl = el("div", { class: "config-hint" });
        const status = el("div", { class: "config-hint" });
        const draw = (r) => {
            foldersI.value = (r.folders || []).join(", ");
            const fixed = Array.isArray(r.fixed) ? r.fixed : [];
            fixedEl.replaceChildren(...(fixed.length ? [
                "Also mounted, set when the project was created (it cannot be " +
                "changed here): " + fixed.map((n) => "/workspace/mnt/" + n).join(", "),
            ] : []));
            const lines = [];
            if (r.problem) {
                lines.push(el("div", { class: "config-box-warn" }, [
                    "⚠ The saved folders cannot be applied: " + r.problem +
                    ". The project keeps its current folders."]));
            } else if (r.pending) {
                lines.push(el("div", { class: "config-box-warn" },
                              ["⚠ " + projectFoldersPendingText(r)]));
            }
            pendingEl.replaceChildren(...lines);
        };
        saveBtn.onclick = async () => {
            const folders = parseFolderList(foldersI.value);
            const bad = folders.find((p) => p.startsWith("/") || p.startsWith("~"));
            if (bad) {
                status.replaceChildren(el("div", {}, [
                    "Folder paths are relative to RS's data folder (got: " + bad + ")."]));
                return;
            }
            saveBtn.disabled = true;
            status.replaceChildren(el("div", {}, ["Saving…"]));
            try {
                const r = await fetch(url, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ folders }),
                });
                let rb; try { rb = await r.json(); } catch (e) { rb = {}; }
                if (!r.ok || !rb.ok || !rb.result) {
                    status.replaceChildren(el("div", {},
                        ["Couldn't save: " + (mgmtErrText(rb) || r.status)]));
                    return;
                }
                draw(rb.result);
                // A waiting change is announced by its own warning line
                // (which starts "Saved."); only the no-op save needs a word.
                status.replaceChildren(...(rb.result.pending || rb.result.problem ? [] : [
                    el("div", {}, ["Saved — the project already has these folders."])]));
            } catch (e) {
                status.replaceChildren(el("div", {}, ["Couldn't save: connection failed."]));
            } finally {
                saveBtn.disabled = false;
            }
        };
        draw(first);
        const info = infoIcon(
            "Comma-separated paths relative to RS's data folder on this host" +
            (first.data_dir ? " (" + first.data_dir + ")" : "") + ", for example " +
            "my-dataset or surveys/2024. Each is mounted read-only at " +
            "/workspace/mnt/<last name>/ inside the project: the agent, every " +
            "worker and every role service see it, and a box when it is given to " +
            "it. A folder that does not exist yet is created; a link is refused. " +
            "Saving does not restart anything: the change applies the next time " +
            "the project starts, and a folder taken away disappears from the " +
            "project then.");
        label.append(" ", info.icon);
        content.replaceChildren(
            info.detail,
            foldersI,
            el("div", { class: "config-box-row config-save-row" }, [saveBtn]),
            el("div", { class: "config-hint" }, [PROJECT_FOLDERS_SHORT]),
            fixedEl,
            pendingEl,
            status,
        );
    })();
}

// ⚙ Box folders: which project folders a box reads, read-only at /mnt/<path>.
// Shown on a dind project that has boxes; dev boxes are not listed (they take
// none). Reads the boxes list — each box's folder paths + dev flag, plus the
// project's top-level folders for the example line — and saves the whole new
// set for one box from the same comma-separated field the box window uses. A
// recorded folder that is gone right now stays in the field, so saving
// another change keeps it (deleting it from the field removes it). Saving
// restarts the box fresh; an unchanged set does nothing on a running box.
function appendBoxFoldersSection(box, project, enabled) {
    const isDind = !!(enabled && enabled.supervisor && enabled.supervisor.box_harness);
    const hasBoxes = Object.keys(enabled || {}).some((id) => id.startsWith("box-term-"));
    if (!isDind || !hasBoxes) return;
    const section = el("div", { class: "config-section" });
    const label = el("div", { class: "config-section-label" }, ["Box folders"]);
    section.appendChild(label);
    const content = el("div", {}, [el("div", { class: "config-empty" }, ["Loading…"])]);
    section.appendChild(content);
    box.appendChild(section);
    (async () => {
        let res = null, b = null;
        try {
            res = await fetch(`/broker/project/${encodeURIComponent(project.name)}/boxes`);
            try { b = await res.json(); } catch (e) { b = {}; }
        } catch (e) { b = null; }
        if (!b || !res.ok || !b.ok || !b.result) {
            content.replaceChildren(el("div", { class: "config-empty" },
                [(b && mgmtErrText(b)) || "Couldn't load the boxes."]));
            return;
        }
        const allBoxes = b.result.boxes || [];
        const boxes = allBoxes.filter((x) => !x.dev);
        const choices = b.result.folder_choices || [];
        const choicesError = b.result.folder_choices_error || "";
        // Which boxes share files — every box, dev boxes included (fixed at
        // creation, so a line to read, not a control).
        const shareLine = el("div", { class: "config-hint" }, [boxShareLine(allBoxes)]);
        if (!boxes.length) {
            content.replaceChildren(shareLine, el("div", { class: "config-empty" },
                ["No box here takes folders."]));
            return;
        }
        const sel = el("select", { class: "config-port-box" },
                       boxes.map((x) => el("option", { value: x.name }, [x.name])));
        const foldersI = el("input", { type: "text", autocomplete: "off",
                                       class: "config-folders-input",
                                       placeholder: "data, results/run3" });
        const status = el("div", { class: "config-hint" });
        const saveBtn = el("button", { class: "btn btn-secondary" }, ["Save"]);
        const current = () => boxes.find((x) => x.name === sel.value) || boxes[0];
        const draw = () => { foldersI.value = (current().folders || []).join(", "); };
        sel.onchange = () => { status.replaceChildren(); draw(); };
        saveBtn.onclick = async () => {
            const cur = current();
            const folders = parseFolderList(foldersI.value);
            const bad = folderListProblem(folders);
            if (bad) { status.replaceChildren(el("div", {}, [bad])); return; }
            saveBtn.disabled = true;
            status.replaceChildren(el("div", {}, ["Saving…"]));
            try {
                const r = await fetch(
                    `/broker/project/${encodeURIComponent(project.name)}/box-folders`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ name: cur.name, folders }),
                    });
                let rb; try { rb = await r.json(); } catch (e) { rb = {}; }
                if (!r.ok || !rb.ok || !rb.result) {
                    status.replaceChildren(el("div", {},
                        ["Couldn't save: " + (mgmtErrText(rb) || r.status)]));
                    return;
                }
                const out = rb.result;
                cur.folders = Array.isArray(out.folders) ? out.folders : folders;
                draw();
                const lines = [el("div", {}, [out.changed
                    ? "Saved — the box restarted fresh." : "Nothing changed."])];
                for (const w of (Array.isArray(out.warnings) ? out.warnings : [])) {
                    lines.push(el("div", { class: "config-box-warn" }, ["⚠ " + w]));
                }
                status.replaceChildren(...lines);
                if (out.changed) await refreshAfterBoxChange(project.name);
            } catch (e) {
                status.replaceChildren(el("div", {}, ["Couldn't save: connection failed."]));
            } finally {
                saveBtn.disabled = false;
            }
        };
        draw();
        // The full explanation rides the title's ⓘ (added once the list read
        // succeeded, so it carries this project's folders).
        const info = infoIcon(BOX_FOLDERS_HINT + " Saving restarts the box fresh: " +
            "its agent must log in again and its setup (for example OpenKnowledge's " +
            "install) runs again. Files in the box's own folder are untouched.\n\n" +
            folderChoicesLine(choices, choicesError));
        label.append(" ", info.icon);
        content.replaceChildren(
            info.detail,
            shareLine,
            el("div", { class: "config-box-row" }, [sel, saveBtn]),
            foldersI,
            el("div", { class: "config-hint" }, [BOX_FOLDERS_SHORT]),
            status,
        );
    })();
}

function appendExportedPortsSection(box, project) {
    // `box` is the panel's Ports box; its title is the heading (Detected apps
    // follows in the same box, under its own sub-heading).
    const section = el("div", { class: "config-section" });
    const listWrap = el("div", { class: "config-ports-list" });
    section.appendChild(listWrap);
    // The warning lines of an add/remove: a reserved entry dropped from the
    // port list, and the folders a box started without after a port change
    // re-ran it (the box arm).
    const warnEl = el("div", { class: "config-hint config-box-warn" });
    section.appendChild(warnEl);
    const showWarnings = (result) => showPortWarnings(warnEl, result);

    const portI = el("input", { type: "number", min: "1", max: "65535",
                                class: "config-port-num", placeholder: "port" });
    const labelI = el("input", { type: "text", class: "config-port-label",
                                 placeholder: "label" });
    const addBtn = el("button", { class: "btn btn-secondary" }, ["Add"]);
    // Box selector (F3 Slice 2b): a dind project's boxes each surface a box-term-<name>
    // service; the operator can expose a 127.0.0.1 port on one of them, or on the
    // supervisor (value ""). Absent when the project has no boxes.
    const svcs = (state.projectServices && state.projectServices[project.name]) || {};
    const boxNames = Object.keys(svcs)
        .filter((id) => id.startsWith("box-term-"))
        .map((id) => id.slice("box-term-".length))
        .sort();
    let boxSel = null;
    if (boxNames.length) {
        boxSel = el("select", { class: "config-port-box" });
        boxSel.appendChild(el("option", { value: "" }, ["supervisor"]));
        for (const b of boxNames) boxSel.appendChild(el("option", { value: b }, [b]));
    }

    // Rows are built first and swapped in at the end, so two overlapping loads
    // (the initial one and a quick Add) can never interleave their rows.
    async function loadPorts() {
        let body;
        try {
            const res = await fetch(
                `/broker/project/${encodeURIComponent(project.name)}/ports`);
            try { body = await res.json(); } catch (e) { body = {}; }
            if (!res.ok || !body.ok || !body.result) {
                listWrap.replaceChildren(el("div", { class: "config-empty" },
                    [mgmtErrText(body) || "Couldn't load ports."]));
                return;
            }
        } catch (e) {
            listWrap.replaceChildren(el("div", { class: "config-empty" },
                ["Couldn't load ports."]));
            return;
        }
        const ports = body.result.ports || [];
        if (ports.length === 0) {
            listWrap.replaceChildren(el("div", { class: "config-empty" },
                ["No ports exported yet."]));
            return;
        }
        // A web-page tab's own address, for an app that admits only its
        // declared public URL. Read from the ALREADY-loaded services — never
        // fetched here, so opening this box allocates nothing; a project whose
        // tabs were never loaded simply shows no address yet.
        const svcs = state.projectServices[project.name] || {};
        const rows = [];
        for (const e of ports) {
            const isPt = !!e.passthrough;
            // A box's own web-app tab belongs to the box (removing the box
            // removes it), a docker project's to the project, and each is told
            // its address by itself: no ✕, no address line.
            const isWeb = e.web === true;
            const rmKey = isPt ? e.host_port : e.port;
            const rm = el("button", { class: "close-tab-btn",
                                      title: `Remove ${isPt ? "claim" : "port"} ${rmKey}` }, ["✕"]);
            rm.onclick = () => removePort(rmKey, isPt);
            // A box entry (F3 Slice 2b) shows its box + in-box app port; a
            // pass-through claim shows its host span + target; a
            // supervisor/docker entry shows the plain port.
            const cnt = e.count || 1;
            const ptSpan = isPt
                ? (cnt > 1 ? `${e.host_port}-${e.host_port + cnt - 1}`
                           : `${e.host_port}`)
                : "";
            const text = isPt
                ? `⇉ ${ptSpan} → ${e.port} — ${e.label}`
                : (e.box
                    ? `${e.label} — box ${e.box}:${e.app_port}`
                    : `${e.port} — ${e.label}`);
            // The address goes AFTER the ✕ as its own block line, so the label
            // and ✕ keep their one-line layout and a row without one is unchanged.
            const addr = (isPt || isWeb) ? "" : ((svcs[`port-${e.port}`] || {}).origin_url || "");
            rows.push(el("div", { class: "config-box-row" }, [
                el("span", { class: "config-box-name" }, [text]),
                ...(isWeb ? [] : [rm]),
                ...(addr ? [el("span", {
                    class: "config-port-addr",
                    title: "This tab's address — give it to an app that asks for its public URL",
                }, [addr.replace(/\/$/, "")])] : []),
            ]));
        }
        listWrap.replaceChildren(...rows);
    }

    async function addPort() {
        const port = parseInt(portI.value, 10);
        const label = labelI.value.trim();
        if (!Number.isInteger(port) || port < 1 || port > 65535) {
            alert("Enter a port between 1 and 65535."); return;
        }
        if (!label) { alert("Enter a label for the tab."); return; }
        const box = boxSel ? boxSel.value : "";
        addBtn.disabled = true;
        try {
            const res = await fetch(
                `/broker/project/${encodeURIComponent(project.name)}/port`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ port, label, box }),
                });
            let body; try { body = await res.json(); } catch (e) { body = {}; }
            if (!res.ok || !body.ok) {
                alert("Couldn't add port: " + (mgmtErrText(body) || res.status));
                return;
            }
            showWarnings(body.result);
            portI.value = ""; labelI.value = "";
            // Refresh first: it drops the cached services, so the list below
            // can show the new tab's address once the project re-activates.
            await refreshAfterBoxChange(project.name);
            await loadPorts();
        } finally {
            addBtn.disabled = false;
        }
    }

    async function removePort(port, passthrough) {
        try {
            const res = await fetch(
                `/broker/project/${encodeURIComponent(project.name)}/port-remove`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ port, passthrough: !!passthrough }),
                });
            let body; try { body = await res.json(); } catch (e) { body = {}; }
            if (!res.ok || !body.ok) {
                alert("Couldn't remove port: " + (mgmtErrText(body) || res.status));
                return;
            }
            showWarnings(body.result);
            if (passthrough) {
                await loadPorts();
                await refreshAfterBoxChange(project.name);
            } else {
                await refreshAfterBoxChange(project.name);
                await loadPorts();
            }
        } catch (e) { /* best-effort */ }
    }

    addBtn.onclick = addPort;
    // Mode toggle (STAGE_PASSTHROUGH_PORTS): "Web page" keeps the embedded
    // page-relay row; "Pass-through" swaps in the shared raw-TCP claim form
    // (top-level only — never a box). style.display (not the hidden attr) —
    // an author display rule on these rows would beat the UA [hidden] style.
    const modeSel = el("select", { class: "config-port-box" });
    modeSel.appendChild(el("option", { value: "page" }, ["Web page"]));
    modeSel.appendChild(el("option", { value: "pass" }, ["Pass-through"]));
    const pageRow = el("div", { class: "config-box-row config-port-add" },
                       [boxSel, portI, labelI, addBtn].filter(Boolean));
    const ptForm = buildPassthroughClaimForm({
        projectName: project.name,
        onClaimed: async () => {
            await loadPorts();
            await refreshAfterBoxChange(project.name);
        },
    });
    ptForm.style.display = "none";
    modeSel.onchange = () => {
        const pass = modeSel.value === "pass";
        pageRow.style.display = pass ? "none" : "";
        ptForm.style.display = pass ? "" : "none";
    };
    section.appendChild(el("div", { class: "config-box-row" }, [modeSel]));
    section.appendChild(pageRow);
    section.appendChild(ptForm);
    section.appendChild(el("div", { class: "config-hint" }, [
        boxSel
            ? "Web-page tabs relay one HTTP port (serve on 127.0.0.1 in the supervisor or the selected box) and appear as soon as the port is registered; pass-through claims add a launcher tab that opens the app in a new browser tab."
            : "Web-page tabs relay one HTTP port (serve on 127.0.0.1 inside the container) and appear as soon as the port is registered; pass-through claims add a launcher tab that opens the app in a new browser tab.",
    ]));
    box.appendChild(section);
    loadPorts();
}

// Detected web apps (STAGE_AUTO_PORTS) in the project's Ports settings: the
// per-project detection switch (for every browser), the apps opened so far
// (each holding one of the limited tab addresses) with Remove, and the paused
// note. Fetched here through the gated route with alloc=0 — opening this box
// never allocates an address — and never written into the tab map.
const DETECTION_PAUSED_TEXT = "Detection is paused until you unlock again or open Sandboxes.";

function appendDetectedAppsSection(box, project) {
    const section = el("div", { class: "config-section" });
    section.appendChild(el("div", { class: "config-section-label" }, ["Detected apps"]));
    const body = el("div", {});
    section.appendChild(body);
    box.appendChild(section);
    const name = encodeURIComponent(project.name);

    async function load() {
        if (state.detectPaused) {
            body.replaceChildren(el("div", { class: "config-hint" }, [DETECTION_PAUSED_TEXT]));
            return;
        }
        let res = null, b = {};
        try {
            res = await fetch(`/broker/project/${name}/services?detect=0&alloc=0`);
            try { b = await res.json(); } catch (_) { b = {}; }
        } catch (_) { res = null; }
        if (res && res.status === 401) {
            body.replaceChildren(el("div", { class: "config-hint" }, [DETECTION_PAUSED_TEXT]));
            return;
        }
        if (!res) {
            body.replaceChildren(el("div", { class: "config-empty" },
                ["Couldn't load detected apps."]));
            return;
        }
        if (!b.ok) {
            body.replaceChildren(el("div", { class: "config-empty" },
                ["Couldn't load detected apps."]));
            return;
        }
        const on = b.detection_enabled !== false;
        const cb = el("input", { type: "checkbox" });
        cb.checked = on;
        cb.onchange = async () => {
            cb.disabled = true;
            try {
                const r = await fetch(`/broker/project/${name}/ports/detection`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ enabled: cb.checked }),
                });
                let rb = {}; try { rb = await r.json(); } catch (_) { rb = {}; }
                if (!r.ok || !rb.ok) alert("Couldn't change detection: " + (mgmtErrText(rb) || r.status));
            } finally {
                await load();
                if (state.activeProject === project.name) refreshActiveServices();
            }
        };
        const rows = [el("label", { class: "mgmt-check" }, [cb,
            el("span", {}, ["Give web apps running in this project a tab by themselves"])])];
        const opened = b.opened || [];
        if (opened.length) {
            rows.push(el("div", { class: "config-hint" }, [
                "Opened apps — each keeps a tab address until removed:"]));
            for (const o of opened) {
                const rm = el("button", { class: "close-tab-btn",
                                          title: `Remove ${o.port}` }, ["✕"]);
                rm.onclick = async () => {
                    rm.disabled = true;
                    try {
                        const r = await fetch(`/broker/project/${name}/ports/${o.port}/origin`,
                                              { method: "DELETE" });
                        let rb = {}; try { rb = await r.json(); } catch (_) { rb = {}; }
                        if (!r.ok || !rb.ok) alert("Couldn't remove: " + (mgmtErrText(rb) || r.status));
                    } finally {
                        await load();
                        if (state.activeProject === project.name) refreshActiveServices();
                    }
                };
                rows.push(el("div", { class: "config-box-row" }, [
                    el("span", { class: "config-box-name" }, [`${o.port} — ${o.label}`]),
                    rm,
                ]));
            }
        }
        body.replaceChildren(...rows);
    }
    load();
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
        project: name,
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
        project: name,
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
    syncProjectUiState(project);

    await persistVault();

    if (state.activeProject !== project.name) return;
    const enabled = state.projectServices[project.name] || {};
    renderServiceTabs(project.name, enabled);
    // Reconcile the column layout against the changed visible set (a hidden
    // tab leaves its column; the engine repairs that column's active).
    applyColumnLayout();
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

// Tab TYPE for the strip's color coding (a colored top border per family):
// "cli" = terminals, "editor" = code-server + box editors (the spec stamp),
// "git" = gitea fork tabs + fetch panes, "other" = everything else (reader,
// exported ports, unknown). Keyed on spec DATA where the spec carries it —
// an explicit tab_type, then the fields openHttpService itself dispatches
// on — never on id prefixes.
function tabTypeOf(id, svc) {
    if (svc.tab_type) return svc.tab_type;
    if (svc.gitea_path || svc.fetch_repo || svc.fetch_all) return "git";
    if (id === "code-server") return "editor";
    return surfaceOf(id, svc) === "cli" ? "cli" : "other";
}

// Per-tab refresh glyph (a ¾ ring + clockwise arrowhead). Hand-authored
// inline SVG (CSP-clean: inline markup, currentColor, no external refs).
// Built via innerHTML — the makePinButton() precedent — because el() routes
// through createElement, which makes an inert HTML-namespace <svg> that
// does not render.
const REFRESH_TAB_SVG =
    '<svg viewBox="0 0 16 16" fill="currentColor" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M8 1.5A6.5 6.5 0 1 1 1.5 8h2A4.5 4.5 0 1 0 8 3.5z"/>' +
    '<path d="M8 0l4.5 2.5L8 5z"/></svg>';

// ---- tab layout (vault `tab_columns`) --------------------------------------

// Default tab order for a project that has never been arranged: Visual
// (editors / iframe surfaces) first — the editor is the primary work
// surface — then CLI (terminals), preserving SERVICES insertion order within
// each group. This partition is the ONLY survivor of the old grouped-strip
// rendering (its divider is gone); it seeds order and nothing else.
function defaultTabOrder(visibleIds, enabled) {
    const visual = [], cli = [];
    for (const id of visibleIds) {
        (surfaceOf(id, enabled[id]) === "visual" ? visual : cli).push(id);
    }
    return visual.concat(cli);
}

// Reconcile the project's persisted tab layout (vault `tab_columns`, an array
// of columns each holding service ids — single-column at this stage) against
// the currently VISIBLE ids. Pure and EPHEMERAL: ids that vanished are
// dropped, duplicates collapse, never-seen ids append to column 0's tail in
// default relative order, empty columns fold away. It never writes the
// vault — persistence happens only on user arrangement actions (a drop), so
// a flapping service probe can't churn vault writes. A project without the
// field decodes to a single column in default order — vault schema stays v1
// (the tolerant-decode precedent of the retired pin fields).
function reconcileTabLayout(project, visibleIds, enabled) {
    const stored = Array.isArray(project?.tab_columns) ? project.tab_columns : [];
    const seen = new Set();
    const columns = [];
    for (const col of stored) {
        if (!Array.isArray(col)) continue;
        const kept = [];
        for (const id of col) {
            if (typeof id === "string" && visibleIds.includes(id) && !seen.has(id)) {
                seen.add(id);
                kept.push(id);
            }
        }
        if (kept.length) columns.push(kept);
    }
    // Overflow columns fold into the last kept one rather than dropping tabs.
    if (columns.length > MAX_TAB_COLUMNS) {
        const extra = columns.splice(MAX_TAB_COLUMNS);
        columns[MAX_TAB_COLUMNS - 1].push(...extra.flat());
    }
    if (columns.length === 0) columns.push([]);
    for (const id of defaultTabOrder(visibleIds, enabled)) {
        if (!seen.has(id)) columns[0].push(id);
    }
    return columns;
}

async function persistTabLayout(project, columns, ratios) {
    project.tab_columns = columns;
    if (columns.length > 1 && Array.isArray(ratios)) project.column_ratios = ratios;
    else delete project.column_ratios;
    syncProjectUiState(project);
    await persistVault();
}

// Apply a drop target to the working layout {columns, ratios}. Targets:
//   {type:"strip", col, index} — insert at `index` in column `col`'s list
//   {type:"append", col}       — move to column `col`'s end (pane-center drop)
//   {type:"split", col, side}  — new column "before"/"after" column `col`,
//                                taking half its width
// Returns true iff the layout changed. Emptied source columns collapse with
// their width folding into a neighbor. Known accepted edge: at
// MAX_TAB_COLUMNS every split refuses — including the sole-tab move that
// would net the same column count — the same arrangement is reachable via
// an append plus reorders.
function moveTabInLayout(layout, id, target) {
    const { columns, ratios } = layout;
    const src = columns.findIndex((c) => c.includes(id));
    if (src === -1) return false;
    const srcIdx = columns[src].indexOf(id);

    if (target.type === "strip" || target.type === "append") {
        const dst = target.col;
        if (dst < 0 || dst >= columns.length) return false;
        let index = target.type === "append" ? columns[dst].length : target.index;
        columns[src].splice(srcIdx, 1);
        if (dst === src && srcIdx < index) index -= 1;
        index = Math.max(0, Math.min(index, columns[dst].length));
        if (dst === src && index === srcIdx) {
            columns[src].splice(srcIdx, 0, id);   // dropped where it started
            return false;
        }
        columns[dst].splice(index, 0, id);
    } else if (target.type === "split") {
        if (columns.length >= MAX_TAB_COLUMNS) return false;
        const at = target.col + (target.side === "after" ? 1 : 0);
        // A column's sole tab split adjacent to itself recreates the same
        // shape — refuse as a no-op.
        if (columns[src].length === 1 && (at === src || at === src + 1)) return false;
        columns[src].splice(srcIdx, 1);
        const half = ratios[target.col] / 2;
        ratios[target.col] = half;
        columns.splice(at, 0, [id]);
        ratios.splice(at, 0, half);
    } else {
        return false;
    }

    // Collapse emptied columns, folding their width into a neighbor.
    for (let i = columns.length - 1; i >= 0; i--) {
        if (columns[i].length) continue;
        const neighbor = i > 0 ? i - 1 : 1;
        if (ratios[neighbor] !== undefined) ratios[neighbor] += ratios[i];
        columns.splice(i, 1);
        ratios.splice(i, 1);
    }
    return true;
}

// ---- tab drag-to-reorder (desktop only) ------------------------------------

// A press is a CLICK below this slop and a DRAG above it — click-to-activate
// must survive arming. 5px is the common browser drag threshold: small
// enough that a deliberate drag engages immediately, large enough that a
// shaky press still lands as a click.
const TAB_DRAG_SLOP_PX = 5;

// With pointer capture the browser synthesizes the post-drag click on the
// captured tab (possibly already detached by the drop re-render) — this
// module flag lets the tab's onclick swallow exactly that one click.
let tabDragClickPending = false;
function consumeTabDragClick() {
    const pending = tabDragClickPending;
    tabDragClickPending = false;
    return pending;
}

// Pointer-based drag (setPointerCapture), NEVER HTML5 drag-and-drop: captured
// moves keep routing to the tab while the pointer crosses cross-origin
// iframes (the splitter precedent — see installColumnSplitterDrag), where dragover
// events would die inside the frame's browsing context. The drop position is
// resolved mathematically from tab midpoints, never via elementFromPoint.
// ctx = { projectName, enabled, project, columns } closed over by the render.
function armTabDrag(tab, id, ctx) {
    tab.onpointerdown = (ev) => {
        if (mobileModeActive()) return;           // desktop-only affordance
        if (ev.button !== 0) return;
        if (ev.target.closest("button")) return;  // refresh / box ✕ keep their clicks
        if (!ctx.project) return;                 // no vault entry — nothing to persist to
        const area = document.getElementById("terminal-area");
        const startX = ev.clientX, startY = ev.clientY;
        let dragging = false;
        let ghost = null, indicator = null, overlay = null;
        let target = null;

        // Resolve the pointer position to a drop target. The ONE top strip
        // wins over panes: the flat insertion point (midpoint rule over all
        // groups) maps to (column, in-column index) via the anchor tab's
        // data-column stamp — inserting before a group's first tab joins
        // THAT group, so a boundary drop reads as "start of the right
        // group"; appending to a group's end happens via its pane's center.
        // Within a pane, the outer COLUMN_EDGE_BAND_FRAC bands split a new
        // column out (only below MAX_TAB_COLUMNS — at the cap they are
        // inert and the whole pane reads as move-into), the center appends.
        const resolveTarget = (mv) => {
            const layout = state.tabLayout;
            if (!layout) return null;
            const top = document.getElementById("service-tabs");
            if (top) {
                const r = top.getBoundingClientRect();
                if (mv.clientX >= r.left && mv.clientX <= r.right
                    && mv.clientY >= r.top && mv.clientY <= r.bottom) {
                    const tabs = Array.from(top.querySelectorAll(".tab[data-service]"));
                    if (!tabs.length) return null;
                    let flatIndex = tabs.length;
                    for (let i = 0; i < tabs.length; i++) {
                        const tr = tabs[i].getBoundingClientRect();
                        if (mv.clientX < tr.left + tr.width / 2) { flatIndex = i; break; }
                    }
                    let col, index;
                    if (flatIndex < tabs.length) {
                        col = parseInt(tabs[flatIndex].getAttribute("data-column") || "0", 10);
                        index = tabs.slice(0, flatIndex).filter(
                            (t) => t.getAttribute("data-column") === String(col)).length;
                    } else {
                        col = parseInt(tabs[tabs.length - 1].getAttribute("data-column") || "0", 10);
                        index = (layout.columns[col] || []).length;
                    }
                    return { type: "strip", col, index, stripEl: top, tabs, flatIndex };
                }
            }
            if (!area) return null;
            const ar = area.getBoundingClientRect();
            if (mv.clientX < ar.left || mv.clientX > ar.right
                || mv.clientY < ar.top || mv.clientY > ar.bottom) return null;
            const frac = (mv.clientX - ar.left) / ar.width;
            let x = 0, col = 0;
            for (let i = 0; i < layout.ratios.length; i++) {
                col = i;
                if (frac < x + layout.ratios[i] || i === layout.ratios.length - 1) break;
                x += layout.ratios[i];
            }
            const within = (frac - x) / layout.ratios[col];
            if (layout.columns.length < MAX_TAB_COLUMNS) {
                if (within < COLUMN_EDGE_BAND_FRAC) return { type: "split", col, side: "before" };
                if (within > 1 - COLUMN_EDGE_BAND_FRAC) return { type: "split", col, side: "after" };
            }
            return { type: "append", col };
        };

        // Feedback: an insertion caret inside the target strip, or a tinted
        // region over the target pane (full column = move-into; half = the
        // new column a split would create).
        const showFeedback = (t) => {
            if (indicator) { indicator.remove(); indicator = null; }
            if (overlay) { overlay.remove(); overlay = null; }
            if (!t) return;
            if (t.type === "strip") {
                const anchor = t.tabs[Math.min(t.flatIndex, t.tabs.length - 1)];
                if (!anchor) return;
                const sr = t.stripEl.getBoundingClientRect();
                const tr = anchor.getBoundingClientRect();
                const edgeX = (t.flatIndex < t.tabs.length ? tr.left : tr.right)
                    - sr.left + t.stripEl.scrollLeft;
                indicator = el("div", { class: "tab-drop-indicator" });
                indicator.style.left = `${edgeX}px`;
                t.stripEl.appendChild(indicator);
            } else if (area && state.tabLayout) {
                const { ratios } = state.tabLayout;
                let left = 0;
                for (let i = 0; i < t.col; i++) left += ratios[i];
                let w = ratios[t.col];
                if (t.type === "split") {
                    w = w / 2;
                    if (t.side === "after") left += w;
                }
                overlay = el("div", { class: "column-drop-overlay" });
                overlay.style.left = pctOf(left);
                overlay.style.width = pctOf(w);
                area.appendChild(overlay);
            }
        };

        const onMove = (mv) => {
            if (!dragging) {
                if (Math.abs(mv.clientX - startX) < TAB_DRAG_SLOP_PX
                    && Math.abs(mv.clientY - startY) < TAB_DRAG_SLOP_PX) return;
                dragging = true;
                tab.classList.add("dragging");
                document.body.style.userSelect = "none";
                ghost = tab.cloneNode(true);
                ghost.classList.add("tab-drag-ghost");
                document.body.appendChild(ghost);
            }
            ghost.style.left = `${mv.clientX + 10}px`;
            ghost.style.top = `${mv.clientY + 10}px`;
            target = resolveTarget(mv);
            showFeedback(target);
        };
        const cleanup = () => {
            // lostpointercapture FIRST: the explicit release below fires it
            // synchronously, and removing this listener before releasing is
            // what prevents a re-entrant onCancel → cleanup cycle.
            tab.removeEventListener("lostpointercapture", onCancel);
            tab.removeEventListener("pointermove", onMove);
            tab.removeEventListener("pointerup", onUp);
            tab.removeEventListener("pointercancel", onCancel);
            try { tab.releasePointerCapture(ev.pointerId); } catch (_) {}
            if (ghost) ghost.remove();
            if (indicator) indicator.remove();
            if (overlay) overlay.remove();
            tab.classList.remove("dragging");
            document.body.style.userSelect = "";
        };
        const onCancel = () => { cleanup(); };
        const onUp = async () => {
            const didDrag = dragging;
            const t = target;
            cleanup();
            if (!didDrag) return;                 // plain click — let it through
            tabDragClickPending = true;
            const layout = state.tabLayout;
            if (!t || !layout) return;            // dropped nowhere actionable
            const move = t.type === "strip"
                ? { type: "strip", col: t.col, index: t.index } : t;
            if (!moveTabInLayout(layout, id, move)) return;
            await persistTabLayout(ctx.project, layout.columns, layout.ratios);
            // Staleness bail (the refreshActiveServices idiom): the active
            // project may have changed during the await — never paint another
            // project's strip from this closure's captured ctx.
            if (state.activeProject !== ctx.projectName) return;
            renderServiceTabs(ctx.projectName, ctx.enabled);
            applyColumnLayout();
            // The moved tab becomes its new column's shown tab and takes
            // focus (the VS Code convention).
            activateService(id);
        };
        tab.setPointerCapture(ev.pointerId);
        tab.addEventListener("pointermove", onMove);
        tab.addEventListener("pointerup", onUp);
        tab.addEventListener("pointercancel", onCancel);
        // A mid-drag strip re-render (refreshActiveServices on a service-set
        // signature change — e.g. a box editor's stub coming up) detaches the
        // captured tab, which implicitly releases capture and fires
        // lostpointercapture at the detached element; the subsequent pointerup
        // goes elsewhere, so WITHOUT this path neither onUp nor onCancel would
        // run — the ghost would leak and body user-select would stick. The
        // drag dies cleanly at the re-render instead (which also retires the
        // stale-layout splice class — state.tabLayout is re-stashed by the
        // engine on every rebuild). A plain click never reaches it: onUp
        // removes this listener before the implicit post-pointerup release.
        tab.addEventListener("lostpointercapture", onCancel);
    };
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
    // + Add box sits in the chrome cluster — chip-adjacent, BEFORE the
    // separator (PI decision: `Projects | name | + | ─ | tabs`) — for ANY
    // dind project (research + sandbox-dind): the server stamps `box_harness`
    // on the always-present Supervisor tab spec (STAGE_DIND_UNIFY); on
    // research the harness is staged lazily on the first box_add. Boxes are
    // created where they appear, not in a sidebar settings panel.
    const boxHarness = !!(enabled["supervisor"] && enabled["supervisor"].box_harness);
    if (boxHarness) {
        const addBox = el("button", {
            class: "add-box-tab-btn", title: "Add a box",
        }, ["+"]);
        addBox.onclick = () => mgmtBoxAddDialog(projectName);
        strip.appendChild(addBox);
    }
    const ids = visibleServiceIds(projectName, enabled);
    if (ids.length === 0) {
        strip.appendChild(el("div", { class: "tab-group-divider" }));
        strip.appendChild(el("div", { class: "empty" }, [
            "No services enabled for this project.",
        ]));
        // A rebuild under an open host page (a probe-driven tab change) must
        // keep the page's strip mode — the wipe above removed its .host-strip.
        if (state.hostPage) setServiceTabsHidden(true);
        return;
    }
    // Tab placement: ONE strip, always — no second row (PI decision). Mobile
    // renders the flat set (its CSS whitelist hides what it hides; columns
    // never render there). Desktop single-column keeps the plain inline flow.
    // Desktop multi-column wraps EVERY column's tabs in an absolutely-
    // positioned, WIDTH-CAPPED .column-tab-group anchored above its pane —
    // group 0 from where the chrome ends to the first boundary, each further
    // group spanning exactly its pane (PI decisions, three dogfood rounds:
    // no extra strip row, tabs on top of their column, and NO OVERLAP — a
    // too-narrow column shrinks its tabs to fit instead, labels ellipsizing,
    // the wrapper clipping as the last resort). positionStripGroups sets the
    // geometry and positionColumnChrome keeps it tracking the splitters
    // live. data-column stamps carry each tab's group for the drag's
    // strip-drop resolution and the per-column active repaint.
    const project = state.vault.projects.find((p) => p.name === projectName);
    const columns = reconcileTabLayout(project, ids, enabled);
    const ctx = { projectName, enabled, project };
    strip.appendChild(el("div", { class: "tab-group-divider" }));
    if (mobileModeActive() || columns.length === 1) {
        columns.forEach((col, i) => {
            for (const id of col) strip.appendChild(makeServiceTab(id, ctx, i));
        });
    } else {
        const ratios = reconcileColumnRatios(project, columns.length);
        columns.forEach((col, i) => {
            const group = el("div", {
                class: i === 0
                    ? "column-tab-group column-tab-group-first"
                    : "column-tab-group",
            });
            for (const id of col) group.appendChild(makeServiceTab(id, ctx, i));
            strip.appendChild(group);
        });
        positionStripGroups(ratios);
    }
    // Same as the early return above: keep an open host page's strip.
    if (state.hostPage) setServiceTabsHidden(true);
}

// One service tab. Order/columns come from the vault layout
// (reconcileTabLayout); the old visual-then-CLI partition survives only
// inside defaultTabOrder as the never-arranged default. colIdx stamps
// data-column — the tab's group — read by the drag's strip-drop resolution
// and paintColumnActives.
// A box-term-* tab is a disposable BOX (gets the ✕) when box_kind === "sandbox".
function makeServiceTab(id, ctx, colIdx = 0) {
    const svc = ctx.enabled[id];
    const refreshBtn = el("button", {
        class: "refresh-tab-btn", title: "Refresh tab",
    });
    refreshBtn.innerHTML = REFRESH_TAB_SVG;
    refreshBtn.onclick = (ev) => { ev.stopPropagation(); refreshService(id); };
    const kids = [refreshBtn, el("span", {}, [svc.label || id])];
    // A detected web app (STAGE_AUTO_PORTS): Hide is the per-project tab
    // hiding. (To keep its tab when the app is down, add the port by hand in
    // the Ports section — same id, so the address carries over.)
    if (svc.detected) {
        const hide = el("button", { class: "close-tab-btn", title: "Hide this tab" }, ["✕"]);
        hide.onclick = (ev) => {
            ev.stopPropagation();
            if (ctx.project) setServiceHidden(ctx.project, id, true);
        };
        kids.push(hide);
    }
    // A real box tab (box_kind === "sandbox") carries a ✕ that discards the box
    // in place — box deletion lives on the tab, not in a sidebar settings panel.
    const boxHarness = !!(ctx.enabled["supervisor"] && ctx.enabled["supervisor"].box_harness);
    if (boxHarness && id.startsWith("box-term-") && svc.box_kind === "sandbox") {
        const boxName = id.slice("box-term-".length);
        const closeBtn = el("button", {
            class: "close-tab-btn", title: `Remove box "${boxName}"`,
        }, ["✕"]);
        closeBtn.onclick = (ev) => {
            ev.stopPropagation();
            mgmtBoxRemoveDialog(ctx.projectName, boxName);
        };
        kids.push(closeBtn);
    }
    const tabAttrs = {
        class: "tab" + (svc.detected ? " tab-detected" : "") + (svc.stopped ? " tab-stopped" : ""),
        "data-service": id,
        "data-column": String(colIdx),
        // Surface stamp for the mobile CSS (visual tabs are hidden there);
        // nothing desktop keys on it.
        "data-surface": surfaceOf(id, svc),
        // Type stamp for the color-coded top border (style.css
        // [data-tabtype] rules); scoping on the attribute keeps the
        // Projects tab / project chip border-free.
        "data-tabtype": tabTypeOf(id, svc),
        // consumeTabDragClick swallows the one click the browser
        // synthesizes on the captured tab after a real drag.
        onclick: () => {
            if (consumeTabDragClick()) return;
            // click: an operator open — the only kind that may reserve a
            // detected app's tab address.
            activateService(id, { click: true });
        },
    };
    // el() sets every attribute it is given, so a title exists only on a
    // detected tab (a null would render as the text "null").
    if (svc.detected) {
        tabAttrs.title = svc.stopped ? "Detected automatically — the app has stopped"
                                     : "Detected automatically";
    }
    const tab = el("div", tabAttrs, kids);
    armTabDrag(tab, id, ctx);
    return tab;
}

// Refresh a tab's content in place: tear down the live pane (terminal,
// iframe, or fetch panel) and re-run the open path via activateService. ssh
// tabs reattach byobu (the session and its content survive server-side; only
// the browser-side xterm scrollback resets), http tabs re-mint their session
// and reload the upstream, fetch panes re-fetch. Nulling onclose BEFORE the
// close is load-bearing for LIVE terminals: the old socket's async close
// event recomputes the key at fire time and would stamp `disconnected` on
// the FRESH entry the reopen registers under it (the disconnected-branch
// teardown in activateService doesn't need this — there the ws is already
// closed, so close() cannot re-fire the handler).
function refreshService(serviceId) {
    if (!state.activeProject) return;
    const key = tkey(state.activeProject, serviceId);
    const t = state.terminals[key];
    if (t) {
        try { if (t.ws) { t.ws.onclose = null; t.ws.close(); } } catch (_) {}
        try { if (t.term) t.term.dispose(); } catch (_) {}
        try { if (t.container) t.container.remove(); } catch (_) {}
        delete state.terminals[key];
    }
    activateService(serviceId, { click: true });   // the tab's own ⟳: an operator open
}

function activateService(serviceId, opts) {
    if (!state.activeProject) return;

    const project = state.vault.projects.find((p) => p.name === state.activeProject);
    if (!project) return;
    const enabled = state.projectServices[state.activeProject] || {};
    const svc = enabled[serviceId];
    if (!svc) return;

    // Background activation (opts.background) opens a pane WITHOUT taking
    // focus or the landing bookkeeping — the multi-column auto-open path
    // (the successor of the retired pin's !isPinned guard).
    const background = !!(opts && opts.background);

    // Column bookkeeping: the activated tab becomes its column's shown one.
    // Single-column layouts resolve to column 0; on mobile tabLayout is null
    // and the flat model below applies.
    const layout = state.tabLayout;
    if (layout) {
        const col = layout.columns.findIndex((c) => c.includes(serviceId));
        if (col !== -1) state.columnActive[col] = serviceId;
    }

    if (!background) {
        // Underline: this global toggle is the mobile/single-column path; in
        // a multi-column layout it transiently strips the other columns'
        // actives, and the engine call below repaints them all from
        // columnActive in the same synchronous frame (paintColumnActives).
        document.querySelectorAll("#service-tabs .tab").forEach((t) => t.classList.remove("active"));
        const tabEl = document.querySelector(`#service-tabs .tab[data-service="${CSS.escape(serviceId)}"]`);
        if (tabEl) tabEl.classList.add("active");
        state.activeService = serviceId;
        state.projectLastService[state.activeProject] = serviceId;
    }
    // Key-bar visibility follows the resolved KIND of the active service, so
    // it must be derived here — BEFORE the existing-terminal fast path below
    // returns early — or a reader→CLI re-activation would skip it.
    updateMobileKeybar();

    if (mobileModeActive()) {
        // Mobile keeps the flat model: hide everything except the active.
        const activeKey = state.activeService ? tkey(state.activeProject, state.activeService) : null;
        for (const [k, t] of Object.entries(state.terminals)) {
            if (!t.container) continue;
            if (k === activeKey) t.container.classList.remove("hidden");
            else t.container.classList.add("hidden");
        }
    } else {
        // Desktop: the engine owns visibility + geometry + per-column strip
        // actives (columnActive was updated above).
        applyColumnLayout();
    }
    const welcome = document.getElementById("welcome");
    if (welcome) welcome.style.display = "none";

    const key = tkey(state.activeProject, serviceId);
    const existing = state.terminals[key];
    const keptNote = !!(existing && existing.disconnected && existing.note
                        && opts && opts.keepNote);
    if (existing && (!existing.disconnected || keptNote)) {
        if (existing.container) existing.container.classList.remove("hidden");
        // Un-hide first, sync second, fit third: the pane is visible here, so
        // a pending font-size change applies with a correct re-measure before
        // the fit consumes the metrics. Mobile-critical — this fast path (plus
        // the drawer-close refit) is the whole mobile show path.
        syncTermFontSize(existing);
        if (existing.fitAddon) existing.fitAddon.fit();
        if (existing.term && !background) existing.term.focus();
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
        openHttpService(project, serviceId, svc, opts);
    } else {
        const parent = document.getElementById("terminal-area");
        const placeholder = el("div", { class: "welcome" }, [
            `Unknown service kind: ${svc.kind}`,
        ]);
        if (parent) parent.appendChild(placeholder);
    }
    // A freshly-created container has no inline geometry — position it (and
    // repaint strip actives) now. The open paths append synchronously even
    // when their session mint is still in flight, so the rect lands before
    // any content does. Desktop-only; mobile containers use the base inset.
    if (!mobileModeActive()) applyColumnLayout();
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
    // Sweep the column engine's chrome (strips/splitters) and stale inline
    // geometry — the welcome states (zero-visible, project-less) must not
    // keep orphaned strips floating over the empty area.
    applyColumnLayout();
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
    // Under an open host page, put its strip back too — AFTER the openers, so
    // the page's chip can never stand in for the project placeholder chip.
    if (state.hostPage) setServiceTabsHidden(true);
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
            // Refit every VISIBLE terminal: the focused one plus each
            // column's shown tab (the successor of the old active+pinned
            // pair — percent-based column rects resize with the window, so
            // this re-measure is the only per-resize work the engine needs).
            const keys = new Set();
            if (state.activeService) keys.add(tkey(state.activeProject, state.activeService));
            for (const cid of state.columnActive) {
                if (cid) keys.add(tkey(state.activeProject, cid));
            }
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

// Touch scrolling for the CLI tab. xterm has no touch path a finger can use
// inside a full-screen app: 6.0 has no touch scrolling at all, and 5.x's only
// moved xterm's own scrollback viewport (dead in the alternate buffer, where
// byobu/tmux always keep us) and bailed outright while the app had mouse
// tracking on. Everything a desktop wheel does lives in xterm's `wheel`
// listener on term.element — the mouse report when tracking is on, the
// wheel→arrow-key conversion when the buffer has no scrollback — so a
// vertical finger drag is re-expressed as wheel events there: one LINE-mode
// event per row of travel (both arms emit exactly one sequence per event, so
// N rows = N arrows = N reports; pixel deltas would fight xterm's own
// per-version accumulator), carrying the touch point so the report arm's
// coordinate lookup lands inside the screen. In a plain shell (normal buffer,
// no wheel-requesting tracking) the history is scrolled through the public scrollLines
// instead — the 6.0 scrollable element ignores synthetic wheels. Capture
// phase + stopPropagation keep xterm's own bubble-phase touch listeners (5.x)
// and the document-level gesture recogniser (6.0) from acting on the same
// drag; style.css's `touch-action: pinch-zoom` on the pad keeps the browser
// from claiming the gesture as a native pan first. A sub-row jitter stays
// uncancelled so a shaky tap keeps its click/focus. Touch-only by
// construction (a touchscreen laptop gets it too) — deliberately NOT behind
// mobileModeActive(); the mouse/wheel path stays byte-identical.
function installTouchScroll(term) {
    const el = term.element;
    let lastY = null;
    let carry = 0;
    let consumed = false;   // this drag has scrolled: cancel + stop from here on
    const seed = (touch) => { lastY = touch.clientY; carry = 0; consumed = false; };
    const reset = () => { lastY = null; carry = 0; consumed = false; };
    el.addEventListener("touchstart", (ev) => {
        if (ev.touches.length !== 1) { reset(); return; }   // pinch etc. stay native
        seed(ev.touches[0]);
    }, { capture: true, passive: true });
    el.addEventListener("touchmove", (ev) => {
        if (lastY === null || ev.touches.length !== 1) return;
        // xterm sizes .xterm-screen to exactly rows × cellHeight (both the DOM
        // and the WebGL renderer), so this is the live row height with no
        // private API. A hidden pane (0) cannot be touched — no fallback.
        const screen = el.querySelector(".xterm-screen");
        const rowPx = screen && term.rows ? screen.clientHeight / term.rows : 0;
        if (!(rowPx > 0)) return;
        const t = ev.touches[0];
        carry += lastY - t.clientY;          // finger up ⇒ positive ⇒ toward newer lines
        lastY = t.clientY;
        const rows = Math.trunc(carry / rowPx);
        carry -= rows * rowPx;
        // A sub-row jitter is not a scroll: leave it uncancelled so a slightly
        // shaky tap keeps its click/focus (cancelling the first touchmove drops
        // the gesture's compat mouse events).
        if (rows === 0 && !consumed) return;
        consumed = true;
        if (rows !== 0) {
            // x10 tracking reports button presses only — no wheel — so it
            // takes the scrollLines arm outside the alternate buffer; the
            // three wheel-requesting modes are the ones xterm detaches its
            // own viewport scrolling for.
            const mode = term.modes.mouseTrackingMode;
            if (term.buffer.active.type === "alternate"
                || mode === "vt200" || mode === "drag" || mode === "any") {
                // A finger that wanders into the fit addon's scrollbar gutter
                // would make xterm drop the report (no cell under it): clamp
                // the point to the screen so no row of travel is lost.
                const r = screen.getBoundingClientRect();
                const cx = Math.min(Math.max(t.clientX, r.left), r.right - 1);
                const cy = Math.min(Math.max(t.clientY, r.top), r.bottom - 1);
                const sign = rows > 0 ? 1 : -1;
                for (let i = 0; i < Math.abs(rows); i++) {
                    el.dispatchEvent(new WheelEvent("wheel", {
                        deltaY: sign, deltaMode: WheelEvent.DOM_DELTA_LINE,
                        clientX: cx, clientY: cy,
                        bubbles: true, cancelable: true,
                    }));
                }
            } else {
                term.scrollLines(rows);
            }
        }
        ev.stopPropagation();
        // Once the browser has let an uncancelled sub-row move through, the
        // rest of that gesture arrives non-cancelable (a Chrome intervention);
        // the pad's touch-action already forbids the pan, so only cancel what
        // can be cancelled rather than log a warning per move.
        if (ev.cancelable) ev.preventDefault();
    }, { capture: true, passive: false });
    // A pinch that ends with one finger still down re-seeds from that finger
    // so the drag can continue; the last finger lifting resets.
    const endOrCancel = (ev) => {
        if (ev.touches.length === 1) seed(ev.touches[0]); else reset();
    };
    el.addEventListener("touchend", endOrCancel, { capture: true });
    el.addEventListener("touchcancel", endOrCancel, { capture: true });
}

function openSshTerminal(project, serviceId, svc) {
    const container = el("div", { class: "terminal-instance ssh-instance" });
    // Inset wrapper: gives the visual breathing room WITHOUT putting
    // padding on the element xterm-fit measures. See style.css comment
    // on .terminal-pad for the fit-addon quirk this works around.
    const pad = el("div", { class: "terminal-pad" });
    container.appendChild(pad);
    document.getElementById("terminal-area").appendChild(container);

    const term = new Terminal({
        cursorBlink: true,
        fontFamily: "ui-monospace, Menlo, Consolas, monospace",
        fontSize: state.termFontSize,
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
    installTouchScroll(term);
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

// Mount a dev consumer's gitea fork in a service pane. The shared gitea origin
// is gated by the sentinel cookie that POST /broker/dev/gitea-session mints
// (Management-session-anchored — the route lives under /broker/ because that
// cookie is Path=/broker), so the mint comes first and the fork's path rides on
// top of the origin URL it hands back.
//
// TRANSPORT failure and VERB failure are distinguishable by construction and are
// kept apart here: a throw or a 503 means the broker is genuinely unreachable; a
// 200 carrying {ok:false} means it ANSWERED and refused. Collapsing the two would
// send the operator to restart a daemon that is answering fine.
async function mountGiteaFork(container, status, giteaPath) {
    const fail = (msg, retry) => {
        status.textContent = msg;
        if (retry) {
            const b = el("button", { class: "btn-small" }, ["Retry"]);
            b.onclick = () => {
                status.textContent = "Authenticating…";
                const stale = container.querySelector("button");
                if (stale) stale.remove();
                mountGiteaFork(container, status, giteaPath);
            };
            container.appendChild(b);
        }
    };
    let res;
    try {
        res = await fetch("/broker/dev/gitea-session", { method: "POST" });
    } catch (e) {
        return fail("Gitea isn't reachable right now.", true);
    }
    if (res.status === 503) return fail("Gitea isn't reachable right now.", true);
    if (res.status === 401) {
        // The vault unlock auto-logs into Management, so a 401 here means the
        // vault and broker passwords disagree — a host-side fix, not a retry.
        return fail("Not signed in to Management — unlock the vault again.", false);
    }
    if (res.status === 403) return fail("Origin rejected.", false);
    let data;
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok || !data.ok || !data.url) {
        return fail(`Could not open the fork: ${mgmtErrText(data)}`, true);
    }
    // clipboard-write delegation is what lets Gitea's native code-block copy
    // button work inside the cross-origin frame (Permissions-Policy features
    // whose default allowlist is 'self' are silently revoked otherwise). NO
    // sandbox attribute — this follows the Development gitea frame, not the
    // neighbouring per-origin http tab, which does sandbox its iframe.
    const iframe = el("iframe", {
        class: "http-iframe",
        src: data.url.replace(/\/$/, "") + giteaPath,
        allow: "fullscreen; clipboard-read; clipboard-write",
    });
    iframe.setAttribute("allowfullscreen", "");
    status.remove();
    container.appendChild(iframe);
}

async function openHttpService(project, serviceId, svc, opts) {
    const container = el("div", { class: "terminal-instance http-instance" });
    const status = el("div", { class: "http-status" }, ["Authenticating…"]);
    container.appendChild(status);
    document.getElementById("terminal-area").appendChild(container);

    const key = tkey(project.name, serviceId);
    // This pane's own entry: a refresh or teardown replaces or deletes it, and
    // every wait below checks it is still the registered one.
    const me = { kind: "http", container, project, service: serviceId };
    state.terminals[key] = me;

    // A PASS-THROUGH claim renders a LAUNCHER pane — the app is encrypted
    // and/or multi-port, so nothing embeds: the buttons open
    // https://<this host>:<host_port> in a NEW browser tab (window.open
    // inside the click gesture — popup blockers eat it outside one). Branch
    // BEFORE the /session mint below (a launcher never loads a per-container
    // origin, so minting a cookie for it would be pure waste) and before the
    // `!svc.origin_url` bail. The bookkeeping above is shared deliberately:
    // teardown stays identical (no ws/term/iframe to reap).
    if (svc.passthrough_port) {
        renderPassthroughLauncher(container, status, svc);
        return;
    }

    // A FETCH pane renders RS-side off the broker relay — no iframe, no
    // upstream origin, no session mint (branch BEFORE the mint and the
    // gitea_path branch; neither applies). The bookkeeping above is shared
    // deliberately: teardown stays identical. Two markers, one renderer:
    // `fetch_repo` is the per-repo dev tab, `fetch_all` the project-wide tab of
    // an rs-fetch-enabled project (null repo ⇒ every mirror). The
    // dev-fetch-pane class is load-bearing on BOTH — it supplies the pane's
    // scroll + padding.
    if (svc.fetch_repo || svc.fetch_all) {
        container.classList.add("dev-fetch-pane");
        await renderProjectFetchPane(container, svc.fetch_repo || null);
        return;
    }

    // A detected web app with no address yet is opened only by the operator
    // (a click on its tab or its ⟳). A landing, a column's auto-open or a
    // fallback leaves a note to click instead: reserving one of the limited
    // tab addresses is the operator's act, never a side effect.
    if (svc.detected && !svc.origin_url && !(opts && opts.click)) {
        status.textContent = CLICK_TO_OPEN_TEXT;
        me.disconnected = true; me.note = true;
        return;
    }

    // A dev consumer's gitea FORK tab serves off the SHARED -gitea- sentinel
    // origin, not a per-container origin port — so it has no origin_url, and it
    // must branch BEFORE the /session/<project> mint below (which would mint a
    // cookie for an origin this tab never loads) and before that mint's
    // `!svc.origin_url` bail (which would otherwise reject this tab outright).
    // The bookkeeping above is shared deliberately: teardown stays identical.
    if (svc.gitea_path) {
        await mountGiteaFork(container, status, svc.gitea_path);
        return;
    }

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

    // A detected web app (STAGE_AUTO_PORTS) has no address until it is first
    // opened: reserve one now (an operator act, like adding a port by hand),
    // and write it into the tab map so a redraw keeps it.
    if (svc && svc.detected && !svc.origin_url) {
        if (state.terminals[key] !== me) return;
        status.textContent = "Opening…";
        const port = serviceId.slice("port-".length);
        const gen = state.loginGen;
        let res = null, b = {};
        try {
            res = await fetch(`/broker/project/${encodeURIComponent(project.name)}` +
                              `/ports/${encodeURIComponent(port)}/origin`, { method: "POST" });
            try { b = await res.json(); } catch (_) { b = {}; }
        } catch (_) { res = null; }
        if (state.terminals[key] !== me) return;
        // A failed open leaves its pane as a disconnected note: a click then
        // takes activateService's teardown-and-reopen branch (a retry), and
        // no refresh or landing retries by itself.
        if (res && res.status === 401) {
            if (gen === state.loginGen) state.detectPaused = true;
            status.textContent = DETECTION_PAUSED_TEXT + " Then click the tab again.";
            me.disconnected = true; me.note = true;
            return;
        }
        if (!b.ok || !b.origin_url) {
            status.textContent = ((b.error && b.error.message) || "Couldn't open this app.") +
                " Click the tab to try again.";
            me.disconnected = true; me.note = true;
            return;
        }
        svc.origin_url = b.origin_url;
        const cur = (state.projectServices[project.name] || {})[serviceId];
        if (cur) cur.origin_url = b.origin_url;
    }
    // Mount the iframe at the service's own browser origin (a dedicated
    // webui port per container — /services/<project> allocates it and hands
    // back origin_url). The session cookie minted above is host-keyed and
    // port-blind, so it reaches the origin port with no extra handshake.
    if (!svc || !svc.origin_url) {
        status.textContent = "No origin port available for this service.";
        return;
    }
    // A box's (or a docker project's) own web app answers only at the exact
    // address its tab is opened at: tell it first, and wait until the app
    // answers there. The pane
    // may be torn down (a refresh) while this waits — then stop, mounting
    // nothing. Whatever the outcome, the iframe loads in the end.
    if (svc.follow_address) {
        if (state.terminals[key] !== me) return;
        const label = svc.label || "the app";
        status.textContent = `Starting ${label} at this address…`;
        const url = `/broker/project/${encodeURIComponent(project.name)}/web-address`;
        const out = await waitForWebApp({
            post: async () => {
                const res = await fetch(url, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ service: serviceId }),
                });
                let b; try { b = await res.json(); } catch (_) { b = {}; }
                return { status: res.status, body: b };
            },
            sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
            isCurrent: () => state.terminals[key] === me,
            attempts: WEB_READY_ATTEMPTS,
            pollMs: WEB_READY_POLL_MS,
        });
        if (out.state === "gone") return;
        if (out.state === "timeout") {
            status.textContent = `${label} has not answered yet. The ` +
                `terminal tab shows why if it does not come up.`;
        } else if (out.state === "error") {
            status.textContent = `Could not hand ${label} its address ` +
                `(${out.message}); loading it anyway.`;
        }
        if (out.state !== "ready") {
            // Keep the reason visible above the frame until dismissed.
            const close = el("button", { class: "close-tab-btn",
                                         title: "Dismiss" }, ["✕"]);
            const banner = el("div", { class: "http-status http-banner" },
                [el("span", {}, [status.textContent]), close]);
            close.onclick = () => banner.remove();
            container.appendChild(banner);   // overlays the frame's top edge
        }
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
    // The address this pane loaded: followTabAddresses closes the pane when
    // its tab's address goes away or changes.
    state.terminals[key].loadedUrl = svc.origin_url;
}

// A box's own web-app tab: how often the tab asks the box again while the app
// is not answering yet, and how many times. The box answers "not ready" at
// once while its setup still installs the app (about a minute, measured, after
// a fresh restart), so the asks are spaced by the delay; 40 × 3 s covers twice
// that install. Once the setup is done, one ask waits a few seconds in the box.
const WEB_READY_POLL_MS = 3000;
const WEB_READY_ATTEMPTS = 40;

// Ask the box (through `post`) until its web app answers at this tab's
// address. Returns {state}: "ready"; "timeout" after `attempts`; "gone" when
// the pane was torn down meanwhile (`isCurrent` turned false — nothing more is
// posted); "error" (with a message) on any other refusal or a dead transport.
// A refusal of kind "busy" (the project is starting, stopping or updating) is
// waited out like "not ready". Standalone so it can run outside a page.
async function waitForWebApp({ post, sleep, isCurrent, attempts, pollMs }) {
    for (let i = 0; i < attempts; i++) {
        let r;
        try {
            r = await post();
        } catch (e) {
            if (!isCurrent()) return { state: "gone" };
            return { state: "error", message: "the webui is not reachable" };
        }
        if (!isCurrent()) return { state: "gone" };
        const b = (r && r.body) || {};
        if (r.status === 200 && b.ok && b.result) {
            if (b.result.ready === true) return { state: "ready" };
        } else if (!(r.status === 200 && b.error && b.error.kind === "busy")) {
            const msg = (b.error && b.error.message) ||
                (r.status === 401 ? "Management is logged out" :
                 r.status === 503 ? "Management is not reachable" :
                 `error ${r.status}`);
            return { state: "error", message: msg };
        }
        await sleep(pollMs);
        if (!isCurrent()) return { state: "gone" };
    }
    return { state: "timeout" };
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
            `Click OK only if you intentionally recreated the supervisor (e.g. you changed its services from Sandboxes) — otherwise this could be a man-in-the-middle.`,
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
    // Terminal choice first: applyTheme's repaint reads it.
    state.termTheme = loadStoredTermTheme();
    applyTheme(loadStoredTheme());
    state.railPinned = loadRailPinned();
    state.railExpanded = state.railPinned;
    state.railWidth = loadRailWidth();
    applyRailWidth(state.railWidth);
    state.iframeZoom = loadIframeZoom();
    applyIframeZoomVar(state.iframeZoom);
    state.cliMaxWidth = loadCliMaxWidth();
    applyCliMaxWidthVar(state.cliMaxWidth);
    state.termFontSize = loadTermFontSize();
    // Mobile class before the first render — the unlock/setup cards are
    // already styled by the html.mobile scope.
    applyMobileClass();
    installMobileModeWatcher();
    installGlobalTermRefit();
    installMobileProjectsOutsideClose();
    installRailOutsideClickHandlers();
    installHostStatsResume();
    if (loadStored()) renderUnlock();
    else renderSetup();
});
