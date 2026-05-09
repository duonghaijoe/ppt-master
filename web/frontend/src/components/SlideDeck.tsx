import { useState } from "react";
import { useSlideWatcher } from "../hooks/useSlideWatcher";

export function SlideDeck({ project }: { project: string }) {
  const { slides, version } = useSlideWatcher(project);
  const [index, setIndex] = useState(0);

  if (slides.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-gray-400">
        No slides yet — ask the agent to start generating.
      </div>
    );
  }

  const safeIndex = Math.min(index, slides.length - 1);
  const current = slides[safeIndex];
  const url = `/api/projects/${encodeURIComponent(project)}/svg/${current.file}?v=${version}-${current.mtime}`;

  return (
    <div className="h-full flex flex-col">
      <header className="flex items-center justify-between px-4 py-2 border-b border-gray-200 bg-white">
        <div className="text-sm">
          <span className="font-mono">{current.name}</span>
          <span className="text-gray-400 ml-2">
            {safeIndex + 1} / {slides.length}
          </span>
        </div>
        <a
          href={`/api/projects/${encodeURIComponent(project)}/export.pptx`}
          className="text-sm px-3 py-1 border border-brand-green text-brand-green rounded hover:bg-brand-green hover:text-white"
        >
          Download .pptx
        </a>
      </header>
      <div className="flex-1 flex items-center justify-center p-6 bg-gray-100 overflow-auto">
        <div className="bg-white shadow-lg" style={{ aspectRatio: "16 / 9", width: "min(100%, 1280px)" }}>
          <object
            type="image/svg+xml"
            data={url}
            className="w-full h-full"
            aria-label={current.name}
          />
        </div>
      </div>
      <nav className="border-t border-gray-200 bg-white p-2 flex gap-2 overflow-x-auto">
        {slides.map((s, i) => (
          <button
            key={s.file}
            onClick={() => setIndex(i)}
            className={`shrink-0 w-32 h-18 border rounded overflow-hidden text-xs ${
              i === safeIndex ? "border-brand-green ring-2 ring-brand-green/30" : "border-gray-200"
            }`}
            style={{ aspectRatio: "16 / 9" }}
          >
            <object
              type="image/svg+xml"
              data={`/api/projects/${encodeURIComponent(project)}/svg/${s.file}?v=${version}-${s.mtime}`}
              className="w-full h-full pointer-events-none"
            />
          </button>
        ))}
      </nav>
    </div>
  );
}
