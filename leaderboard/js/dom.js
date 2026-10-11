/* Shared helpers for the leaderboard page: DOM building, number formats, design tokens, tables and the
   small controls every board reuses. The page loads plain scripts in order (dom, details, scenes, micro,
   macro, app). They share one global scope and, unlike ES modules, still run from a file:// preview. */
const el = id => document.getElementById(id);
function node(tag, className = '', text) {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
}
function svgNode(tag, attributes = {}, text) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    if (text !== undefined) element.textContent = text;
    return element;
}

// One number format for every viewer: a locale must never turn 1,000 rows into "1.000".
const fmt = n => n.toLocaleString('en-US');
const plural = (n, word, many = `${word}s`) => `${fmt(n)} ${n === 1 ? word : many}`;
const seconds = value => value === null ? 'Failed' : `${value.toLocaleString('en-US', {minimumFractionDigits: 3, maximumFractionDigits: 6, useGrouping: false})}s`;
const ordinal = n => `${n}${n % 100 >= 11 && n % 100 <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] || 'th'}`;
const gib = (bytes, digits) => typeof bytes === 'number' ? `${(bytes / 2 ** 30).toFixed(digits)} GiB` : null;
const uniq = values => [...new Set(values)].sort((a, b) => a.localeCompare(b, undefined, {numeric: true}));

// Colors are defined once, as tokens in styles.css. Code that paints pixels itself (the Canvas scenes) reads them from there.
const tokenCache = new Map();
function token(name) {
    if (!tokenCache.has(name)) tokenCache.set(name, getComputedStyle(document.documentElement).getPropertyValue(name).trim());
    return tokenCache.get(name);
}

const ICON = {check: ['M3.5 8.4l3 3 6-6.6'], chevron: ['M6 3.5L10.5 8 6 12.5']};
function icon(paths, className = 'icon') {
    const svg = svgNode('svg', {class: className, viewBox: '0 0 16 16', 'aria-hidden': 'true', focusable: 'false'});
    for (const d of paths) svg.append(svgNode('path', {d}));
    return svg;
}
function bestMark(text = 'Fastest') {
    const mark = node('span', 'best-mark'); mark.setAttribute('role', 'img'); mark.setAttribute('aria-label', text);
    mark.append(icon(ICON.check));
    return mark;
}
// A thin bar whose length is a share of its track; the number beside it carries the value.
function bar(share, className = 'bar') {
    const track = node('span', className), fill = node('span', 'bar-fill'); track.setAttribute('aria-hidden', 'true');
    fill.style.width = `${Math.max(2, Math.min(100, share * 100)).toFixed(1)}%`;
    track.append(fill);
    return track;
}
// Meta lines wrap between items, never inside one: a date or a machine name stays whole.
function parts(items, className = '') {
    const line = node('span', className);
    items.filter(Boolean).forEach((text, i) => { if (i) line.append(' · '); line.append(node('span', 'nowrap', text)); });
    return line;
}
// A small flag beside a system's name. The full reason is its title.
function flag(text, title) { const tag = node('span', 'flag', text); tag.title = title; return tag; }
// Heat: how close a time is to the fastest in its row or column, painted with the --heat token at that strength.
function shade(cell, fastest, value) {
    cell.classList.add('heat');
    cell.style.setProperty('--strength', (0.035 + (value === 0 ? 1 : fastest / value) * 0.19).toFixed(3));
}

// Tables: an accessible name, one row of column headings, and a body to fill.
function colHead(content, className = '') { const th = node('th', className); th.scope = 'col'; th.append(content); return th; }
function rowHead(content, className = '') { const th = node('th', className); th.scope = 'row'; th.append(...[content].flat()); return th; }
function dataTable(className, label, headings) {
    const table = node('table', className); table.setAttribute('aria-label', label);
    const head = table.createTHead().insertRow();
    head.append(...headings.map(h => typeof h === 'string' ? colHead(h) : h));
    return {table, head, body: table.createTBody()};
}
// A wide table scrolls inside its own region; its hint shows only while the region overflows.
let scrollers = [];
function scrolling(table, hintText = 'Scroll sideways to see every column.') {
    const box = node('div', 'scroll'); box.tabIndex = 0; box.setAttribute('role', 'region');
    box.setAttribute('aria-label', table.getAttribute('aria-label')); box.append(table);
    const hint = node('p', 'scroll-hint', hintText); hint.hidden = true; scrollers.push([box, hint]);
    const wrap = node('div', 'scroll-wrap'); wrap.append(box, hint);
    return wrap;
}
function syncScrollHints() {  // every width is read before any hint is written, so the check costs one layout
    const overflowing = scrollers.map(([box]) => box.scrollWidth > box.clientWidth);
    scrollers.forEach(([, hint], i) => { hint.hidden = !overflowing[i]; });
}

