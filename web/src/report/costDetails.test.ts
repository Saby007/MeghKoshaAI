import { describe, expect, it } from 'vitest';
import { comparisonDate, isCalendarMonth, monthCostWindow, previousCostWindow } from './costDetails';

describe('calendar-month cost windows', () => {
  it('recognises whole calendar months only', () => {
    expect(isCalendarMonth({ startDate: '2026-08-01', endDate: '2026-08-31' })).toBe(true);
    expect(isCalendarMonth({ startDate: '2026-02-01', endDate: '2026-02-28' })).toBe(true);
    expect(isCalendarMonth({ startDate: '2026-08-02', endDate: '2026-08-31' })).toBe(false);
    expect(isCalendarMonth({ startDate: '2026-08-01', endDate: '2026-08-30' })).toBe(false);
  });

  it('compares a month with the previous calendar month, across a year boundary', () => {
    expect(previousCostWindow({ startDate: '2026-09-01', endDate: '2026-09-30' })).toEqual({ startDate: '2026-08-01', endDate: '2026-08-31' });
    expect(previousCostWindow({ startDate: '2026-01-01', endDate: '2026-01-31' })).toEqual({ startDate: '2025-12-01', endDate: '2025-12-31' });
  });

  it('pairs days by day of month and leaves days without a counterpart unpaired', () => {
    const august = { startDate: '2026-08-01', endDate: '2026-08-31' };
    expect(comparisonDate(august, 1)).toBe('2026-07-02');
    const october = { startDate: '2026-10-01', endDate: '2026-10-31' };
    expect(comparisonDate(october, 29)).toBe('2026-09-30');
    expect(comparisonDate(october, 30)).toBe('');
  });

  it('keeps day-count shifting for windows that are not a calendar month', () => {
    const window = { startDate: '2026-08-02', endDate: '2026-08-31' };
    expect(previousCostWindow(window)).toEqual({ startDate: '2026-07-03', endDate: '2026-08-01' });
    expect(comparisonDate(window, 0)).toBe('2026-07-03');
  });

  it('builds the assessed month window from report metadata, rejecting invalid input', () => {
    expect(monthCostWindow('2026-08-01', '2026-08-31')).toEqual({ startDate: '2026-08-01', endDate: '2026-08-31' });
    expect(monthCostWindow('', '2026-08-31')).toBeNull();
    expect(monthCostWindow('2026-08-31', '2026-08-01')).toBeNull();
  });
});
