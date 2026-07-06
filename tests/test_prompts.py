from docker_mcp.tools.prompts import (
    audit_container_security,
    audit_docker_contexts,
    audit_image_cves,
    audit_swarm_health,
    backup_volume,
    clean_environment,
    compare_image_versions,
    create_multiarch_manifest,
    debug_container_networking,
    deploy_compose_project,
    deploy_container,
    deploy_swarm_stack,
    find_latest_image_tag,
    inspect_multiarch_manifest,
    inspect_stack,
    investigate_disk_usage,
    lookup_docker_docs,
    migrate_container,
    migrate_from_docker_manifest,
    monitor_container_fleet,
    prune_managed,
    plan_compose_stack,
    plan_multiarch_build,
    recommend_base_image,
    restore_volume,
    review_dockerfile,
    survey_hosts,
    triage_incident,
    troubleshoot_compose_project,
    troubleshoot_container,
    verify_docker_method,
)


def test_lookup_docker_docs_references_resource_uri():
    out = lookup_docker_docs("containers")
    assert "docker-docs://containers" in out
    assert "summarize" in out.lower()


def test_verify_docker_method_includes_method_and_section():
    out = verify_docker_method("containers.run", "containers")
    assert "containers.run" in out
    assert "docker-docs://containers" in out


def test_deploy_container_lists_steps_in_order():
    out = deploy_container("nginx:1.27", "web")
    assert "nginx:1.27" in out
    assert "web" in out
    assert out.index("image_pull") < out.index("container_run")
    assert out.index("container_run") < out.index("container_wait")
    assert "container_logs" in out
    # health:null (no HEALTHCHECK) must not be conflated with unhealthy
    assert "no HEALTHCHECK" in out


def test_troubleshoot_container_covers_logs_and_state():
    out = troubleshoot_container("api-1")
    assert "api-1" in out
    for tool in ("container_inspect", "container_logs", "container_stats", "container_exec"):
        assert tool in out


def test_monitor_container_fleet_enumerates_via_resource_and_ranks():
    out = monitor_container_fleet()
    # Enumeration starts from the index resource, then drills via the per-container resources.
    assert "docker://containers" in out
    assert "docker-stats://" in out
    assert "docker-logs://" in out
    # It's a read-only sweep that ranks by pressure, not a single-target tool.
    assert out.index("docker://containers") < out.index("docker-stats://")
    assert "troubleshoot_container" in out  # hands off the deep dive
    assert "destructive" in out.lower() or "read-only" in out.lower()


def test_monitor_container_fleet_threads_top_argument():
    out = monitor_container_fleet(top=3)
    assert "top 3" in out


def test_triage_incident_correlates_events_with_current_state():
    out = triage_incident()
    # Symptom-first: start from what changed (events) and reconcile with the live index.
    assert "events" in out
    assert "docker://containers" in out
    assert out.index("events") < out.index("docker://containers")
    # Must separate a single-container fault from host-wide pressure, and hand off the deep dive.
    assert "df" in out
    assert "troubleshoot_container" in out
    assert "root cause" in out.lower()


def test_triage_incident_threads_window_into_events_since():
    out = triage_incident(window_minutes=15)
    # `events(since=...)` takes an absolute timestamp (Unix epoch / RFC3339), not a relative
    # duration — the prompt threads the window in as a count to convert, never as a "15m" string.
    assert "15 minutes ago" in out
    assert "15m" not in out
    assert "epoch" in out.lower()


def test_migrate_container_preserves_config_with_rename_rollback():
    out = migrate_container("api-1", "myorg/api:v2")
    assert "api-1" in out
    assert "myorg/api:v2" in out
    # New flow keeps the old container as a rollback: capture -> stop -> rename to -old -> run new
    # under the original name, and only remove the old one last.
    assert out.index("container_inspect") < out.index("container_stop")
    assert out.index("container_stop") < out.index("container_rename")
    assert out.index("container_rename") < out.index("container_run")
    assert out.index("container_run") < out.index("container_wait")
    assert out.index("container_wait") < out.rindex("container_remove")
    assert "api-1-old" in out
    assert "rollback" in out.lower()
    # health:null (no HEALTHCHECK) must not be conflated with unhealthy
    assert "not unhealthy" in out


