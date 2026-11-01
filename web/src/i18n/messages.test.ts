import { describe, expect, it } from 'vitest';
import ar from '../../messages/ar.json';
import en from '../../messages/en-US.json';
import es from '../../messages/es.json';
import pt from '../../messages/pt-BR.json';

type Tree = { [key: string]: string | Tree };

function leaves(tree: Tree, prefix = ''): Array<[string, string]> {
  return Object.entries(tree).flatMap(([key, value]) => typeof value === 'string' ? [[`${prefix}${key}`, value] as [string, string]] : leaves(value, `${prefix}${key}.`));
}
// ICU arguments such as {count} or {name}; plural/select branches are compared by their argument names only.
const args = (text: string) => [...text.matchAll(/\{\s*([a-zA-Z]+)\s*[,}]/g)].map(match => match[1]).sort();

describe('messages', () => {
  const reference = new Map(leaves(en as Tree));

  it.each([['pt-BR', pt], ['es', es], ['ar', ar]])('%s has exactly the en-US keys and no empty strings', (_, messages) => {
    const entries = new Map(leaves(messages as Tree));
    expect([...entries.keys()].sort()).toEqual([...reference.keys()].sort());
    for (const [key, value] of entries) expect(value.trim(), key).not.toBe('');
  });

  it.each([['pt-BR', pt], ['es', es], ['ar', ar]])('%s keeps the ICU arguments of every en-US message', (_, messages) => {
    for (const [key, value] of leaves(messages as Tree)) expect(args(value), key).toEqual(args(reference.get(key) ?? ''));
  });

  it('en-US defines every namespace the app reads', () => {
    expect(Object.keys(en)).toEqual(expect.arrayContaining(['Metadata', 'Nav', 'Header', 'Footer', 'Language', 'Theme', 'Common', 'NotFound']));
  });

  it('en-US has no empty strings', () => {
    for (const [key, value] of reference) expect(value.trim(), key).not.toBe('');
  });
});
