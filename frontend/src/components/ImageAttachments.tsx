import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X, ZoomIn, ZoomOut } from "lucide-react";
import type { DisplayImageAttachment } from "../types";
import { safeImageSource } from "../imageSources";

function ImagePreview({
  src,
  label,
  onClose,
}: {
  src: string;
  label: string;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const [actualSize, setActualSize] = useState(false);

  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    closeButton.current?.focus();
    return () => element.close();
  }, []);

  return createPortal(
    <dialog
      ref={dialog}
      aria-label={label}
      onClose={onClose}
      onClick={(event) => {
        if (event.target === event.currentTarget) dialog.current?.close();
      }}
      className="fixed inset-0 m-auto max-h-[95vh] max-w-[95vw] rounded-xl border border-border bg-bg-secondary p-0 text-gray-200 shadow-2xl backdrop:bg-black/80"
    >
      <div className="flex items-center gap-4 border-b border-border px-4 py-2 text-sm">
        <span className="mr-auto">{label}</span>
        <button
          type="button"
          onClick={() => setActualSize((value) => !value)}
          className="flex items-center gap-2 rounded-sm p-2 hover:bg-bg-hover"
        >
          {actualSize ? <ZoomOut className="h-4 w-4" /> : <ZoomIn className="h-4 w-4" />}
          {actualSize ? "Fit to window" : "Actual size"}
        </button>
        <button
          ref={closeButton}
          type="button"
          onClick={() => dialog.current?.close()}
          aria-label="Close image preview"
          className="rounded-sm p-2 hover:bg-bg-hover"
        >
          <X className="h-5 w-5" />
        </button>
      </div>
      <div className="max-h-[80vh] max-w-[90vw] overflow-auto p-2">
        <img
          src={src}
          alt={label}
          className={actualSize ? "max-w-none" : "max-h-[76vh] max-w-full object-contain"}
        />
      </div>
    </dialog>,
    document.body
  );
}

function ImageThumbnail({ image, label }: { image: DisplayImageAttachment; label: string }) {
  const trigger = useRef<HTMLButtonElement>(null);
  const src = safeImageSource(image.src ?? `data:${image.mimeType};base64,${image.data}`);
  const [expanded, setExpanded] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setFailed(false);
  }, [src]);

  if (failed || src === null)
    return (
      <span role="status" className="rounded-sm border border-border p-3 text-xs text-gray-500">
        {label} unavailable
      </span>
    );

  return (
    <>
      <button
        ref={trigger}
        type="button"
        onClick={() => setExpanded(true)}
        aria-label={`Enlarge ${label.toLowerCase()}`}
        className="overflow-hidden rounded-lg border border-border bg-black/10 hover:border-accent focus-visible:outline focus-visible:outline-accent"
      >
        <img
          src={src}
          alt={label}
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
          className="h-40 max-w-full object-contain sm:h-48"
        />
      </button>
      {expanded && (
        <ImagePreview
          src={src}
          label={label}
          onClose={() => {
            setExpanded(false);
            trigger.current?.focus();
          }}
        />
      )}
    </>
  );
}

export function ImageAttachments({
  attachments,
  label = "Image",
}: {
  attachments: DisplayImageAttachment[];
  label?: string;
}) {
  return (
    <div className="mt-2 flex flex-wrap items-start gap-3">
      {attachments.map((image, index) => (
        <ImageThumbnail key={index} image={image} label={`${label} ${index + 1}`} />
      ))}
    </div>
  );
}
