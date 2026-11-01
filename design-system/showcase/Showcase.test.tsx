import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Showcase } from './Showcase';

const FLAGS = { br: 'br.svg', us: 'us.svg', es: 'es.svg', eg: 'eg.svg' };

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe('Showcase theme switch', () => {
  it('switches theme even when the saved choice cannot be overwritten', async () => {
    localStorage.setItem('editonny-ds-theme', 'dark');
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError'); });
    render(<Showcase image="city.jpg" photo="photo.jpg" flags={FLAGS} guide={[]} />);
    const [light] = screen.getAllByRole('radio', { name: 'Light' });
    await act(async () => { await userEvent.click(light!); });
    expect(document.documentElement.dataset.theme).toBe('light');
  });
});
