#!/usr/bin/env bash
# Deterministic test entrypoint for the active, non-Discord Edge Agent scope.
set -uo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
failed=0

for test_file in "$ROOT"/tests/test_*.py; do
  # Discord is retired. Its adapter, shared-helper, and integration tests stay
  # in the tree for quarantine/reference but are outside the active contract.
  case "$(basename -- "$test_file")" in
    test_agent_profile_integration.py|test_atomic_json.py|test_external_skill_repositories.py|test_provider_health.py|test_repo_lock_worktree.py|test_shared_channel_environment.py|test_worktree_parallel_pilot.py)
      continue
      ;;
  esac
  python3 "$test_file" || failed=1
done

for test_file in "$ROOT"/skills/*/tests/test_*.py; do
  python3 "$test_file" || failed=1
done

exit "$failed"
