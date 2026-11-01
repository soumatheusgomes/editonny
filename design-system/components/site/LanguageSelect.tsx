'use client';
import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Icon } from '../core/Icon';
import { cx, type BaseProps } from '../lib';

export type LanguageOption = {
  /** BCP 47 code, e.g. "en-US". */
  code: string;
  /** Two-letter label shown in the trigger. */
  short: string;
  /** Name in its own language. */
  native: string;
  /** Region line. */
  sub?: string;
  /** File stem in `flagBase` (br | us | es | eg). */
  flag?: string;
  /** Full image URL instead of `flag`. */
  flagUrl?: string;
};

/**
 * Round-flag pill that opens a searchable, keyboard-navigable language list (↑ ↓ Enter Esc); the current language is tagged orange.
 * Persisting the choice and setting `<html lang>` belong to the page.
 */
export type LanguageSelectProps = BaseProps & {
  value?: string;
  onChange?: (code: string) => void;
  options?: LanguageOption[];
  flagBase?: string;
  showCode?: boolean;
  label?: string;
  searchPlaceholder?: string;
  emptyText?: string;
  defaultOpen?: boolean;
};

export const LANGUAGES: LanguageOption[] = [
  { code: 'en-US', short: 'EN', native: 'English', sub: 'United States', flag: 'us' },
  { code: 'pt-BR', short: 'PT', native: 'Português', sub: 'Brasil', flag: 'br' },
  { code: 'es', short: 'ES', native: 'Español', sub: 'España · Latinoamérica', flag: 'es' },
  { code: 'ar', short: 'AR', native: 'العربية', sub: 'مصر · الوطن العربي', flag: 'eg' },
];

const fold = (value: string) => value.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

export function LanguageSelect({ value = 'en-US', onChange, options = LANGUAGES, flagBase = 'assets/flags/', showCode = true, label = 'Language', searchPlaceholder = 'Search languages…', emptyText = 'No language found', defaultOpen = false, className, style }: LanguageSelectProps) {
  const id = useId();
  const current = options.find(option => option.code === value) ?? options[0]!;
  const [open, setOpen] = useState(defaultOpen);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const visible = options.filter(option => !query || fold(`${option.native} ${option.sub ?? ''} ${option.code}`).includes(fold(query.trim())));
  const activeIndex = Math.min(active, Math.max(0, visible.length - 1));

  // Keep the open list inside the viewport: on narrow screens the trigger sits too far left for a right-aligned panel.
  useLayoutEffect(() => {
    const node = panel.current;
    if (!open || !node) return;
    // Measure layout boxes, not the panel's rect: its entrance animation scales it while this runs.
    const anchor = root.current?.getBoundingClientRect();
    if (!anchor) return;
    const margin = 12;
    const right = anchor.right;
    const left = right - node.offsetWidth;
    const shift = left < margin ? margin - left : right > window.innerWidth - margin ? window.innerWidth - margin - right : 0;
    node.style.setProperty('--lang-shift', `${Math.round(shift)}px`);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);

  const toggle = (next: boolean) => {
    setOpen(next);
    if (next) { setQuery(''); setActive(Math.max(0, options.indexOf(current))); }
  };
  const pick = (option: LanguageOption) => {
    setOpen(false);
    trigger.current?.focus();
    onChange?.(option.code);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive(Math.min(activeIndex + 1, visible.length - 1)); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setActive(Math.max(activeIndex - 1, 0)); }
    else if (event.key === 'Enter') { event.preventDefault(); const option = visible[activeIndex]; if (option) pick(option); }
    else if (event.key === 'Escape') { event.preventDefault(); setOpen(false); trigger.current?.focus(); }
  };
  // Set the image directly: url() inside a custom property resolves against the stylesheet, not the page.
  const flag = (option: LanguageOption, size: number) => (
    <span className="ds-lang__flag" aria-hidden="true" style={{ '--flag-size': `${size}px`, backgroundImage: `url("${option.flagUrl ?? `${flagBase}${option.flag}.svg`}")` }} />
  );

  return (
    <div ref={root} className={cx('ds-lang', className)} style={style} data-open={String(open)}
      onBlur={event => {
        // Close when keyboard focus moves to another control; clicks on options keep focus inside (relatedTarget is null).
        if (open && event.relatedTarget instanceof Node && !root.current?.contains(event.relatedTarget)) setOpen(false);
      }}>
      <button ref={trigger} type="button" className="ds-lang__trigger" aria-haspopup="listbox" aria-expanded={open} aria-controls={`${id}-panel`}
        onClick={() => toggle(!open)}>
        {flag(current, 22)}
        {/* The name keeps the visible code, so voice control can target it (WCAG 2.5.3). */}
        <span className="ds-visually-hidden">{`${label}: `}<span lang={current.code}>{current.native}</span>{' '}</span>
        <span>{showCode ? current.short : ''}</span>
        <Icon name="chevron-down" size={14} className="ds-lang__chevron" />
      </button>
      <div ref={panel} className="ds-lang__panel" id={`${id}-panel`} hidden={!open}>
        <label className="ds-lang__field">
          <Icon name="search" size={15} />
          <input ref={search} className="ds-lang__search" type="search" placeholder={searchPlaceholder} aria-label={searchPlaceholder} autoComplete="off" spellCheck={false}
            role="combobox" aria-controls={`${id}-list`} aria-expanded={open} aria-autocomplete="list"
            aria-activedescendant={visible.length ? `${id}-o${activeIndex}` : undefined}
            value={query} onChange={event => { setQuery(event.target.value); setActive(0); }} onKeyDown={onKeyDown} />
          <span className="ds-lang__esc" aria-hidden="true">esc</span>
        </label>
        <div className="ds-lang__list" role="listbox" id={`${id}-list`} aria-label={label}>
          {visible.map((option, i) => (
            <div key={option.code} className="ds-lang__option" role="option" id={`${id}-o${i}`} aria-selected={option.code === current.code}
              data-active={String(i === activeIndex)} onPointerEnter={() => setActive(i)} onClick={() => pick(option)}>
              {flag(option, 28)}
              <span className="ds-lang__names" lang={option.code}><strong>{option.native}</strong>{option.sub && <span>{option.sub}</span>}</span>
              <span className="ds-lang__code">{option.code}</span>
            </div>
          ))}
          {!visible.length && <p className="ds-lang__empty">{emptyText}</p>}
        </div>
      </div>
    </div>
  );
}
