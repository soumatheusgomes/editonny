import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useLibraryState } from './useLibraryState';

// The router updates window.location only when a navigation commits; these mocks never commit, like a slow server.
const replace = vi.fn();
const push = vi.fn();
vi.mock('next/navigation', () => ({ useSearchParams: () => new URLSearchParams(window.location.search) }));
vi.mock('@/i18n/navigation', () => ({ usePathname: () => '/catalog', useRouter: () => ({ replace, push, back: vi.fn() }) }));

describe('useLibraryState', () => {
  beforeEach(() => { vi.useFakeTimers(); replace.mockClear(); push.mockClear(); window.history.replaceState(null, '', '/en/catalog?q=comparison'); });
  afterEach(() => vi.useRealTimers());

  it('keeps a category chosen while a search write is pending and the previous navigation has not committed', () => {
    const { result } = renderHook(() => useLibraryState('scene', ['transitions'], []));
    act(() => result.current.setQuery(''));
    act(() => result.current.setCategory('transitions'));
    act(() => { vi.advanceTimersByTime(300); });
    expect(replace).toHaveBeenCalledTimes(1);
    expect(replace).toHaveBeenLastCalledWith({ pathname: '/catalog', query: { category: 'transitions' } }, { scroll: false });
  });

  it('opens an item in one history entry that carries a search still being typed', () => {
    const { result } = renderHook(() => useLibraryState('scene', [], ['fade']));
    act(() => result.current.setQuery('fade'));
    act(() => result.current.open('fade'));
    act(() => { vi.advanceTimersByTime(300); });
    expect(push).toHaveBeenCalledWith({ pathname: '/catalog', query: { q: 'fade', scene: 'fade' } }, { scroll: false });
    expect(replace).not.toHaveBeenCalled();
  });

  it('drops a search still being typed when a link starts a navigation that has not committed', () => {
    const { result } = renderHook(() => useLibraryState('scene', [], []));
    const link = document.body.appendChild(document.createElement('a'));
    link.href = '/en/catalog';
    link.addEventListener('click', event => event.preventDefault());
    act(() => result.current.setQuery('new search'));
    act(() => link.click());
    act(() => { vi.advanceTimersByTime(300); });
    expect(replace).not.toHaveBeenCalled();
    link.remove();
  });
});
