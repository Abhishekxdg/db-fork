/* The microbench: eight timed operations plus load and provisioning, three category scores ranked one at a
   time, the workload heatmap, the trade-off plot, the operation definitions and a system's details. */
if (typeof suite === 'undefined') {
    el('groups').textContent = 'data.js is missing. From the repository root, run: python -m leaderboard.site --phase local';
    throw new Error('data.js is missing');
}
const SHIFT = 0.01;
const NAMES = {time_to_first_query: 'Time to first query', branch_create: 'Branch create', branch_connect: 'Branch connect', read: 'Point read', range_read: 'Range read', insert: 'Insert', update: 'Point update', range_update: 'Range update'};
const DESCRIPTIONS = {
    time_to_first_query: 'Create a child branch, connect to it, and read one row. The timer spans all three steps: how soon can a new branch answer a query?',
    branch_create: 'Create one child branch from the tip of the existing chain. This measures creation only, without connecting or querying.', branch_connect: `Connect to each existing branch in a chain of ${suite.chain_length}, cycling through them for the batch. No new branches are created.`,
    read: 'Look up one row by its primary key and return it.', range_read: `Read a consecutive range of ${suite.range_size} keys in one operation. The batch repeats that range read, not individual point reads.`,
    insert: 'Generate and insert one new row with a unique primary key. Writes run on a dedicated child branch.', update: 'Find one row by its primary key and update its values on a dedicated child branch.', range_update: `Update a consecutive range of ${suite.range_size} keys in one operation, on a dedicated child branch.`,
};
function pick(tries, mode) {
    if (mode === 'first') return tries[0];
    return tries[1] !== null && tries[2] !== null ? Math.min(tries[1], tries[2]) : null;
}
const suiteRows = suite.rows.map((row, i) => ({...row, label: NAMES[row.op] || row.op, title: DESCRIPTIONS[row.op], shift: SHIFT, value: e => pick(e.result[i], state.mode)}));
const loadRow = {op: 'load', label: 'Load', title: 'Load the SQL dump into the newly provisioned database. This is measured once, before the workload trials.', shift: 0, value: e => e.load_time};
const provisionRow = {op: 'provision', label: 'Provisioning', title: 'Create the empty database or hosted service before loading data. Recorded separately and never included in the ranking.', shift: 0, value: e => e.provision_time};
const allRows = [...suiteRows, loadRow, provisionRow];
const VIEWS = {
    branch: {label: 'First query', rows: suiteRows.slice(0, 3), ranked: rows => rows.slice(0, 1)},
    data: {label: 'Reads & writes', rows: suiteRows.slice(3), ranked: rows => {
        const ticked = rows.filter(r => state.ticked.has(r)); return ticked.length ? ticked : rows;
    }},
    load: {label: 'Load', rows: [loadRow, provisionRow], ranked: rows => rows.slice(0, 1)},
};
const SORTS = {branch: 'First query', data: 'Reads & writes', load: 'Load'};
const HOSTING = {no: 'Self-hosted', yes: 'Managed service'};
const tryLabel = (capital = false) => (state.mode === 'first' ? ['first try', 'First try'] : ['best repeat', 'Best repeat'])[+capital];
const FILTERS = {
    hosting: e => state.hosted[e.hosted], system: e => state.system[e.system], machine: e => state.machine[e.machine],
    tags: e => Object.values(state.tag).every(Boolean) || e.tags.some(t => state.tag[t]),
};
const label = e => `${e.system} (${e.machine})`;
const nameOf = e => data.filter(x => x.system === e.system && x.hosted === e.hosted).length > 1 ? label(e) : e.system;

