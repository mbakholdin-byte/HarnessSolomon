---
id: install-macos
title: Installation on macOS
sidebar_position: 2
slug: /installation/macos
description: Install Harness on macOS (Intel and Apple Silicon) via Homebrew or pip in a Python 3.12+ virtual environment.
---

# Installation on macOS

Harness supports both **Intel** and **Apple Silicon** (M1/M2/M3/M4) Macs running macOS 12 Monterey or newer.

## Prerequisites

| Requirement | Minimum | Recommended |
| ----------- | ------- | ----------- |
| macOS | 12 Monterey | 14 Sonoma or newer |
| Python | 3.12 | 3.12 or 3.13 |
| pip | 24.0 | latest |
| Git | 2.30 | latest |
| RAM | 4 GB free | 16 GB+ (local model inference) |
| Disk | 500 MB | 10 GB+ (models, vectors) |

:::note
The Homebrew formula installs Harness and its Python runtime into the Homebrew prefix. The manual method lets you manage Python yourself via Homebrew, `pyenv`, or the official installer from python.org.
:::

## Step 1 — Install Homebrew (if needed)

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Verify:

```bash
brew --version
```

## Step 2 — Install Harness

### Method 1: Homebrew (recommended)

```bash
brew install solomon-labs/tap/harness
```

:::info
The Homebrew formula is maintained in the `solomon-labs/tap` tap. If it is not yet merged into `homebrew-core`, the explicit tap prefix above is required.
:::

Update later with:

```bash
brew upgrade harness
```

Skip to [Step 3 — Verify](#step-3--verify-the-installation).

### Method 2: pip from PyPI

First install Python 3.12+ from Homebrew (recommended over the python.org installer on Apple Silicon):

```bash
brew install python@3.12 git
```

Create and activate a virtual environment:

```bash
python3.12 -m venv ~/.venvs/harness
source ~/.venvs/harness/bin/activate
python -m pip install --upgrade pip
```

Install Harness:

```bash
pip install solomon-harness
```

Optional extras for plugin development and testing:

```bash
pip install "solomon-harness[dev,test]"
```

### Method 3: From source

```bash
git clone https://github.com/solomon-labs/harness.git
cd harness
pip install -e ".[dev,test]"
```

:::warning
Building from source requires the **Rust toolchain** (`brew install rust`) for PyO3 extensions. See [Install from Source](./from-source.md) for full build instructions.
:::

## Step 3 — Verify the installation

```bash
harness --version
# harness 0.1.0
```

Run the self-check:

```bash
harness doctor
```

If `harness doctor` reports issues, see [Troubleshooting](/troubleshooting/).

## Apple Silicon notes

- All Harness wheels published to PyPI include native `arm64` binaries — no Rosetta 2 required.
- If you run a local LLM via Ollama or `mlx-lm`, install the `arm64` build of the provider to get full Metal acceleration.
- The Homebrew prefix differs by architecture: `/opt/homebrew` on Apple Silicon, `/usr/local` on Intel. Harness detects this automatically.

## Step 4 — Initialize your first project

```bash
harness init
```

The `init` wizard creates a `harness.toml` config in the current directory and starts the first agent automatically. For a guided walkthrough, continue to the [Quickstart tutorial](../tutorials/quickstart.md).

## Next steps

- [Quickstart tutorial](../tutorials/quickstart.md) — build your first agent in 10 minutes
- [Configuration overview](../configuration/overview.md) — providers, memory layers, hooks
- [Install from Source](./from-source.md) — Rust extensions and advanced build flags
- [Troubleshooting](/troubleshooting/) — common macOS install issues
