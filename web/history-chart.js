"use strict";

(() => {
  const ns = "http://www.w3.org/2000/svg";
  const plots = new WeakMap();
  const height = 252;
  const kinds = {
    thermal: ["Термозащита", "M0 -3v3.2m0 2.6v.4"],
    "host-reboot": ["Перезагрузка узла", "M3 -1a3 3 0 1 0 0 2M3 -3v2H1"],
    "service-start": ["Запуск панели", "M-1.5 -3L3 0l-4.5 3z"],
    exit: ["Сбой воркера", "M-2 -2l4 4m0-4l-4 4"],
  };
  let nextId = 0;

  function svg(tag, attrs = {}, content) {
    const node = document.createElementNS(ns, tag);
    attributes(node, attrs);
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function attributes(node, attrs) {
    for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  }

  function line(x1, y1, x2, y2, className) {
    return svg("line", {x1, y1, x2, y2, class: className});
  }

  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const valid = value => Number.isFinite(value) && value >= 0;

  function makePlot(chart) {
    const id = "neb-history-fill-" + ++nextId;
    const gradient = svg("linearGradient", {id, x1: "0%", y1: "0%", x2: "0%", y2: "100%"});
    gradient.append(svg("stop", {offset: "0%", class: "history-fill-start"}),
      svg("stop", {offset: "100%", class: "history-fill-end"}));
    const defs = svg("defs");
    defs.append(gradient);

    const axis = svg("g", {"aria-hidden": "true"});
    const series = svg("g", {"aria-hidden": "true", "data-series": ""});
    const guides = svg("g", {"aria-hidden": "true"});
    const cursor = svg("rect", {class: "history-hit", role: "slider", tabindex: "-1"});
    const markerLayer = svg("g", {role: "group", "aria-label": "События за выбранный период"});
    const crossX = line(0, 0, 0, 0, "history-crosshair");
    const crossY = line(0, 0, 0, 0, "history-crosshair");
    const point = svg("circle", {class: "history-probe-point", r: 4});
    const tip = svg("g");
    const tipBody = svg("rect", {rx: 6, class: "history-tip-body"});
    const tipTime = svg("text", {x: 10, y: 16, class: "history-tip-time"});
    const tipValue = svg("text", {x: 10, y: 35, class: "history-tip-value"});
    tip.append(tipBody, tipTime, tipValue);
    const probe = svg("g", {class: "history-probe", "aria-hidden": "true"});
    probe.append(crossX, crossY, point, tip);
    probe.setAttribute("hidden", "");
    chart.replaceChildren(defs, axis, series, guides, cursor, markerLayer, probe);
    chart.setAttribute("role", "group");

    const view = {id, chart, axis, series, guides, cursor, markerLayer, crossX, crossY,
      point, tip, tipBody, tipTime, tipValue, probe, markers: new Map(), ready: false, hovering: false};
    cursor.addEventListener("pointermove", event => {
      if (event.pointerType !== "touch") pointFromPointer(view, event);
    });
    cursor.addEventListener("pointerdown", event => pointFromPointer(view, event));
    cursor.addEventListener("pointerleave", () => {
      view.hovering = false;
      if (document.activeElement !== cursor) probe.setAttribute("hidden", "");
    });
    cursor.addEventListener("focus", () => showProbe(view, view.moment ?? view.latest?.t));
    cursor.addEventListener("blur", () => probe.setAttribute("hidden", ""));
    cursor.addEventListener("keydown", event => stepCursor(view, event));
    plots.set(chart, view);
    return view;
  }

  function pointFromPointer(view, event) {
    if (!view.values.length) return;
    const bounds = view.chart.getBoundingClientRect();
    const {width, left, right, from, to} = view.layout;
    if (!bounds.width) return;
    const x = (event.clientX - bounds.left) / bounds.width * width;
    view.hovering = true;
    showProbe(view, Math.round(from + clamp((x - left) / (right - left), 0, 1) * (to - from)));
  }

  function nearest(values, stamp) {
    let candidate;
    for (const sample of values) {
      if (!candidate || Math.abs(sample.t - stamp) < Math.abs(candidate.t - stamp)) candidate = sample;
      if (sample.t >= stamp) break;
    }
    return candidate;
  }

  function stepCursor(view, event) {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const stamps = view.values;
    const current = view.moment ?? view.latest?.t;
    let step;
    if (event.key === "Home") step = stamps[0];
    else if (event.key === "End") step = stamps.at(-1);
    else if (event.key === "ArrowLeft" || event.key === "ArrowDown") step = stamps.findLast(row => row.t < current);
    else step = stamps.find(row => row.t > current);
    if (step) showProbe(view, step.t);
  }

  function showProbe(view, stamp, marker) {
    if (!Number.isFinite(stamp)) return;
    const {layout, crossX, crossY, point, tip, tipBody, tipTime, tipValue, probe} = view;
    if (!layout) return;
    const {left, right, top, bottom, from, to, x, y, width} = layout;
    stamp = clamp(stamp, from, to);
    view.moment = stamp;
    const hit = marker ? null : nearest(view.values, stamp);
    const sample = hit && Math.abs(hit.t - stamp) <= view.step / 2 ? hit : null;
    const selected = sample?.t ?? stamp;
    const position = x(selected);
    const value = marker ? kinds[marker.type][0] : sample
      ? view.formatNumber(sample.value, view.digits) + (view.unit ? " " + view.unit : "") : "Нет замера";
    const time = view.formatTime(selected);
    attributes(crossX, {x1: position, x2: position, y1: top, y2: bottom});
    if (sample) {
      attributes(crossY, {x1: left, x2: right, y1: y(sample.value), y2: y(sample.value)});
      attributes(point, {cx: position, cy: y(sample.value)});
    }
    crossY.toggleAttribute("hidden", !sample);
    point.toggleAttribute("hidden", !sample);
    const tipWidth = Math.min(204, width - 18);
    const offset = position + 10 + tipWidth <= right ? position + 10 : position - tipWidth - 10;
    const tipTop = sample && y(sample.value) - top < 62 ? bottom - 54 : top + 9;
    tip.setAttribute("transform", "translate(" + clamp(offset, 8, width - tipWidth - 8) + " " + tipTop + ")");
    attributes(tipBody, {width: tipWidth, height: 44});
    tipTime.textContent = time;
    tipValue.textContent = value;
    probe.removeAttribute("hidden");
    if (!marker && view.values.length) attributes(view.cursor, {"aria-valuenow": selected, "aria-valuetext": time + ", " + value});
  }

  function drawMarkers(view, markers) {
    const {from, to, top, bottom, x, left, right} = view.layout;
    const active = new Set();
    const lanes = [left - 30, left - 30, left - 30, left - 30];
    const ordered = [...markers].filter(marker => marker && Object.hasOwn(kinds, marker.type) &&
      (marker.type !== "exit" || marker.status === "failed") && Number.isFinite(marker.time) && marker.time >= from && marker.time <= to).sort((a, b) => a.time - b.time);
    for (const marker of ordered) {
      const key = String(marker.id ?? [marker.type, marker.time, marker.worker].join("/"));
      if (active.has(key)) continue;
      active.add(key);
      let entry = view.markers.get(key);
      if (!entry) {
        const [label, glyph] = kinds[marker.type];
        const node = svg("g", {class: "history-ping", role: "button", tabindex: "0", "data-kind": marker.type});
        const title = svg("title");
        const ring = svg("circle", {r: 7, class: "history-ping-ring"});
        const body = svg("circle", {r: 7, class: "history-ping-body"});
        node.append(title, svg("circle", {r: 10, class: "history-ping-target"}), ring, body,
          svg("path", {d: glyph, class: "history-ping-icon"}));
        if (view.ready) ring.setAttribute("data-new", "");
        entry = {node, title, label, marker};
        const show = () => showProbe(view, entry.marker.time, entry.marker);
        const hide = () => view.probe.setAttribute("hidden", "");
        const open = () => view.onEvent?.(entry.marker);
        node.addEventListener("pointerenter", show);
        node.addEventListener("pointerleave", () => {
          if (document.activeElement !== node) hide();
        });
        node.addEventListener("focus", show);
        node.addEventListener("blur", hide);
        node.addEventListener("click", open);
        node.addEventListener("keydown", event => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          open();
        });
        view.markers.set(key, entry);
        view.markerLayer.append(node);
      }

      entry.marker = marker;
      const tx = x(marker.time);
      const position = clamp(tx, left + 8, right - 8);
      const free = lanes.findIndex(last => position - last >= 20);
      const lane = free < 0 ? lanes.indexOf(Math.min(...lanes)) : free;
      lanes[lane] = position;
      const y = top - 4 + lane * 19;
      entry.node.setAttribute("transform", "translate(" + position + " " + y + ")");
      const description = [entry.label, view.formatTime(marker.time), marker.worker, marker.detail]
        .filter(Boolean).join(" · ").slice(0, 280);
      entry.title.textContent = description;
      entry.node.setAttribute("aria-label", description);
      const guide = line(tx, y + 8, tx, bottom, "history-event-guide");
      guide.setAttribute("data-kind", marker.type);
      view.guides.append(guide);
      if (document.activeElement === entry.node || entry.node.matches(":hover")) showProbe(view, marker.time, marker);
    }
    for (const [key, entry] of view.markers) {
      if (active.has(key)) continue;
      if (document.activeElement === entry.node && view.values.length) view.cursor.focus({preventScroll: true});
      entry.node.remove();
      view.markers.delete(key);
    }
  }

  function axes(values, stepMin) {
    if (!values.length) return {low: 0, high: 1, step: .25};
    const min = Math.min(...values);
    const max = Math.max(...values);
    const pad = Math.max((max - min) * .12, max === min ? max * .04 : 0, stepMin);
    const rough = Math.max(stepMin, (max - min + 2 * pad) / 4);
    const magnitude = 10 ** Math.floor(Math.log10(rough));
    const step = Math.max(stepMin, magnitude * ([1, 2, 5].find(n => n >= rough / magnitude) ?? 10));
    const low = Math.max(0, Math.floor((min - pad) / step) * step);
    return {low, high: Math.max(low + step, Math.ceil((max + pad) / step) * step), step};
  }

  window.clearHistoryPlot = chart => {
    plots.delete(chart);
    chart.replaceChildren();
    chart.removeAttribute("viewBox");
    delete chart.dataset.plot;
  };

  window.renderHistoryPlot = ({chart, width, data, metric, label, unit, segments = [], markers = [],
    formatNumber, formatTime, formatTick, onEvent}) => {
    const previous = plots.get(chart);
    const view = previous?.cursor.parentNode === chart ? previous : makePlot(chart);
    view.axis.replaceChildren();
    view.series.replaceChildren();
    view.guides.replaceChildren();

    const {from, to} = data || {};
    width = Math.max(240, width || 240);
    const left = width < 415 ? 48 : 61;
    const right = width - 13;
    const top = 29;
    const bottom = height - 32;
    const digits = metric === "efficiency" ? 4 : metric === "rate" ? 3 : metric === "active" ? 0 : 1;
    const hasRange = Number.isFinite(from) && Number.isFinite(to) && from < to;
    const samples = hasRange && Array.isArray(data.series) ? data.series.slice(0, 1500)
      .filter(row => row && Number.isFinite(row.t) && row.t >= from && row.t <= to)
      .sort((a, b) => a.t - b.t) : [];
    const values = samples.filter(row => valid(row[metric])).map(row => ({t: row.t, value: row[metric]}));
    const limits = hasRange ? segments.filter(row => valid(row.limit) && Number.isFinite(row.start) &&
      Number.isFinite(row.end) && row.start < row.end && row.end > from && row.start < to) : [];
    Object.assign(view, {values, latest: values.at(-1), digits, unit, formatNumber, formatTime, onEvent,
      step: data?.step > 0 ? data.step : 15});
    chart.setAttribute("viewBox", "0 0 " + width + " " + height);
    if (!chart.hasAttribute("aria-label")) chart.setAttribute("aria-label", "История · " + label);
    chart.dataset.plot = ["edge", "hot", "hbm"].includes(metric) ? "thermal" : "default";
    view.cursor.setAttribute("tabindex", values.length ? "0" : "-1");
    view.cursor.toggleAttribute("hidden", !values.length);
    if (!hasRange) {
      view.markerLayer.replaceChildren();
      view.markers.clear();
      view.layout = null;
      view.moment = undefined;
      view.probe.setAttribute("hidden", "");
      return false;
    }

    const {low, high, step} = axes([...values.map(row => row.value), ...limits.map(row => row.limit)], 10 ** -digits);
    const x = stamp => left + (stamp - from) / (to - from) * (right - left);
    const y = value => bottom - (value - low) / (high - low) * (bottom - top);
    view.layout = {width, left, right, top, bottom, from, to, x, y};
    attributes(view.cursor, {x: left, y: top, width: right - left, height: bottom - top,
      "aria-label": label + ". Стрелки выбирают замер", "aria-valuemin": from, "aria-valuemax": to,
      "aria-valuenow": clamp(view.moment ?? view.latest?.t ?? from, from, to)});

    for (let i = 0; i <= Math.round((high - low) / step); i++) {
      const value = low + i * step;
      const py = y(value);
      view.axis.append(line(left, py, right, py, "chart-grid"));
      if (values.length || limits.length) view.axis.append(svg("text",
        {x: left - 7, y: py + 4, "text-anchor": "end", class: "chart-text"}, formatNumber(value, digits)));
    }
    if (unit && (values.length || limits.length)) {
      view.axis.append(svg("text", {x: left, y: 15, class: "chart-text"}, unit));
    }
    const ticks = width < 460 ? 2 : width < 760 ? 4 : 6;
    for (let i = 0; i <= ticks; i++) {
      const stamp = from + (to - from) * i / ticks;
      const px = x(stamp);
      view.axis.append(line(px, top, px, bottom, "chart-grid"), svg("text",
        {x: px, y: height - 11, class: "chart-text", "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle"},
        formatTick(stamp)));
    }

    let run = [];
    function flush() {
      if (!run.length) return;
      const start = x(run[0].t);
      const end = x(run.at(-1).t);
      const path = "M" + run.map(row => x(row.t) + "," + y(row.value)).join(" L");
      if (run.length === 1) view.series.append(svg("circle", {cx: start, cy: y(run[0].value), r: 2, class: "chart-point"}));
      else {
        view.series.append(svg("path", {d: path + " L" + end + "," + bottom + " L" + start + "," + bottom + " Z",
          fill: "url(#" + view.id + ")", class: "history-area"}), svg("path", {d: path, class: "chart-line"}));
      }
      run = [];
    }

    let last;
    for (const row of samples) {
      if (!valid(row[metric])) { flush(); last = undefined; continue; }
      if (last !== undefined && row.t - last > view.step * 1.15) flush();
      run.push({t: row.t, value: row[metric]});
      last = row.t;
    }
    flush();

    for (const limit of limits) {
      const py = y(limit.limit);
      const guide = line(x(Math.max(from, limit.start)), py, x(Math.min(to, limit.end)), py, "chart-limit");
      guide.append(svg("title", {}, formatNumber(limit.limit, digits) + " " + unit));
      view.guides.append(guide);
    }
    if (view.latest) {
      const {t, value} = view.latest;
      const pointX = x(t);
      const pointY = y(value);
      const stale = to - t > view.step * 1.5;
      if (stale) view.guides.append(line(pointX, pointY, pointX, bottom, "history-cutoff"));
      const lastPoint = svg("circle", {cx: pointX, cy: pointY, r: stale ? 3.5 : 4.5,
        class: "history-last", "data-stale": String(stale)});
      lastPoint.append(svg("title", {}, "Последний замер · " + formatTime(t)));
      view.guides.append(lastPoint);
    }

    if (view.hovering || document.activeElement === view.cursor) showProbe(view, view.moment ?? view.latest?.t);
    else view.probe.setAttribute("hidden", "");
    drawMarkers(view, markers);
    view.ready = true;
    return Boolean(values.length);
  };
})();
