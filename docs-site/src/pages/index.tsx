import React from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

import styles from './index.module.css';

function HomepageHeader() {
  const { siteConfig } = useDocusaurusContext();
  return (
    <header className={clsx('hero hero--primary', styles.heroBanner)} role="banner">
      <div className="container">
        <Heading as="h1" className="hero__title">
          {siteConfig.title}
        </Heading>
        <p className="hero__subtitle">{siteConfig.tagline}</p>
        <div className={styles.buttons} role="group" aria-label="Primary actions">
          <Link
            className="button button--secondary button--lg"
            to="/tutorials/quickstart"
            aria-label="Quickstart tutorial — 5 minutes to first agent">
            🚀 Quickstart — 5 minutes
          </Link>
          <Link
            className="button button--outline button--secondary button--lg"
            to="/configuration/overview"
            aria-label="Configuration reference">
            ⚙️ Configuration
          </Link>
          <a
            className="button button--outline button--lg"
            href="https://github.com/mbakholdin-byte/HarnessSolomon"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="View Harness on GitHub (opens in new tab)">
            <svg width="20" height="20" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" style={{ marginRight: '0.5rem', verticalAlign: 'middle' }}>
              <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"/>
            </svg>
            GitHub
          </a>
        </div>
      </div>
    </header>
  );
}

function FeatureList() {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className="row">
          <div className="col col--4">
            <h3>🧠 4-Layer Memory</h3>
            <p>
              Working / session / long-term / episodic+semantic. Dual-write
              guarantees consistency. Hot-reload via file watcher.
            </p>
          </div>
          <div className="col col--4">
            <h3>🔌 Plugin Marketplace</h3>
            <p>
              Extend Harness with Python/Node plugins. Manifest v2 with
              permissions, ed25519 signature verification, hot-reload.
            </p>
          </div>
          <div className="col col--4">
            <h3>📊 Production Observability</h3>
            <p>
              JSONL logs, Prometheus metrics, OpenTelemetry traces, per-task
              cost. 16 hook events for custom integration.
            </p>
          </div>
        </div>
        <div className="row">
          <div className="col col--4">
            <h3>🎯 Cost-Aware Routing</h3>
            <p>
              Heuristic tier selection (T1 cheap / T2 mid / T3 premium)
              calibrated on production data. Confidence cascade with
              auto-fallback.
            </p>
          </div>
          <div className="col col--4">
            <h3>🔐 Scope-gated API</h3>
            <p>
              10 RBAC scopes, Bearer token auth, capabilities discovery
              endpoint. SHA-256 token storage in SQLite.
            </p>
          </div>
          <div className="col col--4">
            <h3>🐳 Docker-Sandbox per Agent</h3>
            <p>
              seccomp profiles, resource limits, clean environment per agent
              type. Production-grade isolation.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}

function ComparisonTable() {
  return (
    <section className={styles.comparison}>
      <div className="container">
        <h2 className="text--center">Why Harness?</h2>
        <p className="text--center" style={{ marginBottom: '2rem', opacity: 0.75 }}>
          Compared to other agent shells and orchestrators
        </p>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Feature</th>
              <th>Harness</th>
              <th>Claude Code</th>
              <th>OpenCode</th>
              <th>Mavis</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Multi-model routing (T1/T2/T3)</td>
              <td>✅ Calibrated</td>
              <td>❌</td>
              <td>⚠️ Manual</td>
              <td>⚠️ Basic</td>
            </tr>
            <tr>
              <td>Plugin marketplace</td>
              <td>✅ v2 manifest + signature</td>
              <td>❌</td>
              <td>⚠️ Beta</td>
              <td>❌</td>
            </tr>
            <tr>
              <td>Hook framework</td>
              <td>✅ 16 events, 4 transports</td>
              <td>⚠️ Limited</td>
              <td>❌</td>
              <td>✅ 8 events</td>
            </tr>
            <tr>
              <td>Cost analytics per-task</td>
              <td>✅ Built-in</td>
              <td>❌</td>
              <td>❌</td>
              <td>⚠️ Session-level</td>
            </tr>
            <tr>
              <td>Privacy / Local-first</td>
              <td>✅ 100% local capable</td>
              <td>❌ Cloud-only</td>
              <td>⚠️ Partial</td>
              <td>✅</td>
            </tr>
            <tr>
              <td>RU-first UX + i18n</td>
              <td>✅</td>
              <td>❌</td>
              <td>❌</td>
              <td>✅</td>
            </tr>
            <tr>
              <td>Open-source (MIT)</td>
              <td>✅</td>
              <td>❌</td>
              <td>✅</td>
              <td>❌</td>
            </tr>
            <tr>
              <td>Documentation site</td>
              <td>✅ 144+ pages, i18n</td>
              <td>✅</td>
              <td>⚠️ Basic</td>
              <td>❌</td>
            </tr>
            <tr>
              <td>E2E tests</td>
              <td>✅ Playwright 13 tests</td>
              <td>❌</td>
              <td>⚠️</td>
              <td>❌</td>
            </tr>
          </tbody>
        </table>
        <p className="text--center" style={{ marginTop: '1.5rem', fontSize: '0.875rem', opacity: 0.6 }}>
          Last updated: v1.36.0 (Phase 7.2 Playwright E2E)
        </p>
      </div>
    </section>
  );
}

export default function Home(): JSX.Element {
  return (
    <Layout
      title="Home"
      description="Solomon Harness — open-source multi-model agent shell, stronger than Claude Code and OpenCode">
      <HomepageHeader />
      <main>
        <FeatureList />
        <ComparisonTable />
      </main>
    </Layout>
  );
}
