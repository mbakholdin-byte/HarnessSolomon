import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CodeBlock } from '../index';

describe('CodeBlock', () => {
  it('renders code content', () => {
    render(<CodeBlock code="console.log('hello');" language="ts" />);
    expect(screen.getByText("console.log('hello');")).toBeInTheDocument();
  });

  it('renders language label when provided', () => {
    render(<CodeBlock code="{}" language="json" />);
    expect(screen.getByText('json')).toBeInTheDocument();
  });

  it('renders copy button by default', () => {
    render(<CodeBlock code="test" />);
    expect(screen.getByText('Copy')).toBeInTheDocument();
  });

  it('does not render copy button when copyButton=false', () => {
    render(<CodeBlock code="test" copyButton={false} />);
    expect(screen.queryByText('Copy')).not.toBeInTheDocument();
  });

  it('does not render language label when not provided', () => {
    render(<CodeBlock code="test" />);
    const wrapper = screen.getByTestId('codeblock');
    expect(wrapper.querySelector('[class*=lang]')).toBeNull();
  });
});
