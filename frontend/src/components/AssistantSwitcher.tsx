import { useEffect, useRef } from "react";
import { ChevronDown, CircleAlert, Settings } from "lucide-react";

export type AssistantPane = "devin" | "chatgpt";

interface Props {
  active: AssistantPane;
  onSelect: (pane: AssistantPane) => void;
  devinStatus: string;
  devinNeedsAttention: boolean;
  chatgptStatus: string;
  chatgptEnabled: boolean;
  onDiagnostics: () => void;
  diagnosticsNeedsAttention: boolean;
}

export function AssistantSwitcher({ active, onSelect, devinStatus, devinNeedsAttention, chatgptStatus, chatgptEnabled, onDiagnostics, diagnosticsNeedsAttention }: Props) {
  const selector = useRef<HTMLSelectElement>(null);
  const previousActive = useRef(active);
  useEffect(() => {
    if (previousActive.current !== active && selector.current?.getClientRects().length) {
      selector.current.focus();
    }
    previousActive.current = active;
  }, [active]);
  const status = active === "devin" ? devinStatus : chatgptStatus;
  const statusColor = status === "Disconnected" || /unavailable|disconnected/i.test(status)
    ? "bg-red-400"
    : status === "Ready" || status === "Connected"
      ? "bg-green-400"
      : status === "Working" || status === "Loading" || status === "Queued" || status === "Checking connection"
        ? "bg-accent"
        : "bg-amber-400";
  const needsApproval = active === "chatgpt" && devinNeedsAttention;

  return (
    <div aria-label="Assistant status and settings" className="flex min-w-0 flex-1 items-center gap-2">
      <div className="flex min-w-0 flex-1 items-center gap-1.5">
        <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${statusColor}`} />
        {chatgptEnabled ? (
          <div className="relative shrink-0">
            <select
              ref={selector}
              aria-label="Assistant"
              value={active}
              onChange={(event) => onSelect(event.target.value as AssistantPane)}
              title={`Devin: ${devinStatus}\nChatGPT: ${chatgptStatus}`}
              className={`appearance-none rounded-sm bg-bg-secondary py-1 pr-4 text-sm font-semibold text-gray-200 hover:bg-bg-hover focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent ${active === "devin" ? "w-14" : "w-20"}`}
            >
              <option value="devin">{active === "devin" ? "Devin" : `Devin · ${devinStatus}`}</option>
              <option value="chatgpt">{active === "chatgpt" ? "ChatGPT" : `ChatGPT · ${chatgptStatus}`}</option>
            </select>
            <ChevronDown aria-hidden="true" className="pointer-events-none absolute right-0 top-1/2 h-3 w-3 -translate-y-1/2 text-gray-500" />
          </div>
        ) : <span className="shrink-0 text-sm font-semibold text-gray-200">Devin</span>}
        <span role="status" title={status} aria-label={`${active === "devin" ? "Devin" : "ChatGPT"} status: ${status}`} className={`min-w-0 truncate text-xs ${status === "Approval needed" ? "text-amber-300" : "text-gray-400"}`}>{status}</span>
        {needsApproval && (
          <button
            type="button"
            aria-label="Devin needs approval — switch to Devin"
            title="Devin needs approval — switch to Devin"
            onClick={() => onSelect("devin")}
            className="shrink-0 rounded-sm p-1 text-amber-300 hover:bg-bg-hover"
          >
            <CircleAlert className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      <button
        type="button"
        aria-label="Diagnostics & integrations"
        title={diagnosticsNeedsAttention ? "Diagnostics & integrations — setup needs attention" : "Diagnostics & integrations"}
        onClick={onDiagnostics}
        className="relative shrink-0 rounded-sm p-1.5 text-gray-400 hover:bg-bg-hover hover:text-gray-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
      >
        <Settings className="h-4 w-4" />
        {diagnosticsNeedsAttention && <span aria-label="Setup needs attention" className="absolute right-0.5 top-0.5 h-1.5 w-1.5 rounded-full bg-amber-400" />}
      </button>
    </div>
  );
}
