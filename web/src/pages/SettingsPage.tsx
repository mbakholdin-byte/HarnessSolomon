/**
 * WI-04: Settings page — static informational layout.
 *
 * No backend endpoints yet. Displays placeholder sections for
 * General settings, API Keys, and About/version information.
 */

import styles from "./SettingsPage.module.css";

/* ── Constants ───────────────────────────────────────────────────── */

const APP_VERSION = "1.0.0";
const BUILD_DATE = "2026-06-20";

/* ── Component ───────────────────────────────────────────────────── */

export function SettingsPage(): JSX.Element {
  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Settings</h1>

      {/* General */}
      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>General</h2>
        <p className={styles.sectionBody}>
          Configure general application settings such as admin username,
          default session timeout, and log level.
        </p>
        <div className={styles.placeholder}>
          Settings management will be available in a future update.
        </div>
      </section>

      {/* API Keys */}
      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>API Keys</h2>
        <p className={styles.sectionBody}>
          Manage API tokens and access keys for the Harness backend.
          Tokens are CLI-generated and stored client-side.
        </p>
        <div className={styles.placeholder}>
          Token management UI will be available in a future update.
        </div>
      </section>

      {/* About */}
      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>About</h2>
        <p className={styles.sectionBody}>
          Harness Admin Dashboard — a local-first agent shell with
          composable architecture, 4-layer memory, and multi-provider
          support. Open-source (MIT).
        </p>
        <table className={styles.versionTable}>
          <tbody>
            <tr>
              <td className={styles.versionLabel}>Version</td>
              <td className={styles.versionValue}>{APP_VERSION}</td>
            </tr>
            <tr>
              <td className={styles.versionLabel}>Build</td>
              <td className={styles.versionValue}>{BUILD_DATE}</td>
            </tr>
            <tr>
              <td className={styles.versionLabel}>Stack</td>
              <td className={styles.versionValue}>
                React 18 + Vite + TypeScript
              </td>
            </tr>
            <tr>
              <td className={styles.versionLabel}>License</td>
              <td className={styles.versionValue}>MIT</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>
  );
}

export default SettingsPage;
