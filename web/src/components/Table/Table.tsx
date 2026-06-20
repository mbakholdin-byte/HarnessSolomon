import { useState, useMemo, type ReactNode } from 'react';
import styles from './Table.module.css';

export interface TableColumn<T> {
  /** Unique key for the column (used as id for sorting). */
  key: string;
  /** Column header text. */
  header: string;
  /** Custom render function. Receives the full row object. */
  render?: (row: T) => ReactNode;
  /** Whether this column is sortable (default true). */
  sortable?: boolean;
}

export interface TableProps<T> {
  /** Array of column definitions. */
  columns: TableColumn<T>[];
  /** Array of data rows. */
  data: T[];
  /** Default sort column key. */
  defaultSortKey?: string;
  /** Default sort direction. */
  defaultSortDirection?: 'asc' | 'desc';
  /** Called when sort changes. */
  onSort?: (key: string, direction: 'asc' | 'desc') => void;
}

type SortState = {
  key: string;
  direction: 'asc' | 'desc';
};

function getCellValue<T>(row: T, key: string): string {
  const val = (row as Record<string, unknown>)[key];
  if (val === null || val === undefined) return '';
  return String(val);
}

export function Table<T>({
  columns,
  data,
  defaultSortKey,
  defaultSortDirection = 'asc',
  onSort,
}: TableProps<T>): JSX.Element {
  const [sort, setSort] = useState<SortState>({
    key: defaultSortKey ?? columns[0]?.key ?? '',
    direction: defaultSortDirection,
  });

  const sortedData = useMemo(() => {
    if (!sort.key) return data;
    const col = columns.find((c) => c.key === sort.key);
    if (col?.sortable === false) return data;
    const dir = sort.direction === 'asc' ? 1 : -1;
    return [...data].sort((a, b) => {
      const aVal = getCellValue(a, sort.key);
      const bVal = getCellValue(b, sort.key);
      return aVal.localeCompare(bVal) * dir;
    });
  }, [data, sort, columns]);

  const handleSort = (key: string) => {
    const col = columns.find((c) => c.key === key);
    if (col?.sortable === false) return;
    const direction: 'asc' | 'desc' =
      sort.key === key && sort.direction === 'asc' ? 'desc' : 'asc';
    setSort({ key, direction });
    onSort?.(key, direction);
  };

  const sortIndicator = (key: string): string => {
    if (sort.key !== key) return '';
    return sort.direction === 'asc' ? ' ▲' : ' ▼';
  };

  return (
    <div className={styles.wrapper}>
      <table className={styles.table}>
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                className={`${styles.th} ${col.sortable !== false ? styles.sortable : ''}`}
                onClick={() => handleSort(col.key)}
                data-testid={`header-${col.key}`}
              >
                {col.header}
                <span className={styles.sortIndicator} data-testid={`sort-${col.key}`}>
                  {sortIndicator(col.key)}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sortedData.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className={styles.empty}>
                No data
              </td>
            </tr>
          ) : (
            sortedData.map((row, rowIdx) => (
              <tr key={rowIdx} className={styles.row}>
                {columns.map((col) => (
                  <td key={col.key} className={styles.td} data-testid={`cell-${rowIdx}-${col.key}`}>
                    {col.render ? col.render(row) : getCellValue(row, col.key)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

export default Table;
