// Run from the repository root: node leaderboard/test_page.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Only the DOM operations used by the page; execute its real script unchanged.
class Element {
    constructor(tag, document) {
        this.tag = tag; this.ownerDocument = document; this.children = []; this.style = {setProperty(name, value) { this[name] = String(value); }};
        this.attributes = {}; this.listeners = {}; this.hidden = false;
    }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    set textContent(text) { this.children = [String(text)]; }
    get textContent() { return this.children.map(c => typeof c === 'string' ? c : c.textContent).join(''); }
    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === 'id') this.id = value;
        if (name === 'class') this.className = value;
        if (name === 'hidden') this.hidden = true;
    }
    getAttribute(name) { return this.attributes[name] ?? null; }
    removeAttribute(name) { delete this.attributes[name]; }
    matches(selector) { return selector === ':popover-open' && Boolean(this.popoverOpen); }
    showPopover() { this.popoverOpen = true; }
    hidePopover() { this.popoverOpen = false; }
    showModal() { this.open = true; }
    close() { this.open = false; for (const callback of this.listeners.close ?? []) callback(); }
    closest() { return null; }
    getBoundingClientRect() { return {left: 20, right: 44, top: 20, bottom: 44, width: 344, height: 310}; }
    addEventListener(type, callback) { (this.listeners[type] ??= []).push(callback); }
    click() { for (const callback of this.listeners.click ?? []) callback(); }
    change() { for (const callback of this.listeners.change ?? []) callback(); }
    focus() { this.ownerDocument.activeElement = this; }
    child(tag) { const node = new Element(tag, this.ownerDocument); this.append(node); return node; }
    insertRow() { return this.child('tr'); }
    insertCell() { return this.child('td'); }
    createTHead() { return this.child('thead'); }
    createTBody() { return this.child('tbody'); }
}
const descendants = node => [node, ...node.children.flatMap(c => typeof c === 'string' ? [] : descendants(c))];
const classNodes = (root, name) => descendants(root).filter(e => (e.className ?? '').split(' ').includes(name));
const panelScores = page => page.byClass('score-panel').map(panel => classNodes(panel, 'score-value').map(e => e.textContent));
const heatmapColors = page => page.byClass('details').flatMap(table => descendants(table)
    .filter(e => e.tag === 'td' && e.className !== 'batch-column').map(e => e.style.backgroundColor));
const suite = JSON.parse(fs.readFileSync(`${__dirname}/suite.json`, 'utf8'));
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
assert.match(html.match(/<main\b[^>]*>\s*<section\b([^>]*)>/)?.[1] ?? '', /\bid="leaderboard"/,
    'The leaderboard is the first main section, not a marketing hero');
assert.match(html, /<h1\b[^>]*\bid="leaderboard-title"[^>]*>[^<]+<\/h1>/);
assert.doesNotMatch(html, /branch-visual|<section\b[^>]*class="hero\b/);
const entry = (system, value, tags, mock = false) => ({
    system, machine: 'test', hosted: 'no', date: mock ? '2035-01-01' : '2026-09-30', source: 'test.json', tags, mock,
    basis: 'Illustrative only', result: suite.rows.map(() => [value, value, value]),
    load_time: value, provision_time: value,
});
function mount(data = [], excluded = []) {
    const elements = new Map();
    const document = {
        createElement: tag => new Element(tag, document),
        createElementNS: (_, tag) => new Element(tag, document),
        addEventListener() {},
        getElementById: id => elements.get(id) ?? [...elements.values()]
            .flatMap(descendants).find(e => e.id === id) ?? null,
    };
    // A missing static ID must fail, rather than silently inventing its element.
    for (const [, tag, attrs] of html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)) {
        const id = attrs.match(/\bid="([^"]+)"/)?.[1];
        if (!id) continue;
        assert.equal(elements.has(id), false, `Duplicate static ID: ${id}`);
        const element = new Element(tag, document);
        for (const [, name, value] of attrs.matchAll(/([\w:-]+)(?:="([^"]*)")?/g)) element.setAttribute(name, value ?? '');
        elements.set(id, element);
    }
    document.body = new Element('body', document);
    const context = vm.createContext({document, suite, data, excluded, setTimeout, clearTimeout,
        window: {addEventListener() {}, location: {hash: ''}, innerWidth: 1440, innerHeight: 1000}});
    const run = source => vm.runInContext(source, context);
    const byClass = name => classNodes(elements.get('groups'), name);
    run(script);
    return {document, elements, run, byClass};
}
const data = [entry('Tagless', 0.03, []), entry('Measured', 0.06, ['sql']), entry('Mock', 0.001, ['sql'], true)];
data[0].date = '2026-09-29';
const page = mount(data);
const {document, elements, run, byClass} = page;
const click = id => { const element = document.getElementById(id); assert.ok(element, id); element.click(); };

