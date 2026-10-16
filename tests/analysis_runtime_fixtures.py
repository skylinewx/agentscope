"""Current selected resource bindings for isolated runtime and HTTP tests."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from analysis_fixtures import OWNER


def resources_for(app, names=("data-analysis",)):
    """User-selected, registered packages; no model or real source connection."""
    packages = {name: SimpleNamespace(name=name, user_id=OWNER.user_id, artifact_id="installed-v1") for name in names}
    mounted = [SimpleNamespace(name=name + " (5)", dir="installed-" + name) for name in names]
    bindings = {skill.dir: SimpleNamespace(resource_id=name, bound=True, enabled=True, artifact_id="installed-v1")
                for name, skill in zip(names, mounted)}
    async def effective(user, agent, session):
        return [(SimpleNamespace(kind="skill", resource_id=binding.resource_id,
                    enabled=binding.enabled, binding=binding, skill=skill),
                 packages.get(binding.resource_id))
                for skill in mounted
                if (binding := bindings.get(skill.dir)) is not None and binding.bound] + resources.mcps

    async def session_skill(user, workspace, agent, session, selection, *, install=False):
        return selection.skill

    resources = SimpleNamespace(
        effective=AsyncMock(side_effect=effective),
        _session_skill=AsyncMock(side_effect=session_skill),
        _skill_hash=AsyncMock(return_value="installed-v1"),
        mcps=[], packages=packages)
    app.state.resource_service = resources
    app.state.storage.get_skill = AsyncMock(side_effect=lambda user, rid: packages.get(rid))
    app.state.storage.get_mcp = AsyncMock()
    return resources, next(iter(packages.values()), None), mounted, bindings
