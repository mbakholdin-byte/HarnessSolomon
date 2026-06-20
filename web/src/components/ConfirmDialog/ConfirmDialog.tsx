import { Modal } from '../Modal/Modal';
import styles from './ConfirmDialog.module.css';

export interface ConfirmDialogProps {
  /** Whether the dialog is visible. */
  open: boolean;
  /** Dialog title. */
  title: string;
  /** Explanatory message. */
  message: string;
  /** Called when the user confirms the action. */
  onConfirm: () => void;
  /** Called when the user cancels. */
  onCancel: () => void;
  /** Confirm button label (default: "Confirm"). */
  confirmLabel?: string;
  /** Cancel button label (default: "Cancel"). */
  cancelLabel?: string;
}

export function ConfirmDialog({
  open,
  title,
  message,
  onConfirm,
  onCancel,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
}: ConfirmDialogProps): JSX.Element {
  return (
    <Modal open={open} onClose={onCancel} title={title}>
      <p className={styles.message}>{message}</p>
      <div className={styles.actions}>
        <button className={styles.cancelBtn} onClick={onCancel} data-testid="confirm-cancel">
          {cancelLabel}
        </button>
        <button className={styles.confirmBtn} onClick={onConfirm} data-testid="confirm-ok">
          {confirmLabel}
        </button>
      </div>
    </Modal>
  );
}

export default ConfirmDialog;