// Filter chips: one checklist per dimension inside a native disclosure, part of the board's filter bar.
// One opens at a time, a click outside or Escape closes it, and a dimension with a single value has no chip.
const CHIPS = {hosted: k => HOSTING[k], system: k => k, machine: k => k, tag: k => k};
function resetFilters() {
    for (const key in CHIPS) for (const value in state[key]) state[key][value] = true;
    render(); el('choice-mode-' + state.mode).focus();
}
function filterOption(id, text, checked, toggle) {
    const box = node('input'); box.type = 'checkbox'; box.id = `filter-${id}`; box.checked = checked;
    box.addEventListener('change', () => { toggle(); render(); el(box.id).focus(); });
    const item = node('label', 'chip-option'); item.append(box, ` ${text}`);
    return item;
}
function renderChips() {
    for (const [key, name] of Object.entries(CHIPS)) {
        const map = state[key], values = Object.keys(map), on = values.filter(v => map[v]), all = on.length === values.length;
        el(`chip-${key}`).hidden = values.length < 2; el(`chip-${key}`).classList.toggle('chip-active', !all);
        el(`value-${key}`).textContent = all ? 'All' : on.length === 1 ? name(on[0]) : on.length ? `${on.length} of ${values.length}` : 'None';
        el(`selectors-${key}`).replaceChildren(filterOption(`${key}-all`, 'All', all, () => { for (const v of values) map[v] = !all; }),
            ...values.map((v, i) => filterOption(`${key}-${i}`, name(v), map[v], () => { map[v] = !map[v]; })));
    }
}
function wireMicro() {
    el('reset-filters').addEventListener('click', resetFilters);
    el('clear-empty-filters').addEventListener('click', resetFilters);
    const chips = Object.keys(CHIPS).map(key => el(`chip-${key}`));
    for (const chip of chips) chip.addEventListener('toggle', () => { if (chip.open) for (const other of chips) if (other !== chip) other.open = false; });
    document.addEventListener('click', event => { for (const chip of chips) if (chip.open && !chip.contains(event.target)) chip.open = false; });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') for (const chip of chips) if (chip.open) { chip.open = false; chip.querySelector('summary').focus(); } });
    bindSelect('sort-metric', value => { state.sort = value in SORTS ? value : 'branch'; });
}
const operationButton = row => definitionButton(`What ${row.label} times`, () => showOperation(row.op));
const scoreButton = () => definitionButton('How the reads & writes score works', () => {
    el('methodology').open = true; el('methodology').scrollIntoView({block: 'start'}); el('methodology-summary').focus({preventScroll: true});
});

// Scores: each workload's time against the fastest in its group, (time + shift) / (fastest + shift), and a
// category score is the geometric mean of its counted workloads' ratios.
function bestOf(entries, row) {
    const values = entries.map(row.value).filter(v => v !== null);
    return values.length ? Math.min(...values) : null;
}
const ratioOf = (row, value, best) => value === null || best === null ? null : (value + row.shift) / (best + row.shift);
function scoresFor(view, measured, best) {
    const ranked = view.ranked(view.rows);
    const usable = ranked.filter(r => best.get(r) !== null), dropped = ranked.filter(r => best.get(r) === null), scored = [], incomplete = [];
    for (const e of usable.length ? measured : []) {
        const missing = usable.filter(r => r.value(e) === null);
        if (missing.length) { incomplete.push({e, missing}); continue; }
        const logs = usable.map(r => Math.log(ratioOf(r, r.value(e), best.get(r))));
        scored.push({e, score: Math.exp(logs.reduce((a, b) => a + b, 0) / logs.length)});
    }
    scored.sort((a, b) => a.score - b.score);
    return {usable, dropped, scored, incomplete};
}
const missingText = (result, e) => { const hit = result.incomplete.find(i => i.e === e); return hit ? `missing ${hit.missing.map(r => r.label).join(', ')}` : result.usable.length ? 'not scored' : 'no measurements'; };
// Everything a microbench group needs to rank, explain and chart its systems.
function microContext(measured) {
    const best = new Map(allRows.map(r => [r, bestOf(measured, r)]));
    const scores = Object.fromEntries(Object.keys(VIEWS).map(k => [k, scoresFor(VIEWS[k], measured, best)]));
    const ranks = Object.fromEntries(Object.keys(VIEWS).map(k => [k, ranksOf(scores[k].scored, 'e', 'score')]));
    return {measured, best, scores, ranks};
}

