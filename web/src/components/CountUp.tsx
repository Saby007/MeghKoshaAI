import { useLayoutEffect, useRef, useState } from 'react';

const DURATION_MS = 700;

function prefersStillness(): boolean {
  // Without matchMedia there is no way to honour the preference, so show the final value.
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? true;
}

/* Runs from zero to `value` on first mount, and from the figure on screen to the new one
   when it later changes. The first render already carries the final value, so nothing
   depends on the animation to be correct. */
function useCountUp(value: number): number {
  const [shown, setShown] = useState(value);
  const current = useRef(value);
  const mounted = useRef(false);

  useLayoutEffect(() => {
    const from = mounted.current ? current.current : 0;
    mounted.current = true;
    if (!Number.isFinite(value) || from === value || prefersStillness()) {
      current.current = value;
      setShown(value);
      return;
    }
    // Timed from the first painted frame, not from the effect: a busy commit can delay that
    // frame, and a clock started earlier would already have run out by the time it arrives.
    let start: number | null = null;
    let frame = 0;
    const step = (now: number) => {
      start ??= now;
      const progress = Math.min(1, (now - start) / DURATION_MS);
      const eased = 1 - (1 - progress) ** 4;
      current.current = progress === 1 ? value : from + (value - from) * eased;
      setShown(current.current);
      if (progress < 1) frame = requestAnimationFrame(step);
    };
    current.current = from;
    setShown(from);
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [value]);

  return shown;
}

export function CountUp({ value, format }: { value: number; format: (value: number) => string }) {
  const shown = useCountUp(value);
  // Whole numbers in flight, so the sub-dollar rule does not flicker at the start of the run.
  return <>{format(shown === value ? value : Math.round(shown))}</>;
}
