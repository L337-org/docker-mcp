from tools.prompts import (
    audit_docker_contexts,
    clean_environment,
    deploy_compose_project,
    deploy_container,
    find_latest_image_tag,
    inspect_stack,
    lookup_docker_docs,
    migrate_container,
    plan_compose_stack,
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


def test_find_latest_image_tag_uses_registry_tools():
    out = find_latest_image_tag("ghcr.io/org/repo")
    assert "ghcr.io/org/repo" in out
    assert "registry_list_tags" in out
    assert "registry_inspect_manifest" in out
    assert "hub_repo_info" in out
    assert "do not pull" in out.lower()