// Controls change state, re-render and keep focus on the control the viewer used.
function selector(text, active, onclick, id) {
    const button = node('button', 'selector' + (active ? ' selector-active' : ''), text);
    button.type = 'button'; button.id = id; button.setAttribute('aria-pressed', String(active));
    button.addEventListener('click', () => { onclick(); render(); el(id).focus(); });
    return button;
}
function choice(container, options, key, after) {
    container.replaceChildren(...Object.entries(options).map(([k, title]) => selector(title, state[key] === k, () => { state[key] = k; if (after) after(); }, `choice-${key}-${k}`)));
}
function option(value, text) { const o = node('option', '', text); o.value = value; return o; }
// A native select changes one piece of state, re-renders and keeps focus; the page never rebuilds the select while it has focus.
function bindSelect(id, apply) { el(id).addEventListener('change', () => { apply(el(id).value); render(); el(id).focus(); }); }
// A "?" opens a definition below the board and moves focus to it.
function definitionButton(ariaLabel, open) {
    const button = node('button', 'help-trigger', '?'); button.type = 'button'; button.setAttribute('aria-label', ariaLabel);
    button.addEventListener('click', open);
    return button;
}
function sortHeader(text, active, onclick, id, help) {
    const button = node('button', 'sort-button', text); button.type = 'button'; button.id = id;
    button.setAttribute('aria-label', `${active ? 'Ranked' : 'Rank'} by ${text}`);
    button.addEventListener('click', () => { onclick(); render(); el(id).focus(); });
    const th = colHead(button, 'sort-col' + (active ? ' sorted' : ''));
    if (active) th.setAttribute('aria-sort', 'ascending');
    if (help) th.append(help);
    return th;
}
// Tied values share a rank and the next rank skips, as in a competition.
function ranksOf(sorted, key, value) {
    const rank = new Map();
    sorted.forEach((x, i) => rank.set(x[key], i && sorted[i - 1][value] === x[value] ? rank.get(sorted[i - 1][key]) : i + 1));
    return rank;
}

// A named system has one identity color, its --sys-* token, for its icon and its chart marks. The stylesheet binds icons
// by data-system; a chart mark of any other system takes a series color by its position in the data.
const systemSlug = e => e.system.toLowerCase().replace(/\s+/g, '-');
function seriesColor(e) {
    const named = `--sys-${systemSlug(e)}`;
    return token(named) ? `var(${named})` : `var(--series-${data.indexOf(e) % 4 + 1})`;
}
function systemIcon(e) {
    const mark = node('span', 'system-icon', e.system.slice(0, 1));
    mark.setAttribute('data-system', systemSlug(e)); mark.setAttribute('aria-hidden', 'true');
    return mark;
}
// The row header of every board: rank, mark, name, one line of context and the one control that opens the row's details.
// Nothing else in a row is clickable. A board that never ranks passes rank false.
function systemHeader(rank, e, name, meta, open, extra = []) {
    const details = node('button', 'details-button', 'Details'); details.type = 'button';
    details.setAttribute('aria-haspopup', 'dialog'); details.setAttribute('aria-label', `${name} details`);
    details.append(icon(ICON.chevron)); details.addEventListener('click', () => open(details));
    const text = node('span', 'system-text'); text.append(node('span', 'system-name', name), parts(meta, 'system-meta'), ...extra, details);
    const identity = node('span', 'system-identity');
    if (rank !== false) identity.append(node('span', 'rank-number' + (rank === 1 ? ' rank-leader' : ''), rank ? String(rank) : '—'));
    identity.append(systemIcon(e), text);
    return rowHead(identity, 'system-cell');
}

// Measured times on one scale, fastest first and marked; a missing time is named after them, never drawn as a bar.
function resultName(e, name) { const span = node('span', 'result-name'); span.append(systemIcon(e), node('span', '', name)); return span; }
function resultBars(label, timed, failed = [], nameOf = e => e.system) {
    const list = node('ol', 'result-bars'), slowest = timed.length ? timed.at(-1).t : 0; list.setAttribute('aria-label', label);
    for (const {e, t} of timed) {
        const li = node('li'); li.append(resultName(e, nameOf(e)), bar(slowest ? t / slowest : 1, 'bar time-bar'), node('span', 'metric-value', seconds(t)));
        if (t === timed[0].t) li.append(bestMark());
        list.append(li);
    }
    for (const e of failed) { const li = node('li'); li.append(resultName(e, nameOf(e)), node('span'), node('span', 'cell-missing', 'Failed')); list.append(li); }
    return list;
}
// One entry of an explorer's picker: the definition's name with a secondary line under it.
function explorerOption(id, name, sub, ariaLabel, active, onclick) {
    const button = selector('', active, onclick, id); button.classList.add('explorer-option'); button.setAttribute('aria-label', ariaLabel);
    button.append(node('span', 'explorer-option-name', name), node('span', 'explorer-option-id', sub));
    return button;
}
// Records the build could not use, each with its reason. It keeps the viewer's open or closed choice across renders.
function excludedPanel(id, title, items, openByDefault = false) {
    const before = el(id), panel = node('details', 'disclosure excluded-panel'), summary = node('summary', '', title), list = node('ul', 'excluded-list');
    panel.id = id; panel.open = before ? before.open : openByDefault;
    summary.append(node('span', 'count-badge', String(items.length)));
    for (const {label, href, reason} of items) {
        const name = node(href ? 'a' : 'span', '', label); if (href) name.href = href;
        const item = node('li', 'excluded-item'); item.append(name, node('span', 'excluded-reason', reason)); list.append(item);
    }
    const content = node('div', 'disclosure-body'); content.append(list); panel.append(summary, content);
    return panel;
}
