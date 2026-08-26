// The in-frame visual editor.
//
// The preview iframe is sandboxed *without* `allow-same-origin`, so its origin
// is opaque and the parent cannot reach into its DOM — which is exactly what we
// want for HTML somebody dropped in. All editing therefore happens inside the
// frame, in the runtime below, and the two sides talk over `postMessage`. The
// frame can neither read nor script the Daino app that hosts it.

export interface ElementInfo {
  path: number[];
  tag: string;
  id: string;
  className: string;
  text: string;
  editableText: boolean;
  href: string;
  src: string;
  alt: string;
  inline: Record<string, string>;
  computed: Record<string, string>;
  crumbs: { tag: string; path: number[] }[];
  canMoveUp: boolean;
  canMoveDown: boolean;
}

export type FrameMessage =
  | { t: "ready" }
  | { t: "selected"; node: ElementInfo }
  | { t: "deselected" }
  | { t: "changed"; html: string };

export type HostMessage =
  | { t: "select"; path: number[] }
  | { t: "ping" }
  | { t: "clear" }
  | { t: "insert"; html: string; position: "before" | "after" | "inside" | "end" }
  | { t: "remove" | "duplicate" | "moveUp" | "moveDown"; path: number[] }
  | { t: "setText"; path: number[]; text: string }
  | { t: "setAttr"; path: number[]; name: string; value: string }
  | { t: "setStyle"; path: number[]; prop: string; value: string }
  | { t: "dragBegin"; html: string }
  | { t: "dragEnd" };

/** True when the source is a bare fragment rather than a whole document. */
export function isFragment(html: string): boolean {
  return !/<html[\s>]/i.test(html);
}

