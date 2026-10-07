import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(cleanup);
afterEach(() => vi.unstubAllGlobals());

Object.defineProperty(Element.prototype, "scrollIntoView", {
  value: vi.fn(),
  configurable: true,
});

// jsdom does not implement native dialog APIs; emulate visibility for DOM tests.
for (const method of ["show", "showModal"] as const) {
  Object.defineProperty(HTMLDialogElement.prototype, method, {
    configurable: true,
    value(this: HTMLDialogElement): void {
      this.setAttribute("open", "");
      this.querySelector<HTMLElement>(
        "[autofocus], button:not(:disabled)",
      )?.focus();
    },
  });
}
Object.defineProperty(HTMLDialogElement.prototype, "close", {
  configurable: true,
  value(this: HTMLDialogElement): void {
    this.removeAttribute("open");
  },
});
Object.defineProperty(window, "matchMedia", {
  value: vi.fn(() => ({
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
  configurable: true,
});
