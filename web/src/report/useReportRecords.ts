import { startTransition, useCallback, useEffect, useRef, useState } from 'react';
import type { RecordQueryOptions, ReportDataReader } from './postgresData';

export function useReportRecords<T>(source: ReportDataReader | undefined, paths: string[], query: RecordQueryOptions = {}) {
  const key = JSON.stringify([paths, query]);
  const [window, setWindow] = useState({ key, limit: 50 });
  const limit = window.key === key ? window.limit : 50;
  const last = useRef<HTMLElement | null>(null);
  const [element, setElement] = useState<HTMLElement | null>(null);
  const loadingMore = useRef(false);
  const page = source && paths.length ? source.records<T>(paths, query, limit) : null;
  const count = page?.items.length ?? 0;
  const total = page?.total ?? 0;
  useEffect(() => {
    setWindow(value => value.key === key ? value : { key, limit: 50 });
  }, [key]);
  useEffect(() => {
    loadingMore.current = false;
    if (!page || count >= total || !last.current || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(entries => {
      if (loadingMore.current || !entries.some(entry => entry.isIntersecting)) return;
      loadingMore.current = true;
      startTransition(() => setWindow(value => ({ key, limit: Math.min(total, (value.key === key ? value.limit : 50) + 50) })));
    }, { rootMargin: '250px' });
    observer.observe(last.current);
    return () => observer.disconnect();
  }, [key, count, total, page !== null, element]);
  const lastRowRef = useCallback((next: HTMLElement | null) => { last.current = next; setElement(next); }, []);
  return { page, lastRowRef };
}
