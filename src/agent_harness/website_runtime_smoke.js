"use strict";

// A dependency-free DOM/canvas smoke harness for generated static game sites.
// It does not judge gameplay quality. It only proves that every registered
// game can enter its factory once without a synchronous runtime exception.

const fs = require("fs");
const vm = require("vm");

const appPath = process.argv[2];
const manifestPath = process.argv[3];
if (!appPath || !manifestPath) {
  process.stderr.write("usage: node website_runtime_smoke.js APP_JS MANIFEST_JSON\n");
  process.exit(2);
}

const source = fs.readFileSync(appPath, "utf8");
const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
const runtimeErrors = [];
const nodesById = new Map();

class StubEventTarget {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    if (typeof listener !== "function") return;
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    this.listeners.set(type, listeners.filter((item) => item !== listener));
  }

  dispatchEvent(event) {
    const normalized = typeof event === "string" ? { type: event } : event;
    normalized.preventDefault = normalized.preventDefault || (() => {});
    normalized.stopPropagation = normalized.stopPropagation || (() => {});
    for (const listener of [...(this.listeners.get(normalized.type) || [])]) {
      listener.call(this, normalized);
    }
    return true;
  }
}

class StubNode extends StubEventTarget {
  constructor(tagName = "div", id = "") {
    super();
    this.tagName = String(tagName).toUpperCase();
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.className = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.width = 300;
    this.height = 150;
    this._innerHTML = "";
    this._queryNodes = new Map();
    this.classList = {
      toggle: (name, force) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        const enabled = force === undefined ? !names.has(name) : Boolean(force);
        if (enabled) names.add(name); else names.delete(name);
        this.className = [...names].join(" ");
        return enabled;
      },
      add: (...names) => {
        const current = new Set(this.className.split(/\s+/).filter(Boolean));
        names.forEach((name) => current.add(name));
        this.className = [...current].join(" ");
      },
      remove: (...names) => {
        const current = new Set(this.className.split(/\s+/).filter(Boolean));
        names.forEach((name) => current.delete(name));
        this.className = [...current].join(" ");
      },
    };
  }

  appendChild(child) {
    if (child == null) return child;
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  append(...children) {
    children.forEach((child) => this.appendChild(
      typeof child === "string" ? new StubNode("#text") : child
    ));
  }

  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    if (child) child.parentNode = null;
    return child;
  }

  get firstChild() {
    return this.children[0] || null;
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    this.children = [];
    for (const match of this._innerHTML.matchAll(/\bid=["']([^"']+)["']/g)) {
      const node = new StubNode("div", match[1]);
      node.parentNode = this;
      this._queryNodes.set("#" + match[1], node);
    }
  }

  get innerHTML() {
    return this._innerHTML;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "id") {
      this.id = String(value);
      nodesById.set(this.id, this);
    }
    if (name === "class") this.className = String(value);
    if (name === "style") this.style.cssText = String(value);
  }

  querySelector(selector) {
    if (!this._queryNodes.has(selector)) {
      this._queryNodes.set(selector, new StubNode("div"));
    }
    return this._queryNodes.get(selector);
  }

  querySelectorAll(selector) {
    if (selector.startsWith(".")) {
      const className = selector.slice(1);
      return this.children.filter((child) => (
        child.className || ""
      ).split(/\s+/).includes(className));
    }
    return [];
  }

  getBoundingClientRect() {
    return {
      left: 0,
      top: 0,
      width: this.width || 300,
      height: this.height || 150,
      right: this.width || 300,
      bottom: this.height || 150,
    };
  }

  focus() {}

  getContext() {
    const gradient = { addColorStop() {} };
    const methods = {
      createLinearGradient: () => gradient,
      measureText: () => ({ width: 0 }),
    };
    return new Proxy(methods, {
      get(target, property) {
        if (property in target) return target[property];
        return () => {};
      },
      set(target, property, value) {
        target[property] = value;
        return true;
      },
    });
  }
}

class StubDocument extends StubEventTarget {
  constructor() {
    super();
    this.readyState = "loading";
  }

  createElement(tagName) {
    return new StubNode(tagName);
  }

  createTextNode(text) {
    const node = new StubNode("#text");
    node.textContent = String(text);
    return node;
  }

  getElementById(id) {
    if (!nodesById.has(id)) nodesById.set(id, new StubNode("div", id));
    return nodesById.get(id);
  }
}