const RUNTIME = `
(function () {
  if (window.__dainoEditorLoaded) return;
  window.__dainoEditorLoaded = true;

  var DOC = document;
  var FRAGMENT = window.__dainoFragment === true;
  var press = null;
  var dragging = null;
  var drop = null;
  var pendingInsert = null;
  var selectedEl = null;
  var line = null;

  function post(message) {
    try { window.parent.postMessage(message, '*'); } catch (err) { /* detached */ }
  }

  function isChrome(el) {
    return !el || el.nodeType !== 1 || !!el.closest('[data-daino-ui]');
  }

  function selectable(el) {
    if (isChrome(el)) return null;
    if (el === DOC.documentElement || el === DOC.body || el === DOC.head) return null;
    return el;
  }

  function pathOf(el) {
    var path = [];
    var node = el;
    while (node && node !== DOC.documentElement && node.parentElement) {
      var kids = node.parentElement.children;
      var index = 0;
      for (var i = 0; i < kids.length; i++) { if (kids[i] === node) { index = i; break; } }
      path.unshift(index);
      node = node.parentElement;
    }
    return path;
  }

  function elAt(path) {
    var node = DOC.documentElement;
    for (var i = 0; i < path.length; i++) {
      if (!node) return null;
      node = node.children[path[i]];
    }
    return node || null;
  }

  function ensureLine() {
    if (line && line.isConnected) return line;
    line = DOC.createElement('div');
    line.setAttribute('data-daino-ui', '1');
    line.className = 'daino-drop-line';
    DOC.body.appendChild(line);
    return line;
  }

  function hideLine() { if (line) line.style.display = 'none'; }

  function describe(el) {
    var computed = window.getComputedStyle(el);
    var crumbs = [];
    var walk = el;
    while (walk && walk.nodeType === 1 && walk !== DOC.documentElement) {
      crumbs.unshift({ tag: walk.tagName.toLowerCase(), path: pathOf(walk) });
      walk = walk.parentElement;
    }
    var leaf = el.children.length === 0;
    return {
      path: pathOf(el),
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      className: el.getAttribute('class') || '',
      text: leaf ? (el.textContent || '') : '',
      editableText: leaf,
      href: el.getAttribute('href') || '',
      src: el.getAttribute('src') || '',
      alt: el.getAttribute('alt') || '',
      inline: {
        color: el.style.color || '',
        backgroundColor: el.style.backgroundColor || '',
        fontSize: el.style.fontSize || '',
        padding: el.style.padding || '',
        margin: el.style.margin || '',
        textAlign: el.style.textAlign || '',
        display: el.style.display || ''
      },
      computed: {
        color: computed.color,
        backgroundColor: computed.backgroundColor,
        fontSize: computed.fontSize,
        padding: computed.padding,
        margin: computed.margin,
        textAlign: computed.textAlign,
        display: computed.display
      },
      crumbs: crumbs.slice(-7),
      canMoveUp: !!el.previousElementSibling,
      canMoveDown: !!el.nextElementSibling
    };
  }

  function select(el) {
    if (selectedEl) selectedEl.removeAttribute('data-daino-sel');
    selectedEl = el || null;
    if (!selectedEl) { post({ t: 'deselected' }); return; }
    selectedEl.setAttribute('data-daino-sel', '1');
    post({ t: 'selected', node: describe(selectedEl) });
  }

  function hover(el) {
    var previous = DOC.querySelector('[data-daino-hover]');
    if (previous && previous !== el) previous.removeAttribute('data-daino-hover');
    if (el && el !== selectedEl) el.setAttribute('data-daino-hover', '1');
  }

  /** Serialize the page back to source, with every editor trace removed. */
  function serialize() {
    var clone = DOC.documentElement.cloneNode(true);
    var chrome = clone.querySelectorAll('[data-daino-ui]');
    for (var i = 0; i < chrome.length; i++) chrome[i].remove();
    var marked = clone.querySelectorAll('[data-daino-sel],[data-daino-hover],[contenteditable]');
    for (var j = 0; j < marked.length; j++) {
      marked[j].removeAttribute('data-daino-sel');
      marked[j].removeAttribute('data-daino-hover');
      marked[j].removeAttribute('contenteditable');
      marked[j].removeAttribute('spellcheck');
    }
    // Page scripts were parked so they could not fight the editor; restore them.
    var parked = clone.querySelectorAll('script[data-daino-script]');
    for (var k = 0; k < parked.length; k++) {
      var original = parked[k].getAttribute('data-daino-type');
      parked[k].removeAttribute('data-daino-script');
      parked[k].removeAttribute('data-daino-type');
      if (original) parked[k].setAttribute('type', original);
      else parked[k].removeAttribute('type');
    }
    if (FRAGMENT) {
      var body = clone.querySelector('body');
      return body ? body.innerHTML.trim() : clone.innerHTML;
    }
    return '<!doctype html>\\n' + clone.outerHTML;
  }

  var changeTimer = null;
  function changed() {
    if (changeTimer) clearTimeout(changeTimer);
    changeTimer = setTimeout(function () {
      post({ t: 'changed', html: serialize() });
      if (selectedEl && selectedEl.isConnected) post({ t: 'selected', node: describe(selectedEl) });
    }, 60);
  }

  /** Which element is under the pointer, ignoring chrome and the dragged node. */
  function under(x, y) {
    var stack = DOC.elementsFromPoint(x, y) || [];
    for (var i = 0; i < stack.length; i++) {
      var el = stack[i];
      if (isChrome(el)) continue;
      if (dragging && (el === dragging || dragging.contains(el))) continue;
      if (el === DOC.documentElement || el === DOC.head) continue;
      return el;
    }
    return null;
  }

  /** Decide where a drop would land, and draw the indicator for it. */
  function updateDrop(x, y) {
    var target = under(x, y);
    if (!target) { drop = null; hideLine(); return; }
    var rect = target.getBoundingClientRect();
    var position;
    if (target === DOC.body || (target.children.length === 0 && !target.textContent.trim())) {
      position = 'inside';
    } else {
      position = y < rect.top + rect.height / 2 ? 'before' : 'after';
    }
    drop = { target: target, position: position };
    var bar = ensureLine();
    bar.style.display = 'block';
    if (position === 'inside') {
      bar.style.left = rect.left + 'px';
      bar.style.width = Math.max(rect.width, 8) + 'px';
      bar.style.top = (rect.top + rect.height / 2) + 'px';
    } else {
      bar.style.left = rect.left + 'px';
      bar.style.width = Math.max(rect.width, 8) + 'px';
      bar.style.top = (position === 'before' ? rect.top : rect.bottom) + 'px';
    }
  }

  function place(node) {
    if (!drop) return false;
    var target = drop.target;
    if (drop.position === 'inside') target.appendChild(node);
    else if (drop.position === 'before') target.parentNode.insertBefore(node, target);
    else target.parentNode.insertBefore(node, target.nextSibling);
    return true;
  }

  function fromHTML(html) {
    var host = DOC.createElement('template');
    host.innerHTML = html.trim();
    var frag = host.content;
    var first = frag.firstElementChild;
    return { frag: frag, first: first };
  }

  // ---- Pointer interaction: click to select, drag to reorder ----
  //
  // Selection happens on pointer *down*, not on pointer up. A click almost
  // always drifts a pixel or two, and deferring selection to pointer-up meant
  // any such drift was read as a drag and the click selected nothing.

  var DRAG_THRESHOLD = 6;

  DOC.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    var el = selectable(under(e.clientX, e.clientY));
    if (!el) { select(null); return; }
    if (el.isContentEditable) return;
    select(el);
    press = { el: el, x: e.clientX, y: e.clientY, moved: false };
  }, true);

  DOC.addEventListener('pointermove', function (e) {
    if (press && !press.moved) {
      var far = Math.abs(e.clientX - press.x) > DRAG_THRESHOLD ||
                Math.abs(e.clientY - press.y) > DRAG_THRESHOLD;
      // A drag has to be a held, deliberate movement; a released button means
      // the interaction already ended and this is just the cursor travelling.
      if (far && e.buttons === 1) {
        press.moved = true;
        dragging = press.el;
        DOC.documentElement.setAttribute('data-daino-dragging', '1');
      } else if (far) {
        press = null;
      }
    }
    if (dragging) {
      e.preventDefault();
      updateDrop(e.clientX, e.clientY);
      return;
    }
    if (!pendingInsert) hover(selectable(under(e.clientX, e.clientY)));
  }, true);

  DOC.addEventListener('pointerup', function (e) {
    if (dragging) {
      var moved = drop && place(dragging);
      // Whether or not the drop was somewhere valid, what was dragged stays
      // selected — losing the selection because a drop missed is disorienting.
      select(dragging);
      if (moved) changed();
    }
    press = null;
    dragging = null;
    drop = null;
    hideLine();
    DOC.documentElement.removeAttribute('data-daino-dragging');
  }, true);

  DOC.addEventListener('pointercancel', function () {
    press = null;
    dragging = null;
    drop = null;
    hideLine();
    DOC.documentElement.removeAttribute('data-daino-dragging');
  }, true);

  // Editing a page is not browsing it: links and submits must not navigate.
  DOC.addEventListener('click', function (e) {
    var anchor = e.target && e.target.closest ? e.target.closest('a') : null;
    if (anchor) { e.preventDefault(); }
  }, true);
  DOC.addEventListener('submit', function (e) { e.preventDefault(); }, true);

  DOC.addEventListener('dblclick', function (e) {
    var el = selectable(under(e.clientX, e.clientY));
    if (!el || el.children.length) return;
    e.preventDefault();
    el.setAttribute('contenteditable', 'true');
    el.setAttribute('spellcheck', 'false');
    el.focus();
    var range = DOC.createRange();
    range.selectNodeContents(el);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }, true);

  DOC.addEventListener('focusout', function (e) {
    var el = e.target;
    if (el && el.nodeType === 1 && el.getAttribute('contenteditable') === 'true') {
      el.removeAttribute('contenteditable');
      el.removeAttribute('spellcheck');
      changed();
    }
  }, true);

  DOC.addEventListener('keydown', function (e) {
    var el = DOC.querySelector('[contenteditable="true"]');
    if (el && (e.key === 'Escape' || (e.key === 'Enter' && !e.shiftKey))) {
      e.preventDefault();
      el.blur();
    }
  }, true);

  // ---- Dragging a component in from the host palette ----

  DOC.addEventListener('dragover', function (e) {
    if (!pendingInsert) return;
    e.preventDefault();
    updateDrop(e.clientX, e.clientY);
  }, true);

  DOC.addEventListener('drop', function (e) {
    if (!pendingInsert) return;
    e.preventDefault();
    updateDrop(e.clientX, e.clientY);
    var built = fromHTML(pendingInsert);
    var first = built.first;
    if (!place(built.frag) && DOC.body) DOC.body.appendChild(built.frag);
    pendingInsert = null;
    hideLine();
    if (first) select(first);
    changed();
  }, true);

  // ---- Host commands ----

  window.addEventListener('message', function (event) {
    if (event.source !== window.parent) return;
    var msg = event.data;
    if (!msg || typeof msg.t !== 'string') return;
    var el = msg.path ? elAt(msg.path) : selectedEl;

    if (msg.t === 'select') { select(elAt(msg.path)); return; }
    if (msg.t === 'ping') { post({ t: 'ready' }); return; }
    if (msg.t === 'clear') { select(null); return; }
    if (msg.t === 'dragBegin') { pendingInsert = msg.html; return; }
    if (msg.t === 'dragEnd') { pendingInsert = null; hideLine(); return; }

    if (msg.t === 'insert') {
      var built = fromHTML(msg.html);
      var first = built.first;
      var anchor = selectedEl;
      if (msg.position === 'end' || !anchor) {
        DOC.body.appendChild(built.frag);
      } else if (msg.position === 'inside') {
        anchor.appendChild(built.frag);
      } else if (msg.position === 'before') {
        anchor.parentNode.insertBefore(built.frag, anchor);
      } else {
        anchor.parentNode.insertBefore(built.frag, anchor.nextSibling);
      }
      if (first) select(first);
      changed();
      return;
    }

    if (!el) return;
    if (msg.t === 'remove') {
      var next = el.nextElementSibling || el.previousElementSibling || el.parentElement;
      el.remove();
      select(selectable(next));
      changed();
    } else if (msg.t === 'duplicate') {
      var copy = el.cloneNode(true);
      copy.removeAttribute('data-daino-sel');
      el.parentNode.insertBefore(copy, el.nextSibling);
      select(copy);
      changed();
    } else if (msg.t === 'moveUp') {
      var before = el.previousElementSibling;
      if (before) { el.parentNode.insertBefore(el, before); select(el); changed(); }
    } else if (msg.t === 'moveDown') {
      var after = el.nextElementSibling;
      if (after) { el.parentNode.insertBefore(after, el); select(el); changed(); }
    } else if (msg.t === 'setText') {
      el.textContent = msg.text;
      changed();
    } else if (msg.t === 'setAttr') {
      if (msg.value) el.setAttribute(msg.name, msg.value);
      else el.removeAttribute(msg.name);
      changed();
    } else if (msg.t === 'setStyle') {
      el.style.setProperty(msg.prop, msg.value);
      if (!msg.value) el.style.removeProperty(msg.prop);
      if (!el.getAttribute('style')) el.removeAttribute('style');
      changed();
    }
  });

  post({ t: 'ready' });
})();
`;

