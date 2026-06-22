---
id: install-linux
title: Installation on Linux
sidebar_position: 1
slug: /installation/linux
description: Install Harness on Ubuntu, RHEL/Fedora, and Arch Linux using pip in a Python 3.12+ virtual environment.
---

# Installation on Linux

Harness runs on any modern Linux distribution with Python 3.12 or newer. This page covers **Ubuntu 22.04/24.04**, **RHEL/Fedora**, and **Arch Linux**.

For containerized deployments, see [Installation with Docker](./docker.md). For Kubernetes, see [Installation on Kubernetes](./kubernetes.md).

## Prerequisites

| Requirement | Minimum | Recommended |
| ----------- | ------- | ----------- |
| OS | Ubuntu 22.04, RHEL 9, Fedora 39, Arch (rolling) | Latest LTS / stable |
| Python | 3.12 | 3.12 or 3.13 |
| pip | 24.0 | latest |
| Git | 2.30 | latest |
| RAM | 2 GB free | 8 GB+ (local model inference) |
| Disk | 500 MB (Harness only) | 10 GB+ (models, vectors) |

:::note
Harness itself needs only Python and pip. The 8 GB RAM recommendation applies if you run a local LLM provider (e.g., Qwen3 via Ollama) on the same machine.
:::

## Step 1 — Install Python 3.12+

### Ubuntu 22.04 / 24.04

Ubuntu 24.04 ships Python 3.12 in the default repository. On 22.04 use the `deadsnakes` PPA:

```bash
# Ubuntu 24.04 — system package is sufficient
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev git

# Ubuntu 22.04 — add deadsnakes PPA for Python 3.12
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev git
```

Verify:

```bash
python3.12 --version
# Python 3.12.x
```

### RHEL 9 / Fedora 39+

```bash
# Fedora 39+ — dnf module
sudo dnf install -y python3.12 python3.12-devel git

# RHEL 9 — enable the python3.12 module stream first
sudo dnf module enable -y python3.12
sudo dnf install -y python3.12 python3.12-devel git
```

### Arch Linux

Arch rolling release already provides Python 3.12+ as `python`:

```bash
sudo pacman -S --needed python python-pip python-virtualenv git
```

## Step 2 — Create a virtual environment

Always install Harness inside a virtual environment to avoid conflicts with system packages.

```bash
python3.12 -m venv ~/.venvs/harness
source ~/.venvs/harness/bin/activate
python -m pip install --upgrade pip
```

:::tip
Add `source ~/.venvs/harness/bin/activate` to your `~/.bashrc` (or `~/.zshrc`) to activate the environment automatically in new terminals.
:::

## Step 3 — Install Harness

### Method 1: From PyPI (recommended)

```bash
pip install solomon-harness
```

Install optional extras for plugin development and testing:

```bash
pip install "solomon-harness[dev,test]"
```

### Method 2: From source

Use this if you need a development checkout or an unreleased branch.

```bash
git clone https://github.com/solomon-labs/harness.git
cd harness
pip install -e ".[dev,test]"
```

:::warning
Building from source requires the **Rust toolchain** for PyO3 extensions. See [Install from Source](./from-source.md) for full build instructions.
:::

## Step 4 — Verify the installation

```bash
harness --version
# harness 0.1.0
```

Run the built-in self-check to confirm the runtime, default provider, and storage backends:

```bash
harness doctor
```

If `harness doctor` reports issues, see [Troubleshooting](/troubleshooting/).

## Step 5 — Initialize your first project

```bash
harness init
```

The `init` wizard creates a `harness.toml` config in the current directory and starts the first agent automatically. For a guided walkthrough, continue to the [Quickstart tutorial](../tutorials/quickstart.md).

## Next steps

- [Quickstart tutorial](../tutorials/quickstart.md) — build your first agent in 10 minutes
- [Configuration overview](../configuration/overview.md) — providers, memory layers, hooks
- [Install from Source](./from-source.md) — Rust extensions and advanced build flags
- [Troubleshooting](/troubleshooting/) — common Linux install issues