def test_clean_environment_default_scope_skips_volumes():
    out = clean_environment()
    assert "container_prune" in out
    assert "image_prune" in out
    assert "buildx_prune" in out  # build cache is often the biggest reclaimable chunk
    assert "volume_prune" not in out
    # Opens and closes with df for a before/after delta.
    assert out.count("`system_df`") >= 2


def test_clean_environment_all_scope_includes_volumes_with_warning():
    out = clean_environment("all")
    assert "volume_prune" in out
    assert "confirm" in out.lower()


def test_prune_managed_scopes_every_step_to_the_managed_label():
    out = prune_managed()
    assert "docker-mcp-server.managed=true" in out
    # Inventory across the managed-aware list tools — including volumes, so a volume prune is never
    # confirmed blind — before removing anything.
    for tool in ("container_list", "network_list", "volume_list", "service_list"):
        assert tool in out
    assert "managed_only=True" in out
    # Default does not *remove* volumes (volume_prune only appears with include_volumes).
    assert "volume_prune" not in out


def test_prune_managed_include_volumes_adds_volume_step_with_confirmation():
    out = prune_managed(include_volumes=True)
    assert "volume_prune" in out
    assert "docker-mcp-server.managed=true" in out
    assert "confirm" in out.lower()


def test_inspect_stack_filters_by_label_across_resource_types():
    out = inspect_stack("com.example.app=web")
    assert "com.example.app=web" in out
    for tool in ("container_list", "network_list", "volume_list"):
        assert tool in out
    assert "do not modify" in out.lower()


def test_plan_compose_stack_requires_plan_before_actions():
    out = plan_compose_stack("wordpress with mysql")
    assert "wordpress with mysql" in out
    assert out.index("plan") < out.index("network_create")
    assert "approve" in out.lower()


def test_deploy_compose_project_includes_config_pull_up_ps_logs_in_order():
    out = deploy_compose_project("/tmp/myproj", project_name="demo")
    assert "/tmp/myproj" in out
    assert "demo" in out
    # The plan must inspect the config before mutating anything.
    assert out.index("compose_config") < out.index("compose_up")
    # And pull before up so failures surface early.
    assert out.index("compose_pull") < out.index("compose_up")
    assert out.index("compose_up") < out.index("compose_ps")
    assert out.index("compose_ps") < out.index("compose_logs")
    # Should warn about destructive teardown flags.
    assert "--volumes" in out


def test_deploy_compose_project_without_project_name_omits_arg():
    out = deploy_compose_project("/tmp/myproj")
    assert 'project_name="' not in out


def test_troubleshoot_compose_project_gathers_state_first():
    out = troubleshoot_compose_project("/tmp/myproj")
    assert "/tmp/myproj" in out
    assert out.index("compose_ps") < out.index("compose_logs")
    assert "root cause" in out.lower()


def test_audit_docker_contexts_reports_host_registry_then_contexts():
    out = audit_docker_contexts()
    assert "host_list" in out  # the host registry is reported first
    assert "context_list" in out
    assert out.index("host_list") < out.index("context_list")
    assert "info" in out


def test_audit_swarm_health_covers_nodes_services_and_tasks():
    out = audit_swarm_health()
    for tool in ("node_list", "service_list", "service_ps", "service_logs"):
        assert tool in out
    # Node enumeration should precede the per-service task drill-down.
    assert out.index("node_list") < out.index("service_ps")
    # Read-only audit: it must not invoke node_remove, only mention it as a follow-up.
    assert "do not call it" in out.lower() or "do not change anything" in out.lower()


def test_find_latest_image_tag_uses_registry_tools():
    out = find_latest_image_tag("ghcr.io/org/repo")
    assert "ghcr.io/org/repo" in out
    assert "registry_tags" in out
    assert "registry_manifest" in out
    assert "hub_repo_info" in out
    assert "do not pull" in out.lower()


