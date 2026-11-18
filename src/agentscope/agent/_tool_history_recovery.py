"""Repair missing protocol receipts without replaying an unknown tool action."""
from ..message import ToolCallBlock, ToolResultBlock, ToolCallState, ToolResultState


async def restore_persisted_tool_results(messages, read_message):
    """Consult only the owning session's matching reply, never another session.

    A complete matching durable receipt wins over an incomplete context snapshot.
    Reading history does not execute a tool or infer whether a write succeeded.
    """
    results = {block.id for message in messages for block in message.content if isinstance(block, ToolResultBlock)}
    restored = []
    for message in messages:
        calls = {block.id: block for block in message.content if isinstance(block, ToolCallBlock)
                 and block.id not in results and block.state not in (ToolCallState.ASKING, ToolCallState.SUBMITTED)}
        if message.role != 'assistant' or not calls: continue
        saved = await read_message(message.id)
        if saved is None or saved.role != 'assistant' or saved.id != message.id: continue
        saved_calls = {block.id: block for block in saved.content if isinstance(block, ToolCallBlock)}
        for block in saved.content:
            call, recorded = calls.get(block.id), saved_calls.get(block.id)
            if (not isinstance(block, ToolResultBlock) or call is None or recorded is None
                    or block.state not in (ToolResultState.SUCCESS, ToolResultState.ERROR)
                    or call.name != recorded.name or call.input != recorded.input or block.name != call.name): continue
            message.content.append(block.model_copy(deep=True))
            call.state = ToolCallState.FINISHED
            message.metadata.setdefault('restoredToolResultIds', []).append(call.id)
            results.add(call.id); restored.append(call.id)
            calls.pop(call.id)
    return restored


def repair_tool_history(messages):
    results = {block.id for message in messages for block in message.content if isinstance(block, ToolResultBlock)}
    repaired = []
    for message in messages:
        if message.role != 'assistant': continue
        for block in list(message.content):
            if not isinstance(block, ToolCallBlock) or block.id in results: continue
            # Live permission/external execution continuations have their own
            # state machine. They are not failed storage receipts.
            if block.state in (ToolCallState.ASKING, ToolCallState.SUBMITTED): continue
            message.content.append(ToolResultBlock(id=block.id, name=block.name,
                state=ToolResultState.ERROR,
                output='HOST_TOOL_RESULT_UNAVAILABLE: 此历史调用缺少完整结果记录，执行结果未知。'
                       '宿主仅修复消息配对，没有重放工具。先读取已保存结果或核实外部状态；'
                       '不要认定执行成功，也不要自动重复可能有副作用的操作。'))
            block.state = ToolCallState.FINISHED
            message.metadata.setdefault('recoveredToolResultIds', []).append(block.id)
            results.add(block.id)
            repaired.append(block.id)
    return repaired
