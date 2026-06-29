# Websearcher box

A web-research box — **Playwright + headless Chromium baked in**, with
search-oriented instructions. The agent is on by default.

## What it is

A disposable container whose agent drives a headless Chromium through an
image-baked Playwright stdio MCP. Pages are read by their **accessibility tree**
(image decode is off by design — it's faster and cheaper). The browser is the
box's for the whole session and keeps state across turns; it dies with the box.

## Good for

- Looking things up, following links, and reading pages during a research thread.
- Scraping structured text from sites that need a real browser.
- Any task where the agent needs to *see the web*, not just call an API.

## Notes

- `browser_evaluate` (arbitrary in-page JS) is **denied** at the client layer —
  page content is treated as untrusted data, not instructions.
- Add **MCP tools** for project-specific lookups on top of the browser.
- Boots credential-free: run `claude` then `/login` inside. Outputs live in
  `/workspace`; egress follows the project's router policy.
