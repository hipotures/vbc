/*!
 * Minimal theme switcher
 *
 * Pico.css - https://picocss.com
 * Copyright 2019-2024 - Licensed under MIT
 */

const themeSwitcher = {
  _scheme: "auto",
  menuTarget: "details.dropdown",
  buttonsTarget: "a[data-theme-switcher]",
  buttonAttribute: "data-theme-switcher",
  rootAttribute: "data-theme",
  localStorageKey: "picoPreferredColorScheme",

  init() {
    this.scheme = this.schemeFromLocalStorage;
    this.initSwitchers();
  },

  get schemeFromLocalStorage() {
    return window.localStorage?.getItem(this.localStorageKey) ?? this._scheme;
  },

  get preferredColorScheme() {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  },

  initSwitchers() {
    const buttons = document.querySelectorAll(this.buttonsTarget);
    buttons.forEach((button) => {
      button.addEventListener(
        "click",
        (event) => {
          event.preventDefault();
          this.scheme = button.getAttribute(this.buttonAttribute);
          document.querySelector(this.menuTarget)?.removeAttribute("open");
        },
        false
      );
    });
  },

  set scheme(scheme) {
    if (scheme == "auto") {
      this._scheme = this.preferredColorScheme;
    } else if (scheme == "dark" || scheme == "light") {
      this._scheme = scheme;
    }
    this.applyScheme();
    this.schemeToLocalStorage();
  },

  get scheme() {
    return this._scheme;
  },

  applyScheme() {
    document.querySelector("html")?.setAttribute(this.rootAttribute, this.scheme);
  },

  schemeToLocalStorage() {
    window.localStorage?.setItem(this.localStorageKey, this.scheme);
  },
};

themeSwitcher.init();