const document = new StubDocument();
for (const [id, tagName] of [
  ["lobby", "main"],
  ["game-list", "ul"],
  ["play-area", "section"],
  ["play-title", "h2"],
  ["play-instructions", "p"],
  ["play-controls", "p"],
  ["game-stage", "div"],
  ["hud", "div"],
  ["back-to-lobby", "button"],
  ["restart-game", "button"],
]) {
  nodesById.set(id, new StubNode(tagName, id));
}
nodesById.get("play-area").hidden = true;

const windowObject = new StubEventTarget();
const storage = new Map();
const localStorage = {
  getItem: (key) => storage.has(key) ? storage.get(key) : null,
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: (key) => storage.delete(key),
  clear: () => storage.clear(),
};
let timerId = 0;
const noTimer = () => ++timerId;
const sandbox = {
  window: windowObject,
  document,
  localStorage,
  console: {
    log() {},
    warn() {},
    error: (...values) => runtimeErrors.push(values.map((value) => (
      value && value.stack ? value.stack : String(value)
    )).join(" ")),
  },
  performance: { now: () => 0 },
  requestAnimationFrame: noTimer,
  cancelAnimationFrame() {},
  setTimeout: noTimer,
  clearTimeout() {},
  setInterval: noTimer,
  clearInterval() {},
};
for (const globalName of ["GameHub", "Hub"]) {
  Object.defineProperty(sandbox, globalName, {
    configurable: true,
    get: () => windowObject[globalName],
    set: (value) => { windowObject[globalName] = value; },
  });
}
windowObject.window = windowObject;
windowObject.document = document;
windowObject.localStorage = localStorage;
windowObject.performance = sandbox.performance;
windowObject.requestAnimationFrame = sandbox.requestAnimationFrame;
windowObject.cancelAnimationFrame = sandbox.cancelAnimationFrame;
windowObject.setTimeout = sandbox.setTimeout;
windowObject.clearTimeout = sandbox.clearTimeout;
windowObject.setInterval = sandbox.setInterval;
windowObject.clearInterval = sandbox.clearInterval;
windowObject.scrollTo = () => {};

vm.createContext(sandbox);
try {
  vm.runInContext(source, sandbox, { filename: appPath, timeout: 2_000 });
  document.readyState = "complete";
  document.dispatchEvent({ type: "DOMContentLoaded" });
} catch (error) {
  runtimeErrors.push(error && error.stack ? error.stack : String(error));
}

const failures = [];
const gameIds = Array.isArray(manifest)
  ? manifest.map((item) => item && item.id).filter(Boolean)
  : [];
const gameHub = windowObject.GameHub;
if (!gameHub || typeof gameHub.get !== "function" || typeof gameHub.list !== "function") {
  failures.push({ id: "<bootstrap>", error: "window.GameHub registry is unavailable" });
} else {
  const entries = gameHub.list();
  const cards = [...document.getElementById("game-list").children];
  for (const id of gameIds) {
    const entryIndex = entries.findIndex((entry) => entry && entry.meta && entry.meta.id === id);
    const card = entryIndex >= 0 ? cards[entryIndex] : null;
    if (!gameHub.get(id) || !card) {
      failures.push({ id, error: "registered game has no launchable lobby card" });
      continue;
    }
    const errorOffset = runtimeErrors.length;
    const stage = document.getElementById("game-stage");
    stage.innerHTML = "";
    stage.textContent = "";
    try {
      card.dispatchEvent({ type: "click" });
    } catch (error) {
      runtimeErrors.push(error && error.stack ? error.stack : String(error));
    }
    const newErrors = runtimeErrors.slice(errorOffset);
    if (newErrors.length) {
      failures.push({ id, error: newErrors.join("\n") });
    } else if (
      stage.children.length === 0 &&
      !String(stage.innerHTML || "").trim() &&
      !String(stage.textContent || "").trim()
    ) {
      failures.push({ id, error: "game factory returned without rendering stage content" });
    }
    document.getElementById("back-to-lobby").dispatchEvent({ type: "click" });
  }
}

const result = {
  passed: failures.length === 0 && runtimeErrors.length === 0,
  checked_games: gameIds.length,
  failures,
  bootstrap_errors: runtimeErrors.slice(0, failures.length ? 0 : runtimeErrors.length),
};
process.stdout.write(JSON.stringify(result));
process.exit(result.passed ? 0 : 1);
