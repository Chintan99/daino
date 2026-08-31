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
  /** The element's cleaned outerHTML (capped), for handing to the agent. */
  html: string;
}

export type FrameMessage =
  | { t: "ready" }
  | { t: "selected"; node: ElementInfo }
  | { t: "deselected" }
  | { t: "changed"; html: string }
  // A block from the palette was just inserted; carries the new element so the
  // host can offer to auto-match its style.
  | { t: "inserted"; node: ElementInfo }
  // The frame cannot own the history stack (a reload would wipe it), so
  // Cmd+Z / Cmd+Shift+Z inside the page ask the host to step it instead.
  | { t: "undo" }
  | { t: "redo" };

export type HostMessage =
  | { t: "select"; path: number[] }
  | { t: "ping" }
  | { t: "clear" }
  | { t: "insert"; html: string; position: "before" | "after" | "inside" | "end" }
  | {
      t: "remove" | "duplicate" | "moveUp" | "moveDown" | "copy" | "paste" | "wrap";
      path: number[];
    }
  | { t: "setText"; path: number[]; text: string }
  | { t: "setAttr"; path: number[]; name: string; value: string }
  | { t: "setStyle"; path: number[]; prop: string; value: string }
  | { t: "dragBegin"; html: string }
  | { t: "dragEnd" }
  // A palette drag can't cross into the sandboxed frame, so the host tracks it
  // over an overlay and forwards the frame-space position here instead.
  | { t: "hostDragOver"; x: number; y: number }
  | { t: "hostDrop"; x: number; y: number };

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
  var clipboardHTML = null;
  var menuEl = null;
  var dragRAF = 0;
  var dragX = 0;
  var dragY = 0;

  function post(message) {
    try { window.parent.postMessage(message, '*'); } catch (err) { /* detached */ }
  }

  function isChrome(el) {
    return !el || el.nodeType !== 1 || !!el.closest('[data-daino-ui]');
  }

  /** outerHTML with every editor trace removed, for handing to the agent. */
  function cleanOuterHTML(el) {
    var c = el.cloneNode(true);
    var traces = c.querySelectorAll('[data-daino-sel],[data-daino-hover],[data-daino-drag],[contenteditable]');
    for (var i = 0; i < traces.length; i++) {
      traces[i].removeAttribute('data-daino-sel');
      traces[i].removeAttribute('data-daino-hover');
      traces[i].removeAttribute('data-daino-drag');
      traces[i].removeAttribute('contenteditable');
    }
    c.removeAttribute('data-daino-sel');
    c.removeAttribute('data-daino-hover');
    c.removeAttribute('data-daino-drag');
    var html = c.outerHTML || '';
    return html.length > 20000 ? html.slice(0, 20000) + '\\n<!-- …truncated -->' : html;
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
        fontWeight: el.style.fontWeight || '',
        lineHeight: el.style.lineHeight || '',
        letterSpacing: el.style.letterSpacing || '',
        textAlign: el.style.textAlign || '',
        padding: el.style.padding || '',
        paddingTop: el.style.paddingTop || '',
        paddingRight: el.style.paddingRight || '',
        paddingBottom: el.style.paddingBottom || '',
        paddingLeft: el.style.paddingLeft || '',
        margin: el.style.margin || '',
        marginTop: el.style.marginTop || '',
        marginRight: el.style.marginRight || '',
        marginBottom: el.style.marginBottom || '',
        marginLeft: el.style.marginLeft || '',
        width: el.style.width || '',
        height: el.style.height || '',
        borderRadius: el.style.borderRadius || '',
        borderWidth: el.style.borderWidth || '',
        borderStyle: el.style.borderStyle || '',
        borderColor: el.style.borderColor || '',
        display: el.style.display || '',
        flexDirection: el.style.flexDirection || '',
        justifyContent: el.style.justifyContent || '',
        alignItems: el.style.alignItems || '',
        gap: el.style.gap || ''
      },
      computed: {
        color: computed.color,
        backgroundColor: computed.backgroundColor,
        fontSize: computed.fontSize,
        fontWeight: computed.fontWeight,
        lineHeight: computed.lineHeight,
        letterSpacing: computed.letterSpacing,
        textAlign: computed.textAlign,
        padding: computed.padding,
        paddingTop: computed.paddingTop,
        paddingRight: computed.paddingRight,
        paddingBottom: computed.paddingBottom,
        paddingLeft: computed.paddingLeft,
        margin: computed.margin,
        marginTop: computed.marginTop,
        marginRight: computed.marginRight,
        marginBottom: computed.marginBottom,
        marginLeft: computed.marginLeft,
        width: computed.width,
        height: computed.height,
        borderRadius: computed.borderRadius,
        borderWidth: computed.borderWidth,
        borderStyle: computed.borderStyle,
        borderColor: computed.borderColor,
        display: computed.display,
        flexDirection: computed.flexDirection,
        justifyContent: computed.justifyContent,
        alignItems: computed.alignItems,
        gap: computed.gap
      },
      crumbs: crumbs.slice(-7),
      canMoveUp: !!el.previousElementSibling,
      canMoveDown: !!el.nextElementSibling,
      html: cleanOuterHTML(el)
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

  // ---- Element operations shared by keyboard shortcuts and host commands ----

  function copyEl(el) {
    if (!el) return;
    var clone = el.cloneNode(true);
    clone.removeAttribute('data-daino-sel');
    clone.removeAttribute('data-daino-hover');
    clipboardHTML = clone.outerHTML;
  }

  function pasteAfter(anchor) {
    if (!clipboardHTML) return;
    var built = fromHTML(clipboardHTML);
    var first = built.first;
    if (anchor && anchor.parentNode)
      anchor.parentNode.insertBefore(built.frag, anchor.nextSibling);
    else DOC.body.appendChild(built.frag);
    if (first) select(first);
    changed();
  }

  function duplicateEl(el) {
    if (!el || !el.parentNode) return;
    var copy = el.cloneNode(true);
    copy.removeAttribute('data-daino-sel');
    el.parentNode.insertBefore(copy, el.nextSibling);
    select(copy);
    changed();
  }

  function removeEl(el) {
    if (!el) return;
    var next = el.nextElementSibling || el.previousElementSibling || el.parentElement;
    el.remove();
    select(selectable(next));
    changed();
  }

  /** Wrap the element in a plain container div, keeping it selected. */
  function wrapEl(el) {
    if (!el || !el.parentNode) return;
    var box = DOC.createElement('div');
    el.parentNode.insertBefore(box, el);
    box.appendChild(el);
    select(box);
    changed();
  }

  // ---- Free (absolute) dragging: move a component anywhere, keep its look ----

  /** Lift the element out of flow at its current size, ready to be positioned. */
  function beginFreeDrag(el, p) {
    var cs = window.getComputedStyle(el);
    // Freeze the size so leaving the flow does not let it collapse or reflow.
    el.style.boxSizing = 'border-box';
    el.style.width = Math.round(p.w) + 'px';
    el.style.height = Math.round(p.h) + 'px';
    el.style.margin = '0';
    // Anything not already absolutely/fixed positioned needs absolute so left/top
    // place it directly, rather than merely offsetting its in-flow spot.
    if (cs.position !== 'absolute' && cs.position !== 'fixed') el.style.position = 'absolute';
    el.style.zIndex = '2147483000';
    el.setAttribute('data-daino-drag', '1');
    DOC.documentElement.setAttribute('data-daino-dragging', '1');
  }

  /** Position the lifted element so the grab point stays under the pointer. */
  function moveFree(el, offX, offY, x, y) {
    var parent = el.offsetParent || DOC.body;
    var prect = parent.getBoundingClientRect();
    var left = x - offX - prect.left - (parent.clientLeft || 0) + (parent.scrollLeft || 0);
    var top = y - offY - prect.top - (parent.clientTop || 0) + (parent.scrollTop || 0);
    el.style.left = Math.round(left) + 'px';
    el.style.top = Math.round(top) + 'px';
  }

  function moveUpEl(el) {
    if (!el) return;
    var before = el.previousElementSibling;
    if (before) { el.parentNode.insertBefore(el, before); select(el); changed(); }
  }

  function moveDownEl(el) {
    if (!el) return;
    var after = el.nextElementSibling;
    if (after) { el.parentNode.insertBefore(after, el); select(el); changed(); }
  }

  /** Turn a leaf element into an inline text field, cursor across its text. */
  function startEditing(el) {
    if (!el || el.children.length) return;
    el.setAttribute('contenteditable', 'true');
    el.setAttribute('spellcheck', 'false');
    el.focus();
    var range = DOC.createRange();
    range.selectNodeContents(el);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  // ---- Right-click context menu (rendered inside the frame as chrome) ----

  function closeMenu() {
    if (menuEl) { menuEl.remove(); menuEl = null; }
  }

  function openMenu(x, y, el) {
    closeMenu();
    menuEl = DOC.createElement('div');
    menuEl.setAttribute('data-daino-ui', '1');
    menuEl.className = 'daino-menu';

    var items = [];
    if (el.children.length === 0) items.push(['Edit text', function () { startEditing(el); }]);
    items.push(['Duplicate', function () { duplicateEl(el); }]);
    items.push(['Copy', function () { copyEl(el); }]);
    if (clipboardHTML) items.push(['Paste after', function () { pasteAfter(el); }]);
    items.push(['Wrap in box', function () { wrapEl(el); }]);
    if (el.previousElementSibling) items.push(['Move up', function () { moveUpEl(el); }]);
    if (el.nextElementSibling) items.push(['Move down', function () { moveDownEl(el); }]);
    items.push(['sep']);
    items.push(['Delete', function () { removeEl(el); }, 'danger']);

    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      if (it[0] === 'sep') {
        var hr = DOC.createElement('div');
        hr.className = 'daino-menu-sep';
        menuEl.appendChild(hr);
        continue;
      }
      (function (label, fn, cls) {
        var b = DOC.createElement('button');
        b.className = 'daino-menu-item' + (cls ? ' ' + cls : '');
        b.textContent = label;
        // Act on click; the capture pointerdown below leaves menu clicks alone.
        b.addEventListener('click', function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          closeMenu();
          fn();
        });
        menuEl.appendChild(b);
      })(it[0], it[1], it[2]);
    }

    DOC.body.appendChild(menuEl);
    // Keep the menu inside the viewport.
    var vw = DOC.documentElement.clientWidth;
    var vh = DOC.documentElement.clientHeight;
    var rect = menuEl.getBoundingClientRect();
    menuEl.style.left = Math.max(4, Math.min(x, vw - rect.width - 4)) + 'px';
    menuEl.style.top = Math.max(4, Math.min(y, vh - rect.height - 4)) + 'px';
  }

  // ---- Pointer interaction: click to select, drag to reorder ----
  //
  // Selection happens on pointer *down*, not on pointer up. A click almost
  // always drifts a pixel or two, and deferring selection to pointer-up meant
  // any such drift was read as a drag and the click selected nothing.

  var DRAG_THRESHOLD = 6;

  DOC.addEventListener('pointerdown', function (e) {
    // A press inside the open menu is the menu's own; let its click through.
    if (menuEl && menuEl.contains(e.target)) return;
    closeMenu();
    if (e.button !== 0) return;
    var el = selectable(under(e.clientX, e.clientY));
    if (!el) { select(null); return; }
    if (el.isContentEditable) return;
    select(el);
    var r = el.getBoundingClientRect();
    press = {
      el: el,
      x: e.clientX,
      y: e.clientY,
      moved: false,
      // Where inside the element it was grabbed, and its size — so it can be
      // lifted out of flow without jumping or resizing.
      offX: e.clientX - r.left,
      offY: e.clientY - r.top,
      w: r.width,
      h: r.height,
    };
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
        beginFreeDrag(dragging, press);
      } else if (far) {
        press = null;
      }
    }
    if (dragging) {
      e.preventDefault();
      // Coalesce movement into one reposition per frame so a fast drag stays
      // smooth; the last pointer position always wins.
      dragX = e.clientX;
      dragY = e.clientY;
      if (!dragRAF) {
        dragRAF = requestAnimationFrame(function () {
          dragRAF = 0;
          if (dragging && press) moveFree(dragging, press.offX, press.offY, dragX, dragY);
        });
      }
      return;
    }
    if (!pendingInsert) hover(selectable(under(e.clientX, e.clientY)));
  }, true);

  function endDrag() {
    if (dragRAF) { cancelAnimationFrame(dragRAF); dragRAF = 0; }
    if (dragging) dragging.removeAttribute('data-daino-drag');
    press = null;
    dragging = null;
    drop = null;
    hideLine();
    DOC.documentElement.removeAttribute('data-daino-dragging');
  }

  DOC.addEventListener('pointerup', function (e) {
    if (dragging && press) {
      if (dragRAF) { cancelAnimationFrame(dragRAF); dragRAF = 0; }
      var target = dragging;
      // Settle it at the exact release point, then drop the drag-only styling.
      moveFree(target, press.offX, press.offY, e.clientX, e.clientY);
      target.removeAttribute('data-daino-drag');
      target.style.zIndex = '';
      select(target);
      changed();
    }
    endDrag();
  }, true);

  DOC.addEventListener('pointercancel', endDrag, true);

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
    startEditing(el);
  }, true);

  // Right-click anything on the page for its edit menu.
  DOC.addEventListener('contextmenu', function (e) {
    if (menuEl && menuEl.contains(e.target)) return;
    var el = selectable(under(e.clientX, e.clientY));
    if (!el) { closeMenu(); return; } // leave chrome / empty space to the browser
    e.preventDefault();
    select(el);
    openMenu(e.clientX, e.clientY, el);
  }, true);

  // Images and links are natively draggable; that native drag would hijack the
  // reorder gesture, so block it (palette drops set pendingInsert and keep it).
  DOC.addEventListener('dragstart', function (e) {
    if (!pendingInsert) e.preventDefault();
  }, true);

  DOC.addEventListener('scroll', closeMenu, true);

  DOC.addEventListener('focusout', function (e) {
    var el = e.target;
    if (el && el.nodeType === 1 && el.getAttribute('contenteditable') === 'true') {
      el.removeAttribute('contenteditable');
      el.removeAttribute('spellcheck');
      changed();
    }
  }, true);

  DOC.addEventListener('keydown', function (e) {
    if (menuEl && (e.key || '').toLowerCase() === 'escape') {
      e.preventDefault();
      closeMenu();
      return;
    }
    var editing = DOC.querySelector('[contenteditable="true"]');
    if (editing) {
      if (e.key === 'Escape' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault();
        editing.blur();
      }
      return; // never hijack keys while text is being typed
    }
    var mod = e.metaKey || e.ctrlKey;
    var key = (e.key || '').toLowerCase();
    // History lives on the host; ask it to step.
    if (mod && key === 'z') { e.preventDefault(); post({ t: e.shiftKey ? 'redo' : 'undo' }); return; }
    if (mod && key === 'y') { e.preventDefault(); post({ t: 'redo' }); return; }
    if (key === 'escape') { e.preventDefault(); select(null); return; }
    if (!selectedEl) return;
    if (mod && key === 'c') { e.preventDefault(); copyEl(selectedEl); return; }
    if (mod && key === 'v') { e.preventDefault(); pasteAfter(selectedEl); return; }
    if (mod && key === 'd') { e.preventDefault(); duplicateEl(selectedEl); return; }
    if (key === 'delete' || key === 'backspace') { e.preventDefault(); removeEl(selectedEl); return; }
    // Alt+Arrows reorder, so plain arrows still scroll the page.
    if (e.altKey && key === 'arrowup') {
      e.preventDefault();
      var before = selectedEl.previousElementSibling;
      if (before) { selectedEl.parentNode.insertBefore(selectedEl, before); select(selectedEl); changed(); }
      return;
    }
    if (e.altKey && key === 'arrowdown') {
      e.preventDefault();
      var after = selectedEl.nextElementSibling;
      if (after) { selectedEl.parentNode.insertBefore(after, selectedEl); select(selectedEl); changed(); }
      return;
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
    if (first) { select(first); post({ t: 'inserted', node: describe(first) }); }
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

    // Host-forwarded palette drag (coords already in this frame's space).
    if (msg.t === 'hostDragOver') {
      if (pendingInsert) updateDrop(msg.x, msg.y);
      return;
    }
    if (msg.t === 'hostDrop') {
      if (!pendingInsert) return;
      updateDrop(msg.x, msg.y);
      var dropped = fromHTML(pendingInsert);
      var droppedFirst = dropped.first;
      if (!place(dropped.frag) && DOC.body) DOC.body.appendChild(dropped.frag);
      pendingInsert = null;
      hideLine();
      if (droppedFirst) {
        select(droppedFirst);
        post({ t: 'inserted', node: describe(droppedFirst) });
      }
      changed();
      return;
    }

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
      if (first) { select(first); post({ t: 'inserted', node: describe(first) }); }
      changed();
      return;
    }

    if (msg.t === 'paste') { pasteAfter(el || selectedEl); return; }
    if (!el) return;
    if (msg.t === 'remove') {
      removeEl(el);
    } else if (msg.t === 'duplicate') {
      duplicateEl(el);
    } else if (msg.t === 'copy') {
      copyEl(el);
    } else if (msg.t === 'wrap') {
      wrapEl(el);
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
  [data-daino-hover] { outline: 1px dashed rgba(108, 191, 141, 0.85) !important; outline-offset: 1px; cursor: move !important; }
  [data-daino-sel] { outline: 2px solid #6cbf8d !important; outline-offset: 1px; cursor: move !important; }
  [contenteditable="true"] { outline: 2px solid #8ad6a5 !important; cursor: text !important; }
  /* While a drag is live the whole page shows the move cursor and the lifted
     element dims and floats, so the gesture reads as "picked up". */
  [data-daino-dragging], [data-daino-dragging] * { cursor: grabbing !important; }
  [data-daino-dragging] { user-select: none !important; }
  [data-daino-drag] { opacity: .75 !important; box-shadow: 0 8px 26px rgba(0,0,0,.35) !important;
    transition: opacity .08s ease; }
  .daino-drop-line { position: fixed; height: 3px; background: #6cbf8d; border-radius: 2px;
    z-index: 2147483647; pointer-events: none;
    box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.35), 0 0 8px rgba(108, 191, 141, 0.7); display: none; }
  .daino-menu { position: fixed !important; z-index: 2147483647 !important; min-width: 168px;
    background: #1b1f1d !important; color: #e8efe9 !important; border: 1px solid rgba(255,255,255,.14) !important;
    border-radius: 8px !important; padding: 4px !important; margin: 0 !important;
    box-shadow: 0 10px 34px rgba(0,0,0,.55) !important;
    font: 13px/1.45 system-ui, -apple-system, sans-serif !important; }
  .daino-menu-item { display: block !important; width: 100% !important; box-sizing: border-box !important;
    text-align: left !important; background: transparent !important; border: 0 !important; color: inherit !important;
    padding: 6px 12px !important; border-radius: 5px !important; cursor: pointer !important;
    font: inherit !important; letter-spacing: 0 !important; }
  .daino-menu-item:hover { background: rgba(108,191,141,.20) !important; }
  .daino-menu-item.danger { color: #ff9b9b !important; }
  .daino-menu-sep { height: 1px !important; background: rgba(255,255,255,.12) !important; margin: 4px 2px !important; }
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