assert.equal(run('FILTERS.tags(data[0])'), true, 'All includes tagless entries');
click('selectors-tag-all');
assert.equal(run('FILTERS.tags(data[0])'), false, 'Turning all tags off excludes tagless entries too');
assert.equal(elements.get('empty-state').hidden, false);
assert.equal(elements.get('clear-empty-filters').hidden, false);
click('clear-empty-filters');
assert.equal(elements.get('empty-state').hidden, true);
assert.equal(elements.get('filter-count').hidden, true);
assert.equal(document.activeElement.id, `choice-mode-${run('state.mode')}`, 'Reset returns focus to the current try mode');
assert.equal(elements.get('shown').getAttribute('aria-live'), 'polite');
assert.equal(elements.get('shown').textContent, '2 measured');
assert.equal(elements.has('stat-measured'), false, 'Measured count is not duplicated');
assert.equal(elements.has('selectors-view'), false, 'All comparisons are visible without view tabs');
assert.equal(run("Object.hasOwn(state, 'view')"), false);
assert.equal(Number(elements.get('stat-workloads').textContent), suite.rows.length);
assert.equal(Number(elements.get('stat-tries').textContent), suite.tries);
assert.equal(Number(elements.get('stat-rows').textContent.replaceAll(',', '')), suite.dataset.row_counts[suite.table]);
assert.match(elements.get('stat-table-label').textContent, new RegExp(`\\b${suite.table}\\b`));
assert.match(elements.get('dataset-size').textContent, /21,121 initial rows · 12 tables/);
assert.equal(elements.get('dataset-tables').children.length, Object.keys(suite.dataset.row_counts).length);
assert.equal(classNodes(elements.get('dataset-tables'), 'dataset-target')[0].children[0].textContent, `${suite.table}Timed table`);
assert.deepEqual(elements.get('workload-guide').children.map(pair => pair.children[0].children[1].textContent),
    suite.rows.map(row => `${row.batch.toLocaleString()} op${row.batch === 1 ? '' : 's'} / try`));
assert.equal(elements.get('dataset-hash').textContent, suite.dataset.sha256);
run("window.location.hash = '#dataset'; openDatasetGuide()");
assert.equal(elements.get('dataset').open, true, 'The run page link opens the guide');
assert.equal(elements.has('mock-note'), false, 'No standalone preview banner precedes the measured leaderboard');
assert.match(elements.get('updated').textContent, /2026-09-30/);
assert.doesNotMatch(elements.get('updated').textContent, /2035/, 'Newer mocks do not claim fresh measurements');

