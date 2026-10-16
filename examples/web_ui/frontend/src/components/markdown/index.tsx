import { cjk } from '@streamdown/cjk';
import { code } from '@streamdown/code';
import { math } from '@streamdown/math';
import { mermaid } from '@streamdown/mermaid';
import * as React from 'react';
import { Streamdown } from 'streamdown';
import type { LinkSafetyModalProps } from 'streamdown';

import { LinkSafetyDialog } from './LinkSafetyDialog';
import { cn } from '@/lib/utils';

const DEFAULT_PLUGINS = { code, math, cjk, mermaid };
const renderLinkSafety = (props: LinkSafetyModalProps) => <LinkSafetyDialog {...props} />;

function Markdown({
	className,
	plugins = DEFAULT_PLUGINS,
	controls = false,
	linkSafety,
	...props
}: React.ComponentProps<typeof Streamdown>) {
	const safeLinks = React.useMemo(() => ({ enabled: true, ...linkSafety, renderModal: linkSafety?.renderModal ?? renderLinkSafety }), [linkSafety]);
	return (
		<Streamdown
			data-slot="markdown"
			plugins={plugins}
			controls={controls}
			linkSafety={safeLinks}
			className={cn('cn-markdown w-full min-w-0 overflow-hidden', className)}
			{...props}
		/>
	);
}

export { Markdown };
