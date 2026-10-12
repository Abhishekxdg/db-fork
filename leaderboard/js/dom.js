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

const ICON = {check: ['M3.5 8.4l3 3 6-6.6'], chevron: ['M6 3.5L10.5 8 6 12.5'], sortable: ['M5 6.5L8 3.5l3 3', 'M5 9.5l3 3 3-3'], up: ['M8 12.5v-9', 'M4.5 7L8 3.5 11.5 7'], down: ['M8 3.5v9', 'M4.5 9L8 12.5 11.5 9']};
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
// Every bar on the page means the same thing: its length is a time against the slowest compared time, so a longer bar
// is slower, and the tick marks the fastest. With nothing to compare there is no bar.
function timeBar(time, slowest, fastest) {
    const track = bar(time / slowest, 'bar time-bar'), tick = node('span', 'bar-tick');
    tick.style.left = `${(fastest / slowest * 100).toFixed(1)}%`; track.append(tick);
    return track;
}
// Meta lines wrap between items, never inside one: a date or a machine name stays whole, and a line never ends on a separator.
function parts(items, className = '') {
    const line = node('span', className);
    items.filter(Boolean).forEach((text, i) => { if (i) line.append(' '); line.append(node('span', 'nowrap', i ? `· ${text}` : text)); });
    return line;
}
// Heat: how close a time is to the fastest in its row or column, painted with the --heat token at that strength. Only a
// row or column with at least two times is a comparison, so only there does a cell get a background or a fastest mark.
const compared = times => times.filter(t => t !== null).length >= 2;
function shade(cell, fastest, value) {
    cell.classList.add('heat');
    cell.style.setProperty('--strength', (0.03 + (value === 0 ? 1 : fastest / value) * 0.1).toFixed(3));
}
// The key to the shading, shown beside every table or panel that shades.
function heatKey(label) {
    const key = node('span', 'heat-key'); key.setAttribute('role', 'img'); key.setAttribute('aria-label', label);
    key.append(node('span', '', 'Slower'), node('span', 'heat-ramp'), node('span', '', 'Faster'));
    return key;
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
    const box = node('div', 'scroll'); box.tabIndex = -1; box.setAttribute('role', 'region');
    box.setAttribute('aria-label', table.getAttribute('aria-label')); box.append(table);
    const hint = node('p', 'scroll-hint', hintText); hint.hidden = true; scrollers.push([box, hint]);
    const wrap = node('div', 'scroll-wrap'); wrap.append(box, hint);
    return wrap;
}
// Only a region that actually scrolls is a tab stop, so a keyboard can scroll it without visiting every table that fits.
// A region left behind by a closed dialog's body is dropped rather than kept alive.
function syncScrollHints() {  // every width is read before any hint is written, so the check costs one layout
    scrollers = scrollers.filter(([box]) => box.isConnected !== false);
    const overflowing = scrollers.map(([box]) => box.scrollWidth > box.clientWidth);
    scrollers.forEach(([box, hint], i) => { hint.hidden = !overflowing[i]; box.tabIndex = overflowing[i] ? 0 : -1; });
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
// A sortable column, as in any data table: the whole header sorts, fastest first; sorting the sorted column again flips
// the order. A neutral icon marks every sortable header until it is the sorted one. The label stays within two lines.
function sortHeader(text, active, descending, sort, id) {
    const button = node('button', 'sort-button'); button.type = 'button'; button.id = id; button.title = text;
    button.append(node('span', 'sort-label', text), icon(active ? (descending ? ICON.down : ICON.up) : ICON.sortable, 'icon sort-icon'));
    button.setAttribute('aria-label', active ? `${text}, sorted ${descending ? 'slowest' : 'fastest'} first; sort ${descending ? 'fastest' : 'slowest'} first` : `Sort by ${text}, fastest first`);
    button.addEventListener('click', () => { sort(active && !descending); render(); el(id).focus(); });
    const th = colHead(button, 'sort-col' + (active ? ' sorted' : ''));
    if (active) th.setAttribute('aria-sort', descending ? 'descending' : 'ascending');
    return th;
}
// The rows a sort shows: those with a value in the sorted order, either way round, then those without one.
const sortedRows = (ranked, rest, descending) => [...(descending ? [...ranked].reverse() : ranked), ...rest];
// Tied values share a rank and the next rank skips, as in a competition.
function ranksOf(sorted, key, value) {
    const rank = new Map();
    sorted.forEach((x, i) => rank.set(x[key], i && sorted[i - 1][value] === x[value] ? rank.get(sorted[i - 1][key]) : i + 1));
    return rank;
}

// A named system has one identity color, its --sys-* token, for its icon and its chart marks. The stylesheet binds icons
// by data-system; a chart mark of any other system takes a series color by its position in the data.
const systemSlug = e => e.system.toLowerCase().replace(/\s+/g, '-');
const slug = (...words) => words.join('-').toLowerCase().replace(/[^a-z0-9]+/g, '-');  // a stable id from a row's identity
function seriesColor(e) {
    const named = `--sys-${systemSlug(e)}`;
    return token(named) ? `var(${named})` : `var(--series-${data.indexOf(e) % 4 + 1})`;
}
function systemIcon(e) {
    const mark = node('span', 'system-icon', e.system.slice(0, 1));
    mark.setAttribute('data-system', systemSlug(e)); mark.setAttribute('aria-hidden', 'true');
    return mark;
}
// The row header of every board: rank, mark, name and one line of context. A board that never ranks passes rank false.
// The rank badge is drawn for the eye; a screen reader hears "Rank 2" or "Not ranked" before the name instead.
function systemHeader(rank, e, name, meta) {
    const text = node('span', 'system-text'), identity = node('span', 'system-identity');
    if (rank !== false) {
        const badge = node('span', 'rank-number' + (rank === 1 ? ' rank-leader' : ''), rank ? String(rank) : '—'); badge.setAttribute('aria-hidden', 'true');
        identity.append(badge); text.append(node('span', 'sr-only', rank ? `Rank ${rank}, ` : 'Not ranked, '));
    }
    text.append(node('span', 'system-name', name), parts(meta, 'system-meta'));
    identity.append(systemIcon(e), text);
    return rowHead(identity, 'system-cell');
}

// The row's one control, in its last column: it opens that row's details. Nothing else in a row is clickable.
function detailsButton(name, open, id) {
    const button = node('button', 'details-button', 'Details'); button.type = 'button'; button.id = id;
    button.setAttribute('aria-haspopup', 'dialog'); button.setAttribute('aria-label', `${name} details`);
    button.append(icon(ICON.chevron)); button.addEventListener('click', () => open(button));
    return button;
}
// Measured times on one scale, fastest first; the fastest is marked only when there is another time to beat. A system
// without a time is named after them with its outcome, never drawn as a bar.
function resultName(e, name) { const span = node('span', 'result-name'); span.append(systemIcon(e), node('span', '', name)); return span; }
function resultBars(label, timed, missing = [], nameOf = e => e.system) {
    const list = node('ol', 'result-bars'), slowest = timed.length ? timed.at(-1).t : 0, comparison = timed.length >= 2; list.setAttribute('aria-label', label);
    for (const {e, t} of timed) {
        const li = node('li'); li.append(resultName(e, nameOf(e)), comparison && slowest ? timeBar(t, slowest, timed[0].t) : node('span'), node('span', 'metric-value', seconds(t)));
        if (comparison && t === timed[0].t) li.append(bestMark());
        list.append(li);
    }
    for (const {e, word} of missing) { const li = node('li'); li.append(resultName(e, nameOf(e)), node('span'), node('span', 'cell-missing', word)); list.append(li); }
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
