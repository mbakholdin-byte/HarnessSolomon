import { useState } from 'react';
import styles from './CodeBlock.module.css';

export interface CodeBlockProps {
  /** Code string to display. */
  code: string;
  /** Language for syntax hint (used as CSS class), optional. */
  language?: string;
  /** Show copy-to-clipboard button (default true). */
  copyButton?: boolean;
}

export function CodeBlock({ code, language, copyButton = true }: CodeBlockProps): JSX.Element {
  const [copied, setCopied] = useState(false);

  const handleCopy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for older browsers — ignore silently
    }
  };

  return (
    <div className={styles.wrapper} data-testid="codeblock">
      <div className={styles.header}>
        {language && <span className={styles.lang}>{language}</span>}
        {copyButton && (
          <button
            className={styles.copyBtn}
            onClick={handleCopy}
            data-testid="codeblock-copy-btn"
          >
            {copied ? 'Copied!' : 'Copy'}
          </button>
        )}
      </div>
      <pre className={styles.pre}>
        <code
          className={`${styles.code} ${language ? `language-${language}` : ''}`}
          data-testid="codeblock-code"
        >
          {code}
        </code>
      </pre>
    </div>
  );
}

export default CodeBlock;
