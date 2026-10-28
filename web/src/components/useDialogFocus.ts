import { useEffect, useLayoutEffect, useRef } from 'react';

export function useDialogFocus(onClose: () => void, fallbackSelector = '#report-page-heading') {
  const dialogRef = useRef<HTMLElement>(null);
  const trigger = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const closeRef = useRef(onClose);
  useLayoutEffect(() => { closeRef.current = onClose; }, [onClose]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const background: HTMLElement[] = [];
    // Exclude sibling branches, never an ancestor containing the dialog.
    let branch: HTMLElement = dialog.closest<HTMLElement>('.remediation-backdrop') ?? dialog;
    while (branch !== document.body && branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        if (sibling instanceof HTMLElement && sibling !== branch && !sibling.hasAttribute('inert')) {
          sibling.setAttribute('inert', '');
          background.push(sibling);
        }
      }
      branch = branch.parentElement;
    }
    const controls = () => [...dialog.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea, summary, [tabindex]')]
      .filter(element => !element.matches(':disabled, [type="hidden"]') && element.tabIndex >= 0
        && !element.closest('[hidden], [inert]') && element.getClientRects().length > 0);
    const focusFirst = () => (controls()[0] ?? dialog).focus({ preventScroll: true });
    focusFirst();
    const keepFocus = (event: FocusEvent) => {
      if (event.target instanceof Node && !dialog.contains(event.target)) focusFirst();
    };
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const items = controls();
      if (!items.length) { event.preventDefault(); dialog.focus(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog)) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener('focusin', keepFocus);
    dialog.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('focusin', keepFocus);
      dialog.removeEventListener('keydown', handleKey);
      background.forEach(element => element.removeAttribute('inert'));
      document.body.style.overflow = previousOverflow;
      const original = trigger.current;
      const target = original?.isConnected && !original.matches(':disabled') && !original.closest('[hidden], [inert]')
        ? original : document.querySelector<HTMLElement>(fallbackSelector);
      target?.focus({ preventScroll: true });
    };
  }, [fallbackSelector]);
  return dialogRef;
}
