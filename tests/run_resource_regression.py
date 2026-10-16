# -*- coding: utf-8 -*-
"""Run the resource lifecycle regression suite without production services.

Usage from the repository root:
    python tests/run_resource_regression.py [--temp-dir .test-tmp]
"""

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

_SQL_CASE = "tests.storage_sql_test.AsyncSQLAlchemyStorageTest."
_TESTS = (
    "tests.resource_resolution_test",
    "tests.resource_scopes_test",
    "tests.resource_persistence_test",
    "tests.agent_config_grouping_test",
    "tests.resource_access_policy_test",
    "tests.analysis_activation_test",
    "tests.analysis_bootstrap_test",
    "tests.analysis_report_skill_test",
    "tests.analysis_routes_test",
    "tests.service_resources_test",
    "tests.resource_router_test",
    "tests.service_toolkit_test",
    "tests.mcp_rename_test",
    "tests.hub_router_test",
    "tests.workspace_skill_hash_test",
    "tests.workspace_skill_partition_publish_test",
    "tests.workspace_skill_archive_test",
    "tests.workspace_manager_local_test",
    "tests.realtime_service_test",
    "tests.webrtc_audio_transport_test",
    "tests.service_chat_channel_delivery_test",
    "tests.service_workspace_files_test",
    "tests.storage_redis_test.TestMCP",
    "tests.storage_redis_test.TestSkill",
    _SQL_CASE + "test_mcps_round_trip",
    _SQL_CASE + "test_mcp_name_is_unique_per_user",
    _SQL_CASE + "test_mcp_rename_frees_the_old_name",
    _SQL_CASE + "test_skills_round_trip",
    _SQL_CASE + "test_skill_name_is_unique_per_user",
    "tests.storage_sql_test.LegacyRecordShapeTest",
    "tests.workspace_local_test.TestLocalWorkspaceSkills",
    "tests.workspace_local_test.TestLocalWorkspaceSkillPartitions",
    "tests.workspace_local_test.TestLocalWorkspaceMCPInit",
    "tests.workspace_local_test.TestLocalWorkspaceMCPScoping",
    "tests.service_chat_run_context_test",
    "tests.service_chat_locking_test",
    "tests.service_chat_middleware_factory_test",
    "tests.app_lifespan_dedicated_test",
    "tests.toolkit_test",
    "tests.toolkit_skill_test",
    "tests.toolkit_task_test",
    "tests.mcp_client_reconnect_test",
    "tests.mcp_runtime_headers_test",
    "tests.service_mcp_render_test",
    "tests.middleware_background_acting_test",
)


def main() -> None:
    """Load the scoped integration suite and return a useful exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temp-dir", help="Directory for temporary fixtures")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    if args.temp_dir:
        directory = Path(args.temp_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(directory)
    suite = unittest.defaultTestLoader.loadTestsFromNames(_TESTS)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
