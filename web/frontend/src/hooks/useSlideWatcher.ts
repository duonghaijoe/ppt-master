import { useEffect, useState } from "react";

export type SlideMeta = { name: string; file: string; mtime: number };

export function useSlideWatcher(project: string) {
  const [slides, setSlides] = useState<SlideMeta[]>([]);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const res = await fetch(`/api/projects/${encodeURIComponent(project)}/slides`);
      if (cancelled) return;
      const data = await res.json();
      setSlides(data.slides ?? []);
    }
    load();

    const es = new EventSource(`/api/projects/${encodeURIComponent(project)}/events`);
    es.onmessage = (ev) => {
      try {
        const evt = JSON.parse(ev.data) as { kind: string; path: string };
        if (evt.path.startsWith("svg_output/")) {
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
  }, [project]);

  return { slides, version };
}