for (const mode of ['first', 'best']) {
    click(`choice-mode-${mode}`);
    const active = document.getElementById(`choice-mode-${mode}`);
    assert.equal(active.getAttribute('aria-pressed'), 'true');
    assert.equal(active.type, 'button');
    assert.equal(document.activeElement, active, 'Re-rendering a choice preserves focus');
    const withMock = byClass('score-value').map(e => e.textContent);
    assert.equal(withMock.length, 6, 'Only measured systems rank, in all three panels');
    assert.deepEqual(panelScores(page), [['×1.00', '×1.75'], ['×1.00', '×1.75'], ['×1.00', '×2.00']]);
    assert.equal(byClass('chart-card').length, 2);
    const tradeoffs = byClass('chart-point').filter(point => point.getAttribute('data-x-score') !== null);
    for (const axis of ['x', 'y']) tradeoffs.forEach((point, i) =>
        assert.ok(Math.abs(Number(point.getAttribute(`data-${axis}-score`)) - [1, 1.75][i]) < 1e-12));
    assert.equal(byClass('chart-point').filter(point => point.getAttribute('data-try') !== null).length, 6);
    assert.ok(byClass('chart-point').every(point => !point.getAttribute('aria-label').includes('Mock')), 'Mocks never enter charts');
    assert.deepEqual(byClass('score-time').map(e => e.textContent), ['0.030s', '0.060s', '0.030s', '0.060s']);
    const table = byClass('details')[0];
    assert.equal(byClass('details').length, 1, 'One shared timing table per hosting group');
    assert.equal(table.children.find(e => e.tag === 'tbody').children.length, suite.rows.length + 2);
    assert.equal(descendants(table).filter(e => e.tag === 'input' && e.type === 'checkbox').length, 5);
    for (const th of table.children[0].children[0].children.slice(2)) assert.match(th.title, /^test · 2026-/);
    assert.equal(byClass('metric-ratio').length, 0, 'Timing cells show raw times, not another ratio');
    assert.equal(byClass('timing-bar').length + byClass('summary-bar').length, 0, 'Rankings and timing cells do not contain bars');
    const colors = heatmapColors(page);
    assert.deepEqual(colors, Array.from({length: suite.rows.length + 2}, () => ['rgba(66, 156, 141, 0.225)', 'rgba(66, 156, 141, 0.13)']).flat());
    assert.equal(byClass('best-mark').length, suite.rows.length + 2, 'Fastest cells have a non-color indicator');
    for (const table of byClass('summary')) {
        assert.deepEqual(table.children[0].children[0].children.map(th => th.textContent), ['Rank', 'System', 'Score ↓', 'Time']);
        for (const row of table.children[1].children) assert.equal(row.children.length, 4, 'Ranking values occupy distinct table columns');
    }
    const disclosure = byClass('preview-section')[0];
    assert.equal(disclosure.tag, 'details', 'Previews use a native disclosure');
    assert.equal(disclosure.children[0].tag, 'summary');
    assert.equal(disclosure.getAttribute('open'), null);
    assert.equal(Boolean(disclosure.open), false, 'Illustrative previews start collapsed');
    const order = descendants(elements.get('groups'));
    assert.ok(byClass('score-value').every(score => order.indexOf(score) < order.indexOf(disclosure)),
        'All measured scores precede previews');
    const preview = byClass('preview-card')[0];
    assert.match(preview.textContent, /MOCK/);
    assert.match(preview.textContent, /0\.001s/);
    const previewText = descendants(preview).flatMap(e => e.children.filter(c => typeof c === 'string')).join('\n');
    assert.doesNotMatch(previewText, /×\d+\.\d+/, 'Preview values have no comparison ratios');
    assert.equal(descendants(preview).filter(e => e.className === 'summary-number').length, 0);
    assert.equal(classNodes(preview, 'best-mark').length, 0, 'Mocks never get measured fastest indicators');
    assert.equal(descendants(preview).filter(e => e.tag === 'dd').length, suite.rows.length + 1);
    for (const table of [...byClass('summary'), ...byClass('details')]) {
        assert.ok(table.getAttribute('aria-label'), 'Tables have accessible names');
        for (const th of descendants(table).filter(e => e.tag === 'th')) assert.ok(['col', 'row'].includes(th.scope));
    }
    run('state.kind.mock = false; render()');
    assert.deepEqual(byClass('score-value').map(e => e.textContent), withMock,
        'Toggling mocks cannot change measured scores');
    assert.deepEqual(heatmapColors(page), colors, 'Mocks cannot change heatmap baselines');
    assert.equal(byClass('preview-card').length, 0);
    run('state.kind.mock = true');
}

assert.equal(run("pick([0.3, 0.2, 0.1], 'first')"), 0.3);
assert.equal(run("pick([null, 0.2, 0.1], 'best')"), 0.1);
assert.equal(run("pick([0.3, null, 0.1], 'best')"), null);
assert.equal(run("pick([0.3, 0.2, null], 'best')"), null);
assert.equal(run('seconds(0.017706)'), '0.017706s', 'Close first-query results remain distinguishable');
assert.equal(run('seconds(0.017746)'), '0.017746s');
assert.doesNotMatch(byClass('group-heading')[0].textContent, /Local/);
const aboutRead = byClass('help-trigger').find(button => button.getAttribute('aria-label') === 'About Point read');
aboutRead.click();
assert.equal(elements.get('benchmark-help').popoverOpen, true);
assert.match(elements.get('benchmark-help').textContent, /1,000 operations per batch/);
assert.equal(aboutRead.getAttribute('aria-describedby'), 'benchmark-help');
run('closeHelp()');
assert.equal(elements.get('benchmark-help').popoverOpen, false);
assert.equal(aboutRead.getAttribute('aria-describedby'), null);
for (const row of run('allRows')) assert.equal(run('HELP_STEPS')[row.op].length, 3, `Illustration covers ${row.op}`);

const resultButton = byClass('system-name').find(button => button.tag === 'button');
resultButton.click();
assert.equal(elements.get('run-dialog').open, true, 'Result opens in a native dialog');
assert.equal(elements.get('run-title').textContent, 'Tagless');
assert.equal(elements.get('run-source').href, 'test.json');
const attempts = classNodes(elements.get('run-body'), 'run-timings')[0];
assert.equal(attempts.children[1].children.length, suite.rows.length);
assert.equal(attempts.children[1].children[0].children.length, suite.tries + 2);
click('close-run');
assert.equal(elements.get('run-dialog').open, false);
assert.equal(document.activeElement, resultButton, 'Closing restores focus to the result');
run('render()');
const previewButton = classNodes(byClass('preview-card')[0], 'system-name').find(button => button.tag === 'button');
previewButton.click();
assert.match(elements.get('run-subtitle').textContent, /Mock preview.*never ranked/);
click('close-run');

