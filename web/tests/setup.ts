import "@testing-library/jest-dom";

// A ResizeObserver that reports each element once, just after it is observed.
globalThis.ResizeObserver = class {
  constructor(private readonly report: ResizeObserverCallback) {}
  observe(el: Element) {
    queueMicrotask(() => this.report([{ target: el } as ResizeObserverEntry], this));
  }
  unobserve() {}
  disconnect() {}
};