// One table per comparable hosting group: three category scores side by side, ranked by one chosen score only.
function summaryTable(title, ctx) {
    const {measured, scores, ranks} = ctx, rank = ranks[state.sort], by = scores[state.sort];
    const order = [...by.scored.map(s => s.e), ...measured.filter(e => !rank.has(e))];
    const {table, body} = dataTable('board summary', `${title}: systems ranked by ${SORTS[state.sort]} score, lower is better`, [colHead('System', 'system-col'),
        ...Object.entries(SORTS).map(([key, text]) => sortHeader(text, key === state.sort, () => { state.sort = key; }, `sort-${key}-${measured[0].hosted}`, key === 'data' ? scoreButton() : operationButton(VIEWS[key].rows[0])))]);
    for (const e of order) {
        const tr = body.insertRow();
        const th = systemHeader(rank.get(e), e, nameOf(e), [e.machine, e.date], opener => showRun(e, opener, ctx), e.mock ? [flag('Mock data', e.basis || 'Illustrative numbers, not a measurement')] : []); tr.append(th);
        if (!rank.has(e)) th.title = `Not ranked by ${SORTS[state.sort]}: ${missingText(by, e)}`;
        for (const key of Object.keys(SORTS)) {
            const td = tr.insertCell(); td.className = 'score-cell' + (key === state.sort ? ' sorted' : '');
            const hit = scores[key].scored.find(s => s.e === e);
            if (!hit) { td.append(node('span', 'cell-absent', '—')); td.title = `No ${SORTS[key]} score: ${missingText(scores[key], e)}`; continue; }
            const usable = scores[key].usable, single = usable.length === 1 ? usable[0].value(e) : null;  // only a one-workload score has one elapsed time
            const line = node('span', 'score-line'); line.append(node('span', 'score-value', `×${hit.score.toFixed(2)}`), node('span', 'score-time', single === null ? 'composite' : seconds(single)));
            td.append(line, bar(1 / hit.score, 'bar score-bar'));
            td.title = `Unrounded ${SORTS[key]} score: ${hit.score}${single === null ? ' · composite of several workloads, no single elapsed time' : ''}`;
        }
    }
    return table;
}
function heatmap(ctx) {
    const {measured, best} = ctx, ranked = Object.values(VIEWS).flatMap(view => view.ranked(view.rows));
    const {table, body} = dataTable('heatmap', `${HOSTING[measured[0].hosted]} measured workload timings`, ['Workload', ...measured.map(e => {
        const identity = node('span', 'system-identity'); identity.append(systemIcon(e), node('span', 'system-name', measured.filter(x => x.system === e.system).length > 1 ? label(e) : e.system));
        const th = colHead(identity); th.title = `${e.machine} · ${e.date}`;
        return th;
    })]);
    for (const row of allRows) {
        const tr = body.insertRow(), name = node('div', 'workload-name');
        if (row === suiteRows[3] || row === loadRow) tr.className = 'workload-divider';
        if (VIEWS.data.rows.includes(row)) {
            const box = node('input'); box.type = 'checkbox'; box.checked = state.ticked.has(row); box.id = `row-${suiteRows.indexOf(row)}-${measured[0].hosted}`;
            box.addEventListener('change', () => { box.checked ? state.ticked.add(row) : state.ticked.delete(row); render(); el(box.id).focus(); });
            const wrap = node('label'); wrap.append(box, ` ${row.label}`); name.append(wrap);
        } else name.append(node('span', '', row.label));
        name.append(operationButton(row));
        tr.append(rowHead([name, node('span', 'row-meta', (row.batch ? `×${fmt(row.batch)}` : 'Setup') + (ranked.includes(row) ? '' : ' · not in score'))], 'row-label'));
        for (const e of measured) {
            const value = row.value(e), td = tr.insertCell(), fastest = value !== null && value === best.get(row);
            td.className = value === null ? 'cell-missing' : fastest ? 'cell-best' : '';
            if (value !== null) { shade(td, best.get(row), value); td.title = `${value}s${fastest ? ' · Fastest in this row' : ''}`; }
            if (fastest) td.append(bestMark('Fastest in this row'));
            td.append(node('span', 'metric-value', seconds(value)));
        }
    }
    return table;
}
function chartPoint(e, x, y, description, attributes, ctx) {
    const point = svgNode('g', {class: 'chart-point', tabindex: 0, role: 'button', 'aria-haspopup': 'dialog', 'aria-label': `${label(e)}: ${description}. View run details.`, ...attributes});
    point.append(svgNode('title', {}, `${label(e)} · ${description}`), svgNode('circle', {cx: x, cy: y, r: 13, class: 'chart-hit'}), svgNode('circle', {cx: x, cy: y, r: 5, class: 'chart-dot', style: `fill: ${seriesColor(e)}`}));
    point.addEventListener('click', () => showRun(e, point, ctx));
    point.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); showRun(e, point, ctx); } });
    return point;
}
function chartLimit(value) {
    const power = 10 ** Math.floor(Math.log10(value || 1));
    return [1, 2, 5, 10].find(factor => factor * power >= value) * power;
}
// Category trade-offs: the table's own scores on two axes, lower-left is faster on both.
function tradeoffChart(ctx) {
    const {measured, scores} = ctx, card = node('figure', 'panel chart-panel');
    const branch = new Map(scores.branch.scored.map(({e, score}) => [e, score]));
    const points = scores.data.scored.filter(({e, score}) => branch.has(e) && Number.isFinite(score)).map(({e, score}) => ({e, x: branch.get(e), y: score}));
    card.append(node('h3', '', 'Trade-offs'), node('p', 'panel-note', 'First-query score against reads & writes score. Lower-left is faster on both.'));
    if (!points.length) { card.append(node('p', 'chart-empty', 'Complete first-query and read/write scores are needed for this comparison.')); return card; }
    const maximum = chartLimit(Math.max(...points.flatMap(p => [p.x, p.y])) * 1.05);
    const x = value => 72 + (value - 1) / (maximum - 1 || 1) * 320, y = value => 198 - (value - 1) / (maximum - 1 || 1) * 156;
    const svg = svgNode('svg', {class: 'benchmark-chart', viewBox: '0 0 440 270', role: 'group', 'aria-label': 'Performance trade-offs. Category scores; lower is better on both axes.'});
    svg.append(svgNode('text', {x: 58, y: 15, class: 'chart-axis-label'}, 'Reads & writes score'));
    for (const tick of [1, (maximum + 1) / 2, maximum]) {
        svg.append(svgNode('line', {x1: x(tick), x2: x(tick), y1: 28, y2: 212, class: 'chart-grid'}), svgNode('line', {x1: 58, x2: 410, y1: y(tick), y2: y(tick), class: 'chart-grid'}),
            svgNode('text', {x: x(tick), y: 235, 'text-anchor': 'middle', class: 'chart-tick'}, `×${Number(tick.toFixed(2))}`), svgNode('text', {x: 49, y: y(tick) + 4, 'text-anchor': 'end', class: 'chart-tick'}, `×${Number(tick.toFixed(2))}`));
    }
    svg.append(svgNode('text', {x: 234, y: 262, 'text-anchor': 'middle', class: 'chart-axis-label'}, 'First-query score'));
    for (const p of points) svg.append(chartPoint(p.e, x(p.x), y(p.y), `first-query score ×${p.x.toFixed(3)}, reads & writes score ×${p.y.toFixed(3)}`, {'data-x-score': p.x, 'data-y-score': p.y}, ctx));
    const legend = node('div', 'chart-legend');
    for (const p of points) { const item = node('span', 'chart-key'), dot = node('span', 'chart-swatch'); dot.style.backgroundColor = seriesColor(p.e); dot.setAttribute('aria-hidden', 'true'); item.append(dot, node('span', '', nameOf(p.e))); legend.append(item); }
    card.append(svg, legend, node('p', 'panel-note', `${tryLabel(true)} · ×1 is the fastest.${points.length < measured.length ? ' Incomplete results are omitted.' : ''}`));
    return card;
}
// One board group inside the card: its heading, the ranking table, and a note for every score that could not be computed.
function boardGroup(title, ctx) {
    const section = node('section', 'board-group'), heading = node('div', 'group-heading');
    heading.append(node('h2', '', title), node('span', 'group-count', `${plural(ctx.measured.length, 'system')} · ranked by ${SORTS[state.sort]} · lower is better`));
    section.append(heading, scrolling(summaryTable(title, ctx)));
    const notes = Object.entries(ctx.scores).flatMap(([key, result]) => [
        ...result.dropped.length ? [`${SORTS[key]}: no measurements for ${result.dropped.map(r => r.label).join(', ')}.`] : [],
        ...result.incomplete.map(({e, missing}) => `${nameOf(e)} has no ${SORTS[key]} score: missing ${missing.map(r => r.label).join(', ')}.`)]);
    if (notes.length) section.append(tagList(notes, 'table-notes'));
    return section;
}
// The evidence behind a group's ranking, below the card: the workload heatmap beside the trade-off plot.
function breakdown(title, ctx, named) {
    const section = node('section', 'breakdown-group');
    if (named) section.append(node('h2', 'breakdown-title', title));
    const grid = node('div', 'breakdown'), matrix = node('figure', 'panel heatmap-panel'), caption = node('div', 'panel-head');
    const key = node('span', 'heat-key'); key.setAttribute('role', 'img'); key.setAttribute('aria-label', 'Heatmap: darker mint is closer to the fastest time within each row');
    key.append(node('span', '', 'Slower'), node('span', 'heat-ramp'), node('span', '', 'Faster'));
    caption.append(node('h3', '', 'Workload breakdown'), key);
    matrix.append(caption, node('p', 'panel-note', 'Batch totals in seconds, shaded within each row. Tick read/write workloads to choose what the reads & writes score includes.'), scrolling(heatmap(ctx), 'Scroll sideways to compare systems.'));
    grid.append(matrix, tradeoffChart(ctx)); section.append(grid);
    return section;
}

