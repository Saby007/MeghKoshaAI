import { costWindowDates, presetCostWindow, type CostWindow } from '../report/costDetails';

/* The report-wide cost window picker.
   Kept in its own module rather than in CostExplorer because App renders it in
   the saved-report strip, and CostExplorer is a large module: importing the
   picker from there would pull the resource tables and export machinery into
   the initial bundle for the sake of four buttons and two date inputs. */
export function CostRangeControls({ dates, value, onChange, month, monthToDate }: { dates: string[]; value: CostWindow; onChange: (value: CostWindow) => void; month?: CostWindow | null; monthToDate?: CostWindow | null }) {
  const latest = dates.at(-1) ?? '';
  const count = costWindowDates(value).length;
  const same = (window?: CostWindow | null) => Boolean(window && value.startDate === window.startDate && value.endDate === window.endDate);
  const isMonth = same(month);
  const isMonthToDate = same(monthToDate);
  return <div className="cost-range-controls" aria-label="Report cost window">
    <div className="time-range-selector" role="group" aria-label="Cost comparison range">
      {/* The open month to date (from the daily pull) is the default when the
          report has one; the assessed calendar month, which every headline
          figure is computed over, follows. */}
      {monthToDate && <button type="button" aria-pressed={isMonthToDate} className={isMonthToDate ? 'active' : ''} title={`${monthToDate.startDate} to ${monthToDate.endDate} (UTC), through yesterday`} onClick={() => onChange(monthToDate)}>MTD</button>}
      {month && <button type="button" aria-pressed={isMonth} className={isMonth ? 'active' : ''} onClick={() => onChange(month)}>Month</button>}
      {[7, 30, 60, 90].map((days) => {
        const active = !isMonth && !isMonthToDate && count === days && value.endDate === latest;
        return <button type="button" key={days} aria-pressed={active} className={active ? 'active' : ''} disabled={!dates.length} onClick={() => onChange(presetCostWindow(dates, days))}>{days}d</button>;
      })}
    </div>
    <label className="billing-filter"><span>From (UTC)</span><input type="date" aria-label="Cost window start" value={value.startDate} max={value.endDate || latest} disabled={!dates.length}     onChange={(event) => onChange({ startDate: event.target.value, endDate: value.endDate })} /></label>
        <label className="billing-filter"><span>To (UTC)</span><input type="date" aria-label="Cost window end" value={value.endDate} min={value.startDate} max={latest} disabled={!dates.length} onChange={(event) => onChange({ startDate: value.startDate, endDate: event.target.value })} /></label>
    {dates.length > 0 && !count && <p role="alert">Select a valid date range of at most 366 days.</p>}
  </div>;
}
