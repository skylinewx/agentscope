import { client } from './client';
import type { SaveSkillFilesRequest, SkillFile, SkillRecord, SkillView } from './types';

/**
 * The user's own library of installed skills, which is where a hub install
 * lands. Separate from `workspaceApi.skill`, which manages the skills one
 * session's workspace actually holds.
 */
export const skillApi = {
	list: () => client.get<SkillView[]>('/skill'),
	create: (body: SaveSkillFilesRequest) =>
		client.post<SkillView>('/skill', body, undefined, { silent: true }),
	files: (skillId: string) =>
		client.get<SkillFile[]>(`/skill/${encodeURIComponent(skillId)}/files`, undefined, {
			silent: true,
		}),
	readFile: (skillId: string, path: string) =>
		client.get<{ content: string }>(
			`/skill/${encodeURIComponent(skillId)}/file`,
			{ path },
			{ silent: true },
		),
	saveFiles: (skillId: string, body: SaveSkillFilesRequest) =>
		client.patch<SkillView>(`/skill/${encodeURIComponent(skillId)}/files`, body, undefined, {
			silent: true,
		}),
	uploadFolder: (files: File[], skillId?: string, version?: string) => {
		const body = new FormData();
		body.append(
			'manifest',
			JSON.stringify({
				entries: files.map((file) => ({
					path: file.webkitRelativePath || `skill/${file.name}`,
					size: file.size,
				})),
			}),
		);
		files.forEach((file) => body.append('files', file));
		return client.upload<SkillView>(
			skillId ? `/skill/${encodeURIComponent(skillId)}/upload` : '/skill/upload',
			body,
			version ? { version } : undefined,
		);
	},
	uploadArchive: (archive: File, skillId?: string, version?: string) => {
		const format =
			archive.name.endsWith('.tar.gz') || archive.name.endsWith('.tgz')
				? 'tar.gz'
				: archive.name.endsWith('.tar')
					? 'tar'
					: 'zip';
		const body = new FormData();
		body.append('archive', archive);
		return client.upload<SkillView>(
			skillId ? `/skill/${encodeURIComponent(skillId)}/package` : '/skill/archive',
			body,
			{ archive_format: format, ...(version ? { version } : {}) },
		);
	},

	/** Unlike the list endpoint, this also carries the `SKILL.md` body. */
	get: (skillId: string) => client.get<SkillRecord>(`/skill/${encodeURIComponent(skillId)}`),
	upgrade: (skillId: string) =>
		client.post<SkillView>(`/skill/${encodeURIComponent(skillId)}/upgrade`),

	/**
	 * Removes it from the library. Workspaces that already hold this skill
	 * keep their files; new runs exclude the uninstalled resource ID.
	 */
	remove: (skillId: string) => client.delete(`/skill/${encodeURIComponent(skillId)}`),
};
