import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { MainLayout } from '../MainLayout';

describe('MainLayout', () => {
  it('renders sidebar and outlet area', () => {
    render(
      <MemoryRouter initialEntries={['/privacy-zones']}>
        <MainLayout />
      </MemoryRouter>,
    );

    // Sidebar is present (logo text confirms it)
    expect(screen.getByText('Harness Admin')).toBeInTheDocument();

    // Nav items are present
    expect(screen.getByText('Privacy Zones')).toBeInTheDocument();
  });
});
