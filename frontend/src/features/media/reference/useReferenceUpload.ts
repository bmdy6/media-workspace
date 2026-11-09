import { useLayoutEffect, useRef, useState } from 'react';
import { createReferenceUploadController, type ReferenceSessionPort, type ReferenceUploadSnapshot } from './controller';

export function useReferenceUpload(owner: ReferenceSessionPort) {
  const ownerRef = useRef(owner);
  const controllerRef = useRef<ReturnType<typeof createReferenceUploadController> | null>(null);
  const [snapshot, setSnapshot] = useState<ReferenceUploadSnapshot>({ preview: null, mutationCardId: null });
  useLayoutEffect(() => { ownerRef.current = owner; });
  useLayoutEffect(() => {
    const controller = createReferenceUploadController({
      capture: () => ownerRef.current.capture(), isCurrent: op => ownerRef.current.isCurrent(op),
      canUseCard: (op, cardId) => ownerRef.current.canUseCard(op, cardId),
      commitUploadedReference: (op, visual) => ownerRef.current.commitUploadedReference(op, visual),
      commitCard: (op, card) => ownerRef.current.commitCard(op, card),
      beginComposerClear: op => ownerRef.current.beginComposerClear(op),
      reportFailure: (op, error, fallback) => ownerRef.current.reportFailure(op, error, fallback),
    });
    controllerRef.current = controller;
    const unsubscribe = controller.subscribe(() => setSnapshot(controller.getSnapshot()));
    setSnapshot(controller.getSnapshot());
    return () => { unsubscribe(); controller.dispose(); controllerRef.current = null; };
  }, []);
  return {
    snapshot, prepare: (cardId: string | null) => controllerRef.current?.prepare(cardId) ?? false,
    upload: (file: File) => controllerRef.current?.upload(file) ?? Promise.resolve(null),
    removeCard: (cardId: string) => controllerRef.current?.removeCard(cardId) ?? Promise.resolve(false),
    clearComposer: () => controllerRef.current?.clearComposer() ?? Promise.resolve(false),
    cancel: () => controllerRef.current?.cancel(),
  };
}
