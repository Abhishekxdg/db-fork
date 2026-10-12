/* The details dialog: one native modal dialog, centered, reused for every row's details. Opening it focuses the
   scrollable body; closing returns focus to the control that opened it, found by id because the page may have rebuilt
   that control while the dialog was open. */
const detailsDialog = el('run-dialog');
let detailsOpener = null;
function openDetails(title, subtitle, source, opener) {
    detailsOpener = opener.id;
    el('run-title').textContent = title; el('run-subtitle').replaceChildren(parts(subtitle.split(' · ')));
    el('run-source').hidden = !source; if (source) el('run-source').href = source; else el('run-source').removeAttribute('href');
    const body = el('run-body'); body.replaceChildren();
    return body;
}
function showDetails() {
    const body = el('run-body');
    if (!detailsDialog.open) detailsDialog.showModal();
    body.scrollTop = 0;  // the dialog is reused, so a previous visit's scroll offset must not hide the new details' start
    body.focus({preventScroll: true});  // the long body is the scroll region; arrow and page keys work from here
    syncScrollHints();
}
function wireDetails() {
    el('close-run').addEventListener('click', () => detailsDialog.close());
    detailsDialog.addEventListener('close', () => { const opener = el(detailsOpener); if (opener) opener.focus(); });
    detailsDialog.addEventListener('click', event => {  // a click on the backdrop, outside the panel, closes it
        if (event.target !== detailsDialog) return;
        const box = detailsDialog.getBoundingClientRect();
        if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) detailsDialog.close();
    });
}
function detailsSection(body, title, note) {
    const head = node('div', 'dialog-section'); head.append(node('h3', '', title));
    if (note) head.append(node('p', 'section-note', note));
    body.append(head);
    return head;
}
// Label and value pairs; an empty value is left out rather than shown as a blank.
function metaGrid(items) {
    const list = node('dl', 'meta-grid');
    for (const [key, value] of items) if (value !== undefined && value !== null && value !== '') { const pair = node('div'); pair.append(node('dt', '', key), node('dd', '', String(value))); list.append(pair); }
    return list;
}
function disclosure(title, ...children) { const box = node('details', 'inline-more'); box.append(node('summary', '', title), ...children); return box; }
function tagList(items, className = 'pill-list') { const list = node('ul', className); list.append(...items.map(t => node('li', '', t))); return list; }
