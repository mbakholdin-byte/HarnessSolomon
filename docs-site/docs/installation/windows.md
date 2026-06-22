---
id: install-windows
title: Installation on Windows
sidebar_position: 3
slug: /installation/windows
description: Install Harness on Windows 10/11 via WSL2 (recommended) or native Python 3.12+ with PowerShell.
---

# Installation on Windows

Harness supports two installation paths on Windows:

- **WSL2 (recommended)** — full Linux environment, best compatibility, matches production deployments.
- **Native Windows** — lighter weight, no VM, but some optional features require extra setup.

:::tip
New to WSL? Microsoft's [official guide](https://learn.microsoft.com/en-us/windows/wsl/install) covers installation and basics.
:::

## Prerequisites

| Requirement | Minimum | Recommended |
| ----------- | ------- | ----------- |
| Windows | 10 version 2004 (build 19041) | Windows 11 |
| WSL2 | Ubuntu 22.04 (if using WSL) | Ubuntu 24.04 |
| Python | 3.12 | 3.12 or 3.13 |
| pip | 24.0 | latest |
| Git | 2.30 | latest |
| RAM | 4 GB free | 16 GB+ (local model inference) |
| Disk | 1 GB (WSL image: ~2 GB) | 20 GB+ (models, vectors) |

## Method 1 — WSL2 with Ubuntu (recommended)

WSL2 gives you a real Linux environment with the same package paths as production. This is the path the Harness team uses for daily development on Windows.

### Step 1.1 — Install WSL2 and Ubuntu

Open **PowerShell as Administrator** and run:

```powershell
wsl --install -d Ubuntu-24.04
```

Restart if prompted, then launch the **Ubuntu** shortcut from the Start menu and complete the Linux user setup.

Verify the WSL version:

```powershell
wsl --status
# Default Version: 2
```

### Step 1.2 — Install Python 3.12 inside Ubuntu

From inside the Ubuntu terminal:

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev git
python3.12 --version
```

### Step 1.3 — Create a virtual environment and install Harness

```bash
python3.12 -m venv ~/.venvs/harness
source ~/.venvs/harness/bin/activate
python -m pip install --upgrade pip
pip install solomon-harness
```

### Step 1.4 — Verify

```bash
harness --version
harness doctor
```

Continue with [Step 3 — Initialize your first project](#step-3--initialize-your-first-project) below.

## Method 2 — Native Windows

Use this path if you cannot run WSL2 (e.g., restricted corporate machine) or want the smallest possible footprint.

### Step 2.1 — Install Python 3.12+

:::warning
During the Python installer, **check "Add python.exe to PATH"** on the first screen. Without this, `python` and `pip` will not resolve in PowerShell.
:::

Option A — Official installer:

1. Download Python 3.12 from the [official downloads page](https://www.python.org/downloads/windows/).
2. Run the installer, enable "Add python.exe to PATH", and complete the setup.

Option B — winget:

```powershell
winget install -e --id Python.Python.3.12
```

Option C — Chocolatey:

```powershell
choco install python --version=3.12.x
```

Verify:

```powershell
python --version
# Python 3.12.x
```

### Step 2.2 — Configure PowerShell execution policy

Python virtual environment activation scripts require script execution, which is disabled by default on some systems.

Check the current policy:

```powershell
Get-ExecutionPolicy -Scope CurrentUser
```

If the policy is `Restricted`, set it to `RemoteSigned` for your user only (no admin rights needed):

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

:::note
`RemoteSigned` allows locally created scripts to run, while scripts downloaded from the internet must be signed. This is the standard recommendation from Microsoft for development machines.
:::

### Step 2.3 — Create a virtual environment and install Harness

```powershell
python -m venv $env:USERPROFILE\.venvs\harness
& $env:USERPROFILE\.venvs\harness\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install solomon-harness
```

Optional extras for plugin development and testing:

```powershell
pip install "solomon-harness[dev,test]"
```

### Step 2.4 — Verify

```powershell
harness --version
harness doctor
```

If `harness doctor` reports issues, see [Troubleshooting](/troubleshooting/).

## VS Code + WSL extension

For the best editing experience with WSL2, install **Visual Studio Code** and the **WSL** extension:

```powershell
winget install -e --id Microsoft.VisualStudioCode
```

Then inside VS Code, install the **"WSL"** extension (Microsoft, `ms-vscode-remote.remote-wsl`) from the marketplace.

Open a Harness project stored inside WSL:

1. Open the WSL Ubuntu terminal.
2. Navigate to your project folder.
3. Run `code .` — VS Code launches on Windows but connects to the Linux file system.

:::warning
Do **not** store your Harness project under `C:\...` and edit it from inside WSL through the `/mnt/c/...` mount. Cross-filesystem I/O is 10–50× slower. Keep the project under `~/projects/` inside the WSL home directory.
:::

## Step 3 — Initialize your first project

```bash
harness init     # WSL (bash)
# or
harness init     # Native Windows (PowerShell)
```

The `init` wizard creates a `harness.toml` config in the current directory and starts the first agent automatically. For a guided walkthrough, continue to the [Quickstart tutorial](../tutorials/quickstart.md).

## Next steps

- [Quickstart tutorial](../tutorials/quickstart.md) — build your first agent in 10 minutes
- [Configuration overview](../configuration/overview.md) — providers, memory layers, hooks
- [Installation on Linux](./linux.md) — WSL2-specific deep dive (Ubuntu packages, SELinux)
- [Install from Source](./from-source.md) — Rust extensions and advanced build flags
- [Troubleshooting](/troubleshooting/) — common Windows install issues
