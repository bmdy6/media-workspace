import { useState, useSyncExternalStore } from 'react';
import { createMediaVisualState } from './visual-state';

export function useMediaVisualState() {
  const [owner] = useState(createMediaVisualState);
  const snapshot = useSyncExternalStore(owner.subscribe, owner.getSnapshot, owner.getSnapshot);
  return { owner, snapshot };
}