// A system's details: why it sits where it does, every try, the environment, then provenance behind a disclosure.
function showRun(e, opener, ctx) {
    const body = openDetails(e.system, `${HOSTING[e.hosted]} · ${e.machine} · ${e.date} · ${e.version ? `v${e.version}` : 'version not recorded'}${e.mock ? ' · mock data' : ''}`, e.source, opener);
    if (e.mock) body.append(node('p', 'notice', e.basis || 'Illustrative data, not a measured run.'));
    whyMicro(body, e, ctx);
    detailsSection(body, 'Every try', `Batch totals in seconds. Best repeat is the faster of tries 2 and 3; both must succeed. The shaded column is the leaderboard's current selection.`);
    const selected = state.mode === 'first' ? 0 : suite.tries;
    const {table, body: rows} = dataTable('detail-table run-timings', `${e.system} timings for all attempts`,
        ['Workload', ...Array.from({length: suite.tries}, (_, i) => `Try ${i + 1}`), 'Best repeat'].map((text, i) => colHead(text, i - 1 === selected ? 'selected-attempt' : '')));
    suiteRows.forEach((row, i) => {
        const tr = rows.insertRow(), name = node('div', 'workload-name'); name.append(node('span', '', row.label));
        tr.append(rowHead([name, node('span', 'row-meta', `×${fmt(row.batch)}`)]));
        [...e.result[i], pick(e.result[i], 'best')].forEach((value, index) => {
            const td = tr.insertCell(); td.className = index === selected ? 'selected-attempt' : '';
            td.append(node('span', value === null ? 'cell-missing' : 'metric-value', seconds(value)));
        });
    });
    body.append(scrolling(table));
    detailsSection(body, 'Environment');
    body.append(metaGrid([['Machine', e.machine], ['Database version', e.version], ['Suite', e.suite], ['Run ID', e.run_id], ['Configuration', e.tuned === 'yes' ? 'Tuned' : e.tuned === 'no' ? 'Default' : null],
        ['Load', seconds(e.load_time)], ['Provisioning', seconds(e.provision_time)], ['Region', e.region], ['Client location', e.client_location], ['Round-trip baseline', e.rtt_ms === undefined ? null : `${e.rtt_ms} ms`]]));
    if (e.tags.length) body.append(tagList(e.tags));
    if (!e.mock) body.append(disclosure('Reproducibility details', metaGrid([['Git commit', e.commit], ['Seed', e.seed], ['Python', e.python], ['Suite SHA-256', e.suite_sha256], ['Dataset SHA-256', e.dataset_sha256], ['Dependency lock SHA-256', e.lock_sha256], ...Object.entries(e.service || {})])));
    showDetails();
}
// Why a measured system sits where it does: each category score, then every workload ratio that feeds it.
function whyMicro(body, e, ctx) {
    const {measured, best, scores, ranks} = ctx, ranked = scores[state.sort].scored.length, hit = scores[state.sort].scored.find(s => s.e === e);
    const fastest = scores[state.sort].scored[0];
    const head = detailsSection(body, 'Why this position');
    head.append(node('p', 'verdict', hit ? `${ordinal(ranks[state.sort].get(e))} of ${ranked} by ${SORTS[state.sort]}, score ×${hit.score.toFixed(2)}${fastest.e !== e ? `; the leader, ${nameOf(fastest.e)}, has ×${fastest.score.toFixed(2)}` : ''}. ×1 is the fastest time in every counted workload.`
        : `Not ranked by ${SORTS[state.sort]}: ${missingText(scores[state.sort], e)}.`));
    const {table, body: tbody} = dataTable('detail-table why-table', `${e.system}: each workload against the fastest in its group`, ['Workload', 'This system', 'Fastest', 'Ratio']);
    for (const [key, text] of Object.entries(SORTS)) {
        const view = VIEWS[key], inScore = view.ranked(view.rows), score = scores[key].scored.find(s => s.e === e), r = ranks[key].get(e);
        const group = tbody.insertRow(); group.className = 'why-category' + (key === state.sort ? ' sorted' : '');
        group.append(rowHead(text));
        const cell = group.insertCell(); cell.colSpan = 3;
        cell.append(node('span', 'score-value', score ? `×${score.score.toFixed(2)}` : '—'), node('span', 'row-meta', score ? `${ordinal(r)} of ${scores[key].scored.length}` : missingText(scores[key], e)));
        for (const row of view.rows) {
            const value = row.value(e), fast = best.get(row), ratio = ratioOf(row, value, fast), tr = tbody.insertRow();
            tr.append(rowHead([node('span', '', row.label), node('span', 'row-meta', inScore.includes(row) ? '' : 'not in score')], 'row-label'));
            tr.insertCell().append(node('span', value === null ? 'cell-missing' : 'metric-value', seconds(value)));
            tr.insertCell().append(node('span', 'metric-value muted', fast === null ? '—' : seconds(fast)));
            const r2 = tr.insertCell(); r2.className = 'ratio-cell';
            if (ratio === null) r2.append(node('span', 'cell-absent', '—'));
            else { r2.append(node('span', 'ratio-value', `×${ratio.toFixed(2)}`), bar(1 / ratio, 'bar ratio-bar')); }
        }
    }
    body.append(scrolling(table), node('p', 'fine', `${plural(measured.length, 'system')} in this group · ${tryLabel()} · bars show speed against the fastest, full length is the fastest.`));
}


