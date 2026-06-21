---
sidebar_position: 1
title: Use Cases
description: Real-world scenarios where Harness shines
---

# Use Cases

Harness is used in production by teams for a variety of scenarios. Here are the
top use cases with concrete ROI numbers.

## 1. Code Generation & Review

**Who:** Engineering teams, especially fintech, devtool, B2B SaaS
**Problem:** Claude Code costs $15/user/month at scale; quality is inconsistent
**With Harness:**

- 80% of routine tasks (autocomplete, refactor, docstring) → **T1 local model (free)**
- 20% complex tasks (architecture, security review) → **T3 cloud (Claude)**
- **Result: 60-80% cost reduction** while keeping quality for hard tasks

## 2. Customer Support Automation

**Who:** B2C companies, e-commerce, SaaS
**Problem:** Support reps spend 40% of time on Tier 1 questions
**With Harness:**

- Plugin "Knowledge Base Sync" loads company docs
- Tier Router routes simple "where is my order?" → T1
- Complex "why was my payment declined?" → T3
- **Result: 3x support throughput** with same team

## 3. Data Pipeline Engineering

**Who:** Data teams, analytics, BI
**Problem:** Writing SQL/Snowflake/BigQuery queries is slow
**With Harness:**

- Plugin "Schema Reader" introspects warehouse schemas
- Agent generates SQL, validates, runs in sandbox
- Hook pre-commit runs `EXPLAIN` to catch expensive queries
- **Result: 5x faster query authoring** with cost guardrails

## 4. Internal Knowledge Search

**Who:** Mid-large companies
**Problem:** "Where is the policy for X?" wastes 30 min per question
**With Harness:**

- 4-layer memory: ingests Confluence, Notion, Slack, GDrive
- Tier Router: simple lookup → T1, complex question → T3
- Privacy zones: redact PII automatically
- **Result: 30 min → 30 sec** (60x faster)

## 5. CI/CD Code Review

**Who:** Any team with GitHub
**Problem:** Manual PR review is bottleneck
**With Harness:**

- GitHub webhook triggers agent on PR open
- Agent reviews diff, comments, suggests fixes
- Tier Router: small PR → T1, large PR → T3 with deep analysis
- **Result: review turnaround 24h → 30 min**

## Get Started

Pick the use case that matches your situation:
- [Code assistant](./code-assistant) — engineering teams
- [Data pipeline](./data-pipeline) — data teams
- [Customer support](./customer-support) — support orgs
- [Knowledge search](#) — internal tools teams
- [CI/CD review](#) — platform teams
