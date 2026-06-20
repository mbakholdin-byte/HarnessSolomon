import type { ReactNode } from 'react';
import styles from './Badge.module.css';

export type BadgeVariant = 'success' | 'warning' | 'error' | 'info';

export interface BadgeProps {
  /** Visual variant determining the color scheme. */
  variant: BadgeVariant;
  /** Badge content. */
  children: ReactNode;
}

const variantClass: Record<BadgeVariant, string> = {
  success: styles.success,
  warning: styles.warning,
  error: styles.error,
  info: styles.info,
};

export function Badge({ variant, children }: BadgeProps): JSX.Element {
  return (
    <span className={`${styles.badge} ${variantClass[variant]}`} data-testid="badge">
      {children}
    </span>
  );
}

export default Badge;
