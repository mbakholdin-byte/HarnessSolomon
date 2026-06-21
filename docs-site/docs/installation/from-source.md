---
id: install-from-source
title: Install from Source
sidebar_position: 6
slug: /installation/from-source
---

# Install from Source

:::info Work in progress
This page is being written. Check back soon.
:::

Prerequisites:

- Python 3.12+
- Git
- Rust toolchain (for PyO3 extensions)

Clone and install:

```bash
git clone https://github.com/solomon-labs/harness.git
cd harness
pip install -e ".[dev,test]"
```

Build all optional components:

```bash
make build
```

This compiles Rust extensions, TypeScript plugins, and runs the full test suite.
