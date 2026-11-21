"use strict";

(() => {
  const menus = new WeakMap();
  let opened = null;

  function close(state) {
    if (opened !== state) return;
    opened = null;
    state.button.setAttribute("aria-expanded", "false");
    state.button.removeAttribute("aria-activedescendant");
    if (state.popover && state.list.matches(":popover-open")) state.list.hidePopover();
    state.list.hidden = true;
    state.query = "";
  }

  function position(state) {
    const rect = state.button.getBoundingClientRect();
    const inset = 8;
    if (!rect.width || !rect.height || rect.bottom <= inset || rect.top >= innerHeight - inset) return close(state);
    const gap = 5;
    const below = innerHeight - rect.bottom - gap - inset;
    const above = rect.top - gap - inset;
    const width = Math.min(innerWidth - 2 * inset, Math.max(rect.width, 170));
    state.list.style.width = width + "px";
    const wanted = Math.min(290, state.list.scrollHeight + 2);
    const up = below < wanted && above > below;
    const limit = Math.max(1, Math.min(wanted, up ? above : below));
    state.list.style.maxHeight = limit + "px";
    state.list.style.left = Math.min(Math.max(inset, rect.left), innerWidth - width - inset) + "px";
    const top = up ? rect.top - gap - state.list.offsetHeight : rect.bottom + gap;
    state.list.style.top = Math.max(inset, top) + "px";
  }

  function highlight(state, item) {
    state.active?.node.removeAttribute("data-active");
    state.active = item;
    if (!item) {
      state.button.removeAttribute("aria-activedescendant");
      return;
    }
    item.node.setAttribute("data-active", "");
    state.button.setAttribute("aria-activedescendant", item.node.id);
    const top = item.node.offsetTop;
    if (top < state.list.scrollTop) state.list.scrollTop = top;
    else if (top + item.node.offsetHeight > state.list.scrollTop + state.list.clientHeight) {
      state.list.scrollTop = top + item.node.offsetHeight - state.list.clientHeight;
    }
  }

  function drawOptions(state, preferEnd = false) {
    const activeIndex = state.active?.index;
    const options = [...state.select.options].filter(option => !option.hidden && !option.closest("optgroup[hidden]"));
    state.items = options.map(option => {
      const node = document.createElement("li");
      const disabled = option.disabled || Boolean(option.closest("optgroup[disabled]"));
      node.id = state.list.id + "-" + option.index;
      node.dataset.index = String(option.index);
      node.className = "select-option";
      node.setAttribute("role", "option");
      node.setAttribute("aria-selected", String(option.selected));
      if (disabled) node.setAttribute("aria-disabled", "true");
      node.textContent = option.label;
      return {index: option.index, node, disabled, label: option.label.toLocaleLowerCase()};
    });
    state.list.replaceChildren(...state.items.map(item => item.node));
    if (!state.items.length) {
      const empty = document.createElement("li");
      empty.className = "select-option";
      empty.setAttribute("role", "option");
      empty.setAttribute("aria-disabled", "true");
      empty.textContent = "Нет вариантов";
      state.list.append(empty);
    }
    position(state);
    if (opened !== state) return;
    const enabled = state.items.filter(item => !item.disabled);
    highlight(state, enabled.find(item => item.index === activeIndex)
      || enabled.find(item => item.index === state.select.selectedIndex) || enabled.at(preferEnd ? -1 : 0));
  }

  function sync(state) {
    const {select, button, value, error} = state;
    const disabled = select.matches(":disabled");
    button.tabIndex = disabled ? -1 : 0;
    button.setAttribute("aria-disabled", String(disabled));
    button.setAttribute("aria-required", String(select.required));
    value.textContent = select.selectedOptions[0]?.label || "Выберите значение";
    if (select.validity.valid) {
      button.removeAttribute("aria-invalid");
      error.hidden = true;
      error.textContent = "";
    }
    if (disabled) close(state);
    else if (opened === state) drawOptions(state);
  }

  function open(state, preferEnd = false) {
    if (state.select.matches(":disabled")) return;
    if (opened) close(opened);
    opened = state;
    state.active = null;
    state.list.hidden = false;
    state.button.setAttribute("aria-expanded", "true");
    if (state.popover) state.list.showPopover();
    drawOptions(state, preferEnd);
  }

  function commit(state, item) {
    if (!item || item.disabled || state.select.matches(":disabled")) return close(state);
    const {select} = state;
    const changed = select.selectedIndex !== item.index;
    select.selectedIndex = item.index;
    close(state);
    sync(state);
    if (changed) for (const kind of ["input", "change"]) select.dispatchEvent(new Event(kind, {bubbles: true}));
  }

  function keys(state, event) {
    if (event.altKey || event.ctrlKey || event.metaKey || state.select.matches(":disabled")) return;
    const openNow = opened === state;
    const {key} = event;
    if (key === "Tab") {
      if (openNow) commit(state, state.active);
      return;
    }
    if (key === "Escape") {
      if (!openNow) return;
      event.preventDefault();
      event.stopPropagation();
      close(state);
      return;
    }
    const navigation = ["ArrowDown", "ArrowUp", "Home", "End"].includes(key);
    if (!(navigation || key === "Enter" || key.length === 1)) return;
    event.preventDefault();
    if (key === " " || key === "Enter") {
      if (openNow) commit(state, state.active);
      else open(state);
      return;
    }
    if (!openNow) open(state, key === "ArrowUp" || key === "End");
    const enabled = state.items.filter(item => !item.disabled);
    if (navigation) {
      const index = enabled.indexOf(state.active);
      if (key === "Home") highlight(state, enabled[0]);
      else if (key === "End") highlight(state, enabled.at(-1));
      else if (openNow) highlight(state, enabled[Math.max(0, Math.min(enabled.length - 1,
        index + (key === "ArrowDown" ? 1 : -1)))]);
      return;
    }
    const now = performance.now();
    const char = key.toLocaleLowerCase();
    state.query = now - state.typedAt > 650 || state.query === char ? char : state.query + char;
    state.typedAt = now;
    const index = enabled.indexOf(state.active) + 1;
    const choices = [...enabled.slice(index), ...enabled.slice(0, index)];
    const match = choices.find(item => item.label.startsWith(state.query));
    if (match) highlight(state, match);
  }

  function enhance(select) {
    const labels = [...select.labels];
    const name = labels.map(label => {
      const text = label.cloneNode(true);
      text.querySelectorAll("select").forEach(node => node.remove());
      return text.textContent.trim();
    }).join(" ");
    const wrap = document.createElement("span");
    wrap.className = "select-menu";
    const button = document.createElement("span");
    const value = document.createElement("span");
    const error = document.createElement("span");
    const list = document.createElement("ul");
    button.className = "select-menu-button";
    value.className = "select-menu-value";
    error.className = "select-menu-error";
    error.id = select.id + "-menu-error";
    error.hidden = true;
    error.setAttribute("role", "status");
    button.setAttribute("role", "combobox");
    button.setAttribute("aria-label", name || "Выберите значение");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-haspopup", "listbox");
    list.id = select.id + "-menu-options";
    list.className = "select-options";
    list.hidden = true;
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", name || "Варианты");
    button.setAttribute("aria-controls", list.id);
    button.setAttribute("aria-describedby", [select.getAttribute("aria-describedby"), error.id].filter(Boolean).join(" "));
    const popover = typeof list.showPopover === "function";
    if (popover) list.setAttribute("popover", "manual");

    button.append(value);
    wrap.append(button, error);
    select.after(wrap);
    const dialog = select.closest("dialog");
    (dialog || document.body).append(list);
    select.classList.add("select-menu-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    const state = {select, wrap, button, value, list, error, popover, items: [], active: null, typedAt: 0, query: ""};
    menus.set(select, state);
    sync(state);

    labels.forEach(label => label.addEventListener("click", event => {
      if (wrap.contains(event.target) || event.target === select) return;
      event.preventDefault();
      if (!select.matches(":disabled")) button.focus({preventScroll: true});
    }));
    button.addEventListener("click", event => {
      event.preventDefault();
      if (select.matches(":disabled")) return;
      button.focus({preventScroll: true});
      if (opened === state) close(state);
      else open(state);
    });
    button.addEventListener("keydown", event => keys(state, event));
    button.addEventListener("blur", () => close(state));
    list.addEventListener("mousedown", event => {
      if (event.target.closest(".select-option")) event.preventDefault();
    });
    list.addEventListener("click", event => {
      const item = state.items.find(entry => entry.node === event.target.closest(".select-option"));
      if (item && !item.disabled) commit(state, item);
    });
    select.addEventListener("change", () => sync(state));
    select.addEventListener("input", () => sync(state));
    select.addEventListener("invalid", event => {
      event.preventDefault();
      button.setAttribute("aria-invalid", "true");
      error.textContent = "Выберите значение.";
      error.hidden = false;
      button.focus();
    });
    select.form?.addEventListener("reset", () => queueMicrotask(() => sync(state)));
    dialog?.addEventListener("close", () => close(state));
    new MutationObserver(() => sync(state)).observe(select, {
      childList: true, subtree: true, characterData: true, attributes: true,
      attributeFilter: ["disabled", "hidden", "required", "selected", "value", "label"],
    });
  }

  window.syncStyledSelect = select => {
    const state = menus.get(select);
    if (state) sync(state);
  };
  document.querySelectorAll("select:is(#period, #metric, #event-filter, #pool):not([multiple])").forEach(enhance);
  document.addEventListener("pointerdown", event => {
    if (opened && !opened.wrap.contains(event.target) && !opened.list.contains(event.target)) close(opened);
  }, true);
  document.addEventListener("scroll", event => {
    if (opened && !opened.list.contains(event.target)) position(opened);
  }, true);
  window.addEventListener("resize", () => { if (opened) position(opened); });
})();
