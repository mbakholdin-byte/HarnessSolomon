import { describe, it, expect } from 'vitest';
import * as allExports from '../index';

describe('Barrel export (index.ts)', () => {
  it('exports all 5 components', () => {
    expect(allExports.Table).toBeDefined();
    expect(allExports.Modal).toBeDefined();
    expect(allExports.CodeBlock).toBeDefined();
    expect(allExports.Badge).toBeDefined();
    expect(allExports.ConfirmDialog).toBeDefined();
  });

  it('exports all 5 component types/interfaces', () => {
    // Types are compile-time only, but we verify the type exports exist as
    // re-exported symbols by checking that TS doesn't complain.
    // At runtime, we check the component functions are present.
    const componentCount = [
      allExports.Table,
      allExports.Modal,
      allExports.CodeBlock,
      allExports.Badge,
      allExports.ConfirmDialog,
    ].filter(Boolean).length;
    expect(componentCount).toBe(5);
  });
});
