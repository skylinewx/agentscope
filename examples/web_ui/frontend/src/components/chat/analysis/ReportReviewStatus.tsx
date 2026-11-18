import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Disclosure } from '@/components/ui/disclosure';
import type { ReportDelivery } from '@/lib/workspace-analysis';

const scopes: Record<string, string> = {
	SOURCE_AND_DELIVERY: '来源、范围及报告内容', COMPLETE_PYTHON_EXECUTIONS: '完整输入与 Python 计算', COMPLETE_REPORT: '报告与计算依据',
	REPORT_WITH_REVIEWED_COMPUTATIONS: '报告内容（复用已审计算）',
	CALCULATION: '计算专项审核', FINAL_BUSINESS: '最终内容确认',
};
const states: Record<string, string> = {
	PASS: '已完成，本范围未发现阻断问题', REVISE: '存在需修正问题', PENDING: '尚未完成', UNAVAILABLE: '调用未完成，尚无审查结论',
};

export function ReportReviewStatus({ delivery }: { delivery: ReportDelivery }) {
	const pending = delivery.semanticReview !== 'PASS';
	const title = delivery.semanticReview === 'REVISE' ? '报告需要修正' : pending ? '当前成果，审核尚未完成' : '当前成果审核已完成';
	return (
		<Alert variant={delivery.semanticReview === 'REVISE' ? 'destructive' : 'default'}>
			<AlertTitle>{title}</AlertTitle>
			<AlertDescription>
				<p>{delivery.notice || (pending ? '完整内容已保存，语义审查尚未全部完成。' : '当前成果已完成必要审核。')}</p>
				<Disclosure summary="查看完成范围与审核详情">
					<p>程序检查：{delivery.programChecks === 'PASS' ? '通过' : delivery.programChecks}；已声明工作：{delivery.requirementsState === 'READY' ? '完成' : delivery.requirementsState}</p>
					{delivery.calculationReview && !delivery.review?.parts.some(part => part.kind === 'CALCULATION') && <section aria-label="计算专项审核意见">
						<p>计算专项审核：{states[delivery.calculationReview.status] || delivery.calculationReview.status}</p>
						{delivery.calculationReview.issues?.map((issue, index) => <p key={index}>{issue.message}</p>)}
					</section>}
					{delivery.review?.parts.map(part => (
						<section key={part.index} aria-label={`审查 ${part.index}`}>
							<p>{scopes[part.kind] || part.kind}：{states[part.status] || part.status}</p>
							{part.issues?.map((issue, index) => <p key={index}>{issue.message || issue.statement || issue.code}{issue.evidenceRefs?.length ? `（依据：${issue.evidenceRefs.join('、')}）` : ''}</p>)}
						</section>
					))}
					{delivery.newerCandidate && <section aria-label="较新候选的审查详情">
						<p>较新候选未被采用；以下意见不代表所交付版本存在同样问题。</p>
						<p>较新候选程序检查：{delivery.newerCandidate.programChecks === 'PASS' ? '通过' : '未通过'}</p>
						{delivery.newerCandidate.review?.parts.map(part => <section key={part.index}>
							<p>{scopes[part.kind] || part.kind}：{states[part.status] || part.status}</p>
							{part.issues?.map((issue, index) => <p key={index}>{issue.message || issue.statement || issue.code}{issue.evidenceRefs?.length ? `（依据：${issue.evidenceRefs.join('、')}）` : ''}</p>)}
						</section>)}
					</section>}
				</Disclosure>
			</AlertDescription>
		</Alert>
	);
}
