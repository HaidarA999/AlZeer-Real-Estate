/* ============================================
  Custom Select — يحوّل أي <select class="js-custom-select"> لقائمة منسدلة بتصميم الموقع
  الـ <select> الأصلي بيضل موجود ومخفي، فالفورم والـ JS القديم بيشتغلوا متل ما هنن.
============================================ */
(function () {
  "use strict";

  const nativeValue = Object.getOwnPropertyDescriptor(
    HTMLSelectElement.prototype,
    "value"
  );
  let uid = 0;
  let openInstance = null;

  function enhance(select) {
    if (select.dataset.csReady) return;
    select.dataset.csReady = "1";
    const id = ++uid;

    const wrap = document.createElement("div");
    wrap.className = "cs";
    if (select.dataset.csClass) wrap.classList.add(select.dataset.csClass);

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "cs-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", "cs-menu-" + id);
    trigger.innerHTML =
      '<span class="cs-value"></span>' +
      '<svg class="cs-arrow" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
    const valueEl = trigger.querySelector(".cs-value");

    const menu = document.createElement("ul");
    menu.className = "cs-menu";
    menu.id = "cs-menu-" + id;
    menu.setAttribute("role", "listbox");
    menu.tabIndex = -1;

    select.classList.add("cs-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    wrap.appendChild(trigger);
    document.body.appendChild(menu);

    let items = [];
    let activeIndex = -1;
    let typed = "";
    let typedTimer;

    function build() {
      menu.innerHTML = "";
      items = [...select.options].map((opt, i) => {
        const li = document.createElement("li");
        li.className = "cs-option";
        li.setAttribute("role", "option");
        li.dataset.index = i;
        li.innerHTML =
          "<span></span>" +
          '<svg class="cs-check" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 5 5L20 7"/></svg>';
        li.firstChild.textContent = opt.textContent;
        if (opt.disabled) li.setAttribute("aria-disabled", "true");
        menu.appendChild(li);
        return li;
      });
      sync();
    }

    function sync() {
      const sel = select.selectedIndex;
      valueEl.textContent = sel >= 0 ? select.options[sel].textContent : "";
      items.forEach((li, i) => {
        const on = i === sel;
        li.classList.toggle("selected", on);
        li.setAttribute("aria-selected", on ? "true" : "false");
      });
      trigger.classList.toggle("is-placeholder", select.value === "all");
    }

    function setActive(i, scroll = true) {
      if (!items.length) return;
      activeIndex = Math.max(0, Math.min(items.length - 1, i));
      items.forEach((li, k) => li.classList.toggle("active", k === activeIndex));
      trigger.setAttribute("aria-activedescendant", "");
      if (scroll) items[activeIndex].scrollIntoView({ block: "nearest" });
    }

    function position() {
      const r = trigger.getBoundingClientRect();
      const gap = 6;
      menu.style.minWidth = r.width + "px";
      menu.style.maxHeight = "";
      const h = Math.min(menu.scrollHeight, 320);
      const below = window.innerHeight - r.bottom - gap - 12;
      const above = r.top - gap - 12;
      const openUp = below < h && above > below;
      const maxH = Math.max(120, Math.min(320, openUp ? above : below));
      menu.style.maxHeight = maxH + "px";
      const realH = Math.min(menu.scrollHeight, maxH);
      menu.style.top = (openUp ? r.top - gap - realH : r.bottom + gap) + "px";
      menu.style.right = "auto";
      const w = Math.max(r.width, menu.offsetWidth);
      let left = r.right - w; // RTL: نحاذي الحافة اليمين
      left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
      menu.style.left = left + "px";
      menu.classList.toggle("open-up", openUp);
    }

    function open() {
      if (openInstance && openInstance !== api) openInstance.close();
      openInstance = api;
      wrap.classList.add("is-open");
      menu.classList.add("show");
      trigger.setAttribute("aria-expanded", "true");
      position();
      setActive(select.selectedIndex);
    }

    function close(refocus) {
      if (openInstance === api) openInstance = null;
      wrap.classList.remove("is-open");
      menu.classList.remove("show");
      trigger.setAttribute("aria-expanded", "false");
      if (refocus) trigger.focus();
    }

    function choose(i) {
      const opt = select.options[i];
      if (!opt || opt.disabled) return;
      const changed = select.selectedIndex !== i;
      nativeValue.set.call(select, opt.value);
      sync();
      close(true);
      if (changed) select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    const api = { close, position, menu, trigger };

    trigger.addEventListener("click", () =>
      menu.classList.contains("show") ? close() : open()
    );

    trigger.addEventListener("keydown", (e) => {
      const isOpen = menu.classList.contains("show");
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          isOpen ? setActive(activeIndex + 1) : open();
          break;
        case "ArrowUp":
          e.preventDefault();
          isOpen ? setActive(activeIndex - 1) : open();
          break;
        case "Home":
          if (isOpen) { e.preventDefault(); setActive(0); }
          break;
        case "End":
          if (isOpen) { e.preventDefault(); setActive(items.length - 1); }
          break;
        case "Enter":
        case " ":
          if (isOpen) { e.preventDefault(); choose(activeIndex); }
          break;
        case "Escape":
          if (isOpen) { e.preventDefault(); close(true); }
          break;
        case "Tab":
          if (isOpen) close();
          break;
        default:
          if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
            typed += e.key.toLowerCase();
            clearTimeout(typedTimer);
            typedTimer = setTimeout(() => (typed = ""), 600);
            const hit = [...select.options].findIndex((o) =>
              o.textContent.trim().toLowerCase().startsWith(typed)
            );
            if (hit > -1) {
              isOpen ? setActive(hit) : choose(hit);
            }
          }
      }
    });

    menu.addEventListener("mousemove", (e) => {
      const li = e.target.closest(".cs-option");
      if (li) setActive(+li.dataset.index, false);
    });

    menu.addEventListener("click", (e) => {
      const li = e.target.closest(".cs-option");
      if (li) choose(+li.dataset.index);
    });

    // لما حدا يضغط على الـ <label for="...">
    select.addEventListener("focus", () => trigger.focus());

    // لما الـ JS القديم يغيّر الخيارات (مثلاً تعبئة المناطق) أو القيمة (تصفير الفلاتر)
    new MutationObserver(build).observe(select, { childList: true, subtree: true });
    select.addEventListener("change", sync);
    Object.defineProperty(select, "value", {
      configurable: true,
      get() { return nativeValue.get.call(this); },
      set(v) { nativeValue.set.call(this, v); sync(); },
    });
    if (select.form) select.form.addEventListener("reset", () => setTimeout(sync));

    build();
  }

  document.addEventListener("click", (e) => {
    if (!openInstance) return;
    const inside =
      openInstance.menu.contains(e.target) || openInstance.trigger.contains(e.target);
    if (!inside) openInstance.close();
  });
  window.addEventListener("resize", () => openInstance && openInstance.close());
  window.addEventListener(
    "scroll",
    (e) => {
      if (!openInstance) return;
      if (openInstance.menu.contains(e.target)) return;
      openInstance.position();
    },
    true
  );

  function init() {
    document.querySelectorAll("select.js-custom-select").forEach(enhance);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
