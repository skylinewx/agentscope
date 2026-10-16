import type { ChatModelConfig, ModelCard } from '../api/types';

type ModelChoice = Omit<ChatModelConfig, 'parameters'>;
type ParameterCard = Pick<ModelCard, 'name' | 'parameter_schema'>;

/** Keep explicit settings when the new model declares the same parameter contract. */
export function selectChatModel(
	current: ChatModelConfig | null | undefined,
	choice: ModelChoice,
	cards: readonly ParameterCard[],
): ChatModelConfig {
	if (
		current?.type === choice.type &&
		current.credential_id === choice.credential_id &&
		current.model === choice.model
	) {
		// Selecting the active item must not issue a destructive configuration PATCH.
		return current;
	}

	let parameters: Record<string, unknown> = {};
	if (current?.type === choice.type) {
		if (current.model === choice.model) {
			parameters = { ...current.parameters };
		} else {
			const source = cards.find((card) => card.name === current.model)?.parameter_schema;
			const target = cards.find((card) => card.name === choice.model)?.parameter_schema;
			const sourceProperties = source?.properties as Record<string, unknown> | undefined;
			const targetProperties = target?.properties as Record<string, unknown> | undefined;
			// Do not carry values across providers, unknown schemas or changed constraints.
			// Include definitions because a property may refer to a shared schema via $ref.
			if (
				sourceProperties && targetProperties &&
				JSON.stringify(source?.$defs) === JSON.stringify(target?.$defs) &&
				JSON.stringify(source?.definitions) === JSON.stringify(target?.definitions)
			) {
				parameters = Object.fromEntries(
					Object.entries(current.parameters).filter(([key]) =>
						Object.hasOwn(sourceProperties, key) &&
						Object.hasOwn(targetProperties, key) &&
						JSON.stringify(sourceProperties[key]) === JSON.stringify(targetProperties[key]),
					),
				);
			}
		}
	}
	return { ...choice, parameters };
}