def test_plan_multiarch_build_uses_buildx_and_emulation_warning():
    out = plan_multiarch_build("ghcr.io/org/app:v1", platforms="linux/amd64,linux/arm64")
    assert "ghcr.io/org/app:v1" in out
    assert "buildx_list" in out
    assert "buildx_imagetools_inspect" in out
    assert "buildx_build" in out
    assert "linux/amd64" in out and "linux/arm64" in out
    assert "emulation" in out.lower()


def test_plan_multiarch_build_creates_docker_container_when_no_buildx_driver():
    out = plan_multiarch_build("ghcr.io/org/app:v1")
    assert "buildx_create" in out
    assert "docker-container" in out


def test_audit_image_cves_walks_quickview_then_cves():
    out = audit_image_cves("alpine:3.19")
    assert "alpine:3.19" in out
    # quickview first, then drill in
    assert out.index("scout_quickview") < out.index("scout_cves")
    # Should mention severity filtering AND base separation
    assert "critical" in out.lower()
    assert "ignore_base" in out


def test_compare_image_versions_uses_scout_compare():
    out = compare_image_versions("org/app:v1", "org/app:v2")
    assert "org/app:v1" in out
    assert "org/app:v2" in out
    assert "scout_compare" in out
    assert "ignore_unchanged" in out
    assert "regression" in out.lower()


def test_recommend_base_image_uses_recommendations_and_verifies_with_compare():
    out = recommend_base_image("org/app:v1")
    assert "scout_recommendations" in out
    assert "scout_compare" in out
    # Manifest verification step must use buildx_imagetools_inspect (accepts a full ref),
    # not registry_manifest (which strips tag/digest from `image`).
    assert "buildx_imagetools_inspect" in out


def test_inspect_multiarch_manifest_uses_buildx_imagetools_and_explains_replacement():
    out = inspect_multiarch_manifest("alpine:3.19")
    assert "alpine:3.19" in out
    assert "buildx_imagetools_inspect" in out
    # The prompt must explain it replaces docker manifest inspect for discovery.
    assert "docker manifest inspect" in out
    # Should mention both image index and manifest list media types.
    assert "image.index" in out or "manifest.list" in out


def test_create_multiarch_manifest_dry_run_first():
    out = create_multiarch_manifest("org/app:v1", "org/app:v1-amd64,org/app:v1-arm64")
    assert "org/app:v1" in out
    assert "buildx_imagetools_create" in out
    assert "dry_run" in out
    # Dry-run before live push
    assert out.index("dry_run=True") < out.lower().rindex("approves")


def test_migrate_from_docker_manifest_returns_mapping_table():
    out = migrate_from_docker_manifest()
    # Must mention each docker manifest subcommand and its replacement.
    for cmd in ("inspect REF", "create NEW SRC", "annotate", "push NEW", "rm NEW"):
        assert cmd in out
    assert "buildx_imagetools_inspect" in out
    assert "buildx_imagetools_create" in out
    # And explain the why
    assert "maintenance mode" in out.lower()


def test_review_dockerfile_reads_docs_and_covers_security():
    out = review_dockerfile("/app/Dockerfile")
    assert "/app/Dockerfile" in out
    # Must point the agent at the authoritative references rather than relying on memory.
    assert "docker-docs://dockerfile" in out
    assert "docker-docs://build-best-practices" in out
    # Core checks.
    for needle in ("USER", "HEALTHCHECK", "secret", "latest"):
        assert needle.lower() in out.lower()


def test_audit_container_security_inspects_hostconfig_risks():
    out = audit_container_security()
    assert "container_list" in out
    assert "container_inspect" in out
    for risk in ("Privileged", "docker.sock", "host", "CapAdd"):
        assert risk in out
    # Read-only audit.
    assert "do not change" in out.lower() or "read-only" in out.lower()


def test_debug_container_networking_compares_networks_and_tests():
    out = debug_container_networking("web", "db")
    assert "web" in out
    assert "db" in out
    assert "container_inspect" in out
    assert "network_connect" in out
    assert "container_exec" in out
    # Should distinguish DNS from connection failure.
    assert "dns" in out.lower()


def test_investigate_disk_usage_breaks_down_by_bucket():
    out = investigate_disk_usage()
    for tool in ("system_df", "image_list", "image_history", "buildx_du", "volume_list"):
        assert tool in out
    # Diagnosis only — defers actual pruning to clean_environment.
    assert "clean_environment" in out


