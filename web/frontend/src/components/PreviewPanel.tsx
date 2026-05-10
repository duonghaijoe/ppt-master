import { useState } from "react";
import { SlideDeck } from "./SlideDeck";
import { ProjectFiles } from "./ProjectFiles";

type Tab = "slides" | "files";

export function PreviewPanel({
  project,
  onAskAi,
}: {
  project: string;
  onAskAi?: (text: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("slides");

  return (
    <div className="h-full flex flex-col">
      <div className="border-b border-gray-200 bg-white px-3 pt-2 flex items-end gap-1">
        <TabButton active={tab === "slides"} onClick={() => setTab("slides")}>
          Slides
        </TabButton>
        <TabButton active={tab === "files"} onClick={() => setTab("files")}>
          Project files
        </TabButton>
        <div className="ml-auto text-xs text-gray-400 pb-2 pr-1 font-mono truncate max-w-[40%]">
          {project}
        </div>
      </div>
      <div className="flex-1 min-h-0">
        {tab === "slides" ? (
          <SlideDeck project={project} onAskAi={onAskAi} />
        ) : (
          <ProjectFiles project={project} onAskAi={onAskAi} />
        )}
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-1.5 text-sm border-b-2 -mb-px ${
        active
          ? "border-brand-green text-brand-green font-medium"
          : "border-transparent text-gray-500 hover:text-gray-700"
      }`}
    >
      {children}
    </button>
  );
}
