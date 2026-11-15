import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AssetArchiveStatus } from '@/features/canvas/ui/AssetArchiveStatus';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }),
}));

describe('asset archive state', () => {
  it.each(['pending', 'saved', 'failed'] as const)('renders accessible %s state', (status) => {
    render(<AssetArchiveStatus status={status} />);
    expect(screen.getByRole('status')).toHaveAttribute('data-archive-status', status);
  });
  it('keeps other deployments without archive state unchanged', () => {
    const { container } = render(<AssetArchiveStatus />);
    expect(container).toBeEmptyDOMElement();
  });
});
