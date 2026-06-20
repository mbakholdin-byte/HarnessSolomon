import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Badge } from '../index';

describe('Badge', () => {
  it('renders children content', () => {
    render(<Badge variant="success">OK</Badge>);
    expect(screen.getByText('OK')).toBeInTheDocument();
  });

  it('applies success class', () => {
    render(<Badge variant="success">OK</Badge>);
    const badge = screen.getByTestId('badge');
    expect(badge.className).toContain('success');
  });

  it('applies warning class', () => {
    render(<Badge variant="warning">Warn</Badge>);
    const badge = screen.getByTestId('badge');
    expect(badge.className).toContain('warning');
  });

  it('applies error class', () => {
    render(<Badge variant="error">Fail</Badge>);
    const badge = screen.getByTestId('badge');
    expect(badge.className).toContain('error');
  });

  it('applies info class', () => {
    render(<Badge variant="info">Info</Badge>);
    const badge = screen.getByTestId('badge');
    expect(badge.className).toContain('info');
  });
});
