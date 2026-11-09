'use strict';

(function () {
    const $ = (id) => document.getElementById(id);

    const els = {
        form: $('meal-form'),
        modeButtons: Array.from(document.querySelectorAll('[data-mode]')),
        panels: { now: $('when-now'), plan: $('when-plan') },
        date: $('meal-date'),
        todayButton: $('date-today-button'),
        protein: $('target-protein'),
        carbs: $('target-carbs'),
        fat: $('target-fat'),
        filters: Array.from(document.querySelectorAll('input[data-filter]')),
        meals: Array.from(document.querySelectorAll('input[name="meal_period"]')),
        exclusions: $('exclusion-input'),
        generate: $('generate-button'),
        reset: $('reset-button'),
        share: $('share-button'),
        status: $('status-label'),
        panel: $('results-panel'),
        placeholder: $('results-placeholder'),
        placeholderText: $('placeholder-text'),
        placeholderAction: $('placeholder-action'),
        switchToPlan: $('switch-to-plan'),
        loading: $('loading-spinner'),
        loadingText: $('loading-text'),
        targetKcal: $('target-kcal'),
        content: $('results-content'),
        header: $('result-header'),
        note: $('result-note'),
        where: $('result-where'),
        match: $('result-match'),
        totals: $('totals-body'),
        nutritionExtra: $('nutrition-extra'),
        items: $('meal-plan-items'),
        leftOff: $('left-off'),
        leftOffList: $('left-off-list'),
        chartSection: $('chart-section'),
        chart: $('macro-chart'),
        optionsSection: $('options-section'),
        options: $('options-list'),
        freshness: $('freshness'),
    };

    // Settings stay in this browser only (see the privacy policy).
    const STORAGE_KEY = 'purdue-macro-finder:v1';
    const DEFAULTS = { mode: 'now', p: 40, c: 60, f: 20, meals: ['Lunch', 'Dinner'], diet: [], exclude: [] };
    const MAX_TARGET = 500;
    const MAX_EXCLUSIONS = 50;
    const MAX_EXCLUSION_LENGTH = 80;
    const RETRY_DELAY_MS = 4000;
    const MAX_RETRIES = 5;
    const REQUEST_TIMEOUT_MS = 30000;

    let mode = 'now';
    let chart = null;
    let retryTimer = null;
    let retries = 0;
    let current = null;   // last successful result from the API
    let selected = 0;     // which of current.options is shown
    let keepCourt = null; // after a swap, stay on the dining court being viewed

    // Purdue's allergen tags, in the order the Avoid filters list them.
    const ALLERGENS = ['Gluten', 'Wheat', 'Milk', 'Eggs', 'Soy', 'Peanuts', 'Tree Nuts',
        'Fish', 'Shellfish', 'Sesame', 'Coconut'];

    // --- small helpers ---

    function localISODate(d) {
        const pad = (n) => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    }
    const todayLocal = () => localISODate(new Date());

    function shiftDays(days) {
        const d = new Date();
        d.setDate(d.getDate() + days);
        return localISODate(d);
    }

    // "2026-04-30" becomes "today", "tomorrow", "yesterday", or "Thu, Apr 30".
    function formatDateLabel(yyyymmdd) {
        const [y, m, d] = yyyymmdd.split('-').map(Number);
        const target = new Date(y, m - 1, d);
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        const diff = Math.round((target - today) / 86400000);
        if (diff === 0) return 'today';
        if (diff === 1) return 'tomorrow';
        if (diff === -1) return 'yesterday';
        return target.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
    }

    // Keeps decimals ("40.5"); blank or invalid input counts as 0.
    function readGrams(input) {
        const n = parseFloat(input.value);
        return Number.isFinite(n) ? n : 0;
    }

    const grams = (v) => `${Math.round(v || 0)} g`;

    // Calories implied by macros, at 4/4/9 kcal per gram.
    const estimateKcal = (m) => Math.round(4 * (m.p || 0) + 4 * (m.c || 0) + 9 * (m.f || 0));

    function updateKcal() {
        const kcal = estimateKcal({ p: readGrams(els.protein), c: readGrams(els.carbs), f: readGrams(els.fat) });
        els.targetKcal.textContent = kcal > 0 ? `About ${kcal.toLocaleString()} kcal` : '';
    }

    function cssVar(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    }

    function setStatus(text, kind = 'info') {
        els.status.textContent = text;
        els.status.className = `status-${kind}`;
    }

    // --- settings: collect, apply, remember, share ---

    function collect() {
        return {
            mode,
            p: readGrams(els.protein),
            c: readGrams(els.carbs),
            f: readGrams(els.fat),
            date: els.date.value || todayLocal(),
            meals: els.meals.filter((cb) => cb.checked).map((cb) => cb.value),
            diet: els.filters.filter((cb) => cb.checked).map((cb) => cb.dataset.filter),
            exclude: exclusionList(),
        };
    }

    function apply(settings) {
        const s = { ...DEFAULTS, ...settings };
        els.protein.value = s.p;
        els.carbs.value = s.c;
        els.fat.value = s.f;
        els.meals.forEach((cb) => { cb.checked = s.meals.includes(cb.value); });
        els.filters.forEach((cb) => { cb.checked = s.diet.includes(cb.dataset.filter); });
        els.exclusions.value = s.exclude.join('\n');
        const inRange = s.date && s.date >= els.date.min && s.date <= els.date.max;
        els.date.value = inRange ? s.date : todayLocal();
        setMode(s.mode === 'plan' ? 'plan' : 'now');
        updateKcal();
    }

    // --- exclusions, including one-tap "Not this" swaps ---

    function exclusionList() {
        return els.exclusions.value.split('\n').map((t) => t.trim()).filter(Boolean);
    }

    function setExclusions(list) {
        els.exclusions.value = list.join('\n');
    }

    // Leave a food off and rebuild the plate at the same dining court.
    function leaveOff(name) {
        const term = name.slice(0, MAX_EXCLUSION_LENGTH);
        const list = exclusionList();
        if (!list.some((t) => t.toLowerCase() === term.toLowerCase())) list.push(term);
        setExclusions(list);
        keepCourt = currentOption().court;
        search({ inPlace: true });
    }

    function putBack(term) {
        setExclusions(exclusionList().filter((t) => t !== term));
        keepCourt = currentOption().court;
        search({ inPlace: true });
    }

    const currentOption = () => (current.options && current.options[selected]) || current;

    function save(settings) {
        // The planning date is deliberately not remembered: tomorrow it's stale.
        const { date, ...rest } = settings;
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify(rest)); } catch (_) { /* storage disabled */ }
    }

    function loadSaved() {
        try {
            const s = JSON.parse(localStorage.getItem(STORAGE_KEY));
            return s && typeof s === 'object' ? s : null;
        } catch (_) {
            return null;
        }
    }

    function forget() {
        try { localStorage.removeItem(STORAGE_KEY); } catch (_) { /* storage disabled */ }
    }

    function toQuery(s) {
        const q = new URLSearchParams({ p: s.p, c: s.c, f: s.f, mode: s.mode });
        if (s.mode === 'plan') {
            q.set('date', s.date);
            q.set('meals', s.meals.join(','));
        }
        if (s.diet.length) q.set('diet', s.diet.join(','));
        if (s.exclude.length) q.set('exclude', s.exclude.join(','));
        return q.toString();
    }

    function fromQuery(search) {
        const q = new URLSearchParams(search);
        if (!['p', 'c', 'f'].some((k) => q.has(k))) return null;
        const num = (k) => {
            const n = parseFloat(q.get(k));
            return Number.isFinite(n) ? n : DEFAULTS[k];
        };
        const list = (k) => (q.get(k) || '').split(',').map((x) => x.trim()).filter(Boolean);
        return {
            mode: q.get('mode') === 'plan' ? 'plan' : 'now',
            p: num('p'), c: num('c'), f: num('f'),
            date: q.get('date') || todayLocal(),
            meals: q.has('meals') ? list('meals') : DEFAULTS.meals,
            diet: list('diet'),
            exclude: list('exclude'),
        };
    }

    async function copyLink() {
        const url = `${location.origin}${location.pathname}?${toQuery(collect())}`;
        try {
            await navigator.clipboard.writeText(url);
            setStatus('Link copied. It opens this search with the same targets and filters.', 'ok');
        } catch (_) {
            // No clipboard access (e.g. plain http): put it in the address bar instead.
            history.replaceState(null, '', url);
            setStatus('The link is now in your address bar. Copy it from there.', 'ok');
        }
    }

    // --- mode (right now vs plan ahead) ---

    function setMode(next) {
        mode = next;
        els.modeButtons.forEach((btn) => {
            const on = btn.dataset.mode === next;
            btn.classList.toggle('active', on);
            btn.setAttribute('aria-selected', String(on));
            btn.tabIndex = on ? 0 : -1;
        });
        els.panels.now.classList.toggle('active', next === 'now');
        els.panels.plan.classList.toggle('active', next === 'plan');
    }

    // --- validation (mirrors the server's rules, with friendlier wording) ---

    function validate(s) {
        for (const [value, name] of [[s.p, 'Protein'], [s.c, 'Carbs'], [s.f, 'Fat']]) {
            if (value < 0) return `${name} can't be negative.`;
            if (value > MAX_TARGET) return `${name} must be ${MAX_TARGET} g or less.`;
        }
        if (s.p + s.c + s.f === 0) return 'Set at least one macro target.';
        if (s.mode === 'plan' && !s.meals.length) return 'Select at least one meal period.';
        if (s.exclude.length > MAX_EXCLUSIONS) return `Exclude at most ${MAX_EXCLUSIONS} foods.`;
        if (s.exclude.some((t) => t.length > MAX_EXCLUSION_LENGTH)) {
            return `Keep each excluded food under ${MAX_EXCLUSION_LENGTH} characters.`;
        }
        return null;
    }

    // --- request flow ---

    async function postJSON(url, body) {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
        });
        let data = null;
        try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
        return { status: res.status, ok: res.ok, data };
    }

    // inPlace keeps the current plate on screen (dimmed) while it's rebuilt,
    // so a swap doesn't make the page jump.
    function setBusy(busy, message, { inPlace = false } = {}) {
        els.generate.disabled = busy;
        els.panel.setAttribute('aria-busy', String(busy));
        els.content.classList.toggle('is-updating', busy && inPlace);
        els.content.querySelectorAll('button').forEach((b) => { b.disabled = busy; });
        if (busy) {
            els.generate.textContent = 'Finding...';
            setStatus(message);
            if (inPlace) return;
            els.loadingText.textContent = message;
            els.placeholder.classList.add('hidden');
            els.content.classList.add('hidden');
            els.loading.classList.remove('hidden');
        } else {
            els.generate.textContent = 'Find a meal';
            els.loading.classList.add('hidden');
        }
    }

    function showMessage(text, kind = 'info', { offerPlan = false } = {}) {
        els.placeholderText.textContent = text;
        els.placeholderAction.classList.toggle('hidden', !offerPlan);
        els.placeholder.classList.remove('hidden');
        els.content.classList.add('hidden');
        els.loading.classList.add('hidden');
        setStatus(text, kind);
    }

    async function search({ isRetry = false, inPlace = false } = {}) {
        clearTimeout(retryTimer);
        if (!isRetry) retries = 0;
        inPlace = inPlace && current !== null && !els.content.classList.contains('hidden');

        const s = collect();
        const problem = validate(s);
        if (problem) {
            keepCourt = null;
            if (inPlace) setStatus(problem, 'error');
            else showMessage(problem, 'error');
            return;
        }
        save(s);

        const payload = {
            mode: s.mode,
            targets: { p: s.p, c: s.c, f: s.f },
            dietary_filters: Object.fromEntries(s.diet.map((d) => [d, true])),
            exclusion_list: s.exclude,
        };
        if (s.mode === 'plan') {
            payload.date = s.date;
            payload.meal_periods = s.meals;
        }

        if (!isRetry) {
            setBusy(true, inPlace ? 'Rebuilding the plate.'
                : s.mode === 'now' ? 'Checking what is being served right now.'
                    : 'Building the best plate.', { inPlace });
        }

        let res;
        try {
            res = await postJSON('/api/find_meal', payload);
        } catch (err) {
            setBusy(false);
            keepCourt = null;
            showMessage(err && err.name === 'TimeoutError'
                ? 'The request timed out. Try again.'
                : 'Could not reach the server. Check your connection and try again.', 'error');
            return;
        }

        // Cold start: the server is fetching menus from Purdue in the background.
        if (res.status === 503 && retries < MAX_RETRIES) {
            retries += 1;
            setStatus(`Loading menus from Purdue Dining (try ${retries} of ${MAX_RETRIES}).`, 'warn');
            retryTimer = setTimeout(() => search({ isRetry: true, inPlace }), RETRY_DELAY_MS);
            return;
        }

        setBusy(false);
        const court = keepCourt;
        keepCourt = null;
        if (inPlace && (!res.ok || !res.data)) {
            // A swap that leaves nothing to eat: keep the plate on screen and
            // undo the change rather than blanking the results.
            setExclusions(current.exclude || []);
            save(collect());
            const reason = res.status === 429
                ? 'Too many requests. Wait a minute and try again.'
                : (res.data && res.data.error) || `Something went wrong (status ${res.status}).`;
            setStatus(`${reason.replace(/\.$/, '')}. Your plate is unchanged.`, 'error');
        } else if (res.status === 429) {
            showMessage('Too many requests. Wait a minute and try again.', 'error');
        } else if (!res.ok || !res.data) {
            const reason = (res.data && res.data.error) || `Something went wrong (status ${res.status}). Try again.`;
            showMessage(reason, 'error');
        } else if (res.data.currently_serving === false && !res.data.upcoming) {
            const hint = (res.data.next_meal_hint || '').replace(/\.+$/, '');
            // "Dining courts are closed until tomorrow" already says it all;
            // "Dinner starts at 5 pm" needs the lead-in.
            const text = /closed/i.test(hint)
                ? `${hint}.`
                : `No dining court is serving right now.${hint ? ` ${hint}.` : ''}`;
            showMessage(text, 'warn', { offerPlan: true });
            revealResults();
        } else {
            current = { ...res.data, exclude: s.exclude };
            const same = court ? (current.options || []).findIndex((o) => o.court === court) : -1;
            selected = Math.max(0, same);
            render();
            const option = currentOption();
            if (current.upcoming) {
                setStatus(`Found a plate for the next meal, at ${option.court}.`, 'ok');
            } else {
                setStatus(`Found a plate at ${option.court}${current.currently_serving ? ', serving now' : ''}.`, 'ok');
            }
            if (!inPlace) revealResults();
        }
    }

    // On a phone the results sit below the whole form; bring them into view
    // so tapping "Find a meal" visibly does something.
    function revealResults() {
        if (!window.matchMedia('(max-width: 959px)').matches) return;
        const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        els.panel.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
    }

    // --- rendering ---

    function render() {
        const option = currentOption();
        const dateLabel = current.date ? formatDateLabel(current.date) : '';

        if (current.currently_serving) {
            els.header.textContent = 'Your meal right now';
        } else if (current.upcoming) {
            els.header.textContent = dateLabel === 'today' ? 'Your next meal' : `Your next meal, ${dateLabel}`;
        } else {
            els.header.textContent = dateLabel ? `Your meal for ${dateLabel}` : 'Your meal';
        }
        const hint = (current.next_meal_hint || '').replace(/\.+$/, '');
        els.note.textContent = current.upcoming
            ? `Nothing is being served right now.${hint ? ` ${hint}.` : ''}`
            : '';
        els.note.classList.toggle('hidden', !current.upcoming);
        renderWhere(els.where, option);
        renderMatch(option.totals, current.targets);
        renderTotals(option.totals, current.targets);
        renderExtraNutrition(option.totals);
        renderItems(option.plan);
        renderLeftOff(current.exclude || []);
        renderChart(option.totals);
        renderOptions();
        renderFreshness(current.menu_updated);

        els.placeholder.classList.add('hidden');
        els.loading.classList.add('hidden');
        els.content.classList.remove('hidden');
    }

    // "Earhart · Dinner · 5 to 9 pm": where and when to go eat it.
    function renderWhere(target, option) {
        target.textContent = [option.court, option.meal_name].filter(Boolean).join(' · ');
        if (option.hours && option.hours.label) {
            const hours = document.createElement('span');
            hours.className = 'hours';
            hours.textContent = ` · ${option.hours.label}`;
            target.appendChild(hours);
        }
    }

    // Largest miss across the three macros, in grams.
    function worstMiss(totals, targets) {
        return Math.max(...['p', 'c', 'f'].map((k) => Math.abs((totals[k] || 0) - (targets[k] || 0))));
    }

    function renderMatch(totals, targets) {
        if (!targets) {
            els.match.textContent = '';
            return;
        }
        const miss = Math.round(worstMiss(totals, targets));
        els.match.textContent = miss <= 2
            ? 'Within 2 g of every target.'
            : `Within ${miss} g of every target. This is the closest plate on the menu.`;
    }

    function renderTotals(totals, targets) {
        els.totals.textContent = '';
        const addRow = (label, m, calories, className) => {
            const tr = document.createElement('tr');
            if (className) tr.className = className;
            const th = document.createElement('th');
            th.scope = 'row';
            th.textContent = label;
            tr.appendChild(th);
            [grams(m.p), grams(m.c), grams(m.f), calories].forEach((value) => {
                const td = document.createElement('td');
                td.textContent = value;
                tr.appendChild(td);
            });
            els.totals.appendChild(tr);
        };
        // Plate calories come from Purdue's labels; a target has none, so
        // estimate it from its macros.
        addRow('Plate', totals, String(totals.cal != null ? Math.round(totals.cal) : estimateKcal(totals)));
        if (targets) addRow('Target', targets, `~${estimateKcal(targets)}`, 'target-row');
    }

    // Reported, not optimized: "Also 9 g fiber · 21 g sugar · ...".
    function renderExtraNutrition(totals) {
        const parts = [
            ['fiber', (v) => `${grams(v)} fiber`],
            ['sugar', (v) => `${grams(v)} sugar`],
            ['sat_fat', (v) => `${grams(v)} saturated fat`],
            ['sodium', (v) => `${Math.round(v).toLocaleString()} mg sodium`],
        ].filter(([key]) => totals[key] != null).map(([key, format]) => format(totals[key]));
        els.nutritionExtra.textContent = parts.length ? `Also ${parts.join(' · ')}` : '';
    }

    // Foods being left off, each with a button to put it back.
    function renderLeftOff(terms) {
        els.leftOffList.textContent = '';
        els.leftOff.classList.toggle('hidden', terms.length === 0);
        terms.forEach((term) => {
            const li = document.createElement('li');
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'chip';
            btn.textContent = term;
            btn.title = 'Put it back';
            btn.setAttribute('aria-label', `Put ${term} back`);
            btn.addEventListener('click', () => putBack(term));
            li.appendChild(btn);
            els.leftOffList.appendChild(li);
        });
    }

    // Purdue's serving labels are free text: "1/2 Cup Serving", "Slice",
    // "Each", "1 Each Serving", "1 Per Serving". Drop a trailing "Serving" and
    // hide labels that only mean "one serving"; they add nothing next to the
    // name or its "× 2" badge. Macros are per line, so the label is per serving.
    function servingLabel(raw) {
        const label = String(raw || '').trim().replace(/\s+/g, ' ').replace(/\s*serving$/i, '');
        return /^(1 )?(each|per)?$/i.test(label) ? '' : label;
    }

    function renderItems(plan) {
        els.items.textContent = '';
        plan.forEach((item) => {
            const servings = item.servings || 1;
            const row = document.createElement('div');
            row.className = 'item-card';
            row.setAttribute('role', 'listitem');

            const info = document.createElement('div');
            info.className = 'item-info';
            const name = document.createElement('h4');
            name.textContent = item.name || 'Unnamed item';
            if (servings > 1) {
                const badge = document.createElement('span');
                badge.className = 'servings';
                badge.textContent = `× ${servings}`;
                badge.setAttribute('aria-label', `${servings} servings`);
                name.appendChild(badge);
            }
            // The station says where in the dining court to find it, e.g. a
            // topping that lives at the "Salad Bar".
            // Sauces, toppings, spreads and buns are listed under the food
            // they go with: "Tzatziki Sauce: Firewall · with Chicken Shawarma".
            if (item.goes_with) row.classList.add('item-addon');
            const detail = document.createElement('p');
            const size = servingLabel(item.serving_size);
            const pairing = item.goes_with ? `with ${item.goes_with}` : '';
            detail.textContent = [item.station, size, pairing].filter(Boolean).join(' · ');
            info.append(name, detail);
            const allergens = ALLERGENS.filter((a) => (item.traits || []).includes(a));
            if (allergens.length) {
                const contains = document.createElement('p');
                contains.className = 'item-contains';
                contains.textContent = `Contains ${allergens.map((a) => a.toLowerCase()).join(', ')}`;
                info.appendChild(contains);
            }

            const side = document.createElement('div');
            side.className = 'item-side';
            const macros = document.createElement('div');
            macros.className = 'item-macros';
            [['p', 'P'], ['c', 'C'], ['f', 'F']].forEach(([key, letter]) => {
                const span = document.createElement('span');
                span.textContent = `${letter} ${grams((item[key] || 0) * servings)}`;
                macros.appendChild(span);
            });
            const swap = document.createElement('button');
            swap.type = 'button';
            swap.className = 'swap-button';
            swap.textContent = 'Not this';
            // Starts with the visible text, so voice control ("click Not this") works.
            swap.setAttribute('aria-label', `Not this: ${item.name}`);
            swap.title = 'Leave this off and rebuild the plate';
            swap.addEventListener('click', () => leaveOff(item.name));
            side.append(macros, swap);

            row.append(info, side);
            els.items.appendChild(row);
        });
    }

    function renderChart(totals) {
        if (chart) {
            chart.destroy();
            chart = null;
        }
        // If the CDN is blocked or offline, show everything except the chart.
        if (typeof window.Chart !== 'function') {
            els.chartSection.classList.add('hidden');
            return;
        }
        els.chartSection.classList.remove('hidden');
        const { p = 0, c = 0, f = 0 } = totals;
        chart = new window.Chart(els.chart.getContext('2d'), {
            type: 'pie',
            data: {
                labels: [`Protein (${grams(p)})`, `Carbs (${grams(c)})`, `Fat (${grams(f)})`],
                datasets: [{
                    data: [p * 4, c * 4, f * 9],
                    backgroundColor: [cssVar('--chart-p'), cssVar('--chart-c'), cssVar('--chart-f')],
                    borderColor: cssVar('--surface'),
                    borderWidth: 2,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                animation: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? false : undefined,
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: { color: cssVar('--ink'), font: { size: 12 }, boxWidth: 12 },
                    },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => {
                                const total = ctx.chart.getDatasetMeta(0).total;
                                const pct = total ? Math.round((ctx.parsed / total) * 100) : 0;
                                return `${ctx.label}: ${pct}% of calories`;
                            },
                        },
                    },
                },
            },
        });
    }

    function renderOptions() {
        const options = current.options || [];
        els.options.textContent = '';
        els.optionsSection.classList.toggle('hidden', options.length < 2);
        options.forEach((option, index) => {
            const li = document.createElement('li');
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'option-button';
            btn.setAttribute('aria-pressed', String(index === selected));

            const where = document.createElement('span');
            where.className = 'option-where';
            renderWhere(where, option);

            const detail = document.createElement('span');
            detail.className = 'option-detail';
            const t = option.totals;
            const parts = [`P ${grams(t.p)}`, `C ${grams(t.c)}`, `F ${grams(t.f)}`];
            if (index === 0) parts.push('best match');
            detail.textContent = parts.join(' · ');

            btn.append(where, detail);
            btn.addEventListener('click', () => {
                selected = index;
                render();
                els.header.focus({ preventScroll: true });
                els.header.scrollIntoView({ behavior: 'smooth', block: 'start' });
            });
            li.appendChild(btn);
            els.options.appendChild(li);
        });
    }

    function renderFreshness(iso) {
        if (!iso) {
            els.freshness.textContent = 'Menu and nutrition data from Purdue Dining.';
            return;
        }
        const when = new Date(iso);
        const sameDay = localISODate(when) === todayLocal();
        const time = when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        const stamp = sameDay ? time : `${when.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`;
        els.freshness.textContent = `Menu and nutrition data from Purdue Dining, fetched ${stamp}.`;
    }

    // --- events ---

    els.form.addEventListener('submit', (e) => {
        e.preventDefault();
        if (!els.generate.disabled) search();
    });

    els.modeButtons.forEach((btn) => {
        btn.addEventListener('click', () => {
            setMode(btn.dataset.mode);
            setStatus(mode === 'now'
                ? 'Searching what is being served right now.'
                : 'Pick a date and meal periods.');
        });
    });

    // Arrow keys move between the two tabs (WAI-ARIA tabs pattern).
    els.modeButtons.forEach((btn, i) => {
        btn.addEventListener('keydown', (e) => {
            if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
            e.preventDefault();
            const next = els.modeButtons[(i + (e.key === 'ArrowRight' ? 1 : -1) + els.modeButtons.length) % els.modeButtons.length];
            next.focus();
            next.click();
        });
    });

    els.switchToPlan.addEventListener('click', () => {
        setMode('plan');
        els.modeButtons.find((b) => b.dataset.mode === 'plan').focus();
        setStatus('Pick a date and meal periods.');
    });

    els.todayButton.addEventListener('click', () => { els.date.value = todayLocal(); });

    els.reset.addEventListener('click', () => {
        clearTimeout(retryTimer);
        forget();
        apply(DEFAULTS);
        if (chart) { chart.destroy(); chart = null; }
        current = null;
        history.replaceState(null, '', location.pathname);
        showMessage('Cleared. Your saved settings were removed from this browser.');
    });

    els.share.addEventListener('click', copyLink);

    [els.protein, els.carbs, els.fat].forEach((input) => input.addEventListener('input', updateKcal));

    // Redraw the chart in the new palette when the system theme changes.
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
        if (current) renderChart(currentOption().totals);
    });

    // --- start ---

    els.date.min = shiftDays(-365);
    els.date.max = shiftDays(30);
    const fromLink = fromQuery(location.search);
    apply(fromLink || loadSaved() || DEFAULTS);
    if (fromLink) {
        // The search saves these settings; a reload should keep any changes
        // made afterwards rather than re-apply the link.
        history.replaceState(null, '', location.pathname);
        search();
    } else {
        setStatus(mode === 'now'
            ? 'Set your targets, then find a meal from what is being served now.'
            : 'Set your targets and meal periods, then find a meal.');
    }
})();
