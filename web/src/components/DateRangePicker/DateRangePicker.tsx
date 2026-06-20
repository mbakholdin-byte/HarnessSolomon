/**
 * WI-05: DateRangePicker — dual date input for filtering by range.
 *
 * Controlled component: receives ``from``/``to`` as ISO date strings
 * (YYYY-MM-DD) and calls ``onChange`` when either input changes.
 */

import styles from './DateRangePicker.module.css';

export interface DateRangePickerProps {
  /** Start date (YYYY-MM-DD). */
  from: string;
  /** End date (YYYY-MM-DD). */
  to: string;
  /** Called with the new (from, to) tuple when either input changes. */
  onChange: (from: string, to: string) => void;
}

export function DateRangePicker({
  from,
  to,
  onChange,
}: DateRangePickerProps): JSX.Element {
  return (
    <div className={styles.wrapper} data-testid="date-range-picker">
      <label className={styles.label}>
        From:
        <input
          type="date"
          className={styles.input}
          value={from}
          onChange={(e) => onChange(e.target.value, to)}
          data-testid="date-from"
        />
      </label>
      <label className={styles.label}>
        To:
        <input
          type="date"
          className={styles.input}
          value={to}
          onChange={(e) => onChange(from, e.target.value)}
          data-testid="date-to"
        />
      </label>
    </div>
  );
}

export default DateRangePicker;