// Operation definitions: each operation drawn in the workflows' language, what is timed, and every system's batch time.
const batchOf = op => fmt(allRows.find(r => r.op === op).batch);
const seedRows = Object.values(suite.dataset.row_counts).reduce((a, b) => a + b, 0), seedTables = Object.keys(suite.dataset.row_counts).length;
const seeded = lane => [L(lane, .04, .12, 0, .15), ...run(lane, [[.12, .15], [.24, .4]])];
const CHAIN_LANES = [['main', 'main', 'seeded', 'main'], ['b1', 'branch 1', 'chain', 'b'], ['b2', 'branch 2', 'chain', 'b'], ['b3', 'branch 3', 'chain tip', 'b']];
const chain = () => [...seeded('main'), C('main', .24, 'b1', .36, .45, .75), H('b1', .36, .75), C('b1', .36, 'b2', .48, .75, 1.05), H('b2', .48, 1.05), C('b2', .48, 'b3', .60, 1.05, 1.35), H('b3', .60, 1.35)];
const dataScene = (op, lane, name, color, mark, text) => ({lanes: [['main', 'main', 'seeded', 'main'], [lane, name, 'own child branch', color]], items: [
    ...seeded('main'), C('main', .24, lane, .40, .6, 1.0), H(lane, .40, 1.0), mark,
    T(lane, .96, 1.7, text, true, 'right'), T(lane, .96, 2.4, `${batchOf(op)} per batch, ${suite.tries} tries`, false, 'right')]});
