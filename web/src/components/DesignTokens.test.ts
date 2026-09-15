import { readFileSync } from 'node:fs';
import { expect, it } from 'vitest';

const tokens = readFileSync(new URL('../design-tokens.css', import.meta.url), 'utf8');
const workspace = readFileSync(new URL('../report-workspace.css', import.meta.url), 'utf8');

it('uses Segoe UI for all application font roles and contrasting section banners', () => {
  expect(tokens).toContain("--font-geist: 'Segoe UI'");
  expect(tokens).toContain("--font-geist-mono: 'Segoe UI'");
  expect(workspace).toContain('background: var(--color-bone);');
  expect(workspace).toContain('color: var(--color-obsidian-canvas);');
});

it('defines five font sizes, three radii and three shadows in the shared token file', () => {
  expect(tokens.match(/--font-size-[\w-]+:/g)).toHaveLength(5);
  expect(tokens.match(/--radius-(?:sm|md|lg):/g)).toHaveLength(3);
  expect(tokens.match(/--shadow-(?:sm|md|lg):/g)).toHaveLength(3);
});

it('uses a scalable four-pixel spacing basis and no literal workspace design values', () => {
  const spaces = [...tokens.matchAll(/--space-(\d+): calc\((\d+)px \* var\(--ui-scale\)\)/g)];
  expect(spaces.length).toBeGreaterThan(0);
  spaces.forEach(([, step, pixels]) => expect(Number(pixels)).toBe(Number(step) * 4));
  expect(workspace).not.toMatch(/#[\da-f]{3,8}\b|rgba?\(/i);
  expect(workspace).not.toMatch(/(?:font-size|border-radius|box-shadow|padding[\w-]*|margin[\w-]*|gap):[^;{}]*\d+(?:px|rem)\b/);
  expect(workspace).toContain('@media (prefers-reduced-motion: reduce)');
});
