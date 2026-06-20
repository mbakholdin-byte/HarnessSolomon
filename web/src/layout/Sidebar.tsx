import { useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import styles from './Sidebar.module.css';

interface NavItem {
  to: string;
  label: string;
  icon: string;
}

const NAV_ITEMS: NavItem[] = [
  { to: '/privacy-zones', label: 'Privacy Zones', icon: '🔒' },
  { to: '/hooks', label: 'Hooks', icon: '🪝' },
  { to: '/observability', label: 'Observability', icon: '📊' },
  { to: '/plugins', label: 'Plugins', icon: '🧩' },
  { to: '/settings', label: 'Settings', icon: '⚙️' },
];

export function Sidebar(): JSX.Element {
  const [collapsed, setCollapsed] = useState(false);
  const location = useLocation();

  function isActive(path: string): boolean {
    return location.pathname.startsWith(path);
  }

  return (
    <aside className={`${styles.sidebar} ${collapsed ? styles.collapsed : ''}`}>
      <div className={styles.logo}>
        <span className={styles.logoIcon}>⚙️</span>
        {!collapsed && <span className={styles.logoText}>Harness Admin</span>}
        <button
          className={styles.toggle}
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? '▶' : '◀'}
        </button>
      </div>

      <ul className={styles.nav}>
        {NAV_ITEMS.map((item) => {
          const active = isActive(item.to);
          const linkClass = `${styles.navLink} ${active ? styles.active : ''}`;
          return (
            <li key={item.to} className={styles.navItem}>
              <NavLink to={item.to} className={linkClass}>
                <span className={styles.navIcon}>{item.icon}</span>
                {!collapsed && <span className={styles.navLabel}>{item.label}</span>}
              </NavLink>
            </li>
          );
        })}
      </ul>
    </aside>
  );
}

export default Sidebar;
