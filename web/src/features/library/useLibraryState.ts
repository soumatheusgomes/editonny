'use client';
import { useSearchParams } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';
import { usePathname, useRouter } from '@/i18n/navigation';

const SEARCH_DELAY = 250;

export type LibraryState = {
  query: string;
  category: string;
  openId: string | null;
  setQuery(value: string): void;
  setCategory(value: string): void;
  open(id: string): void;
  close(): void;
};

/**
 * Search text, category, and the open item live in the URL (q, category, scene|example) so views can be shared.
 * Values that do not match a known category or id are ignored. Opening pushes a history entry, so Back closes it.
 * The search box answers every keystroke; the URL follows after a short pause, and URL changes that did not come from
 * this box (a header link, Back) replace its text.
 */
export function useLibraryState(itemParam: 'scene' | 'example', categories: string[], ids: string[]): LibraryState {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const urlQuery = params.get('q') ?? '';
  const [query, setQueryState] = useState(urlQuery);
  const [seenUrl, setSeenUrl] = useState(urlQuery);
  const [written, setWritten] = useState(urlQuery);
  const [externalChanges, setExternalChanges] = useState(0);
  if (urlQuery !== seenUrl) {
    setSeenUrl(urlQuery);
    if (urlQuery !== written) {
      setQueryState(urlQuery);
      setWritten(urlQuery);
      setExternalChanges(count => count + 1);
    }
  }
  const pendingSearch = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // A navigation that replaced the search (header link, Back) cancels a search the visitor was still typing.
  useEffect(() => () => clearTimeout(pendingSearch.current), [externalChanges]);
  // A link reaches the URL only after the server answers, so on a slow response the pending search would fire first
  // and replace the destination. Any link click therefore cancels it; opening an item carries the search itself.
  useEffect(() => {
    const cancel = (event: MouseEvent) => {
      if (event.target instanceof Element && event.target.closest('a[href]')) clearTimeout(pendingSearch.current);
    };
    document.addEventListener('click', cancel, true);
    return () => document.removeEventListener('click', cancel, true);
  }, []);
  const openedHere = useRef(false);
  const rawCategory = params.get('category') ?? '';
  const rawId = params.get(itemParam);

  const write = (patch: Record<string, string | null>, mode: 'push' | 'replace' = 'replace') => {
    const next = new URLSearchParams(window.location.search);
    for (const [key, value] of Object.entries(patch)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    router[mode]({ pathname, query: Object.fromEntries(next) }, { scroll: false });
  };
  // A navigation in flight has not reached window.location yet, so a debounced search firing after another write would
  // rebuild the URL without it. Other writes therefore carry the typed search themselves and cancel the pending one.
  // shortcut: a search typed while a slower category navigation is still in flight can still drop it; queue writes if seen.
  const withSearch = (patch: Record<string, string | null>) => {
    clearTimeout(pendingSearch.current);
    const next = query.trim() ? query : '';
    setWritten(next);
    return { ...patch, q: next || null };
  };

  return {
    query,
    category: categories.includes(rawCategory) ? rawCategory : '',
    openId: rawId && ids.includes(rawId) ? rawId : null,
    setQuery(value) {
      setQueryState(value);
      clearTimeout(pendingSearch.current);
      pendingSearch.current = setTimeout(() => {
        const next = value.trim() ? value : '';
        setWritten(next);
        write({ q: next || null });
      }, SEARCH_DELAY);
    },
    setCategory(value) { write(withSearch({ category: value || null })); },
    open(id) { openedHere.current = true; write(withSearch({ [itemParam]: id }), 'push'); },
    close() {
      if (openedHere.current) { openedHere.current = false; router.back(); }
      else write({ [itemParam]: null });
    },
  };
}
