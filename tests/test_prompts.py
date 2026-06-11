from docker_mcp.tools.prompts import (
    audit_docker_contexts,
    audit_image_cves,
    audit_swarm_health,
    clean_environment,
    compare_image_versions,
    create_multiarch_manifest,
    deploy_compose_project,
    deploy_container,
    find_latest_image_tag,
    inspect_multiarch_manifest,
    inspect_stack,
    lookup_docker_docs,
    migrate_container,
    migrate_from_docker_manifest,
    plan_compose_stack,
    plan_multiarch_build,
    recommend_base_image,
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
    assert out.index("pull_image") < out.index("run_container")
    assert "list_containers" in out


def test_troubleshoot_container_covers_logs_and_state():
    out = troubleshoot_container("api-1")
    assert "api-1" in out
    for tool in ("get_container", "container_logs", "container_stats", "exec_in_container"):
        assert tool in out


def test_migrate_container_preserves_config():
    out = migrate_container("api-1", "myorg/api:v2")
    assert "api-1" in out
    assert "myorg/api:v2" in out
    assert out.index("get_container") < out.index("stop_container")
    assert out.index("stop_container") < out.index("remove_container")
    assert out.index("remove_container") < out.index("run_container")


def test_clean_environment_default_scope_skips_volumes():
    out = clean_environment()
    assert "prune_containers" in out
    assert "prune_images" in out
    assert "prune_volumes" not in out


def test_clean_environment_all_scope_includes_volumes_with_warning():
    out = clean_environment("all")
    assert "prune_volumes" in out
    assert "confirm" in out.lower()


def test_inspect_stack_filters_by_label_across_resource_types():
    out = inspect_stack("com.example.app=web")
    assert "com.example.app=web" in out
    for tool in ("list_containers", "list_networks", "list_volumes"):
        assert tool in out
    assert "do not modify" in out.lower()


def test_plan_compose_stack_requires_plan_before_actions():
    out = plan_compose_stack("wordpress with mysql")
    assert "wordpress with mysql" in out
    assert out.index("plan") < out.index("create_network")
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


def test_audit_docker_contexts_lists_then_confirms():
    out = audit_docker_contexts()
    assert "context_ls" in out
    assert out.index("context_ls") < out.index("info")
    assert "docker-py" in out.lower() or "sdk" in out.lower()


def test_audit_swarm_health_covers_nodes_services_and_tasks():
    out = audit_swarm_health()
    for tool in ("list_nodes", "list_services", "service_tasks", "service_logs"):
        assert tool in out
    # Node enumeration should precede the per-service task drill-down.
    assert out.index("list_nodes") < out.index("service_tasks")
    # Read-only audit: it must not invoke remove_node, only mention it as a follow-up.
    assert "do not call it" in out.lower() or "do not change anything" in out.lower()


def test_find_latest_image_tag_uses_registry_tools():
    out = find_latest_image_tag("ghcr.io/org/repo")
    assert "ghcr.io/org/repo" in out
    assert "registry_list_tags" in out
    assert "registry_inspect_manifest" in out
    assert "hub_repo_info" in out
    assert "do not pull" in out.lower()


def test_plan_multiarch_build_uses_buildx_and_emulation_warning():
    out = plan_multiarch_build("ghcr.io/org/app:v1", platforms="linux/amd64,linux/arm64")
    assert "ghcr.io/org/app:v1" in out
    assert "buildx_ls" in out
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
    # not registry_inspect_manifest (which strips tag/digest from `image`).
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
