import { createContext } from 'react';

import type { AnalysisWorkspace } from '@/api/analysis';

export const AnalysisDraftContext = createContext<{
	insert: (text: string, workspace: AnalysisWorkspace) => void;
} | null>(null);
