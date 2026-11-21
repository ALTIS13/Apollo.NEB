"use strict";

(() => {
  const readings = new WeakMap();
  const trendHoldMs = 10000;

  function writeTrend(arrow, direction) {
    const up = direction > 0;
    const label = direction ? (up ? "Выше предыдущего показания" : "Ниже предыдущего показания") : "";
    arrow.textContent = direction ? (up ? "↑" : "↓") : "";
    arrow.dataset.direction = direction ? (up ? "up" : "down") : "";
    arrow.title = label;
    arrow.setAttribute("aria-hidden", String(!label));
    if (label) {
      arrow.setAttribute("role", "img");
      arrow.setAttribute("aria-label", label);
    }
  }

  window.showLiveReading = (element, label, value) => {
    let reading = readings.get(element);
    if (!reading) {
      const number = document.createElement("span");
      const arrow = document.createElement("span");
      number.className = "metric-value";
      arrow.className = "metric-trend";
      arrow.setAttribute("aria-hidden", "true");
      element.replaceChildren(number, arrow);
      reading = {number, arrow, value: null, until: 0};
      readings.set(element, reading);
    }

    const now = performance.now();
    if (reading.number.textContent !== label) {
      const direction = Number.isFinite(reading.value) && Number.isFinite(value) ? Math.sign(value - reading.value) : 0;
      writeTrend(reading.arrow, direction);
      reading.until = now + trendHoldMs;
      reading.number.textContent = label;
    }
    if (now >= reading.until && reading.arrow.textContent) writeTrend(reading.arrow, 0);
    reading.value = value;
  };
})();
