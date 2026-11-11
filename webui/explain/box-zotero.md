# Zotero box

A box for your **Zotero library** — the zotero MCP server is wired to it over
Zotero's web API, so the agent can search the library, read full text, and
manage notes and tags. The agent is on by default.

## What it is

A container with the zotero MCP server pre-wired to its agent. The box installs
the zotero MCP server when it boots, and reaches your library over the web with
the API key and library ID you give in the box window; the box keeps them for
its own use.

## Good for

- Finding and reading papers in your library during a project.
- Keeping notes and tags in the library up to date from the agent.
- Literature work alongside the web: tick **Browser** to let the agent search
  the web too.

## The Zotero fields (below Settings)

- **Zotero API key** — create one at zotero.org/settings/keys.
- **Zotero library ID** — your userID, shown on the same page.

Add **MCP tools** if the work needs other project MCP servers. Boots
credential-free: sign the agent in inside (`claude` then `/login`, or `pi` then
`/login`). Outputs live in `/workspace`; egress follows the project's router
policy.
