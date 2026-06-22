---
id: e2e-user-flow-checklist
title: E2E User Flow — 30 minutes from install to first agent
description: Manual end-to-end checklist for verifying a fresh Harness installation works end-to-end
keywords: [e2e, user-flow, verification, checklist, install, first-agent]
---

# E2E User Flow Checklist

**Goal:** Verify that a **brand new user** can install Harness and create their **first agent** within **30 minutes**, without external help.

**Who should run this:** Solomon (release verification), CI smoke test, or any developer doing release QA.

---

## Pre-requisites (5 min)

- [ ] Clean Linux/macOS/Windows machine (or VM/Docker)
- [ ] Python 3.11+ installed
- [ ] Node.js 18+ installed (for Web UI)
- [ ] Git installed
- [ ] At least 2 GB free disk space
- [ ] API key for at least one LLM provider (MiniMax, OpenAI, Anthropic, etc.)

---

## Step 1 — Clone + Install (3 min)

```bash
git clone https://github.com/mbakholdin-byte/HarnessSolomon.git
cd HarnessSolomon
python -m venv .venv
source .venv/bin/activate    # or .venv\Scripts\activate on Windows
pip install -e .
```

✅ **Pass criteria:**
- [ ] `harness --version` returns version string (e.g., `1.35.0`)
- [ ] No Python import errors

---

## Step 2 — Configure (5 min)

```bash
# Create .env file
cp .env.example .env
# Edit .env — add at least one LLM provider API key
nano .env
```

✅ **Pass criteria:**
- [ ] `.env` has at least one `*_API_KEY` set
- [ ] `harness config validate` exits with code 0
- [ ] No "missing required env var" errors

---

## Step 3 — Start Server (2 min)

```bash
harness server start
# → INFO: Uvicorn running on http://127.0.0.1:8765
```

✅ **Pass criteria:**
- [ ] Server reachable at `http://127.0.0.1:8765/health` (HTTP 200)
- [ ] `/api/v1/capabilities` returns JSON with at least 8 scopes

---

## Step 4 — Create First Agent (10 min)

```bash
harness agents create my-first-agent \
  --model MiniMax-M2.7 \
  --system-prompt "You are a helpful assistant"
```

✅ **Pass criteria:**
- [ ] Agent created (returns agent ID)
- [ ] File `~/.harness/agents/my-first-agent.md` exists
- [ ] Markdown content valid

---

## Step 5 — Run Agent (5 min)

```bash
harness agents run my-first-agent \
  --message "Hello, world! What can you do?"
```

✅ **Pass criteria:**
- [ ] Agent returns response in < 30 seconds
- [ ] Response is coherent (not gibberish)
- [ ] No error in logs

---

## Step 6 — Web UI (5 min)

```bash
cd web
npm install
npm run dev
# → Vite on http://localhost:5173
```

✅ **Pass criteria:**
- [ ] Sidebar shows my-first-agent
- [ ] Click "Chat" → input box works
- [ ] Send message → response streams correctly

---

## Final Verification

```bash
harness tests e2e
# → All 13 Playwright tests should pass
```

✅ **Pass criteria:**
- [ ] All 13 tests pass (Phase 7.2 v1.36.0)
- [ ] No flaky tests
- [ ] No console errors in browser

---

## Summary

If all 6 steps pass, Harness is **production-ready** for the new user.

**Total time:** ~30 minutes (matches DoD)

---

## Common Failure Points (Troubleshooting)

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `harness: command not found` | venv not activated | `source .venv/bin/activate` |
| `ModuleNotFoundError: harness` | `pip install -e .` failed | Re-run pip install |
| `/health` returns 500 | Missing API key | Add `*_API_KEY` to `.env` |
| Agent response timeout | Network issue | Check `curl https://api.minimax.io` |
| Web UI shows 404 | Wrong port | Use `:5173` for dev, `:8765/ui` for prod |
| E2E tests fail | Backend not running | Start `harness server` first |

---

## Automated Version (CI smoke test)

```bash
# In CI (after server is up):
cd web
npm run test:e2e

# Expected: 13 passed, 0 failed
```