// Check the wizard's actual command generator without executing any shell commands.
const wizardHtml = fs.readFileSync(`${__dirname}/run.html`, 'utf8');
const wizardScript = wizardHtml.match(/<script>([\s\S]*?)<\/script>/)[1];
const wizardContext = vm.createContext({});
vm.runInContext(wizardScript.slice(0, wizardScript.indexOf('const form =')), wizardContext);
const commandsFor = vm.runInContext('commandsFor', wizardContext);
assert.match(commandsFor(['dolt_mysql'], 'bench-01', ''), /^python -m leaderboard.run dolt_mysql --machine 'bench-01' &&\n/);
const mixedCommands = commandsFor(['dolt', 'neon', 'tiger', 'xata'], 'bench-01', "O'Brien $(touch nope)");
assert.equal(mixedCommands.split(' &&\n').length, 6, 'One run per database, then validate and build once');
assert.match(mixedCommands, /^python -m leaderboard.run dolt --machine 'bench-01' &&\n/);
assert.equal((mixedCommands.match(/--client-location/g) || []).length, 3, 'Only managed services need a location');
assert.ok(mixedCommands.includes("--client-location 'O'\\''Brien $(touch nope)'"));
assert.throws(() => commandsFor([], 'bench-01', ''), /database/);
assert.throws(() => commandsFor(['dolt', 'neon'], 'bench-01', '   '), /location/);
assert.throws(() => commandsFor(['dolt'], 'bad; command', ''), /machine/);
assert.throws(() => commandsFor(['unknown'], 'bench-01', ''), /database/);
assert.throws(() => commandsFor(['tiger'], 'a'.repeat(65), 'US East'), /machine/);
assert.throws(() => commandsFor(['neon'], 'bench-01', 'US\tEast'), /location/);

// Selecting data rows changes only the reads/writes panel; selecting none restores all rows.
const selectionData = [entry('A', 0.03, []), entry('B', 0.06, [])];
selectionData[1].result[3] = [0.15, 0.15, 0.15];
const selection = mount(selectionData), allScores = panelScores(selection);
const selectRow = (index, checked) => {
    const checkbox = selection.document.getElementById(`row-${index}-no`);
    checkbox.checked = checked; checkbox.change();
    assert.equal(selection.document.activeElement.id, checkbox.id);
};
for (let i = 4; i < suite.rows.length; i++) selectRow(i, false);
assert.deepEqual(panelScores(selection), [allScores[0], ['×1.00', '×4.00'], allScores[2]]);
selectRow(3, false);
assert.deepEqual(panelScores(selection), allScores);

data[0].result[0] = [0.03, null, 0.01];
click('choice-mode-best');
assert.deepEqual(panelScores(page).map(scores => scores.length), [1, 2, 2],
    'A failed first-query repeat affects only its panel');
assert.match(elements.get('groups').textContent, /Tagless.*not ranked/);
assert.match(elements.get('groups').textContent, /Failed/);
assert.equal(byClass('chart-series').length, 2, 'The failed middle try breaks both adjacent segments');
assert.equal(byClass('chart-point').filter(point => point.getAttribute('data-try') !== null).length, 5, 'A failed try is not plotted as zero');

// A populated mock must not fill a missing measured row or crash null baselines.
data[0].result = data[1].result = suite.rows.map(() => [null, null, null]);
run('render()');
assert.deepEqual(panelScores(page).map(scores => scores.length), [0, 0, 2], 'Valid loading scores remain visible');
assert.equal(heatmapColors(page).filter(Boolean).length, 4, 'Failed operation cells have no heatmap value; load and provisioning do');
assert.match(elements.get('groups').textContent, /No measurements:/);
run('state.kind.measured = false; render()');
assert.equal(byClass('summary').length, 0, 'Mock-only filters never produce a ranking');
assert.equal(byClass('preview-card').length, 1);
assert.doesNotMatch(elements.get('groups').textContent, /No measurements:/);
click('reset-filters');
assert.equal(run('state.kind.measured'), true);