def test_backup_volume_uses_archive_api_not_stdout_tar():
    out = backup_volume("pgdata", "/backups/pg.tar")
    assert "pgdata" in out
    assert "/backups/pg.tar" in out
    # Single coherent approach: the Docker archive API, not piping tar to stdout.
    assert "container_archive_get_to_file" in out
    assert "container_remove" in out  # helper is cleaned up
    # The archive-root caveat that lets backup/restore round-trip must be stated.
    assert "data/" in out


def test_restore_volume_confirms_clears_and_uses_root_path():
    out = restore_volume("pgdata", "/backups/pg.tar")
    assert "pgdata" in out
    assert "/backups/pg.tar" in out
    assert "container_archive_put" in out
    assert "from_file" in out
    assert "volume_create" in out
    # Existing volume => always confirm (can't tell whether it holds data without mounting).
    assert "confirm" in out.lower()
    # Must clear stale files before extracting, and extract at "/" (not /data) to avoid nesting.
    assert "container_exec" in out
    assert 'path="/"' in out


def test_deploy_swarm_stack_validates_swarm_then_deploys_and_verifies():
    out = deploy_swarm_stack("web", "/srv/app/docker-compose.yml")
    assert "web" in out
    assert "/srv/app/docker-compose.yml" in out
    # Must confirm swarm-manager status before deploying.
    assert out.index("info") < out.index("stack_deploy")
    # Validate the compose file before mutating, then verify convergence after.
    assert out.index("compose_config") < out.index("stack_deploy")
    assert out.index("stack_deploy") < out.index("stack_services")
    assert out.index("stack_services") < out.index("service_wait")
    assert "stack_ps" in out
    # Mentions teardown but does not invoke it.
    assert "stack_remove" in out
    assert "do not call it" in out.lower()


# ---------- slice 6: multi-host prompts ----------

import subprocess  # noqa: E402
import sys  # noqa: E402

import docker_mcp._hosts as _hosts_mod  # noqa: E402
from docker_mcp._hosts import parse_registry  # noqa: E402


def _set_multi(monkeypatch):
    monkeypatch.setattr(_hosts_mod, "_registry", parse_registry("local=unix:///l.sock, prod=tcp://p:2376"))


def test_survey_hosts_explains_model_and_per_host_sweep():
    out = survey_hosts()
    assert "host_list" in out or "docker-mcp://hosts" in out
    assert "host=<name>" in out
    assert "docker://{host}/containers" in out
    assert "read-only" in out.lower() and "require" in out.lower()


def test_monitor_fleet_host_note_only_in_multi_host(monkeypatch):
    assert "Multi-host" not in monitor_container_fleet()  # single host: no host-targeting note
    _set_multi(monkeypatch)
    multi = monitor_container_fleet()
    assert "Multi-host" in multi and "host=<name>" in multi
    # The note must correct the single-host URIs the body uses to the empty-authority / host forms.
    assert "docker:///containers" in multi and "docker://{host}/containers" in multi


def test_triage_incident_host_note_only_in_multi_host(monkeypatch):
    assert "Multi-host" not in triage_incident()
    _set_multi(monkeypatch)
    assert "Multi-host" in triage_incident()


def _registered_prompts(hosts_value: str | None) -> set[str]:
    import os

    env = dict(os.environ)
    env.pop("DOCKER_MCP_SERVER_HOSTS", None)
    if hosts_value:
        env["DOCKER_MCP_SERVER_HOSTS"] = hosts_value
    code = "import docker_mcp; from docker_mcp.server import mcp; print('\\n'.join(mcp._prompt_manager._prompts))"
    out = subprocess.run(  # noqa: S603 — fixed argv, sys.executable, no shell
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    ).stdout
    return {line for line in out.splitlines() if line}


def test_survey_hosts_registered_only_in_multi_host_end_to_end():
    assert "survey_hosts" not in _registered_prompts(None)  # single host: hidden
    assert "survey_hosts" in _registered_prompts("local=ssh://a, prod=ssh://b")  # multi: registered
