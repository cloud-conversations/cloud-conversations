import { useCallback, useEffect, useRef, useState } from "react";
import { browserStorage } from "../storage";

const KEY = "cc.sidebarWidth";
const DEFAULT = 256;
export const MIN = 160;
export const MAX = 640;

function clamp(px: number): number {
  return Math.min(MAX, Math.max(MIN, px));
}

function remembered(): number {
  const saved = Number(browserStorage()?.getItem(KEY));
  return saved ? clamp(saved) : DEFAULT;
}

/** The sidebar's width in pixels, dragged with the mouse and kept per browser. */
export function useSidebarWidth() {
  const [width, setWidth] = useState(remembered);
  const dragging = useRef(false);

  const start = useCallback(() => {
    dragging.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  useEffect(() => {
    function move(e: MouseEvent) {
      if (dragging.current) setWidth(clamp(e.clientX));
    }
    function stop() {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", stop);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", stop);
      stop();
    };
  }, []);

  useEffect(() => {
    browserStorage()?.setItem(KEY, String(width));
  }, [width]);

  return {
    width,
    start,
    nudge: (by: number) => setWidth((w) => clamp(w + by)),
    reset: () => setWidth(DEFAULT),
  };
}
