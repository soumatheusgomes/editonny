import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Chip, CreatorChip, Highlight, LANGUAGES, LanguageSelect, SiteHeader } from '../index';

describe('LanguageSelect', () => {
  it('filters, navigates with the keyboard, and picks a language', async () => {
    const onChange = vi.fn();
    render(<LanguageSelect value="en-US" onChange={onChange} label="Language" searchPlaceholder="Search" />);
    const trigger = screen.getByRole('button', { name: /^Language: English/ });
    await userEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    const search = screen.getByRole('combobox', { name: 'Search' });
    await act(async () => { await userEvent.type(search, 'esp'); });
    expect(screen.getAllByRole('option')).toHaveLength(1);
    await userEvent.keyboard('{Enter}');
    expect(onChange).toHaveBeenCalledWith('es');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('closes on Escape and on an outside pointerdown', async () => {
    render(<><LanguageSelect value="pt-BR" label="Idioma" /><p>outside</p></>);
    const trigger = screen.getByRole('button', { name: /^Idioma: Português/ });
    await userEvent.click(trigger);
    await userEvent.keyboard('{Escape}');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    await userEvent.click(trigger);
    fireEvent.pointerDown(screen.getByText('outside'));
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('marks each language name with its language and closes when focus leaves', async () => {
    render(<><LanguageSelect value="en-US" label="Language" /><button type="button">next control</button></>);
    await userEvent.click(screen.getByRole('button', { name: /^Language: English/ }));
    expect(screen.getByText('Português').closest('[lang]')).toHaveAttribute('lang', 'pt-BR');
    await userEvent.tab();
    await userEvent.tab();
    expect(screen.getByRole('button', { name: /^Language: English/ })).toHaveAttribute('aria-expanded', 'false');
  });

  it('ships the three supported languages', () => {
    expect(LANGUAGES.map(language => language.code)).toEqual(['en-US', 'pt-BR', 'es', 'ar']);
  });
});

describe('site pieces', () => {
  it('SiteHeader marks the active link and renders the right slot', () => {
    render(<SiteHeader links={[{ label: 'Catalog', href: '/catalog' }]} active="/catalog" right={<span>slot</span>} navLabel="Main" />);
    expect(screen.getByRole('link', { name: 'Catalog' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByText('slot')).toBeInTheDocument();
  });

  it('Chip toggles pressed state and shows a count', () => {
    render(<Chip selected count={3}>Edit</Chip>);
    expect(screen.getByRole('button', { name: 'Edit 3' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('CreatorChip links out with a new-tab hint', () => {
    render(<CreatorChip name="Matheus Gomes" role="Creator" href="https://instagram.com/matheusgomes" newTabLabel="(new tab)" />);
    expect(screen.getByRole('link')).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByText('(new tab)')).toBeInTheDocument();
  });

  it('Highlight wraps its content', () => {
    render(<Highlight tone="volt">first cut.</Highlight>);
    expect(screen.getByText('first cut.')).toHaveAttribute('data-tone', 'volt');
  });
});
