import { costWindowDates, presetCostWindow, type CostWindow } from '../report/costDetails';

/* The report-wide cost window picker.
   Kept in its own module rather than in CostExplorer because App renders it in
   the saved-report strip, and CostExplorer is a large module: importing the
   picker from there would pull the resource tables and export machinery into
   the initial bundle for the sake of four buttons and two date inputs. */
export function CostRangeControls({ dates, value, onChange }: { dates: string[]; value: CostWindow; onChange: (value: CostWindow) => void }) {
  const latest = dates.at(-1) ?? '';
  const count = costWindowDates(value).length;
  return <div className="cost-range-controls" aria-label="Report cost window">
    <div className="time-range-selector" role="group" aria-label="Cost comparison range">
      {[7, 30, 60, 90].map((days) => <button type="button" key={days} aria-pressed={count === days && value.endDate === latest} className={count === days && value.endDate === latest ? 'active' : ''} disabled={!dates.length} onClick={() => onChange(presetCostWindow(dates, days))}>{days}d</button>)}
    </div>
    <label className="billing-filter"><span>From (UTC)</span><input type="date" aria-label="Cost window start" value={value.startDate} max={value.endDate || latest} disabled={!dates.length} onChange={(event) => onChange({ ...value, startDate: event.target.value })} /></label>
    <label className="billing-filter"><span>To (UTC)</span><input type="date" aria-label="Cost window end" value={value.endDate} min={value.startDate} max={latest} disabled={!dates.length} onChange={(event) => onChange({ ...value, endDate: event.target.value })} /></label>
    {dates.length > 0 && !count && <p role="alert">Select a valid date range of at most 366 days.</p>}
  </div>;
}