const OP_SCENES = {
    time_to_first_query: {lanes: [['main', 'main', 'seeded', 'main'], ['child', 'child', 'new branch', 'a']], items: [
        ...seeded('main'), R('main', 'main', [[.4, .24]]), C('main', .24, 'child', .42, 1.0, 1.4), H('child', .42, 1.4), R('child', 'child', [[1.4, .42]]), Q('child', .42, 2.4),
        T('main', .96, 1.5, 'create a branch from the tip', false, 'right'), T('child', .96, 2.4, 'connect, then read 1 row', true, 'right'), T('child', .96, 3.2, 'one timer spans all three steps', false, 'right')]},
    branch_create: {lanes: [...CHAIN_LANES, ['new', 'child', 'timed', 'a']], items: [
        ...chain(), T('b1', .96, 1.4, 'the chain exists before timing starts', true, 'right'),
        C('b3', .60, 'new', .74, 2.0, 2.5), H('new', .74, 2.5), R('child', 'new', [[2.5, .74]]), T('new', .96, 2.7, 'create one child from the chain tip', true, 'right')]},
    branch_connect: {lanes: CHAIN_LANES, items: [
        ...chain(), R('session', 'main', [[1.6, .24], [2.1, .36, 'b1'], [2.6, .48, 'b2'], [3.1, .60, 'b3'], [3.6, .24, 'main']]),
        T('main', .96, 1.7, 'connect to each branch in turn', false, 'right'), T('b3', .96, 3.2, `${batchOf('branch_connect')} connections per batch, no new branches`, false, 'right')]},
    read: dataScene('read', 'rb', 'read branch', 'b', Q('rb', .40, 1.6), 'look up 1 row by its primary key'),
    range_read: dataScene('range_read', 'rb', 'read branch', 'b', Q('rb', .40, 1.6, 3), `read ${suite.range_size} consecutive keys in one statement`),
    insert: dataScene('insert', 'wb', 'write branch', 'a', W('wb', .40, 1.6), 'insert 1 new row with a unique key'),
    update: dataScene('update', 'wb', 'write branch', 'a', W('wb', .40, 1.6), 'update 1 row by its primary key'),
    range_update: dataScene('range_update', 'wb', 'write branch', 'a', W('wb', .40, 1.6, 3), `update ${suite.range_size} consecutive keys in one statement`),
    load: {lanes: [['db', 'database', 'new', 'main']], items: [
        H('db', .08, .2), T('db', .08, .5, 'empty', true), L('db', .08, .62, .7, 2.3), W('db', .2, 1.0), W('db', .32, 1.4), W('db', .44, 1.8), D('db', .62, 2.4), R('main', 'db', [[2.5, .62]]),
        T('db', .96, 2.6, `load ${fmt(seedRows)} rows into ${seedTables} tables, timed once`, true, 'right')]},
    provision: {lanes: [['db', 'database', 'new', 'main']], items: [
        H('db', .2, .8), R('database', 'db', [[1.0, .2]]), T('db', .96, 1.4, 'create the empty database or service', true, 'right'), T('db', .96, 2.2, 'recorded separately, never ranked', false, 'right')]},
};
const OP_GROUPS = [['Branching', VIEWS.branch.rows], ['Reads & writes', VIEWS.data.rows], ['Setup', VIEWS.load.rows]];
const opFacts = row => row.batch ? [`${plural(row.batch, 'operation')} per batch`, `${suite.tries} tries; the board shows the ${tryLabel()}`, 'one batch total, not a per-operation average', ...VIEWS.data.rows.includes(row) ? ['on its own child branch'] : []]
    : ['measured once, before the tries', row === provisionRow ? 'never part of a score' : 'provisioning excluded'];