const RUNTIME_STYLE = `
  [data-daino-hover] { outline: 1px dashed rgba(108, 191, 141, 0.85) !important; outline-offset: 1px; }
  [data-daino-sel] { outline: 2px solid #6cbf8d !important; outline-offset: 1px; }
  [contenteditable="true"] { outline: 2px solid #8ad6a5 !important; }
  [data-daino-dragging] * { cursor: grabbing !important; }
  .daino-drop-line { position: fixed; height: 2px; background: #6cbf8d; z-index: 2147483647;
    pointer-events: none; box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.4); display: none; }
`;

/**
 * Park page scripts so they cannot run while the page is being edited.
 *
 * A page that rewrites its own DOM on load would otherwise fight every drag, and
 * the parked tags are restored verbatim when the source is serialized back.
 */
function parkScripts(html: string): string {
  return html.replace(
    /<script\b([^>]*)>/gi,
    (_match: string, attrs: string) => {
      const type = /type\s*=\s*["']([^"']*)["']/i.exec(attrs);
      const cleaned = attrs.replace(/\stype\s*=\s*["'][^"']*["']/i, "");
      const original = type ? ` data-daino-type="${type[1]}"` : "";
      return `<script${cleaned} type="text/plain" data-daino-script="1"${original}>`;
    },
  );
}

/** Wrap artifact source for the iframe, with the editor runtime when editing. */
export function buildFrameDoc(html: string, editable: boolean): string {
  if (!editable) return html;
  const fragment = isFragment(html);
  const injected =
    `<style data-daino-ui="1">${RUNTIME_STYLE}</style>` +
    `<script data-daino-ui="1">window.__dainoFragment=${fragment};</script>` +
    `<script data-daino-ui="1">${RUNTIME}</script>`;
  const parked = parkScripts(html);
  if (/<\/body>/i.test(parked))
    return parked.replace(/<\/body>/i, `${injected}</body>`);
  return `${parked}${injected}`;
}
