import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(cleanup);
afterEach(() => vi.unstubAllGlobals());

Object.defineProperty(Element.prototype, "scrollIntoView", {
  value: vi.fn(),
  configurable: true,
});
Object.defineProperty(window, "matchMedia", {
  value: vi.fn(() => ({
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
  configurable: true,
});
