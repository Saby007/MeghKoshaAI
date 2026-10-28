import { readFileSync } from 'node:fs';
import { expect, it } from 'vitest';

const tokens = readFileSync(new URL('../design-tokens.css', import.meta.url), 'utf8');
const workspace = readFileSync(new URL('../report-workspace.css', import.meta.url), 'utf8');
const polish = readFileSync(new URL('../polish.css', import.meta.url), 'utf8');

const [darkRules, lightRules] = tokens.split(":root[data-theme='light']");
const themeColors = (rules: string) => new Map([...rules.matchAll(/--([\w-]+):\s*(#[\da-f]{6})\s*;/gi)]
  .map(([, name, color]) => [name, color]));
const rgb = (hex: string) => [1, 3, 5].map(index => parseInt(hex.slice(index, index + 2), 16) / 255);
const luminance = (channels: number[]) => channels.map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
  .reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
const contrast = (foreground: number[], background: number[]) => {
  const a = luminance(foreground);
  const b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
};

for (const [theme, rules] of [['dark', darkRules], ['light', lightRules]]) {
  const colors = themeColors(rules);
  const color = (name: string) => {
    const value = colors.get(name);
    if (!value) throw new Error(`Missing ${theme} theme colour: ${name}`);
    return rgb(value);
  };
  it(`keeps ${theme} body, muted and semantic text above 4.5:1 on shared surfaces`, () => {
    for (const foreground of ['color-bone', 'color-graphite-mid', 'color-metric-green', 'color-error', 'color-warning']) {
      for (const background of ['color-obsidian-canvas', 'color-surface', 'color-surface-hover']) {
        expect(contrast(color(foreground), color(background)), `${foreground} on ${background}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
  it(`keeps ${theme} control boundaries above 3:1, including hover surfaces`, () => {
    for (const background of ['color-obsidian-canvas', 'color-surface', 'color-surface-hover']) {
      expect(contrast(color('color-control-stroke'), color(background))).toBeGreaterThanOrEqual(3);
    }
  });
  it(`keeps ${theme} action ink legible on the normal gradient and solid hover fill`, () => {
    const fill = color('color-signal-orange');
    const gradientTop = fill.map(value => value * 0.92 + 0.08);
    for (const background of [fill, gradientTop]) {
      expect(contrast(color('color-on-action'), background)).toBeGreaterThanOrEqual(4.5);
    }
  });
}

it('keeps placeholder and cost-change text at full opacity and removes brightening on action hover', () => {
  expect(polish).toMatch(/:is\(input, textarea\)::placeholder\s*\{[^}]*color: var\(--color-graphite-mid\);[^}]*opacity: 1;/);
  expect(polish).toMatch(/\.cost-delta i\s*\{[^}]*opacity: 1;/);
  expect(polish).toMatch(/:is\(\.primary-command, \.scope-run-button\):hover:not\(:disabled\)\s*\{[^}]*filter: none;/);
});

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
