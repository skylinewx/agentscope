import { createContext } from 'react';

/** Paths rendered from verified tool receipts at the end of this reply. */
export const DeliveredAnalysisPathsContext = createContext<readonly string[]>([]);
