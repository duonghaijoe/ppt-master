import { useEffect, useState } from "react";

// `path` is the project-relative path to the slide (e.g. `svg_output/01.svg`
// or `flashcards/templates/A_clean_front.svg`). It's what SlideEditor uses to
// build /file and /svg-save URLs — `file` (basename) is kept for back-compat
// with consumers that still key by filename.
export type SlideMeta = {
  name: string;
  file: string;
  path: string;
  mtime: number;
};

export function useSlideWatcher(project: string, dir: string) {
  const [slides, setSlides] = useState<SlideMeta[]>([]);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const prefix = dir.replace(/\/+$/, "");
    async function load() {
      const qs = dir
        ? `?dir=${encodeURIComponent(prefix)}`
        : "";
      const res = await fetch(`/api/projects/${encodeURIComponent(project)}/slides${qs}`);
      if (cancelled) return;
      const data = await res.json();
      setSlides(data.slides ?? []);
    }
    load();

    const es = new EventSource(`/api/projects/${encodeURIComponent(project)}/events`);
    es.onmessage = (ev) => {
      try {
        const evt = JSON.parse(ev.data) as { kind: string; path: string };
        // Refresh whenever something inside the watched dir changes — and
        // fall back to refresh-on-anything if no dir is set (legacy callers).
        if (!prefix || evt.path.startsWith(`${prefix}/`)) {
          setVersion((v) => v + 1);
          load();
        }
      } catch {
        /* ignore */
      }
    };
    es.onerror = () => {
      // browser auto-retries
    };

    return () => {
      cancelled = true;
      es.close();
    };
  }, [project, dir]);

  return { slides, version };
}
