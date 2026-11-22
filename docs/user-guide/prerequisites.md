---
title: Prerequisites
parent: User guide
nav_order: 3
---
# Prerequisites

- [Docker](https://www.docker.com/) Engine 29.0 or newer, with Compose v2 (`docker compose`, 2.33.1 or newer); `research start` refuses older versions and says which it found
- [Python](https://www.python.org/) 3.9+ (host CLI is stdlib-only — conda, pyenv, system Python, anything works)
- An [Anthropic Claude](https://www.anthropic.com/) subscription (Pro/Team or API access — you authenticate *inside* the project; there are no host-side API keys)

> [!TIP]
> Strongly recommended: [Sysbox](https://github.com/nestybox/sysbox#installation). It's the cleanest way to run a Docker daemon inside a container, giving user-namespace isolation without `--privileged`. Without it the CLI falls back to `--privileged` mode, which works but is less isolated. Linux only.
