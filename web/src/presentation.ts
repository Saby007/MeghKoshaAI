/* Presentation helpers that touch the DOM directly so no component has to change. */

const MAX_DEPTH = 4;

/* A name cut short with an ellipsis is unrecoverable in a table, and Azure names are long.
   On hover, give the truncated element its full text as a native tooltip. Only elements
   that are actually clipped get one, and a title a component set itself is never replaced. */
export function installTruncationTitles(root: Document = document): () => void {
  function onPointerOver(event: Event) {
    let el = event.target instanceof HTMLElement ? event.target : null;
    for (let depth = 0; el && depth < MAX_DEPTH; depth += 1, el = el.parentElement) {
      if (el.hasAttribute('title') && !el.dataset.autoTitle) return;
      if (getComputedStyle(el).textOverflow !== 'ellipsis') continue;
      if (el.scrollWidth > el.clientWidth) {
        const text = el.textContent?.replace(/\s+/g, ' ').trim();
        if (text) {
          el.title = text;
          el.dataset.autoTitle = '1';
        }
      } else if (el.dataset.autoTitle) {
        el.removeAttribute('title');
        delete el.dataset.autoTitle;
      }
      return;
    }
  }
  root.addEventListener('pointerover', onPointerOver, { passive: true });
  return () => root.removeEventListener('pointerover', onPointerOver);
}