// The measured side of an operation: each system's batch time in each hosting group, fastest first; a failed batch is named, never drawn.
function operationResults(row, groups) {
    const blocks = groups.map(({title, ctx}) => {
        const values = ctx.measured.map(e => ({e, t: row.value(e)})), timed = values.filter(x => x.t !== null).sort((a, b) => a.t - b.t);
        const block = node('div', 'result-group'); if (groups.length > 1) block.append(node('h5', '', title));
        block.append(resultBars(`${row.label}: batch time per system, ${title}`, timed, values.filter(x => x.t === null).map(x => x.e), nameOf));
        return block;
    });
    el('operation-results').replaceChildren(...blocks.length ? [...blocks, node('p', 'fine', `Lower is faster · ${tryLabel()}`)] : [node('p', 'fine', 'No results yet.')]);
}
// A score column's or workload's "?" opens that operation's definition below the board and moves focus to it.
function showOperation(op) {
    state.op = op; writeHash(operationHash()); render();
    el('dataset').scrollIntoView({block: 'start'}); el(`operation-${op}`).focus({preventScroll: true});
}
function renderOperations(groups) {
    if (!allRows.some(r => r.op === state.op)) state.op = allRows[0].op;
    el('selectors-operation').replaceChildren(...OP_GROUPS.flatMap(([title, rows]) => [node('h3', 'explorer-group', title),
        ...rows.map(row => explorerOption(`operation-${row.op}`, row.label, row.batch ? `×${fmt(row.batch)}` : 'once', `${row.label} (${row.batch ? `${fmt(row.batch)} per batch` : 'once'})`,
            state.op === row.op, () => { state.op = row.op; writeHash(operationHash()); }))]));
    const row = allRows.find(r => r.op === state.op);
    el('operation-name').textContent = row.label; el('operation-id').textContent = OP_GROUPS.find(([, rows]) => rows.includes(row))[0];
    el('operation-caption').textContent = row.title;
    el('operation-facts').replaceChildren(...opFacts(row).map(text => node('li', '', text)));
    operationResults(row, groups);
    const key = `op:${row.op}`;
    if (scene.key !== key) mountScene(key, OP_SCENES[row.op], `${row.label}: ${row.title}`, el('operation-scene'), () => node('p', 'diagram-missing', 'This diagram needs Canvas; the description above says what is timed.'));
}
// Facts about the seeded dataset; they never change while the page is open.
function renderDataset() {
    const counts = Object.entries(suite.dataset.row_counts);
    el('dataset-size').textContent = `CH-benCHmark seed · ${fmt(counts.reduce((total, [, count]) => total + count, 0))} initial rows · ${counts.length} tables`;
    el('dataset-target').textContent = `${suite.table}, ${plural(suite.dataset.row_counts[suite.table], 'initial row')}; each data workload gets its own child branch`;
    el('workload-protocol').textContent = `${suite.tries} tries per workload · ${suite.chain_length}-branch chain · batch totals, not per-operation averages`;
    for (const [name, count] of counts) {
        const tr = node('tr', name === suite.table ? 'dataset-target' : ''), th = rowHead(name);
        if (name === suite.table) th.append(node('span', 'dataset-target-label', 'Timed table'));
        tr.append(th, node('td', '', fmt(count))); el('dataset-tables').append(tr);
    }
    el('dataset-path').textContent = suite.dataset.path;
    el('dataset-hash').textContent = suite.dataset.sha256;
    el('dataset-version').textContent = `${suite.suite} / ${suite.seed}`;
    el('suite-badge').textContent = suite.suite;
    el('method').textContent = `seed ${suite.seed} · ${suite.chain_length}-branch chain · isolated writes`;
    const latest = data.filter(e => !e.mock).map(e => e.date).sort().at(-1);  // a mock's date never claims a fresh run
    el('updated').textContent = latest || 'No published runs'; el('updated').title = 'Latest measured run';
}
function renderMicro() {
    renderChips();
    choice(el('selectors-mode'), {first: 'First try', best: 'Best repeat'}, 'mode');
    el('sort-metric').replaceChildren(...Object.entries(SORTS).map(([k, text]) => option(k, `${text} score`))); el('sort-metric').value = state.sort;
    const shown = data.filter(e => Object.values(FILTERS).every(f => f(e))), filtered = Object.keys(CHIPS).some(k => Object.values(state[k]).some(v => !v));
    el('reset-filters').hidden = !filtered;
    el('shown').textContent = `${plural(shown.length, 'system')}${filtered ? ' · filtered' : ''}`;
    el('empty-state').hidden = shown.length > 0;
    el('empty-title').textContent = data.length ? 'No matches' : 'No published results yet';
    el('empty-description').textContent = data.length ? 'Adjust or reset the filters.' : `Microbenchmark results appear here once they are published.${excluded.length ? ` ${plural(excluded.length, 'run')} without a result ${excluded.length === 1 ? 'is' : 'are'} listed below.` : ''}`;
    el('clear-empty-filters').hidden = !filtered;
    const groups = Object.entries(HOSTING).filter(([hosted]) => shown.some(e => e.hosted === hosted))
        .map(([hosted, title]) => ({title, ctx: microContext(shown.filter(e => e.hosted === hosted))}));
    el('groups').replaceChildren(...groups.map(g => boardGroup(g.title, g.ctx)));
    el('breakdowns').replaceChildren(...groups.map(g => breakdown(g.title, g.ctx, groups.length > 1)));
    el('excluded').replaceChildren(...excluded.length ? [excludedPanel('excluded-panel', PROD ? 'Runs without a result' : 'Excluded results',
        excluded.map(x => ({label: `${x.system} / ${x.machine}`, href: x.source, reason: x.reason})), !data.length)] : []);  // with no result at all, the reasons are the news
    renderOperations(groups);
}