const stale = [{system: 'Old', machine: 'test', source: 'old.json', reason: 'invalid: suite differs'}];
for (const excluded of [[], stale]) {
    const empty = mount([], excluded);
    assert.equal(empty.elements.get('empty-state').hidden, false);
    assert.equal(empty.elements.get('empty-guide').hidden, false);
    assert.equal(empty.elements.get('shown').textContent, '0 measured');
    assert.match(empty.elements.get('updated').textContent, /Awaiting measurements/);
    assert.equal(empty.byClass('summary').length, 0);
    if (excluded.length) assert.match(empty.elements.get('excluded').textContent, /invalid: suite differs/);
}
const previewOnly = mount([entry('Preview', 1, [], true)]);
assert.equal(previewOnly.elements.get('shown').textContent, '0 measured');
assert.match(previewOnly.elements.get('updated').textContent, /Awaiting/);
assert.equal(previewOnly.byClass('preview-card').length, 1);
assert.equal(previewOnly.byClass('summary').length, 0);
assert.equal(previewOnly.byClass('chart-card').length, 0, 'Mock-only views have no measured charts');
const grouped = mount([entry('Local preview', 100, [], true), entry('Local', 1, []),
    {...entry('Hosted', 5, []), hosted: 'yes', region: 'test-region', rtt_ms: 10}]);
assert.equal(grouped.byClass('summary').length, 6);
assert.equal(grouped.byClass('details').length, 2);
assert.deepEqual(heatmapColors(grouped), Array(20).fill('rgba(66, 156, 141, 0.225)'),
    'Hosted/local heatmap scales stay separate, and a mock cannot set either baseline');
const groupOrder = descendants(grouped.elements.get('groups'));
for (const score of grouped.byClass('score-value')) {
    assert.equal(score.textContent, '×1.00', 'Local and hosted baselines remain independent');
    assert.ok(groupOrder.indexOf(score) < groupOrder.indexOf(grouped.byClass('preview-section')[0]),
        'Even a local preview follows the measured hosted ranking');
}
for (const point of grouped.byClass('chart-point').filter(point => point.getAttribute('data-x-score') !== null)) {
    assert.equal(point.getAttribute('data-x-score'), '1', 'Chart baselines stay within the hosting group');
    assert.equal(point.getAttribute('data-y-score'), '1');
}
const zeroData = [entry('Zero', 1, []), entry('Missing', 1, [])];
zeroData[0].result = suite.rows.map(() => [0, 0, 0]);
zeroData[1].result = suite.rows.map(() => [null, null, null]);
const zeros = mount(zeroData);
assert.equal(zeros.byClass('chart-point').filter(point => point.getAttribute('data-seconds') === '0').length, suite.tries, 'Zero attempts remain valid plotted measurements');
const timingRows = zeros.byClass('details')[0].children.find(e => e.tag === 'tbody').children;
for (const row of timingRows.slice(0, suite.rows.length)) {
    assert.equal(classNodes(row, 'cell-best')[0].style.backgroundColor, 'rgba(66, 156, 141, 0.225)', 'Zero is a valid fastest timing');
    assert.equal(classNodes(row, 'cell-missing')[0].style.backgroundColor, undefined, 'Missing values are not heatmap values');
}
for (const color of heatmapColors(zeros).filter(Boolean)) {
    const alpha = Number(color.match(/, ([\d.]+)\)$/)[1]);
    assert.ok(Number.isFinite(alpha) && alpha >= 0 && alpha <= 1);
}
// A slow preview for an old input must never replace the script the user can run.
async function checkLivePreview() {
    const nodes = new Map(), replies = [];
    const context = vm.createContext({AbortSignal,
        el: id => { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); },
        fetch: () => new Promise(resolve => replies.push(resolve)),
    });
    const prepareSource = wizardScript.slice(wizardScript.indexOf('async function prepareRun('),
        wizardScript.indexOf("el('run-benchmark').addEventListener"));
    vm.runInContext(`let previewVersion = 1, preparedRun = null; ${prepareSource}`, context);
    const prepare = vm.runInContext('prepareRun', context);
    const old = prepare({machine: 'old-machine'}, 1);
    vm.runInContext('previewVersion = 2', context);
    const current = prepare({machine: 'new-machine'}, 2);
    replies[1]({ok: true, json: async () => ({script: 'new script', can_launch: true})});
    await current;
    replies[0]({ok: true, json: async () => ({script: 'old script', can_launch: true})});
    await old;
    assert.equal(nodes.get('run-command').textContent, 'new script');
    assert.equal(vm.runInContext('preparedRun.machine', context), 'new-machine');
    assert.equal(nodes.get('run-benchmark').disabled, false);
}
checkLivePreview().then(() => console.log('Page regression checks passed'))
    .catch(error => { console.error(error); process.exitCode = 1; });
